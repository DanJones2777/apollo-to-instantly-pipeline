"""
Slack Email Cleaning Bot

Drop a CSV or XLSX of Apollo contacts into the configured Slack channel.
The bot verifies every email through Reoon (safe + catch-all) then
BounceBan (deliverable only) and posts a cleaned CSV back into the thread.

Usage:
    python slack_cleaner_bot.py
"""

from __future__ import annotations

import csv
import io
import logging
import os
import time
from typing import Any, Iterable

import re

import requests

# ---------------------------------------------------------------------------
# Optional dependencies — installed via requirements.txt
# ---------------------------------------------------------------------------
try:
    from slack_bolt import App
    from slack_bolt.adapter.socket_mode import SocketModeHandler
    import pandas as pd
except ImportError as exc:
    raise SystemExit(
        f"Missing dependency: {exc}\n"
        "Run: pip install slack-bolt pandas openpyxl"
    ) from exc

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# API endpoints (same as outbound_workflow.py)
# ---------------------------------------------------------------------------
REOON_CREATE_TASK_URL = "https://emailverifier.reoon.com/api/v1/create-bulk-verification-task/"
REOON_GET_TASK_URL = "https://emailverifier.reoon.com/api/v1/get-result-bulk-verification-task/"

BOUNCEBAN_BULK_VERIFY_URL = "https://api.bounceban.com/v1/verify/bulk"
BOUNCEBAN_BULK_STATUS_URL = "https://api.bounceban.com/v1/verify/bulk/status"
BOUNCEBAN_BULK_DUMP_URL = "https://api.bounceban.com/v1/verify/bulk/dump"

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_APP_TOKEN = os.environ["SLACK_APP_TOKEN"]
SLACK_CHANNEL_ID = os.environ["SLACK_CHANNEL_ID"]
REOON_API_KEY = os.environ["REOON_API_KEY"]
BOUNCEBAN_API_KEY = os.environ["BOUNCEBAN_API_KEY"]

REOON_MAX_WAIT = int(os.environ.get("REOON_MAX_WAIT_SECONDS", "1800"))
REOON_POLL_INTERVAL = int(os.environ.get("REOON_POLL_INTERVAL_SECONDS", "15"))
BOUNCEBAN_MAX_WAIT = int(os.environ.get("BOUNCEBAN_MAX_WAIT_SECONDS", "3600"))
BOUNCEBAN_POLL_INTERVAL = int(os.environ.get("BOUNCEBAN_POLL_INTERVAL_SECONDS", "15"))

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
COMPANY_COLUMN_NAMES = [
    "company", "Company", "company_name", "Company Name",
    "organization", "Organization", "Account Name", "account_name",
]

LEGAL_SUFFIXES = re.compile(
    r"\b(GmbH|Ltd|AG|Inc|LLC|Oy|BV|SA|S\.?r\.?l\.?|Srl|Corp|Corporation|Plc)\b",
    re.IGNORECASE,
)
EMOJI_SYMBOLS = re.compile(
    r"[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF®™•]",
    re.UNICODE,
)
GENERIC_TAILS = {
    "technologies", "solutions", "systems", "group", "holdings",
    "services", "media", "corporation", "company", "networks", "network",
}


