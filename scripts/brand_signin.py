"""Give the Cognito sign-in page the dashboard's own look (Week 15 Day 4).

    python scripts/brand_signin.py

Staff sign in on Amazon Cognito's managed login page, then return to the
dashboard. Without a style, that page uses Cognito's default blue, so the
first screen of the demonstration would not look like FinTrust.

What it does:
    1. Reads the sign-in page style for the dashboard's app client, with every
       default filled in (ReturnMergedResources), so the document is complete.
    2. Changes only colour and corner values that are already in that
       document, to the dashboard's colours in frontend/styles.css. It never
       adds a key, so it cannot produce a document Cognito does not know.
    3. Saves the style with the FinTrust logo on the form and the dashboard's
       icon in the browser tab.

The page is always light: it is predictable on a projector, and it matches the
dashboard's Light appearance used for the presentation.

Safe to run again: the result is the same each time. Only the sign-in page's
look changes; users, passwords and sign-in settings are not touched.

Why deploy_auth.sh failed here before: UpdateManagedLoginBranding needs the
style (Settings) or UseCognitoProvidedValues alongside new Assets, and the
call sent the logo alone. The first version of this script then sent back
Cognito's default images (Facebook and Google button icons this user pool
does not have) and an SVG with role and aria-label attributes; Cognito
refused both. Now only FinTrust's own, cleaned images are sent, and if
Cognito still refuses an image the colours are saved without it.
"""

import copy
import os
import re
import sys
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parents[1]
REGION = os.environ.get("AWS_REGION", "eu-north-1")
ENVIRONMENT = os.environ.get("FINTRUST_ENV", "dev")
LOGO = ROOT / "infrastructure" / "cognito" / "logo.svg"
FAVICON = ROOT / "frontend" / "favicon.svg"

# The dashboard's colours, copied from frontend/styles.css. A unit test fails if
# they drift apart, so the sign-in page and the dashboard always match.
LIGHT = {
    "primary": "#0f2e2a", "primary-hover": "#1d4a43", "on-primary": "#ffffff",
    "paper": "#f1f4f2", "surface": "#ffffff", "line": "#d3dbd7",
    "brass": "#a6792b", "text": "#17211f", "muted": "#586663", "link": "#1d4a43",
}
DARK = {
    "primary": "#1f7a63", "primary-hover": "#26907a", "on-primary": "#ffffff",
    "paper": "#0e1615", "surface": "#15201e", "line": "#2c3c39",
    "brass": "#d6a851", "text": "#e4ebe8", "muted": "#a3b4af", "link": "#8fd3bd",
}
# Where each dashboard colour goes in Cognito's style document. "{mode}" is
# lightMode or darkMode. A path that is not in the document is skipped.
COLOUR_PATHS = (
    ("components.primaryButton.{mode}.defaults.backgroundColor", "primary"),
    ("components.primaryButton.{mode}.defaults.textColor", "on-primary"),
    ("components.primaryButton.{mode}.hover.backgroundColor", "primary-hover"),
    ("components.primaryButton.{mode}.hover.textColor", "on-primary"),
    ("components.primaryButton.{mode}.active.backgroundColor", "primary-hover"),
    ("components.primaryButton.{mode}.active.textColor", "on-primary"),
    ("components.secondaryButton.{mode}.defaults.borderColor", "line"),
    ("components.secondaryButton.{mode}.defaults.textColor", "link"),
    ("components.secondaryButton.{mode}.hover.borderColor", "link"),
    ("components.secondaryButton.{mode}.hover.textColor", "link"),
    ("components.secondaryButton.{mode}.active.borderColor", "link"),
    ("components.secondaryButton.{mode}.active.textColor", "link"),
    ("components.pageBackground.{mode}.color", "paper"),
    ("components.form.{mode}.backgroundColor", "surface"),
    ("components.form.{mode}.borderColor", "line"),
    ("components.pageText.{mode}.headingColor", "text"),
    ("components.pageText.{mode}.bodyColor", "text"),
    ("components.pageText.{mode}.descriptionColor", "muted"),
    ("componentClasses.focusState.{mode}.borderColor", "brass"),
    ("componentClasses.input.{mode}.defaults.borderColor", "line"),
    ("componentClasses.link.{mode}.defaults.textColor", "link"),
    ("componentClasses.link.{mode}.hover.textColor", "primary-hover"),
)
# Values that are the same in both modes.
FIXED_VALUES = (
    ("components.pageBackground.image.enabled", False),   # plain paper colour, like the dashboard
    ("componentClasses.buttons.borderRadius", 6),         # the dashboard's --radius
    ("componentClasses.input.borderRadius", 6),
    ("components.form.borderRadius", 6),
    ("categories.form.displayGraphics", True),             # show the logo on the form
    ("categories.global.colorSchemeMode", "LIGHT"),
)


def as_cognito_colour(hex_colour, like):
    """'#0f2e2a' in the format of the value it replaces ('0f2e2aff' for Cognito's 8-digit form)."""
    rgb = hex_colour.lstrip("#").lower()
    current = str(like)
    prefix = "#" if current.startswith("#") else ""
    alpha = "ff" if len(current.lstrip("#")) == 8 else ""   # the dashboard's colours are opaque
    return f"{prefix}{rgb}{alpha}"


def _get(document, dotted):
    node = document
    for key in dotted.split("."):
        if not isinstance(node, dict) or key not in node:
            return None, False
        node = node[key]
    return node, True


def _set(document, dotted, value):
    keys = dotted.split(".")
    node = document
    for key in keys[:-1]:
        node = node[key]
    node[keys[-1]] = value


