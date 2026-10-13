"""Build every diagram in docs/diagrams from one description each.

    python scripts/build_diagrams.py                                  # write the .drawio files
    python scripts/export_diagrams.py --webapp <draw.io web app>      # draw them as .svg and .pdf

Writes four editable draw.io files in docs/diagrams/:

    architecture.drawio          AWS architecture: every service, the network and
                                 security boundaries, and the numbered data flows
    erd.drawio                   the ledger's entity relationship diagram in UML
                                 class notation, plus the DynamoDB key design
    pipeline-sequence.drawio     UML sequence: one transaction from upload to
                                 alert, and how a failed scoring is recovered
    sign-in-sequence.drawio      UML sequence: sign-in, the session, sign-out

Each .drawio opens and can be edited at https://app.diagrams.net. The pictures
(.svg for GitHub, .pdf as the deliverable) are exported from these files by
draw.io itself, with the official AWS Architecture Icons, so the picture is
always exactly what the editable file shows. A test checks that every picture
was exported from the current .drawio file.

The ERD is read from the UML class diagram in docs/erd.md. A second test reads
the real PostgreSQL schema and fails if docs/erd.md differs from it, so the
drawn ERD always matches the deployed database.

Uses only the Python standard library.
"""

import re
import sys
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "diagrams"

INK = "#232F3E"            # AWS "squid ink": text and normal arrows
MUTED = "#545B64"
FAILURE = "#B42318"        # failure and recovery paths
FONT = "fontFamily=Helvetica;"


# ----------------------------------------------------------------------------
# draw.io file writing
# ----------------------------------------------------------------------------
def attr(text):
    """Escape a value for an XML attribute. draw.io reads html=1 labels as HTML."""
    return escape(str(text), quote=True)


class Drawing:
    """The cells of one draw.io page, written in a fixed order so the file is reproducible."""

    def __init__(self, name):
        self.name = name
        self.cells = []
        self.ids = set()

    def _id(self, cell_id):
        if cell_id in self.ids:
            raise ValueError(f"duplicate cell id {cell_id} in {self.name}")
        self.ids.add(cell_id)
        return cell_id

    def vertex(self, cell_id, value, style, x, y, w, h, parent="1"):
        self.cells.append(
            f'<mxCell id="{self._id(cell_id)}" value="{attr(value)}" style="{attr(style)}" vertex="1" parent="{parent}">'
            f'<mxGeometry x="{x}" y="{y}" width="{w}" height="{h}" as="geometry"/></mxCell>')
        return cell_id

    def edge(self, cell_id, style, source=None, target=None, value="", points=(), start=None, end=None,
             labels=(), parent="1"):
        """An arrow. `source`/`target` join it to cells; `start`/`end` are free (x, y) points instead.
        `labels` = [(text, position from -1 at the source to 1 at the target, dx, dy)]."""
        ends = (f' source="{source}"' if source else "") + (f' target="{target}"' if target else "")
        geometry = []
        if start:
            geometry.append(f'<mxPoint x="{start[0]}" y="{start[1]}" as="sourcePoint"/>')
        if end:
            geometry.append(f'<mxPoint x="{end[0]}" y="{end[1]}" as="targetPoint"/>')
        if points:
            geometry.append('<Array as="points">' + "".join(f'<mxPoint x="{x}" y="{y}"/>' for x, y in points)
                            + "</Array>")
        self.cells.append(
            f'<mxCell id="{self._id(cell_id)}" value="{attr(value)}" style="{attr(style)}" edge="1" parent="{parent}"'
            f'{ends}><mxGeometry relative="1" as="geometry">{"".join(geometry)}</mxGeometry></mxCell>')
        for index, (text, position, dx, dy) in enumerate(labels):
            self.cells.append(
                f'<mxCell id="{self._id(f"{cell_id}-label{index}")}" value="{attr(text)}" '
                f'style="{attr(EDGE_LABEL)}" vertex="1" connectable="0" parent="{cell_id}">'
                f'<mxGeometry x="{position}" relative="1" as="geometry"><mxPoint x="{dx}" y="{dy}" as="offset"/>'
                f'</mxGeometry></mxCell>')
        return cell_id

    def text(self, cell_id, value, x, y, w, h, size=12, align="left", style=""):
        return self.vertex(cell_id, value, f"text;html=1;whiteSpace=wrap;{FONT}fontSize={size};align={align};"
                           f"verticalAlign=top;fontColor={INK};spacing=0;{style}", x, y, w, h)

    def xml(self, width, height):
        return (f'<mxfile host="FinTrust build_diagrams.py" type="device">\n'
                f'  <diagram name="{attr(self.name)}" id="{attr(self.name)}">\n'
                f'    <mxGraphModel dx="{width}" dy="{height}" grid="0" gridSize="10" guides="1" tooltips="1" '
                f'connect="1" arrows="1" fold="1" page="1" pageScale="1" pageWidth="{width}" pageHeight="{height}" '
                f'math="0" shadow="0" background="#FFFFFF">\n'
                f'      <root>\n        <mxCell id="0"/>\n        <mxCell id="1" parent="0"/>\n'
                + "".join(f"        {cell}\n" for cell in self.cells)
                + "      </root>\n    </mxGraphModel>\n  </diagram>\n</mxfile>\n")


EDGE_LABEL = (f"edgeLabel;html=1;resizable=0;{FONT}fontSize=10;fontColor={INK};labelBackgroundColor=#FFFFFF;"
              "align=center;verticalAlign=middle;")


def title_block(drawing, title, subtitle, width):
    drawing.text("title", f"<b>{escape(title)}</b>", 30, 14, width - 60, 26, size=20)
    drawing.text("subtitle", escape(subtitle), 30, 42, width - 60, 18, size=12, style=f"fontColor={MUTED};")


# ----------------------------------------------------------------------------
# 1. Architecture
# ----------------------------------------------------------------------------
# AWS Architecture Icons: each service is drawn with its official icon, in the
# colour of its AWS category.
CATEGORY = {
    "compute": "#ED7100", "storage": "#7AA116", "database": "#C925D1", "integration": "#E7157B",
    "network": "#8C4FFF", "security": "#DD344C", "management": "#E7157B", "devtools": "#C925D1",
    "business": "#DD344C",
}
ICON = {   # service -> (draw.io AWS4 icon, category)
    "lambda": ("lambda", "compute"), "ec2": ("ec2", "compute"),
    "s3": ("s3", "storage"),
    "rds": ("rds", "database"), "dynamodb": ("dynamodb", "database"),
    "sqs": ("sqs", "integration"), "eventbridge": ("eventbridge", "integration"), "sns": ("sns", "integration"),
    "cloudfront": ("cloudfront", "network"), "apigateway": ("api_gateway", "network"),
    "cognito": ("cognito", "security"), "iam": ("identity_and_access_management", "security"),
    "kms": ("key_management_service", "security"),
    "cloudwatch": ("cloudwatch_2", "management"), "ssm": ("systems_manager", "management"),
    "cloudtrail": ("cloudtrail", "management"), "xray": ("xray", "devtools"),
    "ses": ("simple_email_service", "business"),
}
CARD_W, CARD_H, ICON_SIZE = 214, 58, 38

