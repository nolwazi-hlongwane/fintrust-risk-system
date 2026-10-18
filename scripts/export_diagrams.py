"""Export the draw.io diagrams to SVG and PDF with draw.io's own renderer.

    python scripts/build_diagrams.py                         # write the .drawio files
    python scripts/export_diagrams.py --webapp <draw.io web app folder>

For each docs/diagrams/*.drawio this writes the .svg (shown in the README and
docs on GitHub) and the .pdf (the brief's diagram deliverable). draw.io's own
export page (export3.html) draws them, so the pictures are exactly what the
editable files show, with the official AWS icons.

Needs Playwright with Chromium, and a copy of the draw.io web app: the folder
src/main/webapp of https://github.com/jgraph/drawio. Without them, open a
.drawio file in draw.io and use File > Export as > SVG, and > PDF: the result
is the same.

Every SVG starts with a comment naming the SHA-256 of the .drawio file it was
exported from, and a test checks it, so a picture can never silently fall
behind its source.
"""

import argparse
import functools
import hashlib
import sys
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIAGRAMS = ROOT / "docs" / "diagrams"
BORDER = 24


def source_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def stamp(name, digest):
    return f"<!-- Exported by draw.io from {name} (sha256 {digest}) -->\n"


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *arguments):
        pass


def serve(folder):
    server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(QuietHandler, directory=str(folder)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def export(page, base, drawio):
    page.goto(f"{base}/export3.html")
    page.wait_for_function("typeof render === 'function'")
    page.evaluate("(xml) => { window.__graph = render({xml: xml, format: 'svg', border: %d, "
                  "bg: '#ffffff', scale: 1}); }" % BORDER, drawio.read_text(encoding="utf-8"))
    page.wait_for_selector("#LoadingComplete", state="attached", timeout=60000)
    return page.evaluate("""() => new XMLSerializer().serializeToString(
        window.__graph.getSvg('#ffffff', 1, %d, false, null, true, null, null, null, null, null, 'light'))""" % BORDER)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--webapp", required=True, type=Path, help="the draw.io web app folder (has export3.html)")
    arguments = parser.parse_args(argv)
    if not (arguments.webapp / "export3.html").is_file():
        sys.exit(f"ERROR: {arguments.webapp} has no export3.html; give the draw.io src/main/webapp folder")

    from playwright.sync_api import sync_playwright

    server, base = serve(arguments.webapp)
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page()
            for drawio in sorted(DIAGRAMS.glob("*.drawio")):
                svg = export(page, base, drawio)
                svg_path = drawio.with_suffix(".svg")
                svg_path.write_text(stamp(drawio.name, source_hash(drawio)) + svg + "\n", encoding="utf-8")
                size = page.evaluate("""(text) => {
                    const svg = new DOMParser().parseFromString(text, 'image/svg+xml').documentElement;
                    return [parseFloat(svg.getAttribute('width')), parseFloat(svg.getAttribute('height'))]; }""", svg)
                printer = browser.new_page()
                printer.set_content(f'<html><body style="margin:0">{svg}</body></html>')
                printer.pdf(path=str(drawio.with_suffix(".pdf")), width=f"{size[0]}px", height=f"{size[1] + 2}px",
                            print_background=True, page_ranges="1")
                printer.close()
                print(f"Exported {drawio.name} -> {svg_path.name}, {drawio.with_suffix('.pdf').name}")
            browser.close()
    finally:
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
