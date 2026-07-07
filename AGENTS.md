# Agent Playbook

This repo is driven by Claude. The user describes an outcome; Claude handles every step below.

---

## Slack Channel

All messages go to **#outbound-ops** — channel ID `C0BCWR5CTT3` (private channel, Calyptus workspace). All user interaction happens via **thread replies** — Claude posts a message, records the `ts`, and reads replies with `slack_read_thread`.

---

## Operating Rules

- Never run external APIs (Reoon, BounceBan, Instantly) without showing the user a dry-run first and receiving approval.
- Never print API keys or `.env` contents in chat or logs.
- Preserve stable lead order throughout the pipeline.
- Keep Reoon and BounceBan calls email-only — do not send full lead metadata.
- Draft campaign name, subject, and body for approval before creating anything in Instantly.
- If any required information is missing at any step, pause and ask rather than guess.
- Prefer changing `workflow_settings.json` or CLI arguments over editing `outbound_workflow.py`.

---

## End-to-End Workflow

### STEP 1 — Apollo List Creation

User describes the list they want (persona, industry, filters, geography, etc.).

1. Use the Apollo MCP tools (`apollo_mixed_people_api_search` or `apollo_contacts_search`) to search for matching contacts.
2. Show the user a preview: total results, a sample of 5–10 names/companies/titles.
3. Ask: "Does this look right? Should I adjust any filters?"
4. Iterate until approved.
5. **The moment the user says this is the list they want**, immediately ask three things in one message:
   - *"What's the name for this list?"* (e.g. "Web3 People Leaders")
   - *"What function / role type?"* (e.g. "HR & People, not Talent", "Founders & Execs", "Investment")
   - *"What geography?"* (e.g. "UK", "Europe", "North Am", "Global")

   Do not proceed to Step 2 until you have all three answers.

---

### STEP 2 — Export from Apollo

Once you have the list name, generate the spreadsheet name and campaign slug:

```bash
python3 outbound_workflow.py make-spreadsheet-name --list-name "Web3 People Leaders"
# returns: { "spreadsheet_name": "S065-Web3_People_Leaders", "gdrive_folder_id": "1U9DrZSZRN2o3ToCnwq-ja2xQauELofMM" }
```

Use the returned `spreadsheet_name` as both the Drive filename and the Instantly campaign name. Use a lowercased, hyphenated version as the local folder slug (e.g. `s065-web3-people-leaders`).

Then fetch all contacts from Apollo:

```bash
python3 outbound_workflow.py export-apollo-label \
  --label-id <APOLLO_LABEL_ID> \
  --output "runs/<campaign-slug>/apollo_export.csv"
```

The export CSV includes `email`, `first_name`, `last_name`, `company_name`, **`company_name_cleaned`** (auto-applied), `linkedin_url`, `job_title`, `industry`, and other metadata.

---

### STEP 3 — Save to Google Drive

The Apollo folder ID is always `1U9DrZSZRN2o3ToCnwq-ja2xQauELofMM`.

**3a — Upload the S0XX export sheet**

Read the local CSV file and upload it to Drive. The Drive MCP auto-converts `text/csv` → Google Sheet:

```
Drive MCP: create_file
  title: "<spreadsheet_name>"          ← e.g. "S065-Web3_People_Leaders"
  parentId: "1U9DrZSZRN2o3ToCnwq-ja2xQauELofMM"
  textContent: <contents of apollo_export.csv>
  contentMimeType: "text/csv"
```

Note the returned file `id` and the URL (`https://docs.google.com/spreadsheets/d/<id>`). This is the `apollo_sheet_url`.

**3b — Log the run locally**

```bash
python3 outbound_workflow.py log-run \
  --sequence-code S065 \
  --list-name "Web3 People Leaders" \
  --function "HR & People, not Talent" \
  --geography "Global" \
  --spreadsheet-name "S065-Web3_People_Leaders" \
  --total-exported <N> \
  --apollo-sheet-url "https://docs.google.com/spreadsheets/d/<id>"
```

**3c — Append the new row to the master Google Sheet**

The master sheet ID is `1jzx33xTHhiT-vZDIflZrtWkyYbJspmL27gTDx7V3Bvk` (constant — never changes).