GROUPS = {   # official AWS group styles: (icon, border, label colour)
    "cloud": ("group_aws_cloud_alt", "#232F3E", "#232F3E"),
    "region": ("group_region", "#00A4A6", "#147EBA"),
    "vpc": ("group_vpc2", "#8C4FFF", "#8C4FFF"),
    "subnet": ("group_security_group", "#00A4A6", "#147EBA"),
}

# Where each card goes (left x, top y). Columns and rows leave lanes between
# the cards for the arrows, so that only two arrows cross (drawn with a hop).
X_USER = 24
L0, L1, L2 = 300, 584, 868                # outside the VPC
VPC_X, VPC_Y, VPC_W, VPC_H = 1120, 118, 586, 542
V0, V1 = 1164, 1448                       # inside the VPC
D0, D1 = 1780, 2064                       # DynamoDB, alerting
Y0, Y1, Y2, Y3, Y4, Y5 = 190, 312, 434, 556, 678, 800


def architecture_cards():
    """name -> (x, y, service, title, detail, failure path?)"""
    return {
        "users": (X_USER, Y1, None, "Compliance staff", "officer or analyst", False),
        "cloudfront": (L0, Y0, "cloudfront", "Amazon CloudFront", "HTTPS, security headers", False),
        "web": (L1, Y0, "s3", "S3 dashboard bucket", "private, CloudFront only", False),
        "eventbridge": (L2, Y0, "eventbridge", "EventBridge rule", "cron: 1st of each month", False),
        "cognito": (L0, Y1, "cognito", "Amazon Cognito", "user pool, MFA, two roles", False),
        "reports": (L2, Y1, "s3", "S3 reports bucket", "monthly CSV + JSON", False),
        "apigw": (L0, Y2, "apigateway", "Amazon API Gateway", "REST: API key + Cognito", False),
        "raw": (L0, Y3, "s3", "S3 raw bucket", "uploaded CSV files", False),
        "ingest": (L1, Y3, "lambda", "ingest Lambda", "validates every row", False),
        "fifo": (L2, Y3, "sqs", "SQS FIFO queue", "in order per account", False),
        "quarantine": (L0, Y4, "s3", "S3 quarantine bucket", "invalid rows + reasons", False),
        "processed": (L1, Y4, "s3", "S3 processed bucket", "clean rows", False),
        "fifodlq": (L2, Y4, "sqs", "FIFO dead-letter queue", "after 3 failed attempts", True),
        "ingestdlq": (L1, Y5, "sqs", "ingest dead-letter queue", "after 2 retries", True),
        "report": (V1, Y0, "lambda", "report Lambda", "monthly report, read only", False),
        "tunnel": (V0, Y1, "ec2", "EC2 tunnel instance", "admin SQL, no public IP", False),
        "query": (V0, Y2, "lambda", "query Lambda", "the REST API", False),
        "rds": (V1, Y2, "rds", "RDS PostgreSQL", "the ledger, private", False),
        "score": (V0, Y3, "lambda", "score Lambda", "applies the risk rules", False),
        "sessions": (D0, Y1, "dynamodb", "DynamoDB sessions", "one record per sign-in", False),
        "events": (D0, Y3, "dynamodb", "DynamoDB events", "scores + journey steps", False),
        "alert": (D0, Y4, "lambda", "alert Lambda", "Stream: HIGH scores only", False),
        "ses": (D1, Y4, "ses", "Amazon SES", "email to compliance", False),
        "alertdlq": (D0, Y5, "sqs", "alert dead-letter queue", "emails that failed", True),
    }


SUPPORT = [   # services every component uses: (service, title, detail)
    ("cloudwatch", "Amazon CloudWatch", "logs, FinTrust/ETL metrics, alarms"),
    ("xray", "AWS X-Ray", "traces: API stage, every Lambda"),
    ("sns", "Amazon SNS", "alarm emails, encrypted topic"),
    ("kms", "AWS KMS", "customer key: S3, SQS, SNS"),
    ("ssm", "SSM Parameter Store", "settings, no secrets in code"),
    ("eventbridge", "EventBridge Scheduler", "stops RDS + tunnel at 20:00"),
]
GLOBAL = [
    ("iam", "AWS IAM", "one least-privilege role per Lambda"),
    ("cloudtrail", "AWS CloudTrail", "who changed what in the account"),
]

