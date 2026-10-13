"""Checks on the dashboard's files and its hosting configuration.

The dashboard runs in a browser, so its behaviour is exercised in a real browser
during development and by scripts/verify_dashboard.py and scripts/verify_auth.py
on the deployed system. These tests guard the security and design properties
that can be read from the files themselves.
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
AUTH = (FRONTEND / "auth.js").read_text()
THEME = (FRONTEND / "theme.js").read_text()
CHARTS = (FRONTEND / "charts.js").read_text()
STYLES = (FRONTEND / "styles.css").read_text()
CODE = {"app.js": SCRIPT, "auth.js": AUTH, "theme.js": THEME, "charts.js": CHARTS}


class TestNoSecretsInTheBrowserFiles:
    def test_no_api_key_or_account_specific_address_is_committed(self):
        for text in (HTML, STYLES, (FRONTEND / "config.js").read_text(), *CODE.values()):
            assert not re.search(r"execute-api\.[a-z0-9-]+\.amazonaws\.com/", text)
            assert not re.search(r"\.auth\.[a-z0-9-]+\.amazoncognito\.com", text)
            assert not re.search(r"\b[A-Za-z0-9]{40}\b", text)            # the shape of an API Gateway key
            assert not re.search(r"\b\d{12}\b", text)                     # an AWS account id

    def test_committed_config_is_empty_and_filled_in_at_deploy_time(self):
        config = (FRONTEND / "config.js").read_text()
        assert 'apiUrl: ""' in config and 'apiKey: ""' in config and 'signIn: { domain: "", clientId: "" }' in config
        deploy = (HOSTING / "deploy_frontend.sh").read_text()
        for line in ('apiUrl: "${API_URL}"', 'apiKey: "${DASHBOARD_KEY}"',
                     'signIn: { domain: "${SIGN_IN_URL}", clientId: "${CLIENT_ID}" }'):
            assert line in deploy

    def test_the_dashboard_gets_its_own_api_key_not_the_scripts_key(self):
        deploy = (HOSTING / "deploy_frontend.sh").read_text()
        assert "${PREFIX}-dashboard-key" in deploy and "compliance-key" not in deploy

    def test_the_page_never_asks_for_a_password(self):
        """Passwords are typed on Cognito's own sign-in page, never into this page."""
        assert 'type="password"' not in HTML
        for name, code in CODE.items():
            assert '"password"' not in code and "PASSWORD" not in code, name

    def test_tokens_live_in_session_storage_only(self):
        """This tab only, gone when it closes. No cookie, nothing that outlives the tab."""
        assert "window.sessionStorage.setItem(key" in AUTH
        for name, code in CODE.items():
            assert "document.cookie" not in code, name
            if name != "theme.js":
                assert "localStorage" not in code, name

    def test_only_the_appearance_choice_is_kept_in_local_storage(self):
        assert re.findall(r"localStorage\.(\w+)\((\w+)", THEME) == [("getItem", "KEY"), ("setItem", "KEY")]
        assert 'const KEY = "fintrust.theme"' in THEME

    def test_the_id_token_travels_in_a_header_never_in_an_address(self):
        assert '"x-api-key": API_KEY, Authorization: token' in SCRIPT
        assert "id_token=" not in SCRIPT + AUTH and "token=" not in SCRIPT


class TestSignIn:
    """auth.js: the OAuth 2.0 authorization code flow with PKCE (RFC 7636)."""

    def test_uses_the_code_flow_never_the_implicit_flow(self):
        assert 'response_type: "code"' in AUTH and 'response_type: "token"' not in AUTH

    def test_uses_pkce_with_sha256(self):
        assert 'code_challenge_method: "S256"' in AUTH
        assert 'crypto.subtle.digest("SHA-256"' in AUTH
        assert "code_verifier: pending.verifier" in AUTH

    def test_randomness_comes_from_the_browsers_secure_generator(self):
        assert "crypto.getRandomValues" in AUTH and "Math.random" not in AUTH + SCRIPT

    def test_the_state_value_must_match_or_the_response_is_refused(self):
        assert "pending.state !== query.get(\"state\")" in AUTH

    def test_the_code_is_removed_from_the_address_bar_and_history(self):
        assert "window.history.replaceState(null, \"\"" in AUTH

    def test_only_https_sign_in_except_on_this_computer(self):
        assert r"https:\/\/|http:\/\/(localhost|127\.0\.0\.1)" in AUTH

    def test_sign_out_ends_the_session_everywhere(self):
        assert '"/oauth2/revoke"' in AUTH and '"/logout"' in AUTH          # Cognito: refresh token and sign-in cookie
        assert 'api("DELETE", "/session")' in SCRIPT                          # the API session in DynamoDB

    def test_the_page_signs_out_after_the_idle_time_and_warns_a_minute_before(self):
        assert "WARN_BEFORE_MS = 60 * 1000" in SCRIPT
        assert 'endSession("IDLE")' in SCRIPT and 'id="idle-dialog"' in HTML

    def test_the_session_is_checked_on_every_page_load(self):
        assert 'await api("POST", "/session") : await api("GET", "/session")' in SCRIPT