Step 1: Download the current master sheet as base64 CSV:
```
Drive MCP: download_file_content
  fileId: "1jzx33xTHhiT-vZDIflZrtWkyYbJspmL27gTDx7V3Bvk"
  exportMimeType: "text/csv"
```

Step 2: Pass the base64 content to Python, which inserts the new row and outputs the full updated CSV:
```bash
python3 outbound_workflow.py append-master-row \
  --base64 "<base64_string_from_drive>" \
  --sequence-code S065 \
  --date-created "Jun-2026" \
  --list-name "Web3 People Leaders" \
  --function "HR & People, not Talent" \
  --geography "Global" \
  --results "<N>" \
  --sheet-url "https://docs.google.com/spreadsheets/d/<id>"
```

Step 3: Upload the updated CSV back to Drive as a new Google Sheet in the Apollo folder:
```
Drive MCP: create_file
  title: "Apollo Master Dashboard"
  parentId: "1U9DrZSZRN2o3ToCnwq-ja2xQauELofMM"
  textContent: <stdout from append-master-row>
  contentMimeType: "text/csv"
```

Note the new file ID. Each run creates a fresh master sheet — the previous version will accumulate in Drive. The Slack message (Step 4) always links to the newest version. The user can periodically delete old master sheet files from Drive.

---

### STEP 4 — Post to Slack for Company Name Review

Generate the message:
```bash
python3 outbound_workflow.py format-slack-company-review \
  --csv "runs/<slug>/apollo_export.csv" \
  --spreadsheet-name "S065-Web3_People_Leaders" \
  --apollo-sheet-url "https://docs.google.com/spreadsheets/d/<id>" \
  --master-sheet-url "https://docs.google.com/spreadsheets/d/<master_id>"
```

Post the output to **#outbound-ops** using the Slack MCP. **Record the returned message `ts`** — you'll need it to read the thread reply.

```
Slack MCP: slack_send_message
  channel_id: <outbound-ops channel ID>
  message: <output of format-slack-company-review>
```

**Reading the reply:**
When the user says "I've replied" or "check Slack", read the thread:
```
Slack MCP: slack_read_thread
  channel_id: <outbound-ops channel ID>
  message_ts: <ts from the post above>
```

Parse the reply:
- If it contains **"approved"** → proceed to Step 5.
- If it contains corrections (e.g. "keep Group on Hg Group", "rename X to Y") → apply overrides to the `company_name_cleaned` column in the CSV, re-run `format-slack-company-review`, re-post as a **reply in the same thread** (`thread_ts` = original `ts`), and wait again.

**Applying manual overrides to the CSV:**
Read the CSV, find the row(s) where `company_name` matches what the user mentioned, update `company_name_cleaned` directly, write the file back. Then re-post the preview.

---

### STEP 5 — Email Verification

Once company names are approved, ask: "Ready to run Reoon + BounceBan verification?"

On approval, run the dry-run first, show the plan, then execute:

```bash
python3 outbound_workflow.py plan-csv \
  --csv "runs/<campaign-slug>/apollo_export.csv" \
  --settings workflow_settings.json

# After user approves the plan:
python3 outbound_workflow.py run-csv \
  --csv "runs/<campaign-slug>/apollo_export.csv" \
  --settings workflow_settings.json
```

Intermediate CSVs are saved automatically:
- `runs/<campaign-slug>/reoon_passed.csv`
- `runs/<campaign-slug>/bounceban_deliverable.csv`

Report the funnel:
```
Verification complete:
• Input: 450 emails
• Reoon passed: 310 (safe or catch-all)
• BounceBan deliverable: 248
```

---

### STEP 6 — Save Cleaned Data Back to Drive

**6a — Upload the cleaned (deliverable) sheet**

```
Drive MCP: create_file
  title: "<spreadsheet_name>_cleaned"   ← e.g. "S065-Web3_People_Leaders_cleaned"
  parentId: "1U9DrZSZRN2o3ToCnwq-ja2xQauELofMM"
  textContent: <contents of runs/<slug>/bounceban_deliverable.csv>
  contentMimeType: "text/csv"
```