# (id, source, target, label, kind, exit, entry, waypoints)
#   exit/entry: (x, y) as fractions of the card's width and height
#   kinds: "flow" (data or a request), "failure" (failure path), "control" (checks, schedules, admin)
ARCHITECTURE_ARROWS = [
    ("e-signin", "users", "cognito", "", "flow", (1, 0.5), (0, 0.5), []),
    ("e-page", "users", "cloudfront", "", "flow", (1, 0.25), (0, 0.5), [(243, 326.5), (243, 219)]),
    ("e-api", "users", "apigw", "", "flow", (1, 0.75), (0, 0.5), [(243, 355.5), (243, 463)]),
    ("e-oac", "cloudfront", "web", "OAC", "flow", (1, 0.5), (0, 0.5), []),
    ("e-authorizer", "apigw", "cognito", "checks the token", "control", (0.5, 0), (0.5, 1), []),
    ("e-query", "apigw", "query", "", "flow", (1, 0.5), (0, 0.5), []),
    ("e-schedule", "eventbridge", "report", "monthly", "control", (1, 0.3), (0, 0.3), []),
    ("e-report-rds", "report", "rds", "read only", "flow", (0.5, 1), (0.5, 0), []),
    ("e-report-s3", "report", "s3endpoint", "", "flow", (0, 0.75), (1, 0.5), [(1142, 233.5), (1142, 341)]),
    ("e-query-s3", "query", "s3endpoint", "", "flow", (0, 0.25), (1, 0.5), [(1142, 448.5), (1142, 341)]),
    ("e-s3-reports", "s3endpoint", "reports", "", "flow", (0, 0.5), (1, 0.5), []),
    ("e-s3-raw", "s3endpoint", "raw", "POST /upload", "flow", (0.5, 1), (0.5, 0), [(1120, 525), (407, 525)]),
    ("e-tunnel", "tunnel", "rds", "admin psql", "control", (1, 0.5), (0.15, 0), [(1480.1, 341)]),
    ("e-query-rds", "query", "rds", "", "flow", (1, 0.5), (0, 0.5), []),
    ("e-ingest", "raw", "ingest", "", "flow", (1, 0.5), (0, 0.5), []),
    ("e-queue", "ingest", "fifo", "", "flow", (1, 0.5), (0, 0.5), []),
    ("e-score", "fifo", "score", "", "flow", (1, 0.5), (0, 0.5), []),
    ("e-processed", "ingest", "processed", "clean", "flow", (0.5, 1), (0.5, 0), []),
    ("e-quarantine", "ingest", "quarantine", "invalid", "flow", (0.2, 1), (0.5, 0), [(626.8, 646), (407, 646)]),
    ("e-ingestdlq", "ingest", "ingestdlq", "on failure", "failure", (1, 0.85), (1, 0.5), [(825, 605.3), (825, 829)]),
    ("e-fifodlq", "fifo", "fifodlq", "3 failures", "failure", (0.5, 1), (0.5, 0), []),
    ("e-score-rds", "score", "rds", "", "flow", (1, 0.25), (0.2, 1), [(1490.8, 570.5)]),
    ("e-score-ddb", "score", "ddbendpoint", "", "flow", (1, 0.5), (0, 0.5), []),
    ("e-query-ddb", "query", "ddbendpoint", "", "flow", (0.9, 1), (0.5, 0), [(1356.6, 524), (1706, 524)]),
    ("e-ddb-events", "ddbendpoint", "events", "", "flow", (1, 0.5), (0, 0.5), []),
    ("e-ddb-sessions", "ddbendpoint", "sessions", "", "flow", (1, 0.2), (0, 0.5), [(1748, 574.2), (1748, 341)]),
    ("e-stream", "events", "alert", "", "flow", (0.5, 1), (0.5, 0), []),
    ("e-email", "alert", "ses", "", "flow", (1, 0.5), (0, 0.5), []),
    ("e-alertdlq", "alert", "alertdlq", "after 3 retries", "failure", (0.5, 1), (0.5, 0), []),
]

# Numbered steps, placed beside the arrow they describe: (number, x, y, text)
STEPS = [
    (1, 268, 312, "Staff sign in with a password and an authenticator code. Cognito issues an ID token for 30 minutes."),
    (2, 268, 186, "The dashboard loads from CloudFront; its bucket stays private."),
    (3, 268, 470, "Every API call carries the API key (which application) and the ID token (which person). "
                  "The query Lambda checks the session and the role."),
    (4, 720, 497, "POST /upload stores the CSV in the raw bucket, through the S3 gateway endpoint."),
    (5, 537, 553, "The S3 event starts the ingest Lambda. Invalid rows go to quarantine with the reason."),
    (6, 821, 553, "One message per valid transaction, grouped by account so its order is kept."),
    (7, 1090, 553, "The score Lambda applies the rules and writes the ledger in one database transaction, "
                   "then the score and its rule breakdown to DynamoDB."),
    (8, 1897, 634, "The DynamoDB Stream passes HIGH scores to the alert Lambda, which emails compliance through SES."),
    (9, 1250, 178, "On the 1st of each month EventBridge runs the report Lambda; CSV and JSON go to the reports bucket."),
]

NOTES = [
    "No NAT Gateway and no internet route. The three VPC Lambdas reach S3 and DynamoDB through free gateway "
    "endpoints, and log in to PostgreSQL with IAM database authentication: no database password exists.",
    "Security groups: the Lambda group has no inbound rules; the RDS group accepts port 5432 only from the "
    "Lambda group and the tunnel instance.",
    "Journey steps (received, validated, queued, scored, alert sent, and every failure) are written to the "
    "events table by the ingest, score and alert Lambdas.",
]


def card(drawing, name, x, y, service, title, detail, failure, w=CARD_W, h=CARD_H):
    """A white card with the service's AWS icon on the left and its name on the right."""
    if service is None:          # the people using the system
        drawing.vertex(name, f"<b>{escape(title)}</b><br><font color=\"{MUTED}\">{escape(detail)}</font>",
                       f"rounded=1;arcSize=10;whiteSpace=wrap;html=1;{FONT}fontSize=12;fillColor=#FFFFFF;"
                       f"strokeColor={INK};strokeWidth=1.5;align=left;spacingLeft=56;fontColor={INK};", x, y, w, h)
        drawing.vertex(name + "-icon", "", f"sketch=0;outlineConnect=0;fontColor={INK};fillColor={INK};"
                       "strokeColor=none;dashed=0;html=1;aspect=fixed;shape=mxgraph.aws4.users;",
                       x + 10, y + (h - ICON_SIZE) / 2, ICON_SIZE, ICON_SIZE)
        return
    icon, category = ICON[service]
    colour = CATEGORY[category]
    border = (f"strokeColor={FAILURE};dashed=1;dashPattern=6 3;" if failure else f"strokeColor={colour};")
    drawing.vertex(name, f"<b>{escape(title)}</b><br><font color=\"{MUTED}\" style=\"font-size:11px\">{escape(detail)}</font>",
                   f"rounded=1;arcSize=10;whiteSpace=wrap;html=1;{FONT}fontSize=12;fillColor=#FFFFFF;{border}"
                   f"strokeWidth=1.5;align=left;spacingLeft=56;spacingRight=4;fontColor={INK};", x, y, w, h)
    drawing.vertex(name + "-icon", "", f"sketch=0;points=[];outlineConnect=0;fontColor={INK};fillColor={colour};"
                   "strokeColor=#FFFFFF;dashed=0;html=1;aspect=fixed;"
                   f"shape=mxgraph.aws4.resourceIcon;resIcon=mxgraph.aws4.{icon};",
                   x + 10, y + (h - ICON_SIZE) / 2, ICON_SIZE, ICON_SIZE)


