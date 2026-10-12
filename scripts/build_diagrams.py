"""Build the architecture diagram and the ERD from one description each.

    python scripts/build_diagrams.py

Writes, in docs/diagrams/:
    architecture.drawio   open and edit at https://app.diagrams.net (draw.io)
    architecture.svg      the same diagram as a picture
    erd.drawio
    erd.svg

Both formats are produced from the same list of boxes and arrows below, so the
editable diagram and the picture cannot drift apart. The ERD is read from
docs/erd.md, which a test compares with the deployed database schema, so the
diagram always matches the schema.

architecture.pdf and erd.pdf are the same diagrams saved as PDF. To export them
again after editing in draw.io: File > Export as > PDF.

Uses only the Python standard library.
"""

import re
import sys
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "diagrams"

# Colours by kind of box: (fill, border, text)
KINDS = {
    "person":   ("#ffffff", "#17211f", "#17211f"),
    "edge":     ("#ede7f6", "#5e35b1", "#311b92"),     # CloudFront, API Gateway
    "compute":  ("#fff3e0", "#e65100", "#8a3b00"),     # Lambda
    "storage":  ("#e8f5e9", "#2e7d32", "#1b5e20"),     # S3
    "database": ("#e3f2fd", "#1565c0", "#0d47a1"),     # RDS, DynamoDB
    "queue":    ("#fce4ec", "#ad1457", "#880e4f"),     # SQS
    "dlq":      ("#ffebee", "#b42318", "#7f1d1d"),     # dead-letter queues
    "service":  ("#f3f4f6", "#4b5563", "#1f2937"),     # SES, EventBridge and supporting services
    "note":     ("#fffde7", "#a6792b", "#4a3a12"),
}
W, H = 164, 64
OFFICER_X = 30
COLUMN = [270, 510, 750, 990, 1230, 1470, 1710]
ROW = [96, 206, 336, 456, 576, 700, 842]


def at(column, row):
    return COLUMN[column], ROW[row]


