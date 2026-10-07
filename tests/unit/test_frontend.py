"""Checks on the dashboard's files and its hosting configuration.

The dashboard runs in a browser, so its behaviour is exercised by hand and by
scripts/verify_dashboard.py. These tests guard the security properties that can
be read from the files themselves.
"""

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "frontend"
HOSTING = ROOT / "infrastructure" / "frontend"

HTML = (FRONTEND / "index.html").read_text()
SCRIPT = (FRONTEND / "app.js").read_text()
STYLES = (FRONTEND / "styles.css").read_text()


class TestNoSecretsInTheBrowserFiles:
    def test_no_api_key_or_account_specific_address_is_committed(self):
        for text in (HTML, SCRIPT, STYLES, (FRONTEND / "config.js").read_text()):
            assert not re.search(r"execute-api\.[a-z0-9-]+\.amazonaws\.com/", text.replace("…execute-api", ""))
            assert not re.search(r"\b[A-Za-z0-9]{40}\b", text)            # the shape of an API Gateway key
            assert not re.search(r"\b\d{12}\b", text)                     # an AWS account id

    def test_committed_config_has_an_empty_address_filled_in_at_deploy_time(self):
        assert 'apiUrl: ""' in (FRONTEND / "config.js").read_text()
        assert "window.FINTRUST_CONFIG = { apiUrl: \"${API_URL}\" };" in (HOSTING / "deploy_frontend.sh").read_text()

    def test_api_key_is_kept_in_session_storage_only(self):
        assert "sessionStorage.setItem(STORAGE_KEY" in SCRIPT
        assert "localStorage" not in SCRIPT and "document.cookie" not in SCRIPT

    def test_api_key_field_is_a_password_field_that_is_not_remembered(self):
        field = re.search(r'<input id="api-key"[^>]*>', HTML).group(0)
        assert 'type="password"' in field and 'autocomplete="off"' in field

    def test_api_key_is_sent_as_a_header_never_in_the_address(self):
        assert '"x-api-key": state.apiKey' in SCRIPT
        assert "searchParams.set(\"api_key\"" not in SCRIPT and "apiKey=" not in SCRIPT


class TestCrossSiteScripting:
    """Text from the API (a merchant name, a description) must never be able to run as script."""

    def test_script_never_writes_html_from_data(self):
        for dangerous in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
            assert dangerous not in SCRIPT, dangerous

    def test_page_has_no_inline_script_or_inline_event_handlers(self):
        assert re.findall(r"<script(?![^>]*\bsrc=)", HTML) == []
        assert not re.search(r"\son[a-z]+\s*=", HTML)
        assert "javascript:" not in HTML

    def test_page_has_no_inline_styles_so_the_content_security_policy_can_forbid_them(self):
        assert "<style" not in HTML and not re.search(r"\sstyle\s*=", HTML)
        assert 'setAttribute("style"' not in SCRIPT

    def test_page_loads_nothing_from_another_site(self):
        sources = re.findall(r'(?:src|href)="([^"]+)"', HTML)
        assert sources and all(not source.startswith(("http:", "https:", "//")) for source in sources)
        assert "@import" not in STYLES and "url(" not in STYLES


class TestRealApiOnly:
    def test_every_api_route_the_dashboard_calls_exists_in_the_api_definition(self):
        paths = json.loads((ROOT / "infrastructure" / "apigateway" / "openapi.json").read_text())["paths"]
        called = set(re.findall(r'api\("(GET|POST)", [`"](/[a-z]+)', SCRIPT))
        assert called == {("GET", "/health"), ("GET", "/insights"), ("GET", "/transactions"), ("GET", "/alerts"),
                          ("GET", "/accounts"), ("POST", "/upload"), ("POST", "/flag"), ("POST", "/resolve")}
        for method, path in called:
            assert method.lower() in paths[path], f"{method} {path}"
        assert "/transactions/{transaction_id}/journey" in paths

    def test_there_is_no_sample_data_in_the_script(self):
        """Nothing shown on screen is written into the code; it all comes from the API."""
        code = SCRIPT.replace('placeholder: "ACC-1007"', "").replace('placeholder: "n.hlongwane"', "")
        for marker in ("TXN-2026", "ACC-10", "lorem", "dummy", "88750", "hlongwane"):
            assert marker not in code, marker


