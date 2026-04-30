import argparse
import csv
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable

import requests


APOLLO_BULK_PEOPLE_ENRICHMENT_URL = "https://api.apollo.io/api/v1/people/bulk_match"
APOLLO_CONTACT_SEARCH_URL = "https://api.apollo.io/api/v1/contacts/search"

REOON_CREATE_TASK_URL = "https://emailverifier.reoon.com/api/v1/create-bulk-verification-task/"
REOON_GET_TASK_URL = "https://emailverifier.reoon.com/api/v1/get-result-bulk-verification-task/"

BOUNCEBAN_BULK_VERIFY_URL = "https://api.bounceban.com/v1/verify/bulk"
BOUNCEBAN_BULK_STATUS_URL = "https://api.bounceban.com/v1/verify/bulk/status"
BOUNCEBAN_BULK_DUMP_URL = "https://api.bounceban.com/v1/verify/bulk/dump"

INSTANTLY_CAMPAIGN_CREATE_URL = "https://api.instantly.ai/api/v2/campaigns"
INSTANTLY_LEAD_CREATE_URL = "https://api.instantly.ai/api/v2/leads"


APOLLO_BATCH_SIZE = 10
DEFAULT_CAMPAIGN_DURATION_DAYS = 30
DEFAULT_INSTANTLY_TIMEZONE = "America/Chicago"
DEFAULT_INSTANTLY_SCHEDULE_FROM = "08:00"
DEFAULT_INSTANTLY_SCHEDULE_TO = "10:00"
DEFAULT_INSTANTLY_EMAIL_SUBJECT = "New role at a startup!"
DEFAULT_INSTANTLY_EMAIL_BODY = "<p>Hi {{first_name}},</p><p>{{personalization}}</p>"


@dataclass(frozen=True)
class WorkflowConfig:
    apollo_api_key: str | None
    reoon_api_key: str
    bounceban_api_key: str
    instantly_api_key: str
    reoon_max_wait_seconds: int
    reoon_poll_interval_seconds: int
    bounceban_max_wait_seconds: int
    bounceban_poll_interval_seconds: int
    instantly_skip_if_in_workspace: bool
    instantly_campaign_duration_days: int
    instantly_fail_fast: bool
    instantly_timezone: str
    instantly_schedule_from: str
    instantly_schedule_to: str
    instantly_email_subject: str
    instantly_email_body: str


def load_settings(path: str | None) -> dict[str, Any]:
    if not path:
        return {}
    with Path(path).open(encoding="utf-8") as f:
        payload = json.load(f)
    if not isinstance(payload, dict):
        raise RuntimeError(f"Settings file must contain a JSON object: {path}")
    return payload


def nested_setting(settings: dict[str, Any], section: str, key: str, default: Any) -> Any:
    section_payload = settings.get(section) or {}
    if not isinstance(section_payload, dict):
        raise RuntimeError(f"Settings section {section!r} must be an object")
    return section_payload.get(key, default)


def resolve_campaign_name(cli_campaign_name: str | None, settings: dict[str, Any]) -> str:
    campaign_name = (
        cli_campaign_name
        or nested_setting(settings, "instantly", "campaign_name", "")
        or nested_setting(settings, "campaign_brief", "campaign_name", "")
    )
    campaign_name = str(campaign_name).strip()
    if not campaign_name:
        raise RuntimeError(
            "Missing campaign name. The AI agent should ask the user, draft one for approval, "
            "then pass --campaign-name or set instantly.campaign_name in the settings file."
        )
    return campaign_name


def campaign_preview_from_settings(settings: dict[str, Any], campaign_name: str) -> dict[str, Any]:
    return {
        "campaign_name": campaign_name,
        "segment": nested_setting(settings, "campaign_brief", "segment", ""),
        "proof_point": nested_setting(settings, "campaign_brief", "proof_point", ""),
        "cta": nested_setting(settings, "campaign_brief", "cta", ""),
        "notes": nested_setting(settings, "campaign_brief", "notes", ""),
        "duration_days": int(nested_setting(settings, "instantly", "campaign_duration_days", DEFAULT_CAMPAIGN_DURATION_DAYS)),
        "timezone": str(nested_setting(settings, "instantly", "timezone", DEFAULT_INSTANTLY_TIMEZONE)),
        "schedule_from": str(nested_setting(settings, "instantly", "schedule_from", DEFAULT_INSTANTLY_SCHEDULE_FROM)),
        "schedule_to": str(nested_setting(settings, "instantly", "schedule_to", DEFAULT_INSTANTLY_SCHEDULE_TO)),
        "skip_if_in_workspace": bool(nested_setting(settings, "instantly", "skip_if_in_workspace", True)),
        "fail_fast": bool(nested_setting(settings, "instantly", "fail_fast", False)),
        "email_subject": str(nested_setting(settings, "instantly", "email_subject", DEFAULT_INSTANTLY_EMAIL_SUBJECT)),
        "email_body": str(nested_setting(settings, "instantly", "email_body", DEFAULT_INSTANTLY_EMAIL_BODY)),
    }