# id: (x, y, width, height, kind, lines of text)
def architecture_boxes():
    boxes = {}

    def box(name, column, row, kind, *lines, w=W, h=H, dx=0, dy=0):
        x, y = at(column, row)
        boxes[name] = (x + dx, y + dy, w, h, kind, lines)

    boxes["officer"] = (OFFICER_X, (ROW[0] + ROW[1]) // 2, W, H, "person", ("Compliance officer", "browser or curl"))
    box("cloudfront", 0, 0, "edge", "CloudFront", "HTTPS, security headers")
    box("webbucket", 1, 0, "storage", "S3 dashboard bucket", "private: CloudFront only")
    box("apigw", 0, 1, "edge", "API Gateway (REST)", "API key + usage plan")
    box("query", 3, 1, "compute", "query Lambda", "the REST API")
    box("rds", 4, 2, "database", "RDS PostgreSQL", "ledger, no public endpoint")
    box("raw", 0, 3, "storage", "S3 raw bucket", "uploaded CSV files")
    box("ingest", 1, 3, "compute", "ingest Lambda", "validate each row (ETL)")
    box("fifo", 2, 3, "queue", "SQS FIFO queue", "ordered per account")
    box("score", 3, 3, "compute", "score Lambda", "apply risk rules")
    box("dynamo", 5, 3, "database", "DynamoDB", "scores + journey events")
    box("alert", 6, 3, "compute", "alert Lambda", "HIGH scores only")
    box("quarantine", 0, 4, "storage", "S3 quarantine bucket", "invalid rows + reasons")
    box("processed", 1, 4, "storage", "S3 processed bucket", "clean rows")
    box("dlq", 2, 4, "dlq", "FIFO dead-letter queue", "after 3 failed attempts")
    box("report", 3, 4, "compute", "report Lambda", "monthly CSV + JSON")
    box("reports", 5, 4, "storage", "S3 reports bucket", "monthly reports")
    box("ses", 6, 4, "service", "Amazon SES", "email to compliance")
    box("ingestdlq", 1, 2, "dlq", "ingest dead-letter queue", "files that failed", h=46, dy=44)
    box("alertdlq", 6, 2, "dlq", "alert dead-letter queue", "emails that failed", h=46, dy=44)
    box("eventbridge", 3, 5, "service", "EventBridge rule", "cron: 1st of the month")
    # supporting services
    wide = 196
    for index, (name, lines) in enumerate([
        ("iam", ("IAM", "one least-privilege role per", "Lambda + permission boundary")),
        ("kms", ("KMS key", "encrypts buckets, queues", "and the alarm topic")),
        ("ssm", ("SSM Parameter Store", "application settings", "no secrets in code")),
        ("cloudwatch", ("CloudWatch + SNS", "logs, FinTrust/ETL metrics,", "7 alarms, dashboard")),
        ("xray", ("X-Ray", "tracing on the API stage", "and every Lambda")),
        ("cloudtrail", ("CloudTrail", "audit: who changed what", "in the account")),
    ]):
        boxes[name] = (270 + index * 268, ROW[6], wide, 78, "service", lines)
    return boxes


# (name, x, y, width, height, label): drawn behind the boxes
ARCHITECTURE_GROUPS = [
    ("region", 244, 46, 1656, 746, "AWS account, Region eu-north-1 (Stockholm)"),
    ("vpc", 966, 176, 452, 484,
     "VPC: two private subnets in two Availability Zones. No internet gateway, no NAT Gateway."),
    ("support", 244, 808, 1656, 126, "Used by every part of the system"),
]

# (from, to, label, waypoints). Waypoints are corner points for an arrow that must go round other boxes.
ARCHITECTURE_ARROWS = [
    # (from, to, label, corner points, where along the arrow the label sits: 0 = start, 1 = end)
    ("officer", "cloudfront", "HTTPS", [], 0.5),
    ("cloudfront", "webbucket", "signed", [], 0.5),
    ("officer", "apigw", "x-api-key", [], 0.5),
    ("apigw", "query", "every route needs the API key", [], 0.45),
    ("query", "raw", "POST /upload stores the file", [(1040, 302), (352, 302)], 0.5),
    ("raw", "ingest", "S3 event", [], 0.5),
    ("ingest", "quarantine", "invalid rows", [], 0.5),
    ("ingest", "processed", "clean rows", [], 0.5),
    ("ingest", "ingestdlq", "on failure", [], 0.5),
    ("ingest", "fifo", "queued", [], 0.5),
    ("fifo", "dlq", "3 failures", [], 0.5),
    ("fifo", "score", "", [], 0.5),
    ("score", "rds", "ledger + alerts", [], 0.5),
    ("score", "dynamo", "score + rules", [], 0.72),
    ("query", "rds", "read, resolve, flag", [], 0.5),
    ("query", "dynamo", "journey", [(1552, 238)], 0.5),
    ("dynamo", "alert", "stream", [], 0.5),
    ("alert", "ses", "", [], 0.5),
    ("alert", "alertdlq", "on failure", [], 0.5),
    ("eventbridge", "report", "", [], 0.5),
    ("report", "rds", "read only", [], 0.22),
    ("report", "reports", "CSV + JSON", [], 0.78),
]

NOTE_Y = 690
ARCHITECTURE_NOTES = [
    (1230, NOTE_Y, 212, 84, ["Security group: the VPC Lambdas", "may reach RDS on port 5432 and", "nothing else. Database login is",
                             "by IAM token: no password exists."]),
    (1456, NOTE_Y, 200, 84, ["S3 and DynamoDB are reached", "from the VPC through gateway", "endpoints: free, and traffic",
                             "stays on the AWS network."]),
    (1670, NOTE_Y, 216, 84, ["Journey events are written to", "DynamoDB by the ingest, score", "and alert Lambdas at every step,",
                             "including failed attempts."]),
]


# ------------------------------------------------------------------ geometry

def centre(box):
    x, y, w, h = box[:4]
    return x + w / 2, y + h / 2


def border_point(box, toward):
    """Where the line from the box's centre toward a point leaves the box."""
    x, y, w, h = box[:4]
    cx, cy = centre(box)
    dx, dy = toward[0] - cx, toward[1] - cy
    if dx == 0 and dy == 0:
        return cx, cy
    scale = min((w / 2) / abs(dx) if dx else 1e9, (h / 2) / abs(dy) if dy else 1e9)
    return cx + dx * scale, cy + dy * scale


def arrow_points(boxes, source, target, waypoints):
    first = waypoints[0] if waypoints else centre(boxes[target])
    last = waypoints[-1] if waypoints else centre(boxes[source])
    return [border_point(boxes[source], first), *waypoints, border_point(boxes[target], last)]


# ----------------------------------------------------------------------- SVG

FONT = "font-family=\"Helvetica, Arial, sans-serif\""


def svg_text(x, y, lines, size, colour, bold_first=True, anchor="middle", line_height=None):
    line_height = line_height or size + 3
    start = y - (len(lines) - 1) * line_height / 2 + size * 0.35
    parts = []
    for index, line in enumerate(lines):
        weight = "bold" if bold_first and index == 0 else "normal"
        line_size = size if index == 0 else size - 1.5
        parts.append(f'<text x="{x:.0f}" y="{start + index * line_height:.0f}" text-anchor="{anchor}" {FONT} '
                     f'font-size="{line_size}" font-weight="{weight}" fill="{colour}">{escape(line)}</text>')
    return "\n".join(parts)


def svg_document(width, height, body, title):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" height="{height}">\n'
            f"<title>{escape(title)}</title>\n"
            '<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
            'orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="#374151"/></marker></defs>\n'
            f'<rect width="{width}" height="{height}" fill="#ffffff"/>\n{body}\n</svg>\n')


