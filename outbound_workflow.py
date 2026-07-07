from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
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
INSTANTLY_CAMPAIGN_EMAIL_ACCOUNTS_URL = "https://api.instantly.ai/api/v2/campaigns/{campaign_id}/emailaccounts"
INSTANTLY_ACCOUNTS_URL = "https://api.instantly.ai/api/v2/accounts"
INSTANTLY_LEAD_CREATE_URL = "https://api.instantly.ai/api/v2/leads"


# ---------------------------------------------------------------------------
# Company name cleaning
# ---------------------------------------------------------------------------

_LEGAL_SUFFIXES_RE = re.compile(
    r"\b(GmbH|Ltd|AG|Inc|LLC|Oy|BV|SA|S\.?r\.?l\.?|Srl|Corp|Corporation|Plc|Pvt|Pte|Pty|Sdn|Bhd|Ltda)\b",
    re.IGNORECASE,
)

_EMOJI_RE = re.compile(
    r"[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF®™•]"
)

_GENERIC_TAILS = frozenset(
    {
        "technologies", "solutions", "systems", "group", "holdings",
        "services", "media", "corporation", "company", "networks", "network",
        "international", "entertainment", "ventures", "global", "worldwide",
        "partners", "associates", "consulting", "management", "industries",
        "enterprises", "innovations", "communications", "logistics", "digital",
        "resources", "financial", "technology", "labs", "lab",
        # geography qualifiers used as division suffixes
        "canada", "australia", "india", "europe", "asia", "africa",
        "america", "americas", "apac", "emea", "latam",
    }
)

# Stop words skipped when building an acronym (except at position 0)
_STOP_WORDS = frozenset({"of", "and", "a", "an", "in", "at", "for", "by", "with", "to", "de", "the"})


def _acronymize(words: list[str]) -> str:
    """Return uppercase initials of significant words (skip stop words after position 0)."""
    letters = []
    for i, word in enumerate(words):
        alpha = re.sub(r"[^\w]", "", word)
        if not alpha:
            continue
        if i > 0 and alpha.lower() in _STOP_WORDS:
            continue
        letters.append(alpha[0].upper())
    return "".join(letters) if letters else "EMPTY"


