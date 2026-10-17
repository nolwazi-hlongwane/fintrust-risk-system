"""Tests for scripts/brand_signin.py: the sign-in page in the dashboard's colours (Week 15 Day 4).

moto does not implement managed login branding, so Cognito is a recorded
stand-in. The style document below has the shape Cognito returns with
ReturnMergedResources: 8-digit colours without '#', a light and a dark mode.
"""

import re
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import brand_signin  # noqa: E402

STYLES = (ROOT / "frontend" / "styles.css").read_text(encoding="utf-8")
LOGO = (ROOT / "infrastructure" / "cognito" / "logo.svg").read_text(encoding="utf-8")
FAVICON = (ROOT / "frontend" / "favicon.svg").read_text(encoding="utf-8")


def tokens(selector):
    """The custom properties defined in one block of styles.css."""
    block = re.search(re.escape(selector) + r"\s*\{(.*?)\n\}", STYLES, re.S).group(1)
    return dict(re.findall(r"--([a-z0-9-]+):\s*(#[0-9a-fA-F]{6})", block))


def mode(primary="0972d3ff"):
    return {
        "defaults": {"backgroundColor": primary, "textColor": "ffffffff"},
        "hover": {"backgroundColor": "033160ff", "textColor": "ffffffff"},
        "active": {"backgroundColor": "033160ff", "textColor": "ffffffff"},
    }


def cognito_default():
    return {
        "components": {
            "primaryButton": {"lightMode": mode(), "darkMode": mode("539fe5ff")},
            "pageBackground": {"image": {"enabled": True}, "lightMode": {"color": "ffffffff"},
                               "darkMode": {"color": "044444ff"}},
            "form": {"borderRadius": 8.0, "lightMode": {"backgroundColor": "ffffffff", "borderColor": "c6c6cdff"},
                     "darkMode": {"backgroundColor": "0f1b2aff", "borderColor": "424650ff"}},
        },
        "componentClasses": {
            "buttons": {"borderRadius": 8.0},
            "focusState": {"lightMode": {"borderColor": "0972d3ff"}, "darkMode": {"borderColor": "539fe5ff"}},
        },
        "categories": {"global": {"colorSchemeMode": "LIGHT"}, "form": {"displayGraphics": True}},
    }


def paths(document, prefix=""):
    out = set()
    for key, value in document.items():
        path = f"{prefix}{key}"
        out.add(path)
        if isinstance(value, dict):
            out |= paths(value, path + ".")
    return out


class TestPalette:
    """The sign-in page and the dashboard must not drift apart."""

    names = {"primary": "primary", "primary-hover": "primary-hover", "on-primary": "on-primary", "paper": "paper",
             "surface": "surface", "line": "line", "brass": "brass", "text": "text", "muted": "muted",
             "link": "ink-soft"}

    def test_light_colours_are_the_dashboards(self):
        light = tokens(":root")
        assert {name: light[css] for name, css in self.names.items()} == brand_signin.LIGHT

    def test_dark_colours_are_the_dashboards(self):
        dark = tokens(':root[data-theme="dark"]')
        assert {name: dark[css] for name, css in self.names.items()} == brand_signin.DARK

    def test_every_path_names_a_colour_in_both_palettes(self):
        for _path, token in brand_signin.COLOUR_PATHS:
            assert token in brand_signin.LIGHT and token in brand_signin.DARK