class TestCrossSiteScripting:
    """Text from the API (a merchant name, a description) must never be able to run as script."""

    @pytest.mark.parametrize("name", list(CODE))
    def test_script_never_writes_html_from_data(self, name):
        for dangerous in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
            assert dangerous not in CODE[name], dangerous

    def test_page_has_no_inline_script_or_inline_event_handlers(self):
        assert re.findall(r"<script(?![^>]*\bsrc=)", HTML) == []
        assert not re.search(r"\son[a-z]+\s*=", HTML)
        assert "javascript:" not in HTML

    def test_page_has_no_inline_styles_so_the_content_security_policy_can_forbid_them(self):
        assert "<style" not in HTML and not re.search(r"\sstyle\s*=", HTML)
        for code in CODE.values():
            assert 'setAttribute("style"' not in code
            assert "cssText" not in code

    def test_page_loads_nothing_from_another_site(self):
        sources = re.findall(r'(?:src|href)="([^"]+)"', HTML)
        assert sources and all(not source.startswith(("http:", "https:", "//")) for source in sources)
        assert "@import" not in STYLES and "url(" not in STYLES


class TestRealApiOnly:
    def test_every_api_route_the_dashboard_calls_exists_in_the_api_definition(self):
        paths = json.loads((ROOT / "infrastructure" / "apigateway" / "openapi.json").read_text())["paths"]
        called = set()
        for method, path in re.findall(r'api\("(GET|POST|DELETE)", [`"](/[^`"]+)[`"]', SCRIPT):
            # A path built in the code names its id; give it the name the API definition uses.
            path = re.sub(r"^/transactions/\$\{[^}]+\}", "/transactions/{transaction_id}", path)
            path = re.sub(r"^/accounts/\$\{[^}]+\}", "/accounts/{account_id}", path)
            called.add((method, path))
        assert called == {("GET", "/insights"), ("GET", "/insights/trends"), ("GET", "/transactions"),
                          ("GET", "/transactions/{transaction_id}/journey"), ("GET", "/alerts"), ("GET", "/accounts"),
                          ("GET", "/accounts/{account_id}/balance"), ("POST", "/upload"), ("POST", "/flag"),
                          ("POST", "/resolve"), ("POST", "/session"), ("GET", "/session"), ("DELETE", "/session")}
        for method, path in called:
            assert method.lower() in paths[path], f"{method} {path}"
        # The sign-in page checks the system with the one route that needs no sign-in.
        assert 'fetch(API_URL + "/health"' in SCRIPT and "get" in paths["/health"]

    def test_ids_put_into_a_path_are_encoded(self):
        for built in re.findall(r"api\(\"GET\", `([^`]+)`", SCRIPT):
            assert all(part.startswith("${encodeURIComponent(") for part in re.findall(r"\$\{[^}]+\}", built)), built

    def test_there_is_no_sample_data_in_the_script(self):
        """Nothing shown on screen is written into the code; it all comes from the API."""
        code = SCRIPT.replace('placeholder: "ACC-1007"', "") + CHARTS + AUTH + HTML
        for marker in ("TXN-2026", "ACC-10", "lorem", "dummy", "88750", "hlongwane", "nkosi", "Math.random() *"):
            assert marker not in code, marker

    def test_charts_draw_only_what_the_api_returned(self):
        """The chart file has no fetch of its own: app.js gets the numbers, charts.js only draws them."""
        assert "fetch(" not in CHARTS and "api(" not in CHARTS and "XMLHttpRequest" not in CHARTS
        for series in ("trends.activity", "distribution.buckets", "flow.periods", "ranking.items", "data.points"):
            assert series in SCRIPT, series

    def test_every_sort_the_dashboard_offers_is_one_the_api_accepts(self):
        from fintrust import query

        for constant, allowed in (("TRANSACTION_SORTS", query.TRANSACTION_SORTS), ("ALERT_SORTS", query.ALERT_SORTS),
                                  ("ACCOUNT_SORTS", query.ACCOUNT_SORTS)):
            block = re.search(rf"const {constant} = \[(.*?)\];", SCRIPT, re.S).group(1)
            offered = re.findall(r'\["([a-z_]+)", "', block)
            assert offered and set(offered) == set(allowed), constant


