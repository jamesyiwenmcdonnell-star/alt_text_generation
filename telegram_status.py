"""
telegram_status.py

Maintains a single, continuously-edited Telegram message showing the live
status of every job -- not per-editor DMs. Uses the Bot API directly via
requests (already a dependency), matching pod.py/gpu_utils.py's existing
style of talking to REST APIs directly rather than pulling in a dedicated
SDK (python-telegram-bot). Purely outbound: no polling, no getUpdates.
"""

import html
import logging
import os
import requests
from datetime import datetime, timedelta, timezone

import job_store

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
REQUEST_TIMEOUT = 15  # seconds

# Telegram display only -- job_store.py keeps storing/comparing everything in
# UTC (the "Z"-suffixed timestamps below are its format). Singapore Standard
# Time has no DST, so a fixed offset is correct year-round -- no timezone
# database dependency needed.
SGT = timezone(timedelta(hours=8))

# The current board message's id, persisted as a plain text file (same
# pattern as controller.py's pod_state.txt) so refresh() edits the same
# message throughout a run instead of posting a new one every time.
MESSAGE_ID_PATH = "./telegram_message_id.txt"

# Every message id this bot has sent this run (board + alerts), one per line,
# appended as they're sent. The Bot API gives no way for a bot to list a
# channel's full history -- this is the only way to know what to clean up --
# so startup_cleanup() can only delete what this bot itself tracked sending,
# not messages posted by humans or other bots.
SENT_MESSAGE_IDS_PATH = "./telegram_sent_message_ids.txt"

MAX_BOARD_LINES = 20

# How long a terminal-state job stays visible on the board after it finished
# (measured from completed_at) -- this only affects what's *displayed*, not
# the underlying job record (still queryable via the API) or, for COMPLETE
# jobs, the files (job_pipeline.py only ever deletes FAILED jobs' files).
FAILED_DISPLAY_WORKING_DAYS = 3   # Mon-Fri only -- weekends don't count
COMPLETE_DISPLAY_DAYS = 7         # calendar days

STATE_EMOJI = {
    "QUEUED": "🟡",
    "EXTRACTING": "🔵",
    "EXTRACTED": "🔵",
    "POD_STARTING": "🔵",
    "GENERATING": "🔵",
    "EMBEDDING": "🔵",
    "COMPLETE": "✅",
    "FAILED": "❌",
}


def _api_url(method: str) -> str:
    return f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"