def architecture_svg(boxes):
    parts = [svg_text(40, 30, ["FinTrust Transaction Insights and Risk Monitoring System: architecture"], 20, "#0f2e2a",
                      anchor="start")]
    for name, x, y, w, h, label in ARCHITECTURE_GROUPS:
        dash = ' stroke-dasharray="7 5"' if name == "vpc" else ""
        fill = {"region": "#fafbfa", "vpc": "#eef6ff", "support": "#fafbfa"}[name]
        stroke = {"region": "#6b7280", "vpc": "#1565c0", "support": "#9ca3af"}[name]
        parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" fill="{fill}" stroke="{stroke}" '
                     f'stroke-width="1.5"{dash}/>')
        parts.append(svg_text(x + 12, y + 16, [label], 12.5, stroke, anchor="start"))
    for x, y, w, h, lines in ARCHITECTURE_NOTES:
        fill, stroke, colour = KINDS["note"]
        parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="4" fill="{fill}" stroke="{stroke}"/>')
        parts.append(svg_text(x + 8, y + h / 2, lines, 11.5, colour, bold_first=False, anchor="start", line_height=15))
    for source, target, label, waypoints, where in ARCHITECTURE_ARROWS:
        points = arrow_points(boxes, source, target, waypoints)
        path = " ".join(f"{x:.0f},{y:.0f}" for x, y in points)
        parts.append(f'<polyline points="{path}" fill="none" stroke="#374151" stroke-width="1.6" marker-end="url(#arrow)"/>')
        if label:
            # label on the longest segment
            longest = max(zip(points, points[1:], strict=False),
                          key=lambda pair: abs(pair[1][0] - pair[0][0]) + abs(pair[1][1] - pair[0][1]))
            mx = longest[0][0] + (longest[1][0] - longest[0][0]) * where
            my = longest[0][1] + (longest[1][1] - longest[0][1]) * where
            width = len(label) * 5.9 + 8
            parts.append(f'<rect x="{mx - width / 2:.0f}" y="{my - 9:.0f}" width="{width:.0f}" height="17" rx="3" '
                         'fill="#ffffff" fill-opacity="0.92"/>')
            parts.append(svg_text(mx, my, [label], 10.5, "#374151", bold_first=False))
    for x, y, w, h, kind, lines in boxes.values():
        fill, stroke, colour = KINDS[kind]
        parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="7" fill="{fill}" stroke="{stroke}" stroke-width="1.6"/>')
        parts.append(svg_text(x + w / 2, y + h / 2, list(lines), 12.5, colour))
    return svg_document(1920, 950, "\n".join(parts), "FinTrust architecture")


# -------------------------------------------------------------------- draw.io

def drawio_document(name, cells, width, height):
    body = "\n".join(cells)
    return ('<mxfile host="app.diagrams.net">\n'
            f'  <diagram name="{escape(name)}" id="{escape(name.lower().replace(" ", "-"))}">\n'
            f'    <mxGraphModel dx="1200" dy="800" grid="1" gridSize="10" page="1" pageWidth="{width}" pageHeight="{height}">\n'
            '      <root>\n        <mxCell id="0"/>\n        <mxCell id="1" parent="0"/>\n'
            f"{body}\n      </root>\n    </mxGraphModel>\n  </diagram>\n</mxfile>\n")


def vertex(cell_id, value, style, x, y, w, h, parent="1"):
    return (f'        <mxCell id="{cell_id}" value="{escape(value, quote=True)}" style="{style}" vertex="1" parent="{parent}">\n'
            f'          <mxGeometry x="{x:.0f}" y="{y:.0f}" width="{w:.0f}" height="{h:.0f}" as="geometry"/>\n        </mxCell>')