class TestRestyle:
    def test_buttons_and_page_take_the_dashboard_colours_in_cognitos_format(self):
        styled, changed, _missing = brand_signin.restyle(cognito_default())
        light = styled["components"]["primaryButton"]["lightMode"]
        assert light["defaults"]["backgroundColor"] == "0f2e2aff"
        assert light["hover"]["backgroundColor"] == "1d4a43ff"
        assert styled["components"]["primaryButton"]["darkMode"]["defaults"]["backgroundColor"] == "1f7a63ff"
        assert styled["components"]["pageBackground"]["lightMode"]["color"] == "f1f4f2ff"
        assert styled["componentClasses"]["focusState"]["lightMode"]["borderColor"] == "a6792bff"
        assert styled["components"]["pageBackground"]["image"]["enabled"] is False
        assert styled["componentClasses"]["buttons"]["borderRadius"] == 6
        assert "components.primaryButton.lightMode.defaults.backgroundColor" in changed

    def test_never_adds_a_key_cognito_did_not_send(self):
        original = cognito_default()
        styled, _changed, missing = brand_signin.restyle(original)
        assert paths(styled) == paths(original)
        assert "componentClasses.link.lightMode.defaults.textColor" in missing

    def test_does_not_modify_the_document_it_was_given(self):
        original = cognito_default()
        brand_signin.restyle(original)
        assert original == cognito_default()

    def test_running_it_twice_changes_nothing_the_second_time(self):
        once, _changed, _missing = brand_signin.restyle(cognito_default())
        twice, changed, _missing = brand_signin.restyle(once)
        assert twice == once and changed == []

    @pytest.mark.parametrize("current, expected", [
        ("0972d3ff", "0f2e2aff"), ("0972d3", "0f2e2a"), ("#0972d3", "#0f2e2a"), ("#0972D3FF", "#0f2e2aff"),
    ])
    def test_keeps_the_format_of_the_value_it_replaces(self, current, expected):
        assert brand_signin.as_cognito_colour("#0F2E2A", current) == expected

    def test_an_empty_document_changes_nothing(self):
        styled, changed, missing = brand_signin.restyle(None)
        assert styled == {} and changed == [] and missing


class TestAssets:
    def test_sends_only_fintrusts_logo_and_tab_icon(self):
        assets = brand_signin.brand_assets(LOGO, FAVICON)
        assert [(a["Category"], a["ColorMode"]) for a in assets] == [
            ("FORM_LOGO", "LIGHT"), ("FORM_LOGO", "DARK"), ("FAVICON_SVG", "DYNAMIC")]
        assert all(a["Extension"] == "SVG" and isinstance(a["Bytes"], bytes) for a in assets)

    def test_images_have_no_attribute_cognito_refuses(self):
        """Cognito: 'element [svg#role|aria-label] is not allowed.' (17 October 2026)."""
        assert 'role="img"' in LOGO and "aria-label" in LOGO        # the file keeps them for the web
        for asset in brand_signin.brand_assets(LOGO, FAVICON):
            svg = asset["Bytes"].decode()
            assert "role=" not in svg and "aria-" not in svg
            assert svg.startswith("<svg") and svg.rstrip().endswith("</svg>")

    def test_cleaning_keeps_the_drawing(self):
        clean = brand_signin.cognito_safe_svg(LOGO)
        assert clean.count("<text") == 2 and clean.count("<rect") == 1 and 'viewBox="0 0 260 56"' in clean

    def test_the_dark_logo_is_readable_on_a_dark_page(self):
        dark = brand_signin.dark_logo(LOGO)
        assert 'fill="#0f2e2a"' not in dark                             # no bottle-green on bottle-black
        assert f'fill="{brand_signin.DARK["text"]}">FinTrust' in dark
        assert dark.count("<text") == LOGO.count("<text")