def group(drawing, name, kind, label, x, y, w, h, dashed=False):
    icon, stroke, text = GROUPS[kind]
    drawing.vertex(name, label, f"points=[];outlineConnect=0;gradientColor=none;html=1;whiteSpace=wrap;{FONT}"
                   f"fontSize=12;fontStyle=1;container=0;pointerEvents=0;collapsible=0;recursiveResize=0;"
                   f"shape=mxgraph.aws4.group;grIcon=mxgraph.aws4.{icon};strokeColor={stroke};fillColor=none;"
                   f"verticalAlign=top;align=left;spacingLeft=30;fontColor={text};dashed={1 if dashed else 0};",
                   x, y, w, h)


def endpoint(drawing, name, title, x, y):
    """A VPC gateway endpoint, drawn on the VPC's edge."""
    drawing.vertex(name, title, f"sketch=0;outlineConnect=0;fontColor={INK};fillColor={CATEGORY['network']};"
                   f"strokeColor=none;dashed=0;html=1;{FONT}fontSize=10;fontStyle=1;aspect=fixed;"
                   "verticalLabelPosition=bottom;verticalAlign=top;align=center;labelBackgroundColor=#FFFFFF;"
                   "shape=mxgraph.aws4.endpoints;", x, y, 36, 36)


ARROW = {
    "flow": f"strokeColor={INK};strokeWidth=1.4;",
    "failure": f"strokeColor={FAILURE};strokeWidth=1.4;dashed=1;dashPattern=6 3;",
    "control": f"strokeColor={MUTED};strokeWidth=1.2;dashed=1;dashPattern=2 3;",
}


def architecture_drawio():
    width, height = 2770, 1140
    d = Drawing("FinTrust architecture")
    title_block(d, "FinTrust Transaction Insights and Risk Monitoring System: AWS architecture",
                "Region eu-north-1 (Stockholm) · serverless and event-driven, within the AWS Free Tier · "
                "the numbers follow one transaction and are explained on the right", width)

    group(d, "g-cloud", "cloud", "AWS Cloud", 262, 64, 2068, 1046)
    group(d, "g-region", "region", "Region eu-north-1 (Stockholm)", 278, 92, 2036, 900, dashed=True)
    group(d, "g-vpc", "vpc", "VPC 10.0.0.0/16 · no internet gateway, no NAT Gateway", VPC_X, VPC_Y, VPC_W, VPC_H)
    group(d, "g-subnets", "subnet", "Private subnets in eu-north-1a and eu-north-1b", VPC_X + 22, VPC_Y + 32,
          VPC_W - 44, VPC_H - 46)
    for name, (x, y, service, title, detail, failure) in architecture_cards().items():
        card(d, name, x, y, service, title, detail, failure)
    endpoint(d, "s3endpoint", "S3 gateway<br>endpoint", VPC_X - 18, 323)
    endpoint(d, "ddbendpoint", "DynamoDB<br>gateway endpoint", VPC_X + VPC_W - 18, 567)

    for edge_id, source, target, label, kind, exit_at, entry_at, points in ARCHITECTURE_ARROWS:
        style = (f"edgeStyle=orthogonalEdgeStyle;rounded=1;html=1;{FONT}fontSize=10;fontColor={INK};"
                 "labelBackgroundColor=#FFFFFF;endArrow=block;endFill=1;endSize=7;jumpStyle=arc;jumpSize=10;"
                 + ARROW[kind]
                 + f"exitX={exit_at[0]};exitY={exit_at[1]};exitDx=0;exitDy=0;"
                 + f"entryX={entry_at[0]};entryY={entry_at[1]};entryDx=0;entryDy=0;")
        d.edge(edge_id, style, source, target, label, points)

    for number, x, y, _text in STEPS:
        d.vertex(f"step-{number}", str(number), f"ellipse;whiteSpace=wrap;html=1;aspect=fixed;{FONT}fontSize=11;"
                 f"fontStyle=1;fillColor={INK};strokeColor=#FFFFFF;strokeWidth=2;fontColor=#FFFFFF;", x, y, 24, 24)

    # Supporting services, then global services.
    d.text("support-title", "<b>Used by every part of the system</b>", 300, 890, 900, 18, size=12,
           style=f"fontColor={MUTED};")
    for index, (service, title, detail) in enumerate(SUPPORT):
        card(d, f"support-{index}", 300 + index * 274, 912, service, title, detail, False, w=256)
    d.text("global-title", "<b>Global services</b>", 300, 1008, 600, 18, size=12, style=f"fontColor={MUTED};")
    for index, (service, title, detail) in enumerate(GLOBAL):
        card(d, f"global-{service}", 300 + index * 274, 1030, service, title, detail, False, w=256)
    d.text("free-tier", "<b>Free Tier sizing:</b> RDS db.t3.micro, stopped every night · DynamoDB 15 of 25 read "
           "and 25 of 25 write units · gateway endpoints instead of a NAT Gateway (about $32 a month saved) · "
           "Cognito Essentials, 10 000 monthly users free", 880, 1040, 1400, 40, size=11,
           style=f"fontColor={MUTED};")

    # The key, outside the AWS Cloud.
    kx = 2370
    d.text("key-title", "<b>How one transaction travels</b>", kx, 70, 360, 20, size=14)
    y = 100
    for number, _x, _y, text in STEPS:
        d.vertex(f"key-{number}", str(number), f"ellipse;whiteSpace=wrap;html=1;aspect=fixed;{FONT}fontSize=11;"
                 f"fontStyle=1;fillColor={INK};strokeColor=none;fontColor=#FFFFFF;", kx, y, 22, 22)
        lines = 1 + len(text) // 50
        d.text(f"key-{number}-text", escape(text), kx + 32, y + 2, 330, 16 * lines, size=11)
        y += 16 * lines + 14
    y += 6
    d.text("legend-title", "<b>Arrows</b>", kx, y, 360, 20, size=14)
    for index, (kind, meaning) in enumerate((("flow", "data or a request"), ("failure", "failure path: nothing is lost"),
                                             ("control", "check, schedule or admin access"))):
        line_y = y + 36 + index * 24
        d.edge(f"legend-{kind}", "html=1;endArrow=block;endFill=1;endSize=7;" + ARROW[kind],
               start=(kx, line_y), end=(kx + 56, line_y))
        d.text(f"legend-{kind}-text", meaning, kx + 68, line_y - 8, 290, 16, size=11)
    y += 36 + 3 * 24 + 8
    d.vertex("legend-failure-card", "dead-letter queue", f"rounded=1;arcSize=10;html=1;{FONT}fontSize=11;"
             f"fillColor=#FFFFFF;strokeColor={FAILURE};dashed=1;dashPattern=6 3;strokeWidth=1.5;fontColor={INK};",
             kx, y, 140, 28)
    d.text("legend-failure-card-text", "keeps what failed until it is fixed", kx + 152, y + 6, 210, 16, size=11)
    y += 52
    d.text("notes-title", "<b>Security and network</b>", kx, y, 360, 20, size=14)
    y += 28
    for index, note in enumerate(NOTES):
        lines = 1 + len(note) // 50
        d.text(f"note-{index}", "• " + escape(note), kx, y, 360, 16 * lines, size=11)
        y += 16 * lines + 10
    return d.xml(width, height)