def summarize_csv_source(path: str) -> dict[str, Any]:
    leads = load_leads_csv(path)
    with_email = sum(1 for lead in leads if normalize_email(lead.get("email")))
    missing_email = len(leads) - with_email
    sample_fields = sorted({field for lead in leads[:10] for field, value in lead.items() if value})

    return {
        "type": "csv",
        "path": path,
        "rows_loaded": len(leads),
        "rows_with_email": with_email,
        "rows_missing_email": missing_email,
        "apollo_enrichment_needed": missing_email > 0,
        "sample_populated_fields": sample_fields,
    }


def build_dry_run_plan(
    source: dict[str, Any],
    campaign_name: str,
    settings: dict[str, Any],
    enrich_missing_with_apollo: bool = True,
) -> dict[str, Any]:
    steps = [
        "Load source leads and preserve order",
        "Normalize and dedupe emails",
    ]

    if source.get("type") == "apollo_label":
        steps.append("Fetch contacts from Apollo label/list")
        steps.append("Use Apollo enrichment if fetched contacts are missing emails")
    elif source.get("apollo_enrichment_needed") and enrich_missing_with_apollo:
        steps.append("Use Apollo bulk people enrichment for rows missing emails")
    elif source.get("apollo_enrichment_needed"):
        steps.append("Stop before validation if rows are missing emails")

    steps.extend(
        [
            "Submit email-only array to Reoon bulk verification",
            "Keep Reoon is_safe_to_send=true OR is_catch_all=true",
            "Submit Reoon survivors as email-only array to BounceBan bulk verification",
            "Keep BounceBan result=deliverable only",
            "Create Instantly campaign with approved campaign name, schedule, subject, and body",
            "Add deliverable leads to Instantly with preserved lead metadata",
        ]
    )

    return {
        "dry_run": True,
        "approval_required_before_run": True,
        "source": source,
        "campaign": campaign_preview_from_settings(settings, campaign_name),
        "steps": steps,
    }


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required env var: {name}")
    return value


def optional_env(name: str) -> str | None:
    value = os.getenv(name, "").strip()
    return value or None


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer, got {raw!r}") from exc


