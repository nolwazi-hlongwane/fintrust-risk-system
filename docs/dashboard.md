# The Web Dashboard

**Brief slot:** Week 14 Day 4 (part 2), confirmed with the facilitator as an addition to the backend brief
**Charts added:** Week 15 Day 1 (corrections and dashboard analytics), see [Charts](#charts-week-15-day-1)
**Files:** `frontend/` (the pages), `infrastructure/frontend/` (hosting)

The dashboard is the compliance officer's view of the system. It is five small files of plain HTML, CSS and JavaScript with no framework, no build step and no third-party code. Every number on screen comes from the real API; nothing is mocked.

## What it does

| Section | What you can do | API routes used |
|---|---|---|
| **Overview** | See totals, the split of Low, Medium and High and which rules fired; six charts (activity over time, score spread, alerts raised and resolved, fraud ranking, running balance); for any date range, with quick periods and zoom by selecting a bar | `GET /insights`, `GET /insights/trends`, `GET /accounts/{id}/balance` |
| **Transactions** | Search by account, risk level and date; page through results; select a row to open its journey | `GET /transactions`, `GET /transactions/{id}/journey` |
| **Alerts** | See open or resolved alerts; resolve one with your username and a note | `GET /alerts`, `POST /resolve` |
| **Accounts** | See every account ranked by risk points; flag an account with a reason | `GET /accounts`, `POST /flag` |
| **Upload a file** | Send a CSV, then watch each transaction move through the pipeline live | `POST /upload`, `GET /transactions/{id}/journey` |

### The journey panel

Selecting any transaction opens a panel that answers two questions:

- **Why this score?** The score out of 100 and each rule that fired with its points, for example "+50 High amount: Amount 88750.00 is above 50000".
- **What happened to it?** Each step with its time: received, passed validation, queued, scored, alert emailed. Failed scoring attempts appear in red with the reason, ending in "Moved to the dead-letter queue; nothing is lost."

This is the explainability part of the project: no score is a black box, and a failed transaction is visible, not silently missing.

Transactions loaded as opening data (the three months of seed history) did not pass through the pipeline, so they have no steps. Their score and rules are read from the ledger instead.

### Live progress after an upload

After a file is accepted, the page reads the transaction ids from the file and asks the API where each one is every four seconds, for up to four minutes. It follows the first 12, to stay well inside the API's limit of 10 requests a second. A row that fails validation never gets a record, and the page says so.

## Charts (Week 15 Day 1)

The Overview shows six pictures. Each one is a view of something the backend already works out; the browser only draws it. That keeps the project backend-heavy, as the brief intends, and means every chart can be checked against SQL.

| Chart | The question it answers | Where the numbers come from |
|---|---|---|
| Risk levels (share bar) | What share of transactions is Low, Medium, High? | `GET /insights` |
| Rules that fired | Which risk rule is doing the work? | `GET /insights` |
| Activity over time | How much was processed per day, week or month, and when did risk rise? | `GET /insights/trends` → `activity` (`date_trunc` on South African days) |
| How scores are spread | Where do the thresholds cut the scores? | `GET /insights/trends` → `score_distribution`, with the Medium (40) and High (70) thresholds marked |
| Alerts raised and resolved | Is compliance keeping up? Median time to resolve | `GET /insights/trends` → `alert_flow` (`percentile_cont`) |
| Fraud ranking | Which accounts stand out, and did any break the 30-day burst rule? | `GET /insights/trends` → `account_ranking`: the `PERCENT_RANK` and rolling 30-day window of `sql/risk_report.sql`, tested to agree with it |
| Running balance | What did this account's money do, and where were its high-risk transactions? | `GET /accounts/{id}/balance`: `sql/monthly_summary.sql`, the hand-verified window function, run as it is |

What makes them useful rather than decoration:

- **They are linked.** Selecting an account in the ranking draws its running balance. Selecting a point on the balance opens that transaction's journey: why it got its score and every step it took. Selecting a week in Activity or Alerts zooms the whole page into that week (bars become days).
- **The numbers reconcile.** Activity adds up exactly to the figures at the top; the ranking agrees with `risk_report.sql`; the balance agrees with `monthly_summary.sql` and the eight rows worked by hand in `docs/sql-analytics.md`. Integration tests check all three, and `scripts/verify_dashboard.py` checks the first on the real account.
- **A chosen period keeps the true balance.** The balance is worked out over the account's whole history first, then rows outside the period are dropped. Filtering first would restart it at zero.
- **Every chart has its numbers.** "Show the numbers" opens a table of exactly what is drawn: the accessible version for screen readers, and the auditable version for anyone who wants to check.
- **Keyboard and screen reader.** Each chart can be focused; the arrow keys move from bar to bar (or point to point), Enter selects, and the tooltip is read out.
- **Colour is never the only signal.** The Low, Medium and High fill colours were checked with a colour-vision simulation (OKLab ΔE, deuteranopia and protanopia): the old Medium and High were 3.3 apart for a deuteranope, nearly the same colour; the new ones are at least 8.0 apart. Every chart also has a legend, a tooltip naming the level, and its table.
- **No chart library.** The charts are SVG built by `frontend/charts.js` (under 400 lines). A library would mean loading third-party code, which the Content-Security-Policy (`script-src 'self'`) forbids, or relaxing that policy. It would also be code I could not explain line by line.
- **Failure is contained.** The figures (`/insights`) and the charts (`/insights/trends`) are asked for together but separately. If one fails, the other still shows, with the request id of the failure.

### The Total value figure

The five figures at the top used to share the row equally, so a rand amount such as R 6 038 764,84 (about twice as long as a count) ran into the Active accounts figure. The row is now six equal columns and the money figure takes two; on smaller screens the money figure takes a row of its own and the others go two to a row. The value never wraps and its size scales with the screen (`clamp()`). A test guards the layout. "Active accounts" is now **"Accounts with transactions"**: it counts accounts with activity in the period, which includes flagged accounts, so "active" was misleading next to an account whose status is Flagged.

## How it is hosted

```mermaid
flowchart LR
    B[Browser] -->|HTTPS| CF[CloudFront<br/>adds security headers]
    CF -->|signed request, OAC| S3[(Private S3 bucket<br/>dashboard files)]
    B -->|HTTPS + x-api-key| API[API Gateway]
    API --> Q[query Lambda]
```

| Part | Setting | Why |
|---|---|---|
| S3 bucket `fintrust-capstone-dashboard-<account>` | All public access blocked; not an S3 website | The files can only be reached through CloudFront |
| Origin Access Control (OAC) | CloudFront signs every request to S3 | S3 can tell the request really came from CloudFront |
| Bucket policy | Allows `s3:GetObject` to the CloudFront service, only when the request comes from this one distribution; denies anything not over TLS | Another CloudFront distribution, even in another account, cannot read the bucket |
| CloudFront | Redirects http to https; read-only methods (GET, HEAD) | Nothing is ever sent unencrypted |
| Response headers policy | Content-Security-Policy, HSTS, `X-Frame-Options: DENY`, `nosniff`, no referrer | See below |
| Encryption at rest | SSE-S3 | The files are public web pages with no data in them, and using the project KMS key would mean giving CloudFront rights on it |

Deploy or update it with `./infrastructure/frontend/deploy_frontend.sh`. The script also writes `config.js` with the API address, tells the API to accept browser calls from the dashboard's address only, and redeploys the API Lambda (with `monthly_summary.sql` and `risk_report.sql` packaged inside it) and the API (with any new routes in `openapi.json`).

## Security

| Risk | Control |
|---|---|
| The API key leaks | It is typed in by the user and kept in `sessionStorage`: this tab only, gone when the tab closes. It is never in a file, a URL or the page's HTML. A test fails the build if a key-shaped string appears in `frontend/` |
| A value from the API runs as script (cross-site scripting) | The script puts text on the page with `textContent` only; it never builds HTML from data. A test fails the build if `innerHTML` or similar appears |
| Injected script anyway | The Content-Security-Policy allows only the page's own script file, no inline script or style, and connections only to this project's API address |
| Another website calls the API using a logged-in user's browser | CORS: the API returns the dashboard's address as the only allowed origin, so a browser will not let any other site read a response. API Gateway's own error responses carry the same address |
| The page is shown inside another site's frame to trick clicks | `frame-ancestors 'none'` and `X-Frame-Options: DENY` |
| Someone reads the files straight from S3 | Refused (403). Only the distribution can read the bucket |

What CORS does and does not do: it stops **other websites** from reading the API through a visitor's browser. It is not authentication. A script or `curl` ignores CORS, which is why every route still needs the API key and the usage plan still applies.

A note on the API key in a browser: an API key identifies an application, not a person. For a production bank system the dashboard would sign users in with Amazon Cognito (or the bank's identity provider) and the API would check a token per user. The brief specifies API key authentication, so that is what is built, with the key never stored.

## Running it on your own computer

```bash
cd frontend && python3 -m http.server 8080
```

Open `http://localhost:8080`, enter the API address and key. The deployed API only accepts browser calls from the CloudFront address, so for local development add your local address to the allowed list, redeploy, and remove it afterwards:

```bash
aws ssm put-parameter --name /fintrust/dev/cors_allowed_origin --type String --overwrite \
    --value "$(aws ssm get-parameter --name /fintrust/dev/dashboard_url --query Parameter.Value --output text),http://localhost:8080"
./infrastructure/lambda/deploy_function.sh query
```

## Design

A quiet page for people who read tables all day: bottle-green ink on cool paper, one brass accent for "where you are", and three colours (green, amber, red) used only for risk and never for decoration. The one distinctive element is the journey, drawn as a route with a station for each step. It works on a phone, shows keyboard focus, and respects the reduced-motion setting.