# ----------------------------------------------------------------------------
# 2. ERD in UML class notation, read from docs/erd.md
# ----------------------------------------------------------------------------
ATTRIBUTE = re.compile(
    r"^(?:«(?P<key>PK|FK)» )?(?P<name>[a-z_]+) : (?P<type>[a-z]+(?:\(\d+(?:,\d+)?\))?)"
    r"(?P<nullable> \[0\.\.1\])?(?P<unique> \{unique\})?$"
)
RELATIONSHIP = re.compile(r'^\s*([a-z_]+) "([^"]+)" -- "([^"]+)" ([a-z_]+) : (.+)$', re.M)


def read_erd():
    """The UML class diagram in docs/erd.md, as
    tables:        {table: [(name, type, key, nullable, unique)]}   in drawing order
    relationships: [(parent, parent multiplicity, child multiplicity, child, name)]"""
    text = (ROOT / "docs" / "erd.md").read_text(encoding="utf-8")
    start = text.index("```mermaid")
    block = text[start:text.index("```", start + 3)]
    tables = {}
    for name, body in re.findall(r"^\s*class ([a-z_]+) \{\n(.*?)^\s*\}", block, flags=re.M | re.S):
        columns = []
        for line in body.strip().splitlines():
            line = line.strip()
            if line == "<<table>>":
                continue
            match = ATTRIBUTE.match(line)
            if not match:
                raise ValueError(f"cannot read '{line}' in table {name} in docs/erd.md")
            columns.append((match["name"], match["type"], match["key"] or "", bool(match["nullable"]),
                            bool(match["unique"])))
        tables[name] = columns
    relationships = [(parent, one, many, child, label.strip()) for parent, one, many, child, label
                     in RELATIONSHIP.findall(block)]
    return tables, relationships


ERD_POSITION = {"customers": (40, 130), "accounts": (420, 130), "transactions": (840, 130),
                "alerts": (1290, 130), "risk_events": (420, 540)}
ERD_WIDTH = 300
ROW_H, HEAD_H = 22, 44

# How each relationship is drawn: exit and entry sides, waypoints, and where its
# name sits.
ERD_ROUTES = {
    ("customers", "accounts"): ((1, 0.3), (0, 0.3), []),
    ("accounts", "transactions"): ((1, 0.3), (0, 0.18), []),
    ("accounts", "risk_events"): ((0.5, 1), (0.5, 0), []),
    ("transactions", "risk_events"): ((0.3, 1), (1, 0.35), [(930, 640)]),
    ("transactions", "alerts"): ((1, 0.18), (0, 0.3), []),
}
DYNAMO_TABLES = [
    ("fintrust-capstone-events", [("«PK» account_id : S", "partition key"),
                                  ("«SK» tx_timestamp : S", "sort key: time#transaction id#record kind"),
                                  ("record_type : S", "SCORE, EVENT or ALERT"),
                                  ("transaction_id : S", "key of the by_transaction index"),
                                  ("risk_score : N, risk_level : S, rules : L", "on SCORE items; the Stream filters on risk_level"),
                                  ("stage : S, at : S, detail : S", "on EVENT items: one step of the journey"),
                                  ("expires_at : N", "time-to-live: deleted after 12 months")]),
    ("fintrust-capstone-sessions", [("«PK» session_id : S", "the sign-in's origin_jti from Cognito"),
                                    ("username : S, display_name : S, roles : L", "who, and which role"),
                                    ("status : S, end_reason : S", "ACTIVE or ENDED, and why it ended"),
                                    ("started_epoch, last_seen_epoch, ends_epoch : N", "the 15-minute idle and 8-hour limits"),
                                    ("user_agent : S", "which browser signed in"),
                                    ("expires_at : N", "time-to-live: deleted after 90 days")]),
]


def column_text(name, sql_type, key, nullable, unique):
    text = f"{name} : {sql_type}"
    if nullable:
        text += " [0..1]"
    if unique:
        text += " {unique}"
    if key:
        text = f"«{key}» " + text
    return text