def clean_company_name(name: str | None) -> str:
    """Port of the JS cleanName algorithm. Returns 'EMPTY' for blank input."""
    if not name or not name.strip():
        return "EMPTY"

    clean = name.strip()

    # Strip legal entity suffixes
    clean = _LEGAL_SUFFIXES_RE.sub("", clean)

    # Strip everything after common separators
    for sep in ("|", " - ", " – "):
        if sep in clean:
            clean = clean.split(sep)[0]

    # Strip parenthetical content
    clean = re.sub(r"\s*\(.*?\)\s*", " ", clean)

    # Strip emojis and common decoration symbols
    clean = _EMOJI_RE.sub("", clean)

    # Drop tokens that are pure punctuation (e.g. lone "." left after legal suffix strip)
    words = [w for w in clean.strip().split() if re.sub(r"[^\w]", "", w)]

    # 4+ word names become acronyms (e.g. "East Bay Community Action Program" → EBCAP)
    if len(words) >= 4:
        return _acronymize(words)

    processed: list[str] = []
    for word in words:
        if word.upper() == "AI":
            processed.append("AI")
        elif len(word) <= 3 and word == word.upper() and word.isalpha():
            # Short acronym e.g. IBM, UK
            processed.append(word)
        elif any(c.islower() for c in word) and any(c.isupper() for c in word[1:]):
            # CamelCase — preserve as-is
            processed.append(word)
        elif "." in word:
            # Domain-style token — preserve
            processed.append(word)
        elif len(word) > 3 and word == word.upper() and word.isalpha():
            # ALL-CAPS → Title-case
            processed.append(word[0] + word[1:].lower())
        else:
            processed.append(word)

    # Iteratively strip trailing generic/geography descriptor words
    while len(processed) > 1:
        tail = re.sub(r"[^\w]", "", processed[-1])
        if tail.lower() in _GENERIC_TAILS:
            processed.pop()
        else:
            break

    clean = " ".join(processed)
    clean = re.sub(r"[,\-\|\.\s]+$", "", clean).strip()

    return clean or "EMPTY"


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
    instantly_sequence_steps: list | None
    instantly_email_account_ids: list[str]


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
        "sequence_steps": nested_setting(settings, "instantly", "sequence_steps", None),
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
        instantly_sequence_steps=nested_setting(settings, "instantly", "sequence_steps", None),
        instantly_email_account_ids=nested_setting(settings, "instantly", "email_account_ids", []) or [],
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

    company_name = first_present(
        row,
        ["Company_Name_rounded", "company_name", "Company Name", "Company", "company", "organization_name"],
    )
    job_title = first_present(row, ["Title_rounded", "job_title", "title", "Title"])

    lead = {
        "email": email,
        "first_name": first_name,
        "last_name": last_name,
        "company_name": company_name,
        "company_name_cleaned": clean_company_name(company_name),
        "linkedin_url": first_present(
            row,
            ["linkedin_url", "Person Linkedin Url", "LinkedIn", "linkedin", "person_linkedin_url"],
        ),
        "personalization": first_present(row, ["personalization", "Personalization", "icebreaker"]),
        "job_title": job_title,
        "phone": first_present(row, ["phone", "Phone", "phone_number"]),
        "website": first_present(row, ["website", "Website", "website_url", "company_website", "domain"]),
        "organization_domain": first_present(
            row,
            ["organization_domain", "company_domain", "domain", "website", "Website", "website_url"],
        ),
        "apollo_id": first_present(row, ["apollo_id", "id", "person_id", "contact_id"]),
        "company_description": first_present(row, ["company_description"]),
        "subvertical": first_present(row, ["subvertical"]),
        "team_size_rounded": first_present(row, ["team_size_rounded"]),
        "industry": first_present(row, ["industry", "Industry"]),
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


def list_instantly_email_accounts(api_key: str) -> list[dict[str, Any]]:
    """Return all Instantly sending email accounts for this workspace."""
    accounts: list[dict[str, Any]] = []
    params: dict[str, Any] = {"limit": 100}

    while True:
        response = requests.get(
            INSTANTLY_ACCOUNTS_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            params=params,
            timeout=60,
        )
        response.raise_for_status()
        body = response.json()

        items = body if isinstance(body, list) else body.get("items") or body.get("accounts") or []
        accounts.extend(item for item in items if isinstance(item, dict))

        next_cursor = (body.get("next_starting_after") or body.get("cursor")) if isinstance(body, dict) else None
        if not next_cursor or not items:
            break
        params["starting_after"] = next_cursor

    return accounts


def assign_email_accounts_to_campaign(api_key: str, campaign_id: str, account_ids: list[str]) -> None:
    # Instantly V2: assign sending accounts by PATCHing email_list on the campaign
    url = f"https://api.instantly.ai/api/v2/campaigns/{campaign_id}"
    response = requests.patch(
        url,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"email_list": account_ids},
        timeout=60,
    )
    response.raise_for_status()


PIPELINE_STATE_PATH = Path("pipeline_state.json")
GDRIVE_APOLLO_FOLDER_ID = "1U9DrZSZRN2o3ToCnwq-ja2xQauELofMM"
SLACK_CHANNEL = "#outbound-ops"
SLACK_CHANNEL_ID = "C0BCWR5CTT3"

# How many company name pairs to show in the Slack review message
COMPANY_REVIEW_SAMPLE_SIZE = 20


def _load_pipeline_state() -> dict[str, Any]:
    if PIPELINE_STATE_PATH.exists():
        with PIPELINE_STATE_PATH.open(encoding="utf-8") as f:
            return json.load(f)
    return {"next_sequence": 65}