Note the returned file `id` and URL. This is the `cleaned_sheet_url`.

**6b — Update the local log**

```bash
python3 outbound_workflow.py log-run \
  --sequence-code S065 \
  --deliverable <N> \
  --cleaned-sheet-url "https://docs.google.com/spreadsheets/d/<id>"
```

No master sheet update needed at this stage — the results count gets updated in the master sheet during Step 8 (after the Instantly campaign is created and the final count is known).

---

### STEP 7 — Campaign Setup via Slack

Generate and post the campaign setup form:

```bash
python3 outbound_workflow.py format-slack-campaign-setup \
  --spreadsheet-name "S065-Web3_People_Leaders" \
  --deliverable <N>
```

This fetches live Instantly accounts and builds the form. Post the output to **#outbound-ops** and **record the message `ts`**:

```
Slack MCP: slack_send_message
  channel_id: <outbound-ops channel ID>
  message: <output of format-slack-campaign-setup>
```

**Reading the reply:**
When the user comes back and says "go" or "check Slack", read the thread:
```
Slack MCP: slack_read_thread
  channel_id: <outbound-ops channel ID>
  message_ts: <ts from above>
```

**Mapping the reply to `workflow_settings.json`:**

Extract these fields from the user's thread reply:

| Reply field | Settings key | Notes |
|---|---|---|
| Subject line(s) | `instantly.sequence_steps[N].subject` | One per email |
| Email body/bodies | `instantly.sequence_steps[N].body` | Wrap in `<p>` tags if plain text |
| Delay between emails | `instantly.sequence_steps[N].delay_days` | Integer days; first email = 0 |
| Send window start | `instantly.schedule_from` | Convert to `HH:MM` 24h format |
| Send window end | `instantly.schedule_to` | Convert to `HH:MM` 24h format |
| Timezone | `instantly.timezone` | Use IANA format e.g. `Europe/London` |
| Campaign duration | `instantly.campaign_duration_days` | Integer days |
| Sending account | `instantly.email_account_ids` | Array of account ID strings |

Also set:
- `instantly.campaign_name` = `<spreadsheet_name>` (e.g. `S065-Web3_People_Leaders`)
- `instantly.skip_if_in_workspace` = `true`
- `instantly.fail_fast` = `false`

Show the user the full proposed `workflow_settings.json` for approval before writing it or running the campaign.

---

### STEP 8 — Create Instantly Campaign

Parse the user's reply and update `workflow_settings.json`:

```json
{
  "campaign_brief": {
    "segment": "<who this is for>",
    "proof_point": "<key proof point>",
    "cta": "<call to action>"
  },
  "instantly": {
    "campaign_name": "<name>",
    "campaign_duration_days": 30,
    "timezone": "<timezone>",
    "schedule_from": "<HH:MM>",
    "schedule_to": "<HH:MM>",
    "skip_if_in_workspace": true,
    "fail_fast": false,
    "email_account_ids": ["<account-id>"],
    "sequence_steps": [
      {"subject": "<subject 1>", "body": "<html body 1>", "delay_days": 0},
      {"subject": "<subject 2>", "body": "<html body 2>", "delay_days": 3},
      {"subject": "<subject 3>", "body": "<html body 3>", "delay_days": 4}
    ]
  }
}
```

Show the full config for user approval, then run:

```bash
python3 outbound_workflow.py run-csv \
  --csv "runs/<campaign-slug>/bounceban_deliverable.csv" \
  --settings workflow_settings.json
```

After the campaign is created, log the campaign ID:

```bash
python3 outbound_workflow.py log-run \
  --sequence-code S065 \
  --campaign-id "<instantly_campaign_id>"
```