class TestHosting:
    def test_bucket_policy_allows_only_cloudfront_and_only_this_distribution(self):
        policy = json.loads((HOSTING / "bucket-policy.json").read_text())
        allow = [s for s in policy["Statement"] if s["Effect"] == "Allow"]
        assert len(allow) == 1
        assert allow[0]["Principal"] == {"Service": "cloudfront.amazonaws.com"}
        assert allow[0]["Action"] == "s3:GetObject"
        assert "distribution/__DISTRIBUTION_ID__" in allow[0]["Condition"]["StringEquals"]["AWS:SourceArn"]
        deny = [s for s in policy["Statement"] if s["Effect"] == "Deny"]
        assert deny[0]["Condition"] == {"Bool": {"aws:SecureTransport": "false"}}

    def test_distribution_forces_https_and_uses_origin_access_control(self):
        config = json.loads((HOSTING / "distribution.json").read_text())
        assert config["DefaultCacheBehavior"]["ViewerProtocolPolicy"] == "redirect-to-https"
        assert config["DefaultCacheBehavior"]["AllowedMethods"]["Items"] == ["GET", "HEAD"]      # read-only site
        origin = config["Origins"]["Items"][0]
        assert origin["OriginAccessControlId"] == "__OAC_ID__"
        assert origin["S3OriginConfig"] == {"OriginAccessIdentity": ""}
        assert ".s3-website" not in origin["DomainName"]                 # the private endpoint, not a public website
        assert config["DefaultRootObject"] == "index.html"

    def test_content_security_policy_is_strict(self):
        headers = json.loads((HOSTING / "security-headers.json").read_text())["SecurityHeadersConfig"]
        policy = headers["ContentSecurityPolicy"]["ContentSecurityPolicy"]
        directives = dict(part.strip().split(" ", 1) for part in policy.split(";"))
        assert directives["default-src"] == "'none'"
        assert directives["script-src"] == "'self'" and directives["style-src"] == "'self'"
        assert directives["connect-src"] == "__API_ORIGIN__"             # the page may call this API and nothing else
        assert directives["frame-ancestors"] == "'none'"
        assert "unsafe" not in policy and "*" not in policy
        assert headers["FrameOptions"]["FrameOption"] == "DENY"
        assert headers["StrictTransportSecurity"]["AccessControlMaxAgeSec"] >= 31536000

    def test_deploy_script_blocks_public_access_and_tightens_cors(self):
        script = (HOSTING / "deploy_frontend.sh").read_text()
        assert "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true" in script
        assert 'put_param cors_allowed_origin "$DASHBOARD_URL"' in script
        assert "put-bucket-website" not in script and "public-read" not in script

    @pytest.mark.parametrize("name", ["index.html", "styles.css", "app.js", "config.js", "favicon.svg"])
    def test_every_file_the_page_needs_is_uploaded(self, name):
        script = (HOSTING / "deploy_frontend.sh").read_text()
        assert re.search(rf"^upload {re.escape(name)}\s", script, re.M)


class TestAccessibilityBasics:
    def test_every_input_in_the_page_has_a_label(self):
        for field_id in re.findall(r'<input id="([^"]+)"', HTML):
            assert f'<label for="{field_id}">' in HTML

    def test_page_declares_its_language_and_is_usable_on_a_phone(self):
        assert '<html lang="en">' in HTML and 'name="viewport"' in HTML

    def test_keyboard_focus_is_visible_and_reduced_motion_is_respected(self):
        assert ":focus-visible" in STYLES and "prefers-reduced-motion" in STYLES