def erd_drawio(tables, relationships):
    width, height = 1640, 1010
    d = Drawing("FinTrust ERD")
    title_block(d, "FinTrust ledger: entity relationship diagram (UML class notation)",
                "PostgreSQL 16 on Amazon RDS · third normal form · generated from docs/erd.md, which a test "
                "compares with the deployed schema", width)
    for table, columns in tables.items():
        x, y = ERD_POSITION[table]
        table_height = HEAD_H + ROW_H * len(columns) + 8
        d.vertex(f"t-{table}", f"«table»<br><b>{table}</b>",
                 f"swimlane;html=1;whiteSpace=wrap;{FONT}fontSize=13;fontStyle=0;align=center;verticalAlign=top;"
                 f"childLayout=stackLayout;horizontal=1;startSize={HEAD_H};horizontalStack=0;resizeParent=1;"
                 "resizeParentMax=0;resizeLast=0;collapsible=0;marginBottom=0;fillColor=#E8F1FB;swimlaneFillColor=#FFFFFF;"
                 f"strokeColor=#1F5FA8;strokeWidth=1.5;fontColor={INK};rounded=0;",
                 x, y, ERD_WIDTH, table_height)
        for index, column in enumerate(columns):
            name, sql_type, key, nullable, unique = column
            emphasis = "fontStyle=4;" if key == "PK" else ("fontStyle=2;" if key == "FK" else "")
            d.vertex(f"t-{table}-{name}", escape(column_text(*column)),
                     f"text;html=1;{FONT}fontSize=12;align=left;verticalAlign=middle;spacingLeft=10;"
                     f"strokeColor=none;fillColor=none;fontColor={INK};{emphasis}"
                     "points=[[0,0.5],[1,0.5]];portConstraint=eastwest;",
                     0, HEAD_H + index * ROW_H, ERD_WIDTH, ROW_H, parent=f"t-{table}")
    for parent, one, many, child, label in relationships:
        exit_at, entry_at, points = ERD_ROUTES[(parent, child)]
        style = (f"edgeStyle=orthogonalEdgeStyle;rounded=0;html=1;{FONT}fontSize=11;fontColor={INK};"
                 f"strokeColor={INK};strokeWidth=1.3;endArrow=none;startArrow=none;labelBackgroundColor=#FFFFFF;"
                 f"exitX={exit_at[0]};exitY={exit_at[1]};exitDx=0;exitDy=0;"
                 f"entryX={entry_at[0]};entryY={entry_at[1]};entryDx=0;entryDy=0;")
        d.edge(f"r-{parent}-{child}", style, f"t-{parent}", f"t-{child}", f"<i>{escape(label)}</i> ▸", points,
               labels=[(one, -0.85, 0, -10), (many, 0.85, 0, -10)])

    # The DynamoDB tables are not relational, so they are drawn apart, keys only.
    fx, fy, fw = 1180, 380, 420
    row_h = 34
    heights = [HEAD_H + row_h * len(attributes) for _table, attributes in DYNAMO_TABLES]
    d.vertex("dynamo-frame", "<b>DynamoDB</b>: not relational, so no joins or foreign keys; key design only",
             f"rounded=0;html=1;whiteSpace=wrap;{FONT}fontSize=12;fillColor=none;strokeColor=#C925D1;dashed=1;"
             f"verticalAlign=top;align=left;spacingLeft=10;spacingTop=6;fontColor={INK};",
             fx, fy, fw, 44 + sum(heights) + 18 * len(heights))
    y = fy + 40
    for index, (table, attributes) in enumerate(DYNAMO_TABLES):
        d.vertex(f"dd-{index}", f"«DynamoDB table»<br><b>{table}</b>",
                 f"swimlane;html=1;whiteSpace=wrap;{FONT}fontSize=12;fontStyle=0;align=center;verticalAlign=top;"
                 f"childLayout=stackLayout;horizontal=1;startSize={HEAD_H};horizontalStack=0;resizeParent=1;"
                 "resizeParentMax=0;resizeLast=0;collapsible=0;marginBottom=0;fillColor=#F7EAF8;"
                 f"swimlaneFillColor=#FFFFFF;strokeColor=#C925D1;strokeWidth=1.5;fontColor={INK};",
                 fx + 16, y, fw - 32, heights[index])
        for row, (attribute, meaning) in enumerate(attributes):
            d.vertex(f"dd-{index}-{row}", f"{escape(attribute)}<br><font color=\"{MUTED}\" style=\"font-size:10px\">"
                     f"{escape(meaning)}</font>",
                     f"text;html=1;{FONT}fontSize=12;align=left;verticalAlign=middle;spacingLeft=10;strokeColor=none;"
                     f"fillColor=none;fontColor={INK};", 0, HEAD_H + row * row_h, fw - 32, row_h, parent=f"dd-{index}")
        y += heights[index] + 18

    # How to read it.
    ky = 820
    d.text("key-title", "<b>How to read it</b>", 40, ky, 700, 20, size=14)
    rows = [
        ("«table»", "one PostgreSQL table; each line is a column, as name : type"),
        ("«PK» underlined", "primary key: identifies each row"),
        ("«FK» italic", "foreign key: refers to the primary key of the table it is linked to"),
        ("[0..1]", "the column may be empty (NULL); every other column is NOT NULL"),
        ("{unique}", "no two rows may hold the same value"),
        ("1 · 0..1 · 0..*", "multiplicity at each end: exactly one, zero or one, zero or more"),
        ("owns ▸", "the relationship's name, read in the direction of the arrowhead"),
    ]
    for index, (notation, meaning) in enumerate(rows):
        d.text(f"key-{index}-n", f"<b>{escape(notation)}</b>", 40, ky + 28 + index * 20, 150, 18, size=11)
        d.text(f"key-{index}-m", escape(meaning), 196, ky + 28 + index * 20, 560, 18, size=11)
    d.text("checks", "<b>Rules the diagram cannot show</b><br>CHECK constraints on every type, channel, level and "
           "status · amount &gt; 0 · one alert per transaction · a resolved alert records who and when · only a "
           "manual flag names a person · ON DELETE RESTRICT on every foreign key.<br><br><b>Indexes</b><br>"
           "transactions (account_id), (tx_date), (account_id, tx_date), (risk_level) · accounts (customer_id) · "
           "risk_events (account_id), (transaction_id) · alerts (status)", 800, ky, 350, 170, size=11)
    return d.xml(width, height)


# ----------------------------------------------------------------------------
# 3. UML sequence diagrams
# ----------------------------------------------------------------------------
LIFELINE_GAP = 214
HEAD_TOP, HEAD_HEIGHT = 92, 54
STEP = 40


