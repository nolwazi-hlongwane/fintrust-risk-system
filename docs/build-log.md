# Build Log

Each entry is labelled with its slot in the Capstone Brief. "Completed" is the real calendar date.
Brief calendar: Week 13 Day 1 = 28 September 2026, Week 14 Day 1 = 5 October 2026.

---

## Week 13 Day 1: Design Kickoff and Infrastructure Setup

**Completed:** 6 October 2026

**Brief requirement:** "FinTrust brief scoped, architecture diagram drafted, backend functions and endpoint specs documented. GitHub repo created with correct folder structure, config.py loading from SSM Parameter Store, Lambda execution roles deployed via console or CLI."

### What was built

| Item | Where |
|---|---|
| Brief scoped; phases, decisions and risks agreed | `docs/project-execution-plan.md` |
| Architecture diagram (draft) with data flow and service justification | `docs/architecture.md` |
| Backend functions and endpoint specifications | `docs/api-design.md` |
| Security plan and IAM role matrix | `docs/security-plan.md` |
| Data management design document, first draft (all five elements) | `docs/data-governance.md` |
| ERD (draft) | `docs/erd.md` |
| Repository structure, pinned requirements, `.gitignore`, `.env.example` | repository root |
| `config.py`: settings from environment variables, then SSM Parameter Store | `fintrust/config.py` |
| `log.py`: structured JSON logging helper | `fintrust/log.py` |
| Five Lambda execution roles, permission boundary, log groups (AWS CLI) | `infrastructure/iam/` |
| SSM parameters for alert addresses and risk rules | `infrastructure/ssm/` |
| Billing alarm at 11 USD | `infrastructure/billing/` |
| CI: lint and unit tests on every push | `.github/workflows/ci.yml` |

### Key decisions

1. **Which Lambdas run inside the VPC** is decided by the no-NAT rule: only score, query and report (the ones that use RDS).
2. **IAM database authentication** instead of a stored database password.
3. **SSM is the source of truth for settings**; VPC Lambdas get the same values as environment variables at deploy time.
4. **Roles start with base permissions only.** Data permissions are added on the day each resource is created.
5. **CI started on Day 1** (the brief schedules it for Week 14 Day 3) so that every push in the history is tested.

### Test results

- `pytest`: 80 unit tests passing, 100% coverage of the package (configuration, logging, IAM policy rules).
- `ruff check`: no issues.
- Deployment scripts run end to end, including repeat runs, against a local AWS simulator (moto server) before being run on the real account.

### Evidence to capture (save in `docs/evidence/`)

- [ ] `w13d1-iam-roles.png`: IAM console, the five `fintrust-capstone-*-role` roles
- [ ] `w13d1-permission-boundary.png`: one role's Permissions tab showing the boundary
- [ ] `w13d1-ssm-parameters.png`: Parameter Store, `/fintrust/dev/` parameters
- [ ] `w13d1-billing-alarm.png`: CloudWatch (N. Virginia), the billing alarm
- [ ] `w13d1-ci-green.png`: GitHub Actions, first green run

### Check against the brief and rubric

| Requirement | Status |
|---|---|
| Brief scoped | Met |
| Architecture diagram drafted | Met as Mermaid; draw.io and PDF export due with final architecture |
| Backend functions and endpoint specs documented | Met |
| GitHub repo with correct folder structure | Met once pushed |
| `config.py` loading from SSM Parameter Store | Met, tested with moto |
| Lambda execution roles deployed via CLI | Met once the script is run on the account |
| Rubric C3: boto3 client at module level, pinned requirements, JSON logging | Met for the modules built so far |
| Rubric C1: no wildcard actions | Met and enforced by a test |
| Rubric C6: Conventional Commits | Commit messages provided for this phase |

**Open items carried forward:** draw.io diagrams and PDF exports; ERD confirmed against the deployed schema (Week 13 Day 2); facilitator answers to the clarification questions.
