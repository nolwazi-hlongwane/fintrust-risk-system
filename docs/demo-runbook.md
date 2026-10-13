# Live Demonstration Runbook

**For:** Week 16, Round 1 (live demo and technical panel, rubric C7)
**Shows, in order:** upload → ETL → store → score → dashboard → alert, then failure and recovery, then the monthly report

The rubric's top mark needs the live system shown end to end without failure. A demo fails for dull reasons (a stopped database, an expired login, a phone with a flat battery), so most of this page is preparation.

## The day before

```bash
cd ~/Downloads/Capstone/fintrust-risk-system && source .venv/bin/activate
aws sts get-caller-identity          # still signed in as the IAM user, not root
./scripts/rds_start.sh               # about 5 minutes
./scripts/db_tunnel.sh               # in a second terminal window; leave it open
python scripts/e2e_test.py           # full rehearsal, about 9 minutes; must end in END-TO-END TEST PASSED
./scripts/rds_stop.sh
```

If anything fails, there is a day to fix it. Also check the alert emails are arriving in the inbox and not in spam, and that you can sign in to the dashboard: password, then the code from the authenticator app on your phone.

## One hour before

1. `./scripts/rds_start.sh`, then open the tunnel in a second terminal window.
2. `python scripts/e2e_test.py --skip-dlq` (2 minutes). It must pass. Then clear the message it leaves behind, using the two commands it prints.
3. `python scripts/make_demo_file.py`. Note the file path and the two commands it prints.
4. Phone charged, authenticator app open on the FinTrust entry.
5. Open these browser tabs and sign in to each:
   - the dashboard (`aws ssm get-parameter --name /fintrust/dev/dashboard_url --query Parameter.Value --output text`): sign in with your password and authenticator code; the Overview charts load (if they show an error, the figures above them still work: carry on). Choose the appearance that suits the room: **Light** on a projector, **Dark** on a laptop screen. A session ends after 15 minutes without activity; the page warns a minute before, so choose **Stay signed in** if you are talking for a while
   - CloudWatch → Dashboards → `fintrust-capstone-operations`, time range 30 minutes, auto-refresh 10 seconds
   - S3 → the `fintrust-capstone-reports-...` bucket → `monthly/`
   - the email inbox
   - `docs/diagrams/architecture.pdf`
6. Increase the terminal font size. Close anything with notifications.

The nightly automatic stop runs at 20:00 and does not affect a daytime demo.

## The demonstration (about 10 minutes)