def architecture_drawio(boxes):
    cells = [vertex("title", "FinTrust Transaction Insights and Risk Monitoring System: architecture",
                    "text;html=1;align=left;fontSize=20;fontStyle=1;fontColor=#0f2e2a;", 40, 10, 900, 30)]
    for name, x, y, w, h, label in ARCHITECTURE_GROUPS:
        dashed = "dashed=1;" if name == "vpc" else ""
        fill = {"region": "#fafbfa", "vpc": "#eef6ff", "support": "#fafbfa"}[name]
        stroke = {"region": "#6b7280", "vpc": "#1565c0", "support": "#9ca3af"}[name]
        cells.append(vertex(f"group-{name}", label,
                            f"rounded=1;arcSize=3;whiteSpace=wrap;html=1;{dashed}fillColor={fill};strokeColor={stroke};"
                            f"fontColor={stroke};verticalAlign=top;align=left;spacingLeft=10;fontSize=12;", x, y, w, h))
    for index, (x, y, w, h, lines) in enumerate(ARCHITECTURE_NOTES):
        fill, stroke, colour = KINDS["note"]
        cells.append(vertex(f"note-{index}", "<br>".join(escape(line) for line in lines),
                            f"shape=note;size=10;whiteSpace=wrap;html=1;fillColor={fill};strokeColor={stroke};"
                            f"fontColor={colour};align=left;spacingLeft=6;fontSize=11;", x, y, w, h))
    for name, (x, y, w, h, kind, lines) in boxes.items():
        fill, stroke, colour = KINDS[kind]
        value = f"<b>{escape(lines[0])}</b>" + "".join(f"<br>{escape(line)}" for line in lines[1:])
        cells.append(vertex(name, value, f"rounded=1;whiteSpace=wrap;html=1;fillColor={fill};strokeColor={stroke};"
                                         f"fontColor={colour};fontSize=12;", x, y, w, h))
    for index, (source, target, label, waypoints, _where) in enumerate(ARCHITECTURE_ARROWS):
        points = "".join(f'<mxPoint x="{x}" y="{y}"/>' for x, y in waypoints)
        array = f'<Array as="points">{points}</Array>' if waypoints else ""
        cells.append(
            f'        <mxCell id="arrow-{index}" value="{escape(label, quote=True)}" style="endArrow=block;html=1;rounded=0;'
            f'strokeColor=#374151;fontSize=10;fontColor=#374151;labelBackgroundColor=#ffffff;" edge="1" parent="1" '
            f'source="{source}" target="{target}">\n'
            f'          <mxGeometry relative="1" as="geometry">{array}</mxGeometry>\n        </mxCell>')
    return drawio_document("Architecture", cells, 1920, 950)


# ----------------------------------------------------------------------- ERD

ERD_POSITIONS = {"CUSTOMERS": (40, 80), "ACCOUNTS": (420, 80), "TRANSACTIONS": (800, 80),
                 "ALERTS": (1180, 80), "RISK_EVENTS": (420, 350)}
# (parent, child, verb, "one to many" or "one to zero or one")
TABLE_WIDTH, ROW_HEIGHT, HEADER_HEIGHT = 250, 22, 30


def read_erd():
    """Tables, columns and relationships from the diagram source in docs/erd.md."""
    text = (ROOT / "docs" / "erd.md").read_text()
    block = text[text.index("erDiagram"):]
    block = block[:block.index("```")]
    tables = {}
    for name, body in re.findall(r"^\s{4}([A-Z_]+) \{\n(.*?)^\s{4}\}", block, re.S | re.M):
        tables[name] = [tuple(line.split()) for line in body.strip().splitlines()]       # (type, column[, PK|FK])
    relationships = []
    for parent, link, child, verb in re.findall(r'^\s{4}([A-Z_]+) (\S+) ([A-Z_]+) : "?([^"\n]+)"?', block, re.M):
        relationships.append((parent, child, verb.strip(), "0..1" if link.endswith("o|") else "many"))
    return tables, relationships


def erd_boxes(tables):
    return {name: (*ERD_POSITIONS[name], TABLE_WIDTH, HEADER_HEIGHT + ROW_HEIGHT * len(columns), "table", columns)
            for name, columns in tables.items()}