def _call(method: str, payload: dict) -> dict:
    """POSTs to the Bot API and raises with Telegram's own error description
    (e.g. "chat not found") instead of an opaque HTTPError -- the API always
    returns 200 with {"ok": false, "description": ...} for request-level
    errors, so raise_for_status() alone never surfaces the actual reason."""
    r = requests.post(_api_url(method), json=payload, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    body = r.json()
    if not body.get("ok"):
        raise RuntimeError(f"Telegram API {method} failed: {body.get('description', body)}")
    return body


def _track_sent_message(message_id: int) -> None:
    with open(SENT_MESSAGE_IDS_PATH, "a") as f:
        f.write(f"{message_id}\n")


def _load_tracked_message_ids() -> list[int]:
    if not os.path.exists(SENT_MESSAGE_IDS_PATH):
        return []
    with open(SENT_MESSAGE_IDS_PATH) as f:
        return [int(line.strip()) for line in f if line.strip()]


def _send_message(text: str) -> int:
    body = _call("sendMessage", {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"})
    message_id = body["result"]["message_id"]
    _track_sent_message(message_id)
    return message_id


def _delete_message(message_id: int) -> None:
    """Best-effort -- deletion can fail (message already gone, too old for
    the bot to delete, bot lacks delete rights in the channel, ...); a
    failure here shouldn't block startup, just gets logged."""
    try:
        _call("deleteMessage", {"chat_id": TELEGRAM_CHAT_ID, "message_id": message_id})
    except RuntimeError as exc:
        logging.warning("telegram_status: could not delete message %s: %s", message_id, exc)


def startup_cleanup() -> None:
    """Deletes every message this bot has sent in previous runs (see
    SENT_MESSAGE_IDS_PATH's docstring for why it can't go further than that),
    so a restart starts the channel clean instead of accumulating old status
    boards and alerts. Call this once at worker startup, BEFORE
    ensure_message() -- not from refresh(), which would otherwise delete and
    repost the board on every single job update."""
    for old_id in _load_tracked_message_ids():
        _delete_message(old_id)
    if os.path.exists(SENT_MESSAGE_IDS_PATH):
        os.remove(SENT_MESSAGE_IDS_PATH)
    if os.path.exists(MESSAGE_ID_PATH):
        os.remove(MESSAGE_ID_PATH)


def _edit_message(message_id: int, text: str) -> None:
    try:
        _call("editMessageText", {"chat_id": TELEGRAM_CHAT_ID, "message_id": message_id,
                                   "text": text, "parse_mode": "HTML"})
    except RuntimeError as exc:
        if "message is not modified" in str(exc):
            return  # identical content -- not an error, nothing to do
        raise


def _load_message_id(path: str = MESSAGE_ID_PATH) -> int | None:
    if not os.path.exists(path):
        return None
    content = open(path).read().strip()
    return int(content) if content else None


def _save_message_id(message_id: int, path: str = MESSAGE_ID_PATH) -> None:
    with open(path, "w") as f:
        f.write(f"{message_id}\n")


def ensure_message() -> int:
    """Returns the persistent board message's id, creating it on first-ever
    use. Reused across restarts via MESSAGE_ID_PATH, so a restart edits the
    same message instead of posting a new one."""
    saved = _load_message_id()
    if saved is not None:
        return saved
    message_id = _send_message("📋 Alt-Text Pipeline — starting up...")
    _save_message_id(message_id)
    return message_id


def _parse_utc(iso_utc: str) -> datetime:
    """Parses a job_store-style UTC timestamp ("2026-08-24T15:18:04Z")."""
    return datetime.strptime(iso_utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _sgt_hhmm(iso_utc: str | None) -> str:
    """Converts a job_store-style UTC timestamp to "HH:MM" in Singapore
    time. Returns "?" if there's no timestamp at all."""
    if not iso_utc:
        return "?"
    return _parse_utc(iso_utc).astimezone(SGT).strftime("%H:%M")


def _business_days_ago(n: int, from_dt: datetime) -> datetime:
    """from_dt minus n working days (Mon-Fri) -- weekends don't count toward n."""
    d = from_dt
    remaining = n
    while remaining > 0:
        d -= timedelta(days=1)
        if d.weekday() < 5:  # Mon=0 .. Fri=4
            remaining -= 1
    return d


def _expired_from_board(job: job_store.Job, now: datetime) -> bool:
    """Whether a terminal-state job's display window on the board has
    elapsed. In-progress jobs (QUEUED/EXTRACTING/...) are never expired."""
    if not job.completed_at:
        return False
    completed = _parse_utc(job.completed_at)
    if job.state == "FAILED":
        return completed < _business_days_ago(FAILED_DISPLAY_WORKING_DAYS, now)
    if job.state == "COMPLETE":
        return completed < now - timedelta(days=COMPLETE_DISPLAY_DAYS)
    return False


def _format_job_line(job: job_store.Job) -> str:
    emoji = STATE_EMOJI.get(job.state, "⚪")
    ts_label, ts_value = {
        "QUEUED": ("queued", job.created_at),
        "COMPLETE": ("done", job.completed_at),
        "FAILED": ("failed", job.completed_at),
    }.get(job.state, ("started", job.started_at or job.created_at))
    when = _sgt_hhmm(ts_value)

    # A COMPLETE job only carries an error_message when a non-fatal step
    # failed after the alt text was already produced (embedding) -- worth
    # showing, but flagged as a caveat rather than reading like a failure.
    detail = ""
    if job.error_message:
        marker = "" if job.state == "FAILED" else "⚠ "
        detail = f"  {marker}{job.error_message[:60]}"

    return (
        f"{emoji} {job.state:<12} {job.pdf_filename[:24]:<24} "
        f"editor: {job.editor_id:<10} {ts_label} {when}{detail}"
    )


def _format_board() -> str:
    now = datetime.now(timezone.utc)
    jobs = job_store.list_jobs(limit=50)
    jobs = [j for j in jobs if not _expired_from_board(j, now)]
    jobs.sort(key=lambda j: j.updated_at, reverse=True)  # ISO8601 sorts lexicographically = chronologically
    jobs.sort(key=lambda j: j.state == "FAILED")  # stable: pushes FAILED to the bottom without
                                                   # disturbing the recency order within each group
    jobs = jobs[:MAX_BOARD_LINES]

    updated_label = datetime.now(SGT).strftime("%Y-%m-%d %H:%M")
    header = f"📋 Alt-Text Pipeline — updated {updated_label} SGT"
    if not jobs:
        return f"{header}\n\n(no jobs yet)"

    # Escaped because these lines carry free-form text -- a PDF filename or an
    # error_message containing "<", ">" or "&" would otherwise be parsed as
    # (broken) markup and Telegram would reject the entire board with
    # "can't parse entities", not just mangle the one line.
    lines = "\n".join(f"<code>{html.escape(_format_job_line(j))}</code>" for j in jobs)
    return f"{header}\n\n{lines}"


def refresh() -> None:
    message_id = ensure_message()
    _edit_message(message_id, _format_board())


def alert_orphan_pod(pod_id: str, job_id: str | None) -> None:
    """A pod failed to terminate -- this is a billing risk that must not
    silently blend into a routine board refresh, so it's a separate message,
    not an edit of the status board."""
    job_ref = f" (job {job_id})" if job_id else ""
    _send_message(
        f"⚠️ Pod {pod_id} failed to terminate{job_ref} -- check the RunPod "
        f"console manually, it may still be billing."
    )
