# Outbound Apollo/Reoon/BounceBan/Instantly Workflow

This workflow takes leads from either:

- a CSV export, optionally enriching rows with missing emails through Apollo; or
- an Apollo contact label/list ID.

It then validates email addresses through Reoon, validates the survivors through BounceBan, creates an Instantly campaign, and adds only BounceBan `deliverable` leads to that campaign.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Fill `.env`, then export it before running:

```bash
set -a
source .env
set +a
```

Optional: copy the AI-editable settings file when you want to change campaign name, copy, schedule, or brief without changing Python code:

```bash
cp workflow_settings.example.json workflow_settings.json
```

Required:

```bash
APOLLO_API_KEY=...          # required when emails must be found in Apollo or using Apollo label input
REOON_API_KEY=...
BOUNCEBAN_API_KEY=...
INSTANTLY_API_KEY=...
```

## Run From CSV

Minimum CSV:

```csv
email,first_name,last_name,company_name,linkedin_url,personalization
alex@example.com,Alex,Rivera,Acme,https://linkedin.com/in/alex,"Saw your infra work..."
```

If a row has no `email`, the script will try Apollo bulk people enrichment using the available fields. Useful input columns for that are:

```csv
first_name,last_name,company_name,organization_domain,linkedin_url,job_title,personalization
Alex,Rivera,Acme,acme.com,https://linkedin.com/in/alex,VP Engineering,"Saw your infra work..."
```

Dry-run first. This prints the source summary, campaign name, subject/body, schedule, and planned validation/import steps without calling any external API:

```bash
python outbound_workflow.py plan-csv \
  --csv leads.csv \
  --settings workflow_settings.json
```

Run after the user approves the plan:

```bash
python outbound_workflow.py run-csv \
  --csv leads.csv \
  --settings workflow_settings.json
```

To require the CSV to already contain emails and skip Apollo enrichment:

```bash
python outbound_workflow.py run-csv \
  --csv leads.csv \
  --campaign-name "Apollo Leads - 2026-04-30" \
  --no-apollo-enrich
```

## Run From Apollo Label/List

Apollo exposes saved contact lists as contact labels in the contacts search API. Pass the label ID:

Dry-run first:

```bash
python outbound_workflow.py plan-apollo-label \
  --label-id 6095a710bd01d100a506d4ae \
  --settings workflow_settings.json
```

Run after the user approves the plan:

```bash
python outbound_workflow.py run-apollo-label \
  --label-id 6095a710bd01d100a506d4ae \
  --settings workflow_settings.json
```

You can still override the settings campaign name at runtime:

```bash
python outbound_workflow.py run-apollo-label \
  --label-id 6095a710bd01d100a506d4ae \
  --campaign-name "Apollo Label - 2026-04-30" \
  --settings workflow_settings.json
```

## Preserved Behavior

- Email order remains stable from source through Reoon, BounceBan, and Instantly.
- Reoon is permissive: `is_safe_to_send == true` or `is_catch_all == true` passes.
- BounceBan is strict: only `result == "deliverable"` passes.
- Instantly dedupe is controlled by `INSTANTLY_SKIP_IF_IN_WORKSPACE`.
- Leads that Instantly reports as attached to a different campaign/workspace are counted as skipped, not added.
- Reoon and BounceBan only receive email arrays; full lead metadata stays separate until Instantly insertion.

## API Notes Verified

- Apollo bulk people enrichment: `POST https://api.apollo.io/api/v1/people/bulk_match`, up to 10 people per request, `reveal_personal_emails=true`.
- Apollo contacts from a saved list: `POST https://api.apollo.io/api/v1/contacts/search` with `contact_label_ids`.
- Reoon bulk: `key` is passed in JSON/body for task creation and query params for result polling.
- BounceBan: `Authorization` header is the raw API key.
- Instantly v2: `Authorization: Bearer <token>`, campaign creation at `/api/v2/campaigns`, lead creation at `/api/v2/leads`.

## AI Chat Usage

This repo is designed to be operated by an AI agent. See `AGENTS.md` for the agent-facing playbook.

The AI agent should ask the user for missing campaign context, or draft it and ask for approval, then write the approved values into `workflow_settings.json`:

- `campaign_brief.segment`
- `campaign_brief.proof_point`
- `campaign_brief.cta`
- `instantly.campaign_name`
- `instantly.email_subject`
- `instantly.email_body`
- `instantly.schedule_from`, `instantly.schedule_to`, and `instantly.timezone`

After that, the agent should run `plan-csv` or `plan-apollo-label` and get user approval before running the paid/API workflow.