class TestApply:
    def client(self, settings):
        cognito = MagicMock()
        cognito.exceptions.ResourceNotFoundException = type("ResourceNotFoundException", (Exception,), {})
        cognito.describe_managed_login_branding_by_client.return_value = {"ManagedLoginBranding": {
            "ManagedLoginBrandingId": "brand-1", "Settings": settings, "Assets": [
                # What ReturnMergedResources lists: Cognito's default images, including
                # sign-in buttons for providers this user pool does not have.
                {"Category": "IDP_BUTTON_ICON", "ColorMode": "LIGHT", "Extension": "SVG",
                 "Bytes": b"<svg/>", "ResourceId": "Facebook"},
                {"Category": "PAGE_BACKGROUND", "ColorMode": "LIGHT", "Extension": "SVG", "Bytes": b"<svg/>"},
            ]}}
        return cognito

    @staticmethod
    def refused(code="InvalidParameterException"):
        from botocore.exceptions import ClientError
        message = ("[{category: IDP_BUTTON_ICON, extension: SVG, colorMode: LIGHT, resourceId: Google, "
                   "errorMessage: \"Resource Id 'Google' for asset category 'IDP_BUTTON_ICON' not found\"}]")
        return ClientError({"Error": {"Code": code, "Message": message}}, "UpdateManagedLoginBranding")

    def test_sends_the_style_with_the_logo(self):
        """The Week 15 Day 2 deploy sent the logo alone, and Cognito refused it."""
        cognito = self.client(cognito_default())
        brand_signin.apply(cognito, "pool-1", "client-1", LOGO, FAVICON)
        cognito.describe_managed_login_branding_by_client.assert_called_once_with(
            UserPoolId="pool-1", ClientId="client-1", ReturnMergedResources=True)
        sent = cognito.update_managed_login_branding.call_args.kwargs
        assert sent["ManagedLoginBrandingId"] == "brand-1"
        assert sent["Settings"]["components"]["primaryButton"]["lightMode"]["defaults"]["backgroundColor"] == "0f2e2aff"
        assert sent["Assets"] and "UseCognitoProvidedValues" not in sent

    def test_never_sends_back_cognitos_default_images(self):
        """Cognito refused the Facebook and Google button icons it had listed itself (17 October 2026)."""
        cognito = self.client(cognito_default())
        brand_signin.apply(cognito, "pool-1", "client-1", LOGO, FAVICON)
        sent = cognito.update_managed_login_branding.call_args.kwargs["Assets"]
        assert {asset["Category"] for asset in sent} == {"FORM_LOGO", "FAVICON_SVG"}
        assert all("ResourceId" not in asset for asset in sent)

    def test_if_the_images_are_refused_the_colours_are_still_saved(self):
        cognito = self.client(cognito_default())
        cognito.update_managed_login_branding.side_effect = [self.refused(), {}]
        changed, _missing, refused = brand_signin.apply(cognito, "pool-1", "client-1", LOGO, FAVICON)
        first, second = (call.kwargs for call in cognito.update_managed_login_branding.call_args_list)
        assert "Assets" in first and "Assets" not in second
        assert second["Settings"] == first["Settings"] and changed
        assert "IDP_BUTTON_ICON" in refused

    def test_any_other_error_is_reported_and_not_retried(self):
        from botocore.exceptions import ClientError
        cognito = self.client(cognito_default())
        cognito.update_managed_login_branding.side_effect = self.refused("AccessDeniedException")
        with pytest.raises(ClientError):
            brand_signin.apply(cognito, "pool-1", "client-1", LOGO, FAVICON)
        assert cognito.update_managed_login_branding.call_count == 1

    def test_creates_cognitos_default_first_when_there_is_no_style(self):
        cognito = self.client(cognito_default())
        found = cognito.describe_managed_login_branding_by_client.return_value
        cognito.describe_managed_login_branding_by_client.side_effect = [
            cognito.exceptions.ResourceNotFoundException(), found]
        brand_signin.apply(cognito, "pool-1", "client-1", LOGO, FAVICON)
        cognito.create_managed_login_branding.assert_called_once_with(
            UserPoolId="pool-1", ClientId="client-1", UseCognitoProvidedValues=True)
        cognito.update_managed_login_branding.assert_called_once()

    def test_an_empty_style_is_never_overwritten(self):
        cognito = self.client({})
        with pytest.raises(RuntimeError):
            brand_signin.apply(cognito, "pool-1", "client-1", LOGO, FAVICON)
        cognito.update_managed_login_branding.assert_not_called()

    def test_deploy_auth_runs_it_and_no_longer_sends_the_logo_alone(self):
        script = (ROOT / "infrastructure" / "cognito" / "deploy_auth.sh").read_text(encoding="utf-8")
        assert 'python3 "$ROOT/scripts/brand_signin.py"' in script
        assert "update-managed-login-branding" not in script