class TestOverviewLayout:
    """The fix for the Total value figure running into its neighbour (Week 15 Day 1)."""

    def test_money_figure_gets_two_of_six_columns(self):
        assert "grid-template-columns: repeat(6, minmax(0, 1fr))" in STYLES
        assert ".figure.wide { grid-column: span 2; }" in STYLES
        assert 'figure(formatMoney(data.totals.total_value), "Total value", { wide: true })' in SCRIPT

    def test_a_figure_may_shrink_and_its_value_never_wraps_into_two_lines(self):
        figure = re.search(r"\.figure \{[^}]*\}", STYLES).group(0)
        value = re.search(r"\.figure-value \{[^}]*\}", STYLES).group(0)
        assert "min-width: 0" in figure
        assert "white-space: nowrap" in value and "clamp(" in value

    def test_scripts_load_in_order(self):
        order = [HTML.index(f'src="{name}"') for name in ("theme.js", "config.js", "auth.js", "charts.js", "app.js")]
        assert order == sorted(order)
        assert HTML.index('src="theme.js"') < HTML.index("</head>")      # before the page is drawn: no flash

    def test_every_chart_has_a_table_of_its_numbers(self):
        assert SCRIPT.count("numbers([") == 4          # activity, scores, alerts and balance; the ranking is a list already


class TestResultsTables:
    """Week 15 Day 2: results tables fit the screen on a laptop and become cards on a phone."""

    def test_every_cell_is_labelled_with_its_column_for_the_phone_layout(self):
        assert '"data-label": c.label' in SCRIPT

    def test_results_tables_become_cards_on_a_phone_but_chart_numbers_stay_tables(self):
        phone = STYLES[STYLES.index("@media (max-width: 40rem) {\n  .table-wrap.cards"):]
        assert "content: attr(data-label)" in phone and "grid-template-columns: repeat(2, minmax(0, 1fr))" in phone
        assert 'table(columns, rows, "Nothing to show.", null, null, { cards: false })' in SCRIPT

    def test_long_text_wraps_instead_of_pushing_the_table_off_the_screen(self):
        assert re.search(r'label: "Merchant", wrap: true', SCRIPT)
        assert re.search(r"td\.wrap \{ white-space: normal; min-width: \d+rem; max-width: \d+rem; \}", STYLES)

    def test_open_alerts_do_not_show_an_empty_resolved_by_column(self):
        assert 'if (status !== "OPEN") {' in SCRIPT
        assert 'label: "Action"' in SCRIPT