def is_truthy(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off", ""}


def config_from_env(settings: dict[str, Any] | None = None) -> WorkflowConfig:
    settings = settings or {}
    return WorkflowConfig(
        apollo_api_key=optional_env("APOLLO_API_KEY"),
        reoon_api_key=require_env("REOON_API_KEY"),
        bounceban_api_key=require_env("BOUNCEBAN_API_KEY"),
        instantly_api_key=require_env("INSTANTLY_API_KEY"),
        reoon_max_wait_seconds=env_int("REOON_MAX_WAIT_SECONDS", 1800),
        reoon_poll_interval_seconds=env_int("REOON_POLL_INTERVAL_SECONDS", 15),
        bounceban_max_wait_seconds=env_int("BOUNCEBAN_MAX_WAIT_SECONDS", 600),
        bounceban_poll_interval_seconds=env_int("BOUNCEBAN_POLL_INTERVAL_SECONDS", 15),
        instantly_skip_if_in_workspace=is_truthy(
            os.getenv("INSTANTLY_SKIP_IF_IN_WORKSPACE"),
            bool(nested_setting(settings, "instantly", "skip_if_in_workspace", True)),
        ),
        instantly_campaign_duration_days=int(
            os.getenv(
                "INSTANTLY_CAMPAIGN_DURATION_DAYS",
                str(nested_setting(settings, "instantly", "campaign_duration_days", DEFAULT_CAMPAIGN_DURATION_DAYS)),
            )
        ),
        instantly_fail_fast=is_truthy(
            os.getenv("INSTANTLY_FAIL_FAST"),
            bool(nested_setting(settings, "instantly", "fail_fast", False)),
        ),
        instantly_timezone=os.getenv(
            "INSTANTLY_TIMEZONE",
            str(nested_setting(settings, "instantly", "timezone", DEFAULT_INSTANTLY_TIMEZONE)),
        ).strip()
        or DEFAULT_INSTANTLY_TIMEZONE,
        instantly_schedule_from=os.getenv(
            "INSTANTLY_SCHEDULE_FROM",
            str(nested_setting(settings, "instantly", "schedule_from", DEFAULT_INSTANTLY_SCHEDULE_FROM)),
        ).strip()
        or DEFAULT_INSTANTLY_SCHEDULE_FROM,
        instantly_schedule_to=os.getenv(
            "INSTANTLY_SCHEDULE_TO",
            str(nested_setting(settings, "instantly", "schedule_to", DEFAULT_INSTANTLY_SCHEDULE_TO)),
        ).strip()
        or DEFAULT_INSTANTLY_SCHEDULE_TO,
        instantly_email_subject=os.getenv(
            "INSTANTLY_EMAIL_SUBJECT",
            str(nested_setting(settings, "instantly", "email_subject", DEFAULT_INSTANTLY_EMAIL_SUBJECT)),
        ),
        instantly_email_body=os.getenv(
            "INSTANTLY_EMAIL_BODY",
            str(
                nested_setting(
                    settings,
                    "instantly",
                    "email_body",
                    DEFAULT_INSTANTLY_EMAIL_BODY,
                )
            ),
        ),
    )


def normalize_email(email: str | None) -> str:
    return (email or "").strip().lower()


def dedupe_preserve_order(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def chunks(items: list[dict[str, Any]], size: int) -> Iterable[list[dict[str, Any]]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def first_present(row: dict[str, Any], names: Iterable[str]) -> str:
    for name in names:
        value = row.get(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def canonicalize_lead(row: dict[str, Any]) -> dict[str, Any]:
    email = normalize_email(first_present(row, ["email", "Email", "work_email", "email_address"]))
    first_name = first_present(row, ["first_name", "First Name", "firstName", "first"])
    last_name = first_present(row, ["last_name", "Last Name", "lastName", "last"])

    lead = {
        "email": email,
        "first_name": first_name,
        "last_name": last_name,
        "company_name": first_present(row, ["company_name", "Company", "company", "organization_name"]),
        "linkedin_url": first_present(row, ["linkedin_url", "LinkedIn", "linkedin", "person_linkedin_url"]),
        "personalization": first_present(row, ["personalization", "Personalization", "icebreaker"]),
        "job_title": first_present(row, ["job_title", "title", "Title"]),
        "phone": first_present(row, ["phone", "Phone", "phone_number"]),
        "website": first_present(row, ["website", "website_url", "company_website", "domain"]),
        "organization_domain": first_present(
            row,
            ["organization_domain", "company_domain", "domain", "website", "website_url"],
        ),
        "apollo_id": first_present(row, ["apollo_id", "id", "person_id", "contact_id"]),
    }

    return lead


def lead_order_key(lead: dict[str, Any], index: int) -> str:
    return lead.get("email") or lead.get("apollo_id") or lead.get("linkedin_url") or f"row:{index}"


def load_leads_csv(path: str) -> list[dict[str, Any]]:
    leads_by_key: dict[str, dict[str, Any]] = {}
    order: list[str] = []

    with open(path, newline="", encoding="utf-8") as f:
        for index, row in enumerate(csv.DictReader(f)):
            lead = canonicalize_lead(row)
            key = lead_order_key(lead, index)
            if not key:
                continue
            if key not in leads_by_key:
                order.append(key)
            leads_by_key[key] = lead

    return [leads_by_key[key] for key in order]


def key_by_email(leads: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for lead in leads:
        email = normalize_email(lead.get("email"))
        if email and email not in out:
            out[email] = lead
    return out


def apollo_headers(api_key: str) -> dict[str, str]:
    return {
        "Cache-Control": "no-cache",
        "Content-Type": "application/json",
        "accept": "application/json",
        "x-api-key": api_key,
    }


def apollo_detail_from_lead(lead: dict[str, Any]) -> dict[str, Any]:
    detail: dict[str, Any] = {}
    field_map = {
        "first_name": "first_name",
        "last_name": "last_name",
        "email": "email",
        "organization_name": "company_name",
        "domain": "organization_domain",
        "linkedin_url": "linkedin_url",
        "title": "job_title",
    }

    for apollo_field, lead_field in field_map.items():
        value = lead.get(lead_field)
        if value:
            detail[apollo_field] = value

    return detail


def extract_email_from_apollo_person(person: dict[str, Any]) -> str:
    candidates: list[Any] = [
        person.get("email"),
        person.get("email_address"),
        person.get("personal_email"),
        person.get("sanitized_email"),
    ]

    for collection_name in ("emails", "email_addresses"):
        collection = person.get(collection_name)
        if isinstance(collection, list):
            for item in collection:
                if isinstance(item, dict):
                    candidates.append(item.get("email") or item.get("address") or item.get("value"))
                else:
                    candidates.append(item)

    for candidate in candidates:
        email = normalize_email(str(candidate or ""))
        if email:
            return email
    return ""


def enrich_missing_emails_with_apollo(api_key: str, leads: list[dict[str, Any]]) -> dict[str, Any]:
    missing = [lead for lead in leads if not normalize_email(lead.get("email"))]
    enriched = 0
    unresolved: list[dict[str, Any]] = []

    for batch in chunks(missing, APOLLO_BATCH_SIZE):
        details = [apollo_detail_from_lead(lead) for lead in batch]
        response = requests.post(
            APOLLO_BULK_PEOPLE_ENRICHMENT_URL,
            headers=apollo_headers(api_key),
            params={"reveal_personal_emails": "true"},
            json={"details": details},
            timeout=60,
        )
        response.raise_for_status()
        body = response.json()
        people = body.get("people") or body.get("matches") or body.get("contacts") or []

        if not isinstance(people, list):
            people = []

        for lead, person in zip(batch, people):
            if not isinstance(person, dict):
                unresolved.append(lead)
                continue

            email = extract_email_from_apollo_person(person)
            if email:
                lead["email"] = email
                enriched += 1
                lead.setdefault("apollo_id", person.get("id") or person.get("_id") or "")
                lead["first_name"] = lead.get("first_name") or person.get("first_name") or ""
                lead["last_name"] = lead.get("last_name") or person.get("last_name") or ""
                lead["company_name"] = (
                    lead.get("company_name")
                    or person.get("organization", {}).get("name")
                    if isinstance(person.get("organization"), dict)
                    else lead.get("company_name") or person.get("organization_name") or ""
                )
            else:
                unresolved.append(lead)

    return {"apollo_missing_input": len(missing), "apollo_enriched": enriched, "apollo_unresolved": len(unresolved)}


def search_apollo_contacts_by_label(api_key: str, label_ids: list[str], per_page: int = 100) -> list[dict[str, Any]]:
    page = 1
    leads: list[dict[str, Any]] = []

    while True:
        payload = {"contact_label_ids": label_ids, "page": page, "per_page": per_page}
        response = requests.post(
            APOLLO_CONTACT_SEARCH_URL,
            headers=apollo_headers(api_key),
            json=payload,
            timeout=60,
        )
        response.raise_for_status()
        body = response.json()
        contacts = body.get("contacts") or body.get("people") or []
        if not contacts:
            break

        for contact in contacts:
            if isinstance(contact, dict):
                leads.append(canonicalize_lead(contact))

        if len(contacts) < per_page:
            break
        page += 1

    return leads


def submit_reoon_task(api_key: str, emails: list[str]) -> int:
    response = requests.post(
        REOON_CREATE_TASK_URL,
        json={"key": api_key, "name": "Apollo to Instantly", "emails": emails},
        timeout=60,
    )
    response.raise_for_status()
    body = response.json()

    if body.get("status") != "success" or not body.get("task_id"):
        raise RuntimeError(f"Reoon task creation failed: {body}")

    return int(body["task_id"])


def poll_reoon_task(config: WorkflowConfig, task_id: int) -> dict[str, Any]:
    deadline = time.monotonic() + config.reoon_max_wait_seconds

    while True:
        response = requests.get(
            REOON_GET_TASK_URL,
            params={"key": config.reoon_api_key, "task_id": task_id},
            timeout=60,
        )
        response.raise_for_status()
        body = response.json()

        status = (body.get("status") or "").lower()
        if status == "completed":
            return body

        if status not in {"waiting", "running", "queued", "processing"}:
            raise RuntimeError(f"Unexpected Reoon status: {body}")

        if time.monotonic() >= deadline:
            raise RuntimeError(f"Timed out waiting for Reoon task {task_id}")

        time.sleep(max(1, config.reoon_poll_interval_seconds))


def reoon_results_by_email(reoon_result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw_results = reoon_result.get("results") or {}
    out: dict[str, dict[str, Any]] = {}

    if isinstance(raw_results, dict):
        for email, item in raw_results.items():
            if isinstance(item, dict):
                out[normalize_email(str(email))] = item
    elif isinstance(raw_results, list):
        for item in raw_results:
            if isinstance(item, dict):
                email = normalize_email(item.get("email"))
                if email:
                    out[email] = item

    return out


def filter_reoon_results(reoon_result: dict[str, Any], input_emails: list[str]) -> list[str]:
    results = reoon_results_by_email(reoon_result)
    passed: list[str] = []

    for email in input_emails:
        item = results.get(email) or {}
        if item.get("is_safe_to_send") is True or item.get("is_catch_all") is True:
            passed.append(email)

    return passed


def submit_bounceban_task(api_key: str, emails: list[str]) -> str:
    response = requests.post(
        BOUNCEBAN_BULK_VERIFY_URL,
        headers={"Authorization": api_key, "Content-Type": "application/json"},
        json={"emails": emails},
        timeout=60,
    )
    response.raise_for_status()
    body = response.json()

    task_id = body.get("id") or body.get("task_id") or body.get("taskId")
    if not task_id:
        raise RuntimeError(f"BounceBan task creation failed: {body}")

    return str(task_id)


def poll_bounceban_task(config: WorkflowConfig, task_id: str) -> None:
    deadline = time.monotonic() + config.bounceban_max_wait_seconds

    while True:
        response = requests.get(
            BOUNCEBAN_BULK_STATUS_URL,
            headers={"Authorization": config.bounceban_api_key},
            params={"id": task_id},
            timeout=60,
        )
        response.raise_for_status()
        body = response.json()

        status = (body.get("status") or "").lower()
        if status in {"finished", "completed"}:
            return

        if status not in {"pending", "running", "processing", "verifying"}:
            raise RuntimeError(f"Unexpected BounceBan status: {body}")

        if time.monotonic() >= deadline:
            raise RuntimeError(f"Timed out waiting for BounceBan task {task_id}")

        time.sleep(max(1, config.bounceban_poll_interval_seconds))


def fetch_bounceban_dump(api_key: str, task_id: str) -> dict[str, Any]:
    response = requests.get(
        BOUNCEBAN_BULK_DUMP_URL,
        headers={"Authorization": api_key},
        params={"id": task_id, "retrieve_all": "1"},
        timeout=60,
    )
    response.raise_for_status()
    return response.json()


def bounceban_items(dump_payload: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("items", "results", "data"):
        value = dump_payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def filter_bounceban_deliverables(dump_payload: dict[str, Any], input_emails: list[str]) -> list[str]:
    by_email: dict[str, dict[str, Any]] = {}

    for item in bounceban_items(dump_payload):
        email = normalize_email(item.get("email"))
        if email:
            by_email[email] = item

    deliverable: list[str] = []
    for email in input_emails:
        item = by_email.get(email) or {}
        if (item.get("result") or "").lower() == "deliverable":
            deliverable.append(email)

    return deliverable


def create_instantly_campaign(config: WorkflowConfig, name: str) -> dict[str, str]:
    start_date = date.today()
    end_date = start_date + timedelta(days=max(1, config.instantly_campaign_duration_days))

    payload = {
        "name": name,
        "campaign_schedule": {
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "schedules": [
                {
                    "name": "Morning",
                    "days": {"0": True, "1": True, "2": True, "3": True, "4": True, "5": True, "6": True},
                    "timing": {"from": config.instantly_schedule_from, "to": config.instantly_schedule_to},
                    "timezone": config.instantly_timezone,
                }
            ],
        },
        "sequences": [
            {
                "steps": [
                    {
                        "type": "email",
                        "delay": 2,
                        "delay_unit": "days",
                        "variants": [
                            {
                                "subject": config.instantly_email_subject,
                                "body": config.instantly_email_body,
                            }
                        ],
                    }
                ]
            }
        ],
        "email_list": [],
    }

    response = requests.post(
        INSTANTLY_CAMPAIGN_CREATE_URL,
        headers={"Authorization": f"Bearer {config.instantly_api_key}", "Content-Type": "application/json"},
        json=payload,
        timeout=60,
    )
    response.raise_for_status()
    body = response.json()

    if not body.get("id"):
        raise RuntimeError(f"Instantly campaign creation failed: {body}")

    return {"id": str(body["id"]), "name": name}


def instantly_lead_payload(lead: dict[str, Any], campaign_id: str, skip_if_in_workspace: bool) -> dict[str, Any]:
    payload = {
        "email": lead["email"],
        "campaign": campaign_id,
        "skip_if_in_workspace": skip_if_in_workspace,
        "first_name": lead.get("first_name", ""),
        "last_name": lead.get("last_name", ""),
        "company_name": lead.get("company_name", ""),
        "website": lead.get("website") or lead.get("linkedin_url", ""),
        "personalization": lead.get("personalization", ""),
    }

    if lead.get("job_title"):
        payload["job_title"] = lead["job_title"]
    if lead.get("phone"):
        payload["phone"] = lead["phone"]
    if lead.get("linkedin_url"):
        payload["custom_variables"] = {"linkedin_url": lead["linkedin_url"]}

    return payload


def add_lead_to_instantly(config: WorkflowConfig, campaign_id: str, lead: dict[str, Any]) -> dict[str, Any]:
    response = requests.post(
        INSTANTLY_LEAD_CREATE_URL,
        headers={"Authorization": f"Bearer {config.instantly_api_key}", "Content-Type": "application/json"},
        json=instantly_lead_payload(lead, campaign_id, config.instantly_skip_if_in_workspace),
        timeout=60,
    )
    response.raise_for_status()
    return response.json()


def validate_and_add_to_instantly(
    leads: list[dict[str, Any]],
    campaign_name: str,
    config: WorkflowConfig,
    enrich_missing_with_apollo: bool = True,
) -> dict[str, Any]:
    apollo_summary = {"apollo_missing_input": 0, "apollo_enriched": 0, "apollo_unresolved": 0}
    if enrich_missing_with_apollo and any(not normalize_email(lead.get("email")) for lead in leads):
        if not config.apollo_api_key:
            raise RuntimeError("APOLLO_API_KEY is required because at least one lead is missing email")
        apollo_summary = enrich_missing_emails_with_apollo(config.apollo_api_key, leads)

    leads_by_email = key_by_email(leads)
    all_emails = dedupe_preserve_order(leads_by_email.keys())
    if not all_emails:
        raise RuntimeError("No emails found after CSV/Apollo enrichment")

    reoon_task_id = submit_reoon_task(config.reoon_api_key, all_emails)
    reoon_result = poll_reoon_task(config, reoon_task_id)
    reoon_passed = dedupe_preserve_order(filter_reoon_results(reoon_result, all_emails))

    bounceban_task_id = submit_bounceban_task(config.bounceban_api_key, reoon_passed)
    poll_bounceban_task(config, bounceban_task_id)
    bounceban_dump = fetch_bounceban_dump(config.bounceban_api_key, bounceban_task_id)
    deliverable = dedupe_preserve_order(filter_bounceban_deliverables(bounceban_dump, reoon_passed))

    campaign = create_instantly_campaign(config, campaign_name)

    added = 0
    skipped: list[str] = []
    errors: list[str] = []

    for email in deliverable:
        lead = leads_by_email[email]
        try:
            body = add_lead_to_instantly(config, campaign["id"], lead)
            returned_campaign = str(body.get("campaign") or "")
            returned_id = str(body.get("id") or "")

            if returned_campaign and returned_campaign != campaign["id"]:
                skipped.append(f"{email}: already exists in another Instantly campaign/workspace")
            elif returned_id:
                added += 1
            else:
                errors.append(f"{email}: {body}")
                if config.instantly_fail_fast:
                    raise RuntimeError(body)

        except Exception as exc:
            errors.append(f"{email}: {exc}")
            if config.instantly_fail_fast:
                raise

    return {
        **apollo_summary,
        "input_leads": len(leads),
        "input_emails": len(all_emails),
        "reoon_passed": len(reoon_passed),
        "bounceban_deliverable": len(deliverable),
        "campaign_id": campaign["id"],
        "campaign_name": campaign["name"],
        "leads_added": added,
        "leads_skipped": skipped,
        "lead_errors": errors,
    }


def run_csv_to_instantly(csv_path: str, campaign_name: str | None, settings_path: str | None = None) -> dict[str, Any]:
    settings = load_settings(settings_path)
    resolved_campaign_name = resolve_campaign_name(campaign_name, settings)
    return validate_and_add_to_instantly(load_leads_csv(csv_path), resolved_campaign_name, config_from_env(settings))


def run_apollo_label_to_instantly(
    label_ids: list[str],
    campaign_name: str | None,
    settings_path: str | None = None,
) -> dict[str, Any]:
    settings = load_settings(settings_path)
    resolved_campaign_name = resolve_campaign_name(campaign_name, settings)
    config = config_from_env(settings)
    if not config.apollo_api_key:
        raise RuntimeError("APOLLO_API_KEY is required for Apollo label input")
    leads = search_apollo_contacts_by_label(config.apollo_api_key, label_ids)
    return validate_and_add_to_instantly(leads, resolved_campaign_name, config, enrich_missing_with_apollo=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Apollo/CSV -> Reoon -> BounceBan -> Instantly workflow")
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan_csv_parser = subparsers.add_parser("plan-csv", help="Dry-run plan from a CSV without API calls")
    plan_csv_parser.add_argument("--csv", required=True, help="Path to the input CSV")
    plan_csv_parser.add_argument("--campaign-name", help="Instantly campaign name to create")
    plan_csv_parser.add_argument("--settings", required=True, help="JSON settings file with approved campaign copy")
    plan_csv_parser.add_argument(
        "--no-apollo-enrich",
        action="store_true",
        help="Plan as if missing-email rows will not be enriched through Apollo",
    )

    csv_parser = subparsers.add_parser("run-csv", help="Run workflow from a CSV")
    csv_parser.add_argument("--csv", required=True, help="Path to the input CSV")
    csv_parser.add_argument("--campaign-name", help="Instantly campaign name to create")
    csv_parser.add_argument(
        "--no-apollo-enrich",
        action="store_true",
        help="Do not call Apollo for rows that are missing email",
    )
    csv_parser.add_argument("--settings", help="Optional JSON settings file for AI-editable flow options")

    plan_apollo_parser = subparsers.add_parser(
        "plan-apollo-label",
        help="Dry-run plan from Apollo contact label IDs without API calls",
    )
    plan_apollo_parser.add_argument("--label-id", action="append", required=True, help="Apollo contact label/list ID")
    plan_apollo_parser.add_argument("--campaign-name", help="Instantly campaign name to create")
    plan_apollo_parser.add_argument("--settings", required=True, help="JSON settings file with approved campaign copy")

    apollo_parser = subparsers.add_parser("run-apollo-label", help="Run workflow from Apollo contact label IDs")
    apollo_parser.add_argument("--label-id", action="append", required=True, help="Apollo contact label/list ID")
    apollo_parser.add_argument("--campaign-name", help="Instantly campaign name to create")
    apollo_parser.add_argument("--settings", help="Optional JSON settings file for AI-editable flow options")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "plan-csv":
        settings = load_settings(args.settings)
        campaign_name = resolve_campaign_name(args.campaign_name, settings)
        result = build_dry_run_plan(
            summarize_csv_source(args.csv),
            campaign_name,
            settings,
            enrich_missing_with_apollo=not args.no_apollo_enrich,
        )
    elif args.command == "run-csv":
        settings = load_settings(args.settings)
        config = config_from_env(settings)
        campaign_name = resolve_campaign_name(args.campaign_name, settings)
        result = validate_and_add_to_instantly(
            load_leads_csv(args.csv),
            campaign_name,
            config,
            enrich_missing_with_apollo=not args.no_apollo_enrich,
        )
    elif args.command == "plan-apollo-label":
        settings = load_settings(args.settings)
        campaign_name = resolve_campaign_name(args.campaign_name, settings)
        result = build_dry_run_plan(
            {"type": "apollo_label", "label_ids": args.label_id},
            campaign_name,
            settings,
            enrich_missing_with_apollo=True,
        )
    elif args.command == "run-apollo-label":
        result = run_apollo_label_to_instantly(args.label_id, args.campaign_name, args.settings)
    else:
        parser.error(f"Unknown command: {args.command}")

    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