def restyle(settings):
    """Return (new settings, paths changed, paths not found). The input is not modified."""
    styled = copy.deepcopy(settings or {})
    changed, missing = [], []
    for template, token in COLOUR_PATHS:
        for mode, palette in (("lightMode", LIGHT), ("darkMode", DARK)):
            path = template.format(mode=mode)
            current, found = _get(styled, path)
            if not found or not isinstance(current, str):
                missing.append(path)
                continue
            value = as_cognito_colour(palette[token], current)
            if value != current:
                _set(styled, path, value)
                changed.append(path)
    for path, value in FIXED_VALUES:
        current, found = _get(styled, path)
        if not found:
            missing.append(path)
        elif current != value:
            _set(styled, path, value)
            changed.append(path)
    return styled, changed, missing


def dark_logo(svg_text):
    """The logo for a dark background: light lettering, the mark in the dark primary green."""
    return (svg_text.replace('fill="#0f2e2a"/>', f'fill="{DARK["primary"]}"/>', 1)
            .replace(f'fill="{LIGHT["primary"]}">FinTrust', f'fill="{DARK["text"]}">FinTrust')
            .replace(f'fill="{LIGHT["muted"]}"', f'fill="{DARK["muted"]}"'))


def cognito_safe_svg(svg_text):
    """The SVG without the attributes Cognito's image checker refuses.

    Cognito answered "element [svg#role|aria-label] is not allowed" for the
    logo. Those two attributes are for screen readers on a web page; Cognito
    adds its own alternative text to the image, so nothing is lost.
    """
    return re.sub(r'\s(?:role|aria-[a-z-]+)="[^"]*"', "", svg_text)


def brand_assets(logo_svg, favicon_svg):
    """FinTrust's images for the sign-in page: the logo (light and dark) and the tab icon.

    Only these are sent. Cognito's merged style also lists its default images,
    including sign-in button icons for Facebook, Google and other providers;
    sending those back is refused because this user pool has no such sign-in,
    and any image left out keeps Cognito's default.
    """
    logo = cognito_safe_svg(logo_svg)
    return [
        {"Category": "FORM_LOGO", "ColorMode": "LIGHT", "Extension": "SVG", "Bytes": logo.encode()},
        {"Category": "FORM_LOGO", "ColorMode": "DARK", "Extension": "SVG", "Bytes": dark_logo(logo).encode()},
        {"Category": "FAVICON_SVG", "ColorMode": "DYNAMIC", "Extension": "SVG",
         "Bytes": cognito_safe_svg(favicon_svg).encode()},
    ]


def current_style(cognito, pool_id, client_id):
    """The style with every default filled in, creating Cognito's default first if there is none."""
    try:
        return cognito.describe_managed_login_branding_by_client(
            UserPoolId=pool_id, ClientId=client_id, ReturnMergedResources=True)["ManagedLoginBranding"]
    except cognito.exceptions.ResourceNotFoundException:
        cognito.create_managed_login_branding(UserPoolId=pool_id, ClientId=client_id, UseCognitoProvidedValues=True)
        print("Created the sign-in page style (Cognito's default, to be restyled now)")
        return cognito.describe_managed_login_branding_by_client(
            UserPoolId=pool_id, ClientId=client_id, ReturnMergedResources=True)["ManagedLoginBranding"]


def apply(cognito, pool_id, client_id, logo_svg, favicon_svg):
    """Restyle the sign-in page. Returns (paths changed, paths not found, images refused or None).

    If Cognito refuses the images, the colours are saved without them, so the
    page is never left blue because of a logo.
    """
    style = current_style(cognito, pool_id, client_id)
    settings, changed, missing = restyle(style.get("Settings"))
    if not changed and not style.get("Settings"):
        raise RuntimeError("Cognito returned an empty style document, so there is nothing safe to change")
    request = {"UserPoolId": pool_id, "ManagedLoginBrandingId": style["ManagedLoginBrandingId"], "Settings": settings}
    try:
        cognito.update_managed_login_branding(**request, Assets=brand_assets(logo_svg, favicon_svg))
        refused = None
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") != "InvalidParameterException":
            raise
        refused = error.response["Error"].get("Message", str(error))
        cognito.update_managed_login_branding(**request)     # the colours alone
    return changed, missing, refused


def main():
    session = boto3.Session(region_name=REGION)
    ssm, cognito = session.client("ssm"), session.client("cognito-idp")

    def setting(name):
        return ssm.get_parameter(Name=f"/fintrust/{ENVIRONMENT}/{name}")["Parameter"]["Value"]

    try:
        pool_id, client_id = setting("cognito_user_pool_id"), setting("cognito_client_id")
        changed, missing, refused = apply(cognito, pool_id, client_id,
                                          LOGO.read_text(encoding="utf-8"), FAVICON.read_text(encoding="utf-8"))
    except (ClientError, RuntimeError) as error:
        print("The sign-in page style was not changed. Cognito said:")
        for part in str(error).replace("},{", "}\n{").splitlines():
            print(f"  {part}")
        print("The sign-in page still works, with Cognito's default look.")
        return 1
    if refused:
        print(f"Sign-in page restyled in the dashboard's colours ({len(changed)} values). "
              "Cognito refused the logo, so it keeps its default image. Cognito said:")
        for part in refused.replace("},{", "}\n{").splitlines():
            print(f"  {part}")
    else:
        print(f"Sign-in page restyled in the dashboard's colours ({len(changed)} values), "
              "with the FinTrust logo and tab icon.")
    if missing:
        print(f"  ({len(missing)} optional style values are not used by this version of Cognito and were skipped)")
    print(f"Open {setting('dashboard_url')} and select 'Sign in to the risk desk' to see it.")
    print("Changes can take a minute to show; refresh the sign-in page if it still looks blue.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
