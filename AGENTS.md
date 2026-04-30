# Agent Playbook

This repo is intended to be driven by an AI chat interface. The human user will usually describe an outcome, not edit files or run commands directly.

## Operating Rules

- Prefer changing a per-run `workflow_settings.json`, `.env`, or CLI arguments for campaign name, copy, schedule, and lightweight flow options.
- Change `outbound_workflow.py` only when the requested behavior cannot be represented by settings.
- If campaign name, subject, body, segment, proof point, or CTA are missing, ask the user or draft them and request approval before running external APIs.
- Always run `plan-csv` or `plan-apollo-label` and show the user the dry-run result before starting Reoon, BounceBan, or Instantly work.
- Preserve stable lead order unless the user explicitly asks for sorting or prioritization.
- Keep Reoon and BounceBan calls email-only. Do not send full lead metadata to validation providers.
- Keep full lead metadata in memory until Instantly insertion.
- Treat Instantly leads returned from a different campaign/workspace as skipped/pre-existing.
- Never print API keys or full `.env` contents in chat or logs.

## Main Files

- `outbound_workflow.py`: CLI and workflow implementation.
- `workflow_settings.example.json`: AI-editable brief, campaign name, copy, and schedule defaults.
- `.env.example`: required and optional secrets/config.
- `README.md`: operator-facing usage.
- `tests/test_outbound_workflow.py`: regression tests for pure filtering behavior.

## Common User Requests

Change campaign email copy:

1. Create or edit a JSON settings file based on `workflow_settings.example.json`.
2. Update `campaign_brief`, `instantly.campaign_name`, `instantly.email_subject`, and `instantly.email_body`.
3. Run `plan-csv` or `plan-apollo-label`.
4. Ask the user to approve the dry-run output.
5. Run the workflow with `--settings path/to/settings.json`.

Change sending window or timezone:

1. Edit `instantly.schedule_from`, `instantly.schedule_to`, and `instantly.timezone`.
2. Keep values in Instantly-compatible formats such as `08:00` and `America/Chicago`.

Run from CSV:

```bash
python outbound_workflow.py plan-csv --csv leads.csv --settings workflow_settings.json
python outbound_workflow.py run-csv --csv leads.csv --settings workflow_settings.json
```

Run from Apollo contact list/label:

```bash
python outbound_workflow.py plan-apollo-label --label-id LABEL_ID --settings workflow_settings.json
python outbound_workflow.py run-apollo-label --label-id LABEL_ID --settings workflow_settings.json
```

## Verification

After code changes, run:

```bash
python3 -m unittest discover -s tests
python3 -m py_compile outbound_workflow.py tests/test_outbound_workflow.py
```

For CLI surface changes, also run:

```bash
python3 outbound_workflow.py --help
python3 outbound_workflow.py plan-csv --help
python3 outbound_workflow.py run-csv --help
python3 outbound_workflow.py plan-apollo-label --help
python3 outbound_workflow.py run-apollo-label --help
```