def erd_svg(tables, relationships):
    boxes = erd_boxes(tables)
    parts = [svg_text(40, 32, ["FinTrust ledger: entity relationship diagram (third normal form)"], 20, "#0f2e2a",
                      anchor="start"),
             svg_text(40, 56, ["PK = primary key, FK = foreign key. Generated from docs/erd.md, which is tested against the deployed schema."],
                      12, "#586663", bold_first=False, anchor="start")]
    for parent, child, verb, many in relationships:
        start, end = border_point(boxes[parent], centre(boxes[child])), border_point(boxes[child], centre(boxes[parent]))
        parts.append(f'<line x1="{start[0]:.0f}" y1="{start[1]:.0f}" x2="{end[0]:.0f}" y2="{end[1]:.0f}" stroke="#374151" '
                     'stroke-width="1.6" marker-end="url(#arrow)"/>')
        mx, my = (start[0] + end[0]) / 2, (start[1] + end[1]) / 2
        label = f"{verb} (1 to {many})"
        if abs(start[1] - end[1]) > 40 and abs(start[0] - end[0]) > 40:
            my += 22                 # a slanted line: keep the label clear of the table above
        width = len(label) * 6 + 8
        parts.append(f'<rect x="{mx - width / 2:.0f}" y="{my - 9:.0f}" width="{width:.0f}" height="17" rx="3" fill="#ffffff"/>')
        parts.append(svg_text(mx, my, [label], 10.5, "#374151", bold_first=False))
    for name, (x, y, w, h, _kind, columns) in boxes.items():
        parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="5" fill="#ffffff" stroke="#1565c0" stroke-width="1.6"/>')
        parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{HEADER_HEIGHT}" rx="5" fill="#1565c0"/>')
        parts.append(svg_text(x + w / 2, y + HEADER_HEIGHT / 2, [name.lower()], 13.5, "#ffffff"))
        for index, column in enumerate(columns):
            row_y = y + HEADER_HEIGHT + ROW_HEIGHT * index + ROW_HEIGHT / 2
            key = column[2] if len(column) > 2 else ""
            weight = "bold" if key == "PK" else "normal"
            parts.append(f'<text x="{x + 10}" y="{row_y + 4:.0f}" {FONT} font-size="12" font-weight="{weight}" '
                         f'fill="#17211f">{escape(column[1])}</text>')
            parts.append(f'<text x="{x + w - 44}" y="{row_y + 4:.0f}" text-anchor="end" {FONT} font-size="11" '
                         f'fill="#586663">{escape(column[0])}</text>')
            if key:
                parts.append(f'<text x="{x + w - 10}" y="{row_y + 4:.0f}" text-anchor="end" {FONT} font-size="11" '
                             f'font-weight="bold" fill="{"#a6792b" if key == "PK" else "#1565c0"}">{key}</text>')
    return svg_document(1470, 540, "\n".join(parts), "FinTrust ERD")


def erd_drawio(tables, relationships):
    boxes = erd_boxes(tables)
    cells = [vertex("title", "FinTrust ledger: entity relationship diagram (third normal form)",
                    "text;html=1;align=left;fontSize=20;fontStyle=1;fontColor=#0f2e2a;", 40, 16, 800, 30)]
    for name, (x, y, w, h, _kind, columns) in boxes.items():
        cells.append(vertex(name, name.lower(),
                            f"swimlane;fontStyle=1;childLayout=stackLayout;horizontal=1;startSize={HEADER_HEIGHT};"
                            "horizontalStack=0;resizeParent=1;collapsible=0;fillColor=#1565c0;fontColor=#ffffff;"
                            "strokeColor=#1565c0;fontSize=13;", x, y, w, h))
        for index, column in enumerate(columns):
            key = f" ({column[2]})" if len(column) > 2 else ""
            bold = "fontStyle=1;" if key == " (PK)" else ""
            cells.append(vertex(f"{name}-{column[1]}", f"{column[1]} : {column[0]}{key}",
                                f"text;strokeColor=none;fillColor=none;align=left;spacingLeft=8;{bold}fontSize=12;"
                                "fontColor=#17211f;", 0, HEADER_HEIGHT + ROW_HEIGHT * index, w, ROW_HEIGHT, parent=name))
    for index, (parent, child, verb, many) in enumerate(relationships):
        end = "ERzeroToOne" if many == "0..1" else "ERzeroToMany"
        cells.append(
            f'        <mxCell id="rel-{index}" value="{escape(verb, quote=True)}" style="edgeStyle=entityRelationEdgeStyle;'
            f'html=1;startArrow=ERmandOne;endArrow={end};strokeColor=#374151;fontSize=10;labelBackgroundColor=#ffffff;" '
            f'edge="1" parent="1" source="{parent}" target="{child}">\n'
            '          <mxGeometry relative="1" as="geometry"/>\n        </mxCell>')
    return drawio_document("ERD", cells, 1470, 540)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    boxes = architecture_boxes()
    tables, relationships = read_erd()
    outputs = {
        "architecture.svg": architecture_svg(boxes),
        "architecture.drawio": architecture_drawio(boxes),
        "erd.svg": erd_svg(tables, relationships),
        "erd.drawio": erd_drawio(tables, relationships),
    }
    for name, text in outputs.items():
        (OUT / name).write_text(text, encoding="utf-8")
        print(f"Written docs/diagrams/{name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