class Sequence:
    """A UML sequence diagram, laid out from top to bottom as messages are added."""

    def __init__(self, name, participants):
        self.d = Drawing(name)
        self.order = [key for key, *_ in participants]
        self.participants = {key: (title, subtitle, kind) for key, title, subtitle, kind in participants}
        self.y = HEAD_TOP + HEAD_HEIGHT + 34
        self.number = 0
        self.active = {}          # participant -> list of open activation start ys
        self.bars = []            # (participant, top, bottom, depth)
        self.frames = []          # (kind, guard, top, bottom, left, right, dividers)
        self.items = []           # deferred messages and notes, drawn after the bars

    def x(self, key):
        return 120 + self.order.index(key) * LIFELINE_GAP

    def _edge_x(self, key, toward):
        depth = len(self.active.get(key, []))
        offset = 5 * depth if depth else 0
        centre = self.x(key)
        return centre + offset if toward > centre else centre - offset

    def activate(self, key):
        self.active.setdefault(key, []).append(self.y - 6)

    def deactivate(self, key, extra=10):
        top = self.active[key].pop()
        self.bars.append((key, top, self.y - STEP + extra, len(self.active[key])))

    def message(self, source, target, text, kind="sync", activate=False, deactivate=False):
        """kind: sync (solid, filled head), async (solid, open head) or reply (dashed, open head)."""
        self.number += 1
        if activate:
            self.activate(target)
        x1 = self._edge_x(source, self.x(target))
        x2 = self._edge_x(target, self.x(source))
        self.items.append(("message", x1, x2, self.y, f"{self.number}: {text}", kind))
        self.y += STEP
        if deactivate:
            self.deactivate(source)

    def self_call(self, key, text):
        self.number += 1
        x = self.x(key) + 5 * len(self.active.get(key, []))
        self.items.append(("self", x, self.y, f"{self.number}: {text}"))
        self.y += STEP + 16

    def note(self, first, last, text, height=34):
        left = self.x(first) - 90
        right = self.x(last) + 90
        self.items.append(("note", left, self.y - 14, right - left, height, text))
        self.y += height + 10

    def frame(self, kind, guard, first, last):
        """Open a combined fragment (loop, alt, opt) spanning participants first..last. Returns a handle."""
        handle = {"kind": kind, "guard": guard, "top": self.y - 22, "first": first, "last": last, "dividers": []}
        self.y += 18
        return handle

    def divider(self, handle, guard):
        handle["dividers"].append((self.y - 14, guard))
        self.y += 30

    def close(self, handle):
        left = self.x(handle["first"]) - 96
        right = self.x(handle["last"]) + 96
        self.frames.append((handle["kind"], handle["guard"], handle["top"], self.y - 12, left, right,
                            handle["dividers"]))
        self.y += 18

    def xml(self, title, subtitle):
        bottom = self.y + 30
        width = self.x(self.order[-1]) + 140
        height = bottom + 40
        d = self.d
        title_block(d, title, subtitle, width)
        for key in self.order:
            title_text, subtitle_text, kind = self.participants[key]
            x = self.x(key)
            if kind == "actor":
                d.vertex(f"p-{key}", f"<b>{escape(title_text)}</b>",
                         f"shape=umlActor;verticalLabelPosition=bottom;verticalAlign=top;html=1;{FONT}fontSize=12;"
                         f"fillColor=#FFFFFF;strokeColor={INK};fontColor={INK};", x - 14, HEAD_TOP - 6, 28, 44)
            else:
                d.vertex(f"p-{key}", f"<b>{escape(title_text)}</b><br><font color=\"{MUTED}\" "
                         f"style=\"font-size:10px\">{escape(subtitle_text)}</font>",
                         f"rounded=0;whiteSpace=wrap;html=1;{FONT}fontSize=12;fillColor=#E8F1FB;strokeColor=#1F5FA8;"
                         f"fontColor={INK};", x - 96, HEAD_TOP, 192, HEAD_HEIGHT)
            d.edge(f"life-{key}", f"html=1;endArrow=none;dashed=1;dashPattern=4 4;strokeColor={MUTED};strokeWidth=1;",
                   start=(x, HEAD_TOP + HEAD_HEIGHT), end=(x, bottom))
        for index, (kind, guard, top, frame_bottom, left, right, dividers) in enumerate(self.frames):
            d.vertex(f"frame-{index}", kind, f"shape=umlFrame;whiteSpace=wrap;html=1;{FONT}fontSize=11;fontStyle=1;"
                     f"width=52;height=22;boundedLbl=1;verticalAlign=middle;align=center;fillColor=none;"
                     f"swimlaneFillColor=none;strokeColor={MUTED};fontColor={INK};",
                     left, top, right - left, frame_bottom - top)
            d.text(f"frame-{index}-guard", f"[{escape(guard)}]", left + 60, top + 4, right - left - 70, 16, size=11,
                   style=f"fontColor={MUTED};fontStyle=2;labelBackgroundColor=#FFFFFF;")
            for divider_index, (divider_y, divider_guard) in enumerate(dividers):
                d.edge(f"frame-{index}-else-{divider_index}", f"html=1;endArrow=none;dashed=1;dashPattern=6 4;"
                       f"strokeColor={MUTED};", start=(left, divider_y), end=(right, divider_y))
                d.text(f"frame-{index}-else-{divider_index}-guard", f"[{escape(divider_guard)}]", left + 8,
                       divider_y + 3, right - left - 16, 16, size=11,
                       style=f"fontColor={MUTED};fontStyle=2;labelBackgroundColor=#FFFFFF;")
        for index, (key, top, bar_bottom, depth) in enumerate(sorted(self.bars, key=lambda bar: (bar[3], bar[1]))):
            d.vertex(f"bar-{index}", "", f"rounded=0;html=1;fillColor=#FFFFFF;strokeColor={INK};strokeWidth=1;",
                     self.x(key) - 5 + 5 * depth, top, 10, max(bar_bottom - top, 14))
        for index, item in enumerate(self.items):
            if item[0] == "message":
                _, x1, x2, y, text, kind = item
                style = {"sync": "endArrow=block;endFill=1;", "async": "endArrow=open;endFill=0;",
                         "reply": "endArrow=open;endFill=0;dashed=1;dashPattern=6 4;"}[kind]
                d.edge(f"m-{index}", f"html=1;{FONT}fontSize=11;fontColor={INK};strokeColor={INK};strokeWidth=1.2;"
                       f"endSize=8;verticalAlign=bottom;labelBackgroundColor=#FFFFFF;{style}", value=escape(text),
                       start=(x1, y), end=(x2, y))
            elif item[0] == "self":
                _, x, y, text = item
                d.edge(f"m-{index}", f"html=1;{FONT}fontSize=11;fontColor={INK};strokeColor={INK};strokeWidth=1.2;"
                       "endArrow=block;endFill=1;endSize=8;align=left;spacingLeft=4;labelBackgroundColor=none;"
                       "edgeStyle=none;", value="", start=(x + 5, y), end=(x + 5, y + 22),
                       points=[(x + 40, y), (x + 40, y + 22)])
                d.text(f"m-{index}-text", escape(text), x + 48, y - 2, 560, 18, size=11,
                       style="labelBackgroundColor=#FFFFFF;")
            else:
                _, left, top, w, h, text = item
                d.vertex(f"n-{index}", escape(text), f"shape=note;whiteSpace=wrap;html=1;size=12;{FONT}fontSize=11;"
                         f"fillColor=#FFF8E1;strokeColor=#A6792B;fontColor={INK};align=left;spacingLeft=8;"
                         "verticalAlign=middle;", left, top, w, h)
        return d.xml(width, height)