| Time | Do | Say |
|---|---|---|
| 0:00 | Show the architecture diagram | "Staff sign in through Amazon Cognito with a password and an authenticator code. A file lands in S3. The ingest Lambda validates each row. Good rows go to an SQS FIFO queue. The score Lambda applies the rules and writes the ledger in RDS and the score in DynamoDB. A DynamoDB stream triggers the alert Lambda, which emails through SES. API Gateway and the query Lambda serve the dashboard. Only the three Lambdas that need the database are in the VPC, and there is no NAT Gateway." |
| 1:00 | Dashboard → **Upload a file** → choose the demo file → **Upload file** | "The API answers 202 at once. The file is stored in S3 and processed in the background. This file has 12 rows: 9 good, 2 invalid, and 1 for an account that does not exist yet." |
| 1:30 | Watch the progress table | "Nine are scored in a few seconds. The two invalid rows never get a record: they are in the quarantine bucket with the reason. One is retrying." |
| 2:00 | Click the row marked **Scored, alert sent** | "This is why it scored 70: plus 50 for an amount above R50 000, plus 20 for cross-border. And this is its journey, step by step, from DynamoDB. No score is a black box." |
| 3:00 | Show the inbox | "The alert email. The account number is masked, and there is no customer name: email is not a secure channel." |
| 3:30 | Switch to the CloudWatch dashboard | "Rows read equals rows queued plus rows quarantined: 12 = 10 + 2. One scoring failure so far. These are my custom metrics in the FinTrust/ETL namespace." |
| 4:30 | Dashboard → **Alerts** → **Resolve** the new alert, then show **Resolved** | "It is recorded under my username, taken from my sign-in, not typed in, so nobody can resolve an alert in someone else's name. An analyst would not even see the button, and the API would refuse them anyway. The update only succeeds once, even if two people click." |
| 5:00 | **Transactions**: select the **Amount** heading twice, then **More filters** → Country `GB` | "Sorting and filtering are done by PostgreSQL over all the rows, not just this page. The filter is in the address, so Back works and the view can be shared." |
| 5:15 | **Overview** (All time). Point at the last bar of the activity chart, then the alert chart. In **Fraud ranking**, select ACC-1007 | "This week's bar already includes the file I just uploaded, and the alert I resolved is in the alert chart. These numbers come from PostgreSQL, not the browser. ACC-1007 is marked 30-day burst: the same rolling-window rule as my risk report. This is its running balance, from the window function in monthly_summary.sql; the red dots are its high-risk transactions." |
| 5:30 | Back to **Upload a file**: the retrying row now says **In the dead-letter queue**. Click it | "Three attempts, each with the reason: the account is not in the ledger. After the third, SQS moved the message to the dead-letter queue. It is not lost and it is not blocking any other account, because the queue orders per account." |
| 6:30 | Terminal: `python scripts/onboard_account.py ACC-9…` then `./scripts/redrive_dlq.sh` | "I fix the cause, onboarding the account, then redrive the queue. Nothing is re-uploaded or retyped." |
| 7:30 | Click the row again (or **Transactions**, search the account) | "Scored. The journey shows the whole story: three failures, then success." |
| 8:00 | Terminal: `source ./scripts/api_env.sh` then `curl -s -H "x-api-key: $API_KEY" "$API_URL/transactions?limit=3"` (no token), then the same with `-H "Authorization: $ID_TOKEN"` | "Without a signed-in person, API Gateway answers 401 before my code runs. With the token, the same data as the dashboard. The key says which application; the token says who." |
| 9:00 | S3 tab → `monthly/` → open the latest CSV | "The report Lambda runs on the 1st of each month from an EventBridge schedule. It runs the same SQL files that are in the repository: the running balance window function and the CTE risk report." |

**Timing note for the redrive:** SQS FIFO ignores a message id it has seen in the last 5 minutes. Upload at 1:00 and redrive at 6:30 or later, as in the table. If you are ahead of time, talk through the CloudWatch alarms before redriving.

## If something goes wrong

| Symptom | Likely cause | Do |
|---|---|---|
| Dashboard says the database is not available | Database stopped | `./scripts/rds_start.sh` (5 minutes). Meanwhile explain: uploads are still accepted and queued, nothing is lost, which is the design working |
| Sign-in page says the code is wrong | Phone clock drifted, or the code changed while typing | Wait for the next code and type it straight away. Authenticator codes depend on the phone's clock: turn on automatic time |
| "Your session has ended" | 15 minutes without activity | Sign in again (30 seconds). Then say why: a bank screen left open is a risk, and the session ends on the server, not only in the page |
| No rows progress after upload | Check the ingest Lambda's log | `aws logs tail /aws/lambda/fintrust-capstone-ingest --since 5m` |
| `onboard_account.py` cannot connect | Tunnel closed | `./scripts/db_tunnel.sh` in another window |
| The redriven row does not appear | Redriven inside 5 minutes of the upload | Wait a minute and run `./scripts/redrive_dlq.sh` again |
| No alert email | Inbox filtering | Show the journey step "Alert emailed to compliance" and the `AlertsSent` metric |
| AWS CLI says the token expired | Credentials | `aws sts get-caller-identity`, then sign in again |

Say what you see and what you are checking. A failure diagnosed calmly from the logs and the dashboard scores better than one hidden.

## After the demonstration

```bash
./scripts/rds_stop.sh
```

## The three questions to be ready for

The brief's own example: **"Your scoring Lambda fails: how does the DLQ prevent data loss, and how would you recover?"** The demonstration answers it live. Full answers to this and the other likely questions are in the panel preparation notes, organised by build phase.