def clean_company_name(name: str | None) -> str:
    if not name or not str(name).strip():
        return "EMPTY"

    clean = str(name).strip()

    # Legal suffixes
    clean = LEGAL_SUFFIXES.sub("", clean)

    # Descriptors after separators
    if "|" in clean:
        clean = clean.split("|")[0]
    if " - " in clean:
        clean = clean.split(" - ")[0]
    if " – " in clean:
        clean = clean.split(" – ")[0]
    clean = re.sub(r"\s*\(.*?\)\s*", " ", clean)

    # Emoji & symbol cleanup
    clean = EMOJI_SYMBOLS.sub("", clean)

    # Capitalisation & acronyms
    words = clean.strip().split()
    cleaned_words = []
    for word in words:
        upper = word.upper()
        is_acronym = len(word) <= 3 and word == upper
        is_camel = bool(re.search(r"[a-z]", word)) and bool(re.search(r"[A-Z]", word[1:]))
        is_domain = "." in word
        if upper == "AI":
            cleaned_words.append("AI")
        elif is_acronym or is_camel or is_domain:
            cleaned_words.append(word)
        elif len(word) > 3 and word == upper:
            cleaned_words.append(word[0] + word[1:].lower())
        else:
            cleaned_words.append(word)

    # Drop generic tail word
    if len(cleaned_words) > 1:
        tail = re.sub(r"[^\w]", "", cleaned_words[-1]).lower()
        if tail in GENERIC_TAILS:
            cleaned_words.pop()

    clean = " ".join(cleaned_words)
    clean = re.sub(r"[,\-\|\.\s]+$", "", clean).strip()
    return clean or "EMPTY"


def detect_company_column(df: "pd.DataFrame") -> str | None:
    for name in COMPANY_COLUMN_NAMES:
        if name in df.columns:
            return name
    lower_map = {c.lower(): c for c in df.columns}
    for candidate in ("company", "organization", "account name"):
        if candidate in lower_map:
            return lower_map[candidate]
    return None


EMAIL_COLUMN_NAMES = [
    "email", "Email", "work_email", "Work Email", "email_address",
    "Email Address", "primary_email", "Primary Email",
]


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


def detect_email_column(df: "pd.DataFrame") -> str:
    for name in EMAIL_COLUMN_NAMES:
        if name in df.columns:
            return name
    # Case-insensitive fallback
    lower_map = {c.lower(): c for c in df.columns}
    if "email" in lower_map:
        return lower_map["email"]
    raise ValueError(
        f"Could not find an email column. Columns found: {list(df.columns)}"
    )


def load_file(file_bytes: bytes, filename: str) -> "pd.DataFrame":
    if filename.lower().endswith(".xlsx"):
        return pd.read_excel(io.BytesIO(file_bytes))
    return pd.read_csv(io.BytesIO(file_bytes))


def df_to_csv_bytes(df: "pd.DataFrame") -> bytes:
    buf = io.StringIO()
    df.to_csv(buf, index=False)
    return buf.getvalue().encode("utf-8")


# ---------------------------------------------------------------------------
# Reoon
# ---------------------------------------------------------------------------
def submit_reoon_task(emails: list[str]) -> int:
    response = requests.post(
        REOON_CREATE_TASK_URL,
        json={"key": REOON_API_KEY, "name": "Slack cleaner bot", "emails": emails},
        timeout=60,
    )
    response.raise_for_status()
    body = response.json()
    if body.get("status") != "success" or not body.get("task_id"):
        raise RuntimeError(f"Reoon task creation failed: {body}")
    return int(body["task_id"])