def pipeline_sequence_drawio():
    s = Sequence("FinTrust pipeline sequence", [
        ("officer", "Compliance officer", "", "actor"),
        ("api", "API Gateway + query Lambda", "POST /upload", "box"),
        ("raw", "S3 raw bucket", "KMS-encrypted", "box"),
        ("ingest", "ingest Lambda", "outside the VPC", "box"),
        ("fifo", "SQS FIFO queue", "group = account_id", "box"),
        ("score", "score Lambda", "in the VPC", "box"),
        ("rds", "RDS PostgreSQL", "the ledger", "box"),
        ("ddb", "DynamoDB events", "score + journey", "box"),
        ("alert", "alert Lambda", "Stream, HIGH only", "box"),
        ("ses", "Amazon SES", "compliance mailbox", "box"),
    ])
    s.message("officer", "api", "POST /upload (CSV, API key, ID token)", activate=True)
    s.message("api", "raw", "PutObject uploads/<file>.csv")
    s.message("api", "officer", "202 Accepted: processing continues", "reply", deactivate=True)
    s.message("raw", "ingest", "ObjectCreated event (asynchronous)", "async", activate=True)
    s.self_call("ingest", "validate every row; invalid rows to the quarantine bucket with the reason")
    s.message("ingest", "ddb", "journey: RECEIVED, VALIDATED")
    s.message("ingest", "fifo", "SendMessage, one per valid row (deduplication id = transaction id)")
    s.message("ingest", "ddb", "journey: QUEUED", deactivate=True)
    outcome = s.frame("alt", "scoring succeeds", "fifo", "ses")
    s.message("fifo", "score", "batch of messages (Lambda polls the queue)", "async", activate=True)
    s.message("score", "rds", "BEGIN; account exists? transactions in the last hour?", activate=True)
    s.self_call("score", "apply the rules")
    s.message("score", "rds", "INSERT transaction, risk_events, alert if HIGH; COMMIT")
    s.message("rds", "score", "committed", "reply", deactivate=True)
    s.message("score", "ddb", "PutItem score + rule breakdown; journey: SCORED", deactivate=True)
    s.message("ddb", "alert", "Stream record (filter: risk_level = HIGH)", "async", activate=True)
    s.message("alert", "ses", "SendEmail: transaction, score, rules that fired")
    s.message("alert", "ddb", "journey: ALERT_SENT", deactivate=True)
    s.divider(outcome, "scoring fails, for example the account is not in the ledger yet")
    s.message("fifo", "score", "message delivered", "async", activate=True)
    s.message("score", "rds", "ROLLBACK: nothing is half-written")
    s.message("score", "ddb", "journey: SCORE_FAILED (attempt n)")
    s.message("score", "fifo", "batchItemFailures: [message id]", "reply", deactivate=True)
    s.note("fifo", "score", "SQS shows the message again after the visibility timeout. After 3 failed attempts "
           "it moves to the FIFO dead-letter queue and a CloudWatch alarm emails the team.", height=46)
    s.note("fifo", "rds", "Recovery: fix the cause (scripts/onboard_account.py), then scripts/redrive_dlq.sh "
           "moves the messages back. They are scored with no data re-entered.", height=46)
    s.close(outcome)
    return s.xml("One transaction, from upload to alert (UML sequence diagram)",
                 "Steps 4 to 8 of the architecture diagram, and how a failed scoring is recovered without losing "
                 "the transaction")


def sign_in_sequence_drawio():
    s = Sequence("FinTrust sign-in sequence", [
        ("person", "Compliance officer", "", "actor"),
        ("browser", "Dashboard", "browser: auth.js, app.js", "box"),
        ("cognito", "Amazon Cognito", "managed login, user pool", "box"),
        ("apigw", "API Gateway", "API key + Cognito authorizer", "box"),
        ("query", "query Lambda", "session and role checks", "box"),
        ("sessions", "DynamoDB sessions", "fintrust-capstone-sessions", "box"),
    ])
    s.message("person", "browser", "Sign in to the risk desk", activate=True)
    s.self_call("browser", "random state and PKCE verifier; challenge = SHA-256(verifier)")
    s.message("browser", "cognito", "GET /oauth2/authorize (client id, challenge, state)", activate=True)
    s.message("person", "cognito", "username and password, then the 6-digit authenticator code")
    s.message("cognito", "browser", "redirect back: one-time code + the same state", "reply", deactivate=True)
    s.self_call("browser", "the state must match, or the answer is refused")
    s.message("browser", "cognito", "POST /oauth2/token (code + verifier)", activate=True)
    s.message("cognito", "browser", "ID token (30 minutes) + refresh token (8 hours)", "reply", deactivate=True)
    s.message("browser", "apigw", "POST /session (x-api-key, Authorization: ID token)", activate=True)
    s.self_call("apigw", "API key and usage plan; token signature, issuer and expiry")
    s.message("apigw", "query", "request + claims: username, groups, origin_jti", activate=True)
    s.message("query", "sessions", "PutItem if not exists (session_id = origin_jti)")
    s.message("query", "apigw", "201: name, role, permissions", "reply", deactivate=True)
    s.message("apigw", "browser", "201", "reply", deactivate=True)

    loop = s.frame("loop", "every later request, for example GET /transactions", "browser", "sessions")
    s.message("browser", "apigw", "GET /transactions + ID token (renewed before it expires)", activate=True)
    s.message("apigw", "query", "request + claims", activate=True)
    s.message("query", "sessions", "GetItem: ACTIVE? idle under 15 minutes? under 8 hours?")
    s.message("query", "apigw", "200 rows · 401 SESSION_ENDED · 403 wrong role", "reply", deactivate=True)
    s.message("apigw", "browser", "response", "reply", deactivate=True)
    s.close(loop)

    out = s.frame("opt", "the person signs out, or 15 minutes pass without activity", "browser", "sessions")
    s.message("browser", "apigw", "DELETE /session", activate=True)
    s.message("apigw", "query", "request + claims", activate=True)
    s.message("query", "sessions", "UpdateItem: status ENDED, reason SIGNED_OUT or IDLE")
    s.message("query", "apigw", "200: session ended", "reply", deactivate=True)
    s.message("apigw", "browser", "200", "reply", deactivate=True)
    s.message("browser", "cognito", "POST /oauth2/revoke (refresh token), then GET /logout", deactivate=True)
    s.note("browser", "sessions", "The same ID token used again before it expires gets 401 SESSION_ENDED: "
           "the session record, not the token, decides.", height=34)
    s.close(out)
    return s.xml("Sign-in, session and sign-out (UML sequence diagram)",
                 "OAuth 2.0 authorization code flow with PKCE (RFC 7636) · MFA by authenticator app · "
                 "sessions in DynamoDB")


# ----------------------------------------------------------------------------
DIAGRAMS = {
    "architecture.drawio": architecture_drawio,
    "erd.drawio": lambda: erd_drawio(*read_erd()),
    "pipeline-sequence.drawio": pipeline_sequence_drawio,
    "sign-in-sequence.drawio": sign_in_sequence_drawio,
}


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    for name, build in DIAGRAMS.items():
        (OUT / name).write_text(build(), encoding="utf-8")
        print(f"Wrote docs/diagrams/{name}")
    print("Next: python scripts/export_diagrams.py --webapp <draw.io src/main/webapp> (writes the .svg and .pdf)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