class TestAppearance:
    """Light and dark (Week 15 Day 2)."""

    @staticmethod
    def tokens(block):
        return dict(re.findall(r"(--[a-z0-9-]+):\s*([^;]+);", block))

    def blocks(self):
        light = self.tokens(re.search(r":root \{(.*?)\n\}", STYLES, re.S).group(1))
        dark = self.tokens(re.search(r':root\[data-theme="dark"\] \{(.*?)\n\}', STYLES, re.S).group(1))
        system = self.tokens(re.search(r':root:not\(\[data-theme="light"\]\) \{(.*?)\n  \}', STYLES, re.S).group(1))
        return light, dark, system

    def test_dark_defines_every_colour_the_light_palette_defines(self):
        light, dark, _ = self.blocks()
        colours = {name for name, value in light.items() if value.startswith(("#", "rgba", "0 "))}
        assert colours <= set(dark), sorted(colours - set(dark))

    def test_choosing_dark_and_a_dark_device_give_exactly_the_same_colours(self):
        _, dark, system = self.blocks()
        assert dark == system

    def test_every_colour_the_styles_use_is_defined(self):
        light, _, _ = self.blocks()
        used = set(re.findall(r"var\((--[a-z0-9-]+)\)", STYLES))
        assert used <= set(light), sorted(used - set(light))

    def test_risk_labels_meet_wcag_aa_contrast_in_both_palettes(self):
        def luminance(colour):
            channels = [int(colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
            linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
            return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

        def ratio(a, b):
            high, low = sorted((luminance(a), luminance(b)), reverse=True)
            return (high + 0.05) / (low + 0.05)

        for palette in self.blocks()[:2]:
            for level in ("low", "medium", "high"):
                colour = palette[f"--{level}"].split()[0]
                assert ratio(colour, palette[f"--{level}-tint"]) >= 4.5, level
            assert ratio(palette["--text"].split()[0], palette["--surface"]) >= 7
            assert ratio(palette["--muted"].split()[0], palette["--surface"]) >= 4.5

    def test_the_choice_is_system_light_or_dark(self):
        assert 'const CHOICES = ["system", "light", "dark"]' in THEME
        assert re.findall(r'name="appearance" value="(\w+)"', HTML) == ["system", "light", "dark"]

    def test_native_controls_follow_the_appearance(self):
        assert 'name="color-scheme" content="light dark"' in HTML
        assert "root.style.colorScheme" in THEME


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
        # The page may call this API and its own Cognito sign-in domain, and nothing else.
        assert directives["connect-src"] == "__CONNECT_SRC__"
        assert directives["frame-ancestors"] == "'none'" and directives["form-action"] == "'none'"
        assert "unsafe" not in policy and "*" not in policy
        assert headers["FrameOptions"]["FrameOption"] == "DENY"
        assert headers["StrictTransportSecurity"]["AccessControlMaxAgeSec"] >= 31536000

    def test_deploy_script_fills_connect_src_with_the_api_and_the_sign_in_domain_only(self):
        script = (HOSTING / "deploy_frontend.sh").read_text()
        assert 'CONNECT_SRC="${API_ORIGIN} ${SIGN_IN_URL}"' in script
        assert 'sed "s|__CONNECT_SRC__|${CONNECT_SRC}|g"' in script

    def test_deploy_script_blocks_public_access_and_tightens_cors(self):
        script = (HOSTING / "deploy_frontend.sh").read_text()
        assert "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true" in script
        assert 'put_param cors_allowed_origin "$DASHBOARD_URL"' in script
        assert "put-bucket-website" not in script and "public-read" not in script

    @pytest.mark.parametrize("name", ["index.html", "styles.css", "theme.js", "auth.js", "charts.js", "app.js",
                                      "config.js", "favicon.svg"])
    def test_every_file_the_page_needs_is_uploaded(self, name):
        script = (HOSTING / "deploy_frontend.sh").read_text()
        assert re.search(rf"^upload {re.escape(name)}\s", script, re.M)
        if name != "config.js":            # config.js is written by the script itself
            copied = re.search(r'^cp ((?:.|\\\n)*?)"\$WORK/"', script, re.M).group(1)
            assert f'"$ROOT/frontend/{name}"' in copied, f"{name} is uploaded but never copied"

    def test_every_file_in_the_folder_is_used_by_the_page(self):
        for path in FRONTEND.iterdir():
            if path.name != "index.html":
                assert f'"{path.name}"' in HTML, f"{path.name} is not loaded by index.html"


class TestAccessibilityBasics:
    def test_every_input_in_the_page_has_a_label(self):
        for field_id in re.findall(r'<input id="([^"]+)"', HTML):
            assert f'<label for="{field_id}">' in HTML
        # The appearance choice: each radio button sits inside its own label.
        assert len(re.findall(r'<label><input type="radio" name="appearance"', HTML)) == 3

    def test_page_declares_its_language_and_is_usable_on_a_phone(self):
        assert '<html lang="en">' in HTML and 'name="viewport"' in HTML

    def test_keyboard_focus_is_visible_and_reduced_motion_is_respected(self):
        assert ":focus-visible" in STYLES and "prefers-reduced-motion" in STYLES

    def test_a_keyboard_user_can_skip_to_the_content(self):
        assert '<a class="skip-link" href="#page">' in HTML

    def test_sorted_columns_say_so_to_a_screen_reader(self):
        assert '"aria-sort": direction' in SCRIPT
        assert 'id="announcer" aria-live="polite"' in HTML

    def test_the_current_section_is_marked_for_screen_readers(self):
        assert 'tab.setAttribute("aria-current", "page")' in SCRIPT