Then update the master sheet with the final deliverable count (same flow as Step 3c — download current master as base64, run `append-master-row` but this time the S065 row is already in the sheet so you only need to update the Results and sheet URL fields if they weren't set in Step 3). Re-upload the full updated CSV as a new master sheet Google Sheet.

Post result back into the **same Step 7 thread** in #outbound-ops:

```
Slack MCP: slack_send_message
  channel_id: <outbound-ops channel ID>
  thread_ts: <Step 7 message ts>
  reply_broadcast: true
  message:
    ✅ *Campaign live in Instantly!*
    • *Campaign:* <name>
    • *Leads added:* 248  |  *Skipped:* 0
    • *Sending from:* <email>
    • *Schedule:* 9am–11am GMT, 30 days
    • *Sequence:* 3 emails (day 0, +3 days, +4 days)
```

---

## Settings Reference (`workflow_settings.json`)

| Section | Key | Purpose |
|---|---|---|
| `campaign_brief` | `segment` | Who the list is (context only) |
| `campaign_brief` | `proof_point` | Social proof line |
| `campaign_brief` | `cta` | Call to action |
| `instantly` | `campaign_name` | Instantly campaign name |
| `instantly` | `timezone` | e.g. `UTC`, `America/Chicago` |
| `instantly` | `schedule_from` | Send window start e.g. `09:00` |
| `instantly` | `schedule_to` | Send window end e.g. `11:00` |
| `instantly` | `campaign_duration_days` | Days the campaign runs |
| `instantly` | `email_account_ids` | Array of Instantly account IDs to send from |
| `instantly` | `sequence_steps` | Array of `{subject, body, delay_days}` objects |
| `gdrive` | `apollo_folder_id` | Google Drive folder ID — always `1U9DrZSZRN2o3ToCnwq-ja2xQauELofMM` |
| `gdrive` | `master_sheet_id` | Fixed master dashboard — always `1jzx33xTHhiT-vZDIflZrtWkyYbJspmL27gTDx7V3Bvk` (download from here, upload fresh copy to Apollo folder) |

---

## CLI Reference

```bash
# Peek at the next S0XX code without incrementing
python3 outbound_workflow.py next-sequence

# Allocate the next S0XX code and build the spreadsheet name (increments counter)
python3 outbound_workflow.py make-spreadsheet-name --list-name "Web3 People Leaders"

# Create or update a pipeline log entry (all flags except --sequence-code are optional)
python3 outbound_workflow.py log-run \
  --sequence-code S065 \
  --list-name "Web3 People Leaders" \
  --function "HR & People, not Talent" \
  --geography "Global" \
  --spreadsheet-name "S065-Web3_People_Leaders" \
  --total-exported 450 \
  --deliverable 248 \
  --apollo-sheet-url "https://docs.google.com/spreadsheets/d/..." \
  --cleaned-sheet-url "https://docs.google.com/spreadsheets/d/..." \
  --campaign-id "<instantly_id>"

# Decode master sheet base64, append new S-row, print updated CSV to stdout
python3 outbound_workflow.py append-master-row \
  --base64 "<b64_from_drive_download>" \
  --sequence-code S065 \
  --date-created "Jun-2026" \
  --list-name "Web3 People Leaders" \
  --function "HR & People, not Talent" \
  --geography "Global" \
  --results "450" \
  --sheet-url "https://docs.google.com/spreadsheets/d/..."

# Fetch contacts from Apollo label → CSV (no email verification)
python3 outbound_workflow.py export-apollo-label \
  --label-id LABEL_ID --output leads.csv

# Add company_name_cleaned column to an existing CSV (no API calls)
python3 outbound_workflow.py clean-names \
  --csv input.csv --output output.csv

# List Instantly email accounts in the workspace
python3 outbound_workflow.py list-instantly-accounts

# Dry-run plan (shows steps without calling any external APIs)
python3 outbound_workflow.py plan-csv \
  --csv leads.csv --settings workflow_settings.json
python3 outbound_workflow.py plan-apollo-label \
  --label-id LABEL_ID --settings workflow_settings.json

# Full pipeline run (Reoon → BounceBan → Instantly)
python3 outbound_workflow.py run-csv \
  --csv leads.csv --settings workflow_settings.json
python3 outbound_workflow.py run-apollo-label \
  --label-id LABEL_ID --settings workflow_settings.json
```

---

## Verification (after code changes)

```bash
python3 -m unittest discover -s tests
python3 -m py_compile outbound_workflow.py tests/test_outbound_workflow.py
python3 outbound_workflow.py --help
```