def _save_pipeline_state(state: dict[str, Any]) -> None:
    with PIPELINE_STATE_PATH.open("w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
        f.write("\n")


def allocate_sequence_code() -> str:
    """Return the next S0XX code and persist the incremented counter."""
    state = _load_pipeline_state()
    seq = int(state.get("next_sequence", 65))
    code = f"S{seq:03d}"
    state["next_sequence"] = seq + 1
    _save_pipeline_state(state)
    return code


def peek_sequence_code() -> str:
    """Return the next S0XX code without incrementing the counter."""
    state = _load_pipeline_state()
    seq = int(state.get("next_sequence", 65))
    return f"S{seq:03d}"


def make_spreadsheet_name(list_name: str) -> str:
    """Build the canonical spreadsheet name: S0XX-<list_name>."""
    code = allocate_sequence_code()
    safe = list_name.strip().replace(" ", "_")
    return f"{code}-{safe}"


# ---------------------------------------------------------------------------
# Pipeline run log  (pipeline_log.json)
# ---------------------------------------------------------------------------

PIPELINE_LOG_PATH = Path("pipeline_log.json")
GDRIVE_MASTER_SHEET_ID = "1G7_WImSTQSiA4Msn4XXxbK26sU4Hj6jvZNbrBQ2N15Q"

# Columns that match the "S" table in the master Google Sheet exactly
_MASTER_SHEET_COLS = [
    "List No.",
    "Date Created",
    "List Name",
    "Source",
    "Function",
    "Geography",
    "Sent by what method?",
    "Results",
    "Contact List (Green = enriched, Orange = n/a)",
    "Re-cleaned by Shifu",
    "Notes",
]

# Local log fields (superset — includes technical fields not in master sheet)
_LOG_FIELDS = [
    "sequence_code",
    "date_created",
    "list_name",
    "function",
    "geography",
    "spreadsheet_name",
    "total_exported",
    "deliverable",
    "apollo_sheet_url",
    "cleaned_sheet_url",
    "instantly_campaign_id",
    "notes",
]


def _load_pipeline_log() -> list[dict[str, Any]]:
    if PIPELINE_LOG_PATH.exists():
        with PIPELINE_LOG_PATH.open(encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    return []


def _save_pipeline_log(log: list[dict[str, Any]]) -> None:
    with PIPELINE_LOG_PATH.open("w", encoding="utf-8") as f:
        json.dump(log, f, indent=2)
        f.write("\n")


def upsert_log_entry(sequence_code: str, **fields: Any) -> dict[str, Any]:
    """Create or update the log entry for sequence_code. Skips None values."""
    log = _load_pipeline_log()
    entry = next((e for e in log if e.get("sequence_code") == sequence_code), None)
    if entry is None:
        entry = {"sequence_code": sequence_code}
        log.append(entry)
    for k, v in fields.items():
        if v is not None:
            entry[k] = v
    _save_pipeline_log(log)
    return entry


def format_company_review_message(
    csv_path: str,
    spreadsheet_name: str,
    apollo_sheet_url: str = "",
    master_sheet_url: str = "",
) -> str:
    """
    Build the Slack message for the company-name review step (Step 4).
    Shows up to COMPANY_REVIEW_SAMPLE_SIZE original → cleaned pairs sampled
    to maximise variety (picks rows where the two values actually differ first,
    then fills with identical pairs if needed).
    """
    leads = load_leads_csv(csv_path)
    total = len(leads)
    with_email = sum(1 for l in leads if normalize_email(l.get("email")))

    # Collect unique (original, cleaned) pairs — prefer ones that differ
    seen: set[tuple[str, str]] = set()
    pairs_changed: list[tuple[str, str]] = []
    pairs_same: list[tuple[str, str]] = []
    for lead in leads:
        orig = (lead.get("company_name") or "").strip()
        clean = (lead.get("company_name_cleaned") or "").strip()
        if not orig:
            continue
        key = (orig, clean)
        if key in seen:
            continue
        seen.add(key)
        if orig != clean:
            pairs_changed.append(key)
        else:
            pairs_same.append(key)

    sample = (pairs_changed + pairs_same)[:COMPANY_REVIEW_SAMPLE_SIZE]

    n_changed = len(pairs_changed)
    lines = [
        f"*New Apollo Export: {spreadsheet_name}*  |  {total} contacts  |  {with_email} with email",
    ]
    if apollo_sheet_url:
        lines.append(f"📊 *Review spreadsheet:* {apollo_sheet_url}")
    if master_sheet_url:
        lines.append(f"📋 *Master dashboard:* {master_sheet_url}")
    lines.append("")

    if pairs_changed:
        preview = pairs_changed[:10]
        lines.append(
            f"*Company name changes* ({n_changed} of {len(seen)} unique names were cleaned):"
        )
        for orig, cleaned in preview:
            lines.append(f"• {orig}  →  {cleaned}")
        if n_changed > len(preview):
            lines.append(f"  _…and {n_changed - len(preview)} more — see spreadsheet column E_")
    else:
        lines.append("_All company names passed through unchanged._")

    lines.append("")
    lines.append(
        "Open the spreadsheet above and check column E (*company_name_cleaned*). "
        "Reply in this thread with corrections (e.g. \"keep Group on Hg Group\") or *approved* to continue."
    )
    return "\n".join(lines)


def format_campaign_setup_message(
    spreadsheet_name: str,
    deliverable_count: int,
    instantly_accounts: list[dict[str, Any]],
) -> str:
    """Build the Slack campaign-setup form message (Step 7)."""
    # Group by sending domain and show count per domain
    domain_counts: dict[str, int] = {}
    for a in instantly_accounts:
        email = a.get("email", "")
        domain = email.split("@")[-1] if "@" in email else email
        domain_counts[domain] = domain_counts.get(domain, 0) + 1

    if domain_counts:
        account_lines = [
            f"  • `@{domain}` ({count} account{'s' if count > 1 else ''})"
            for domain, count in sorted(domain_counts.items())
        ]
        accounts_block = "\n".join(account_lines)
    else:
        accounts_block = "  _(run `list-instantly-accounts` to fetch these)_"

    return "\n".join([
        f"*Campaign setup: {spreadsheet_name}* 🚀",
        f"*Deliverable leads ready:* {deliverable_count}",
        "",
        "Please reply in this thread with:",
        "",
        "*1. Subject line(s)* — one per email in the sequence",
        "*2. Email body/bodies* — HTML or plain text, one per email (label them Email 1, Email 2…)",
        "*3. Delay between emails* — e.g. `3 days`, `4 days`",
        "*4. Send window* — e.g. `9am to 11am`",
        "*5. Timezone* — e.g. `GMT`, `America/New_York`, `Europe/London`",
        "*6. Campaign duration* — e.g. `30 days`",
        "*7. Sending account* — pick one from below:",
        "",
        accounts_block,
        "",
        "Reply with all of the above and I'll set up the campaign.",
    ])


def append_master_row(
    current_csv_b64: str,
    sequence_code: str,
    date_created: str,
    list_name: str,
    function: str,
    geography: str,
    results: str,
    sheet_url: str,
    notes: str = "",
) -> str:
    """
    Decode the current master sheet CSV (base64), append a new S-row, and return
    the full updated CSV string ready to upload back to Drive.

    Only appends to the S-table section (rows whose first cell matches S\\d+).
    Leaves the R-table and any other content untouched.
    """
    import base64

    raw_bytes = base64.b64decode(current_csv_b64)
    try:
        raw = raw_bytes.decode("utf-8-sig")  # strip BOM if present
    except UnicodeDecodeError:
        raw = raw_bytes.decode("latin-1")
    lines = raw.splitlines(keepends=True)

    new_row_values = [
        sequence_code,
        date_created,
        list_name,
        "Apollo",
        function,
        geography,
        "Instantly",
        results,
        sheet_url,
        "",   # Re-cleaned by Shifu — always blank on creation
        notes,
    ]

    buf = io.StringIO()
    new_row_csv = io.StringIO()
    csv.writer(new_row_csv).writerow(new_row_values)
    new_row_line = new_row_csv.getvalue()

    # Find the last line that is an S-row (first field matches S followed by digits)
    import re as _re
    last_s_idx = -1
    for i, line in enumerate(lines):
        try:
            parsed = next(csv.reader([line]))
            if not parsed:
                continue
            first_cell = parsed[0]
            if _re.match(r"^S\d+", first_cell.strip()):
                last_s_idx = i
        except (StopIteration, csv.Error):
            continue

    if last_s_idx == -1:
        # No S-rows found — just append at end
        buf.writelines(lines)
        buf.write(new_row_line)
    else:
        buf.writelines(lines[: last_s_idx + 1])
        buf.write(new_row_line)
        buf.writelines(lines[last_s_idx + 1 :])

    return buf.getvalue()


def _build_sequence_steps(config: WorkflowConfig) -> list[dict]:
    if config.instantly_sequence_steps:
        steps = []
        for i, step in enumerate(config.instantly_sequence_steps):
            steps.append({
                "type": "email",
                "delay": int(step.get("delay_days", 2)) if i > 0 else 0,
                "delay_unit": "days",
                "variants": [{"subject": step["subject"], "body": step["body"]}],
            })
        return steps
    return [
        {
            "type": "email",
            "delay": 2,
            "delay_unit": "days",
            "variants": [{"subject": config.instantly_email_subject, "body": config.instantly_email_body}],
        }
    ]


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
                "steps": _build_sequence_steps(config),
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
        "company_name": lead.get("company_name_cleaned") or lead.get("company_name", ""),
        "website": lead.get("website") or lead.get("linkedin_url", ""),
        "personalization": lead.get("personalization", ""),
    }

    if lead.get("job_title"):
        payload["job_title"] = lead["job_title"]
    if lead.get("phone"):
        payload["phone"] = lead["phone"]

    custom: dict[str, str] = {}
    for key in ("linkedin_url", "company_description", "subvertical", "team_size_rounded", "industry"):
        value = lead.get(key)
        if value:
            custom[key] = str(value)
    if custom:
        payload["custom_variables"] = custom

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


def _safe_filename(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name).strip("_") or "campaign"


def _save_leads_csv(path: str, leads: list[dict[str, Any]]) -> None:
    if not leads:
        return
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    fieldnames: list[str] = []
    seen: set[str] = set()
    for lead in leads:
        for key in lead.keys():
            if key not in seen:
                fieldnames.append(key)
                seen.add(key)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for lead in leads:
            writer.writerow({k: lead.get(k, "") for k in fieldnames})


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

    out_dir = Path("runs") / _safe_filename(campaign_name)

    reoon_task_id = submit_reoon_task(config.reoon_api_key, all_emails)
    reoon_result = poll_reoon_task(config, reoon_task_id)
    reoon_passed = dedupe_preserve_order(filter_reoon_results(reoon_result, all_emails))
    _save_leads_csv(
        str(out_dir / "reoon_passed.csv"),
        [leads_by_email[e] for e in reoon_passed if e in leads_by_email],
    )

    bounceban_task_id = submit_bounceban_task(config.bounceban_api_key, reoon_passed)
    poll_bounceban_task(config, bounceban_task_id)
    bounceban_dump = fetch_bounceban_dump(config.bounceban_api_key, bounceban_task_id)
    deliverable = dedupe_preserve_order(filter_bounceban_deliverables(bounceban_dump, reoon_passed))
    _save_leads_csv(
        str(out_dir / "bounceban_deliverable.csv"),
        [leads_by_email[e] for e in deliverable if e in leads_by_email],
    )

    campaign = create_instantly_campaign(config, campaign_name)

    if config.instantly_email_account_ids:
        assign_email_accounts_to_campaign(
            config.instantly_api_key, campaign["id"], config.instantly_email_account_ids
        )

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


def verify_csv_emails(csv_path: str, output_path: str) -> dict[str, Any]:
    """Run Reoon + BounceBan verification on a CSV and save passing rows to output_path."""
    config = config_from_env({})
    leads = load_leads_csv(csv_path)

    leads_by_email = key_by_email(leads)
    all_emails = dedupe_preserve_order(leads_by_email.keys())
    if not all_emails:
        raise RuntimeError("No emails found in CSV")

    reoon_task_id = submit_reoon_task(config.reoon_api_key, all_emails)
    reoon_result = poll_reoon_task(config, reoon_task_id)
    reoon_passed = dedupe_preserve_order(filter_reoon_results(reoon_result, all_emails))

    bounceban_task_id = submit_bounceban_task(config.bounceban_api_key, reoon_passed)
    poll_bounceban_task(config, bounceban_task_id)
    bounceban_dump = fetch_bounceban_dump(config.bounceban_api_key, bounceban_task_id)
    deliverable = dedupe_preserve_order(filter_bounceban_deliverables(bounceban_dump, reoon_passed))

    passing_leads = [leads_by_email[e] for e in deliverable if e in leads_by_email]
    _save_leads_csv(output_path, passing_leads)

    return {
        "input_leads": len(leads),
        "input_emails": len(all_emails),
        "reoon_passed": len(reoon_passed),
        "bounceban_deliverable": len(deliverable),
        "output": output_path,
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

    verify_csv_parser = subparsers.add_parser(
        "verify-csv",
        help="Run Reoon + BounceBan verification on a CSV and save passing rows — no Instantly upload",
    )
    verify_csv_parser.add_argument("--csv", required=True, help="Path to the input CSV")
    verify_csv_parser.add_argument("--output", required=True, help="Path for the verified output CSV")

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

    clean_parser = subparsers.add_parser(
        "clean-names",
        help="Add company_name_cleaned column to a CSV without running any API calls",
    )
    clean_parser.add_argument("--csv", required=True, help="Path to the input CSV")
    clean_parser.add_argument("--output", required=True, help="Path for the output CSV")

    export_parser = subparsers.add_parser(
        "export-apollo-label",
        help="Fetch contacts from Apollo label, clean company names, save CSV — no email verification",
    )
    export_parser.add_argument("--label-id", action="append", required=True, help="Apollo contact label/list ID")
    export_parser.add_argument("--output", required=True, help="Path to save the output CSV")

    subparsers.add_parser(
        "list-instantly-accounts",
        help="List all Instantly sending email accounts in the workspace",
    )

    subparsers.add_parser(
        "next-sequence",
        help="Show the next S0XX code without incrementing (peek only)",
    )

    name_parser = subparsers.add_parser(
        "make-spreadsheet-name",
        help="Allocate the next S0XX code and return the full spreadsheet name (increments counter)",
    )
    name_parser.add_argument("--list-name", required=True, help="Apollo list name provided by the user")

    log_parser = subparsers.add_parser(
        "log-run",
        help="Create or update the pipeline_log.json entry for a given S0XX run",
    )
    log_parser.add_argument("--sequence-code", required=True, help="e.g. S065")
    log_parser.add_argument("--list-name", help="Human-readable list name")
    log_parser.add_argument("--function", help="Role type e.g. 'HR & People, not Talent'")
    log_parser.add_argument("--geography", help="Region e.g. 'Europe', 'UK', 'Global'")
    log_parser.add_argument("--spreadsheet-name", help="Full spreadsheet name e.g. S065-Web3_People_Leaders")
    log_parser.add_argument("--date-created", help="Date label e.g. Jun-2026 (defaults to current month-year)")
    log_parser.add_argument("--total-exported", type=int, help="Total contacts exported from Apollo")
    log_parser.add_argument("--deliverable", type=int, help="Deliverable count after BounceBan")
    log_parser.add_argument("--apollo-sheet-url", help="Google Drive URL for the S0XX export sheet")
    log_parser.add_argument("--cleaned-sheet-url", help="Google Drive URL for the cleaned (deliverable) sheet")
    log_parser.add_argument("--campaign-id", help="Instantly campaign ID")
    log_parser.add_argument("--notes", help="Optional notes")

    review_parser = subparsers.add_parser(
        "format-slack-company-review",
        help="Print the Slack company-name review message for #outbound-ops (Step 4)",
    )
    review_parser.add_argument("--csv", required=True, help="Path to the apollo_export.csv")
    review_parser.add_argument("--spreadsheet-name", required=True, help="e.g. S065-Web3_People_Leaders")
    review_parser.add_argument("--apollo-sheet-url", default="", help="Drive URL for the export sheet")
    review_parser.add_argument("--master-sheet-url", default="", help="Drive URL for the master dashboard")

    campaign_setup_parser = subparsers.add_parser(
        "format-slack-campaign-setup",
        help="Print the Slack campaign-setup form message for #outbound-ops (Step 7)",
    )
    campaign_setup_parser.add_argument("--spreadsheet-name", required=True, help="e.g. S065-Web3_People_Leaders")
    campaign_setup_parser.add_argument("--deliverable", required=True, type=int, help="Deliverable lead count")

    append_parser = subparsers.add_parser(
        "append-master-row",
        help="Decode the current master sheet CSV (base64), append a new S-row, print updated CSV to stdout",
    )
    append_parser.add_argument("--base64", required=True, dest="b64", help="Base64-encoded current master sheet CSV from Drive download")
    append_parser.add_argument("--sequence-code", required=True, help="e.g. S065")
    append_parser.add_argument("--date-created", required=True, help="e.g. Jun-2026")
    append_parser.add_argument("--list-name", required=True, help="Human-readable list name")
    append_parser.add_argument("--function", required=True, help="Role type e.g. 'HR & People, not Talent'")
    append_parser.add_argument("--geography", required=True, help="Region e.g. 'Europe'")
    append_parser.add_argument("--results", default="", help="Contact count")
    append_parser.add_argument("--sheet-url", default="", help="Google Drive URL for the S0XX export sheet")
    append_parser.add_argument("--notes", default="", help="Optional notes")

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
    elif args.command == "verify-csv":
        result = verify_csv_emails(args.csv, args.output)
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
    elif args.command == "clean-names":
        leads = load_leads_csv(args.csv)
        _save_leads_csv(args.output, leads)
        result = {"rows_processed": len(leads), "output": args.output}
    elif args.command == "export-apollo-label":
        apollo_key = require_env("APOLLO_API_KEY")
        leads = search_apollo_contacts_by_label(apollo_key, args.label_id)
        _save_leads_csv(args.output, leads)
        result = {"rows_exported": len(leads), "output": args.output}
    elif args.command == "list-instantly-accounts":
        accounts = list_instantly_email_accounts(require_env("INSTANTLY_API_KEY"))
        summary = [
            {"id": a.get("email", ""), "email": a.get("email", ""), "status": a.get("status", "")}
            for a in accounts
        ]
        result = {"accounts": summary, "count": len(summary)}
    elif args.command == "next-sequence":
        result = {"next_sequence_code": peek_sequence_code(), "note": "counter not incremented"}
    elif args.command == "make-spreadsheet-name":
        name = make_spreadsheet_name(args.list_name)
        result = {
            "spreadsheet_name": name,
            "gdrive_folder_id": GDRIVE_APOLLO_FOLDER_ID,
            "note": "sequence counter has been incremented",
        }
    elif args.command == "log-run":
        today = date.today()
        default_date_label = today.strftime("%b-%Y")
        entry = upsert_log_entry(
            args.sequence_code,
            list_name=args.list_name,
            function=args.function,
            geography=args.geography,
            spreadsheet_name=args.spreadsheet_name,
            date_created=args.date_created or default_date_label,
            total_exported=args.total_exported,
            deliverable=args.deliverable,
            apollo_sheet_url=args.apollo_sheet_url,
            cleaned_sheet_url=args.cleaned_sheet_url,
            instantly_campaign_id=args.campaign_id,
            notes=args.notes,
        )
        result = {"updated": True, "entry": entry}
    elif args.command == "format-slack-company-review":
        msg = format_company_review_message(
            csv_path=args.csv,
            spreadsheet_name=args.spreadsheet_name,
            apollo_sheet_url=args.apollo_sheet_url,
            master_sheet_url=args.master_sheet_url,
        )
        sys.stdout.write(msg + "\n")
        return 0
    elif args.command == "format-slack-campaign-setup":
        accounts = list_instantly_email_accounts(require_env("INSTANTLY_API_KEY"))
        summary = [
            {"id": a.get("id", ""), "email": a.get("email", ""), "status": a.get("status", "")}
            for a in accounts
        ]
        msg = format_campaign_setup_message(
            spreadsheet_name=args.spreadsheet_name,
            deliverable_count=args.deliverable,
            instantly_accounts=summary,
        )
        sys.stdout.write(msg + "\n")
        result = {"message_preview": msg[:200] + "…"}
    elif args.command == "append-master-row":
        updated_csv = append_master_row(
            current_csv_b64=args.b64,
            sequence_code=args.sequence_code,
            date_created=args.date_created,
            list_name=args.list_name,
            function=args.function,
            geography=args.geography,
            results=args.results,
            sheet_url=args.sheet_url,
            notes=args.notes,
        )
        sys.stdout.write(updated_csv)
        return 0
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