def poll_reoon_task(task_id: int) -> dict[str, Any]:
    deadline = time.monotonic() + REOON_MAX_WAIT
    while True:
        response = requests.get(
            REOON_GET_TASK_URL,
            params={"key": REOON_API_KEY, "task_id": task_id},
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
        time.sleep(REOON_POLL_INTERVAL)


def filter_reoon(result: dict[str, Any], input_emails: list[str]) -> list[str]:
    raw = result.get("results") or {}
    by_email: dict[str, dict[str, Any]] = {}
    if isinstance(raw, dict):
        for email, item in raw.items():
            if isinstance(item, dict):
                by_email[normalize_email(email)] = item
    elif isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                e = normalize_email(item.get("email"))
                if e:
                    by_email[e] = item

    passed: list[str] = []
    for email in input_emails:
        item = by_email.get(email) or {}
        if item.get("is_safe_to_send") is True or item.get("is_catch_all") is True:
            passed.append(email)
    return passed


# ---------------------------------------------------------------------------
# BounceBan
# ---------------------------------------------------------------------------
def submit_bounceban_task(emails: list[str]) -> str:
    response = requests.post(
        BOUNCEBAN_BULK_VERIFY_URL,
        headers={"Authorization": BOUNCEBAN_API_KEY, "Content-Type": "application/json"},
        json={"emails": emails},
        timeout=60,
    )
    response.raise_for_status()
    body = response.json()
    task_id = body.get("id") or body.get("task_id") or body.get("taskId")
    if not task_id:
        raise RuntimeError(f"BounceBan task creation failed: {body}")
    return str(task_id)


def poll_bounceban_task(task_id: str) -> None:
    deadline = time.monotonic() + BOUNCEBAN_MAX_WAIT
    while True:
        response = requests.get(
            BOUNCEBAN_BULK_STATUS_URL,
            headers={"Authorization": BOUNCEBAN_API_KEY},
            params={"id": task_id},
            timeout=60,
        )
        response.raise_for_status()
        body = response.json()
        status = (body.get("status") or "").lower()
        if status in {"finished", "completed"}:
            return
        if status not in {"pending", "queued", "importing", "running", "processing", "verifying"}:
            raise RuntimeError(f"Unexpected BounceBan status: {body}")
        if time.monotonic() >= deadline:
            raise RuntimeError(f"Timed out waiting for BounceBan task {task_id}")
        time.sleep(BOUNCEBAN_POLL_INTERVAL)


def fetch_bounceban_dump(task_id: str) -> dict[str, Any]:
    response = requests.get(
        BOUNCEBAN_BULK_DUMP_URL,
        headers={"Authorization": BOUNCEBAN_API_KEY},
        params={"id": task_id, "retrieve_all": "1"},
        timeout=60,
    )
    response.raise_for_status()
    return response.json()


def filter_bounceban(dump: dict[str, Any], input_emails: list[str]) -> list[str]:
    items: list[dict[str, Any]] = []
    for key in ("items", "results", "data"):
        value = dump.get(key)
        if isinstance(value, list):
            items = [i for i in value if isinstance(i, dict)]
            break

    by_email: dict[str, dict[str, Any]] = {}
    for item in items:
        e = normalize_email(item.get("email"))
        if e:
            by_email[e] = item

    deliverable: list[str] = []
    for email in input_emails:
        item = by_email.get(email) or {}
        if (item.get("result") or "").lower() == "deliverable":
            deliverable.append(email)
    return deliverable


# ---------------------------------------------------------------------------
# Core pipeline
# ---------------------------------------------------------------------------
def run_pipeline(
    df: "pd.DataFrame",
    email_col: str,
    progress_cb: Any,
) -> tuple["pd.DataFrame", dict[str, int]]:
    """
    Verify emails in df through Reoon then BounceBan.
    progress_cb(msg) posts status updates to Slack.
    Returns (filtered_df, stats).
    """
    emails = dedupe_preserve_order(
        [normalize_email(str(e)) for e in df[email_col] if str(e).strip()]
    )
    total = len(emails)
    progress_cb(f"Found *{total}* unique emails. Submitting to Reoon...")

    # --- Reoon ---
    reoon_task_id = submit_reoon_task(emails)
    log.info("Reoon task %s submitted", reoon_task_id)
    progress_cb(f"Reoon task `{reoon_task_id}` running — polling every {REOON_POLL_INTERVAL}s...")

    reoon_result = poll_reoon_task(reoon_task_id)
    reoon_passed = dedupe_preserve_order(filter_reoon(reoon_result, emails))
    reoon_dropped = total - len(reoon_passed)
    progress_cb(
        f"Reoon done. *{len(reoon_passed)}* passed, *{reoon_dropped}* dropped. "
        f"Submitting to BounceBan..."
    )

    # --- BounceBan ---
    bb_task_id = submit_bounceban_task(reoon_passed)
    log.info("BounceBan task %s submitted", bb_task_id)
    progress_cb(f"BounceBan task `{bb_task_id}` running — polling every {BOUNCEBAN_POLL_INTERVAL}s...")

    poll_bounceban_task(bb_task_id)
    bb_dump = fetch_bounceban_dump(bb_task_id)
    deliverable = dedupe_preserve_order(filter_bounceban(bb_dump, reoon_passed))
    bb_dropped = len(reoon_passed) - len(deliverable)

    # Filter df to deliverable emails only
    deliverable_set = set(deliverable)
    mask = df[email_col].apply(lambda e: normalize_email(str(e)) in deliverable_set)
    cleaned_df = df[mask].copy()

    # Add cleaned company name column
    company_col = detect_company_column(cleaned_df)
    if company_col:
        cleaned_df.insert(
            cleaned_df.columns.get_loc(company_col) + 1,
            "new_company_name",
            cleaned_df[company_col].apply(clean_company_name),
        )

    stats = {
        "total": total,
        "reoon_passed": len(reoon_passed),
        "reoon_dropped": reoon_dropped,
        "bounceban_passed": len(deliverable),
        "bounceban_dropped": bb_dropped,
    }
    return cleaned_df, stats


# ---------------------------------------------------------------------------
# Slack bot
# ---------------------------------------------------------------------------
app = App(token=SLACK_BOT_TOKEN)


@app.event("file_shared")
def handle_file_shared(event: dict, client: Any, say: Any) -> None:
    channel_id = event.get("channel_id") or event.get("channel")
    if channel_id != SLACK_CHANNEL_ID:
        return

    file_id = event.get("file_id")
    if not file_id:
        return

    # Get file metadata
    file_info = client.files_info(file=file_id)["file"]
    filename: str = file_info.get("name", "")
    if not filename.lower().endswith((".csv", ".xlsx")):
        return  # Ignore non-data files

    thread_ts = file_info.get("shares", {}).get("public", {}).get(
        SLACK_CHANNEL_ID, [{}]
    )[0].get("ts") or event.get("event_ts")

    def progress(msg: str) -> None:
        client.chat_postMessage(
            channel=SLACK_CHANNEL_ID,
            thread_ts=thread_ts,
            text=msg,
        )

    progress(f":mag: Got `{filename}` — loading and verifying emails...")

    try:
        # Download file
        file_url = file_info.get("url_private_download") or file_info.get("url_private")
        resp = requests.get(
            file_url,
            headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
            timeout=60,
        )
        resp.raise_for_status()

        df = load_file(resp.content, filename)
        try:
            email_col = detect_email_column(df)
        except ValueError as exc:
            progress(f":x: {exc}")
            return

        cleaned_df, stats = run_pipeline(df, email_col, progress)

        # Upload cleaned CSV back to thread
        output_name = filename.replace(".xlsx", "").replace(".csv", "") + "_cleaned.csv"
        csv_bytes = df_to_csv_bytes(cleaned_df)

        client.files_upload_v2(
            channel=SLACK_CHANNEL_ID,
            thread_ts=thread_ts,
            content=csv_bytes,
            filename=output_name,
            initial_comment=(
                f":white_check_mark: *Done!*\n"
                f"• Started with *{stats['total']}* contacts\n"
                f"• Reoon: kept *{stats['reoon_passed']}*, dropped *{stats['reoon_dropped']}*\n"
                f"• BounceBan: kept *{stats['bounceban_passed']}*, dropped *{stats['bounceban_dropped']}*\n"
                f"• Final: *{stats['bounceban_passed']}* deliverable contacts"
            ),
        )

    except Exception as exc:
        log.exception("Pipeline failed")
        progress(f":x: Something went wrong: `{exc}`")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    log.info("Starting Slack Email Cleaner Bot (Socket Mode)...")
    handler = SocketModeHandler(app, SLACK_APP_TOKEN)
    handler.start()
