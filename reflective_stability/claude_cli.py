"""Single-turn, tool-free calls to Claude through `claude -p`.

Flags strip out everything Claude Code would normally add that we can control: its own system
prompt, tools, MCP servers, settings, CLAUDE.md files, skills and plugins. Two things cannot be
removed while authenticating with a subscription: a short environment block (working directory,
platform, date, model name) and an account-context reminder. See README "Limitations".

Every call ends in one of four ways:
  answer     - a genuine model reply (no error flag, only the requested model used, tokens produced)
  limit      - a usage or rate limit: every worker pauses until the reset time in the message
               (or 15 minutes if none can be read), then the call is retried, with no cap
  transient  - anything else that may succeed later: retried with backoff, then the run stops
  fatal      - cannot succeed on retry (unknown model, not logged in, prompt too long): stop now
`claude -p` itself already retries 429/529 API errors several times before reporting them.
"""

import json
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

from .runtime import STOP, Stopped, duration, run_child, say

CALL_TIMEOUT_SECONDS = 30 * 60
BACKOFF_SECONDS = [30, 60, 120, 300, 600, 900, 900]  # transient failures; 8 attempts, ~48 minutes
LIMIT_FALLBACK_SECONDS = 15 * 60
LIMIT_MARGIN_SECONDS = 120

_LIMIT = re.compile(r"hit your .*limit|usage limit|limit reached|rate.?limit|too many requests", re.IGNORECASE)
_FATAL = re.compile(
    r"prompt is too long|not logged in|please run /login|invalid api key|authentication|"
    r"issue with the selected model|unrecognized model|does not exist",
    re.IGNORECASE,
)
_RESET = re.compile(
    r"resets\s+(?:at\s+)?(?:(mon|tue|wed|thu|fri|sat|sun)[a-z]*\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", re.IGNORECASE
)


class TransportError(RuntimeError):
    """The CLI kept failing for reasons unrelated to the model's answer."""


class FatalError(RuntimeError):
    """The CLI reported an error that retrying cannot fix."""


@dataclass(frozen=True)
class Reply:
    text: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    duration_ms: int


def cli_version() -> str:
    return subprocess.run(["claude", "--version"], capture_output=True, text=True, check=True).stdout.strip()


def ask(model: str, system_prompt: str, user_message: str, effort: str) -> Reply:
    """Send one message and return the model's reply, waiting out usage limits and retrying glitches."""
    command = [
        "claude", "-p",
        "--model", model,
        "--effort", effort,
        "--system-prompt", system_prompt,
        "--tools", "",
        "--safe-mode",
        "--setting-sources", "",
        "--strict-mcp-config",
        "--no-session-persistence",
        "--output-format", "json",
    ]  # fmt: skip
    transient_failures = 0
    while True:
        _wait_while_paused()
        try:
            returncode, stdout, stderr = run_child(command, user_message, CALL_TIMEOUT_SECONDS)
            outcome, detail = classify(model, returncode, stdout, stderr)
        except subprocess.TimeoutExpired:
            outcome, detail = "transient", f"no reply within {CALL_TIMEOUT_SECONDS // 60} minutes"
        if outcome == "answer":
            return detail
        if STOP.is_set():
            raise Stopped
        if outcome == "fatal":
            raise FatalError(f"{model}: {detail}")
        if outcome == "limit":
            _pause_for_limit(model, detail)
            continue
        if transient_failures == len(BACKOFF_SECONDS):
            raise TransportError(f"{model} failed {transient_failures + 1} times in a row; last error: {detail}")
        wait = BACKOFF_SECONDS[transient_failures]
        transient_failures += 1
        say(f"  {model} call failed ({detail[:200]}); retry {transient_failures} in {duration(wait)}")
        if STOP.wait(wait):
            raise Stopped


def classify(model: str, returncode: int, stdout: str, stderr: str) -> tuple[str, object]:
    """Return ("answer", Reply) or (kind, error text) with kind "limit", "transient" or "fatal"."""
    try:
        result = json.loads(stdout)
        text = result.get("result") or ""
        usage = result.get("usage") or {}
        used = list(result.get("modelUsage") or {})
        output_tokens = usage.get("output_tokens", 0)
        if not result.get("is_error") and used == [model] and output_tokens > 0 and isinstance(text, str) and text:
            return "answer", Reply(
                text=text,
                model=model,
                input_tokens=sum(
                    usage.get(k, 0) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
                ),
                output_tokens=output_tokens,
                cost_usd=result.get("total_cost_usd") or 0.0,
                duration_ms=result.get("duration_ms") or 0,
            )
        status = result.get("api_error_status")
        detail = f"{text or result.get('subtype')} (HTTP {status})" if status else str(text or result.get("subtype"))
    except (json.JSONDecodeError, AttributeError, TypeError):
        status, detail = None, (stderr.strip() or stdout.strip() or f"exit status {returncode}")[:500]
    if status == 429 or _LIMIT.search(detail):
        return "limit", detail
    if status in (400, 401, 403, 404) or _FATAL.search(detail):
        return "fatal", detail
    return "transient", detail


_pause_lock = threading.Lock()
_resume_at = 0.0


def _wait_while_paused() -> None:
    while True:
        with _pause_lock:
            remaining = _resume_at - time.time()
        if remaining <= 0:
            return
        if STOP.wait(min(remaining, 60)):
            raise Stopped


def _pause_for_limit(model: str, message: str) -> None:
    global _resume_at
    resume = reset_time(message, datetime.now())
    resume_at = resume.timestamp() + LIMIT_MARGIN_SECONDS if resume else time.time() + LIMIT_FALLBACK_SECONDS
    with _pause_lock:
        if resume_at <= _resume_at:
            return  # another worker already paused us at least this long
        _resume_at = resume_at
    when = datetime.fromtimestamp(resume_at)
    say(
        f"USAGE LIMIT ({model}): {message[:200]}\n"
        f"          Pausing all workers until {when:%a %H:%M} (in {duration(resume_at - time.time())}). "
        "Nothing is lost; the run continues by itself. Ctrl-C to stop and resume later."
    )


def reset_time(message: str, now: datetime) -> datetime | None:
    """Parse "resets 3:45pm" or "resets Mon 12:00am" (local time) into the next such moment."""
    found = _RESET.search(message)
    if not found:
        return None
    day, hour, minute, meridiem = found.groups()
    hour, minute = int(hour), int(minute or 0)
    if meridiem:
        hour = hour % 12 + (12 if meridiem.lower() == "pm" else 0)
    if hour > 23 or minute > 59:
        return None
    moment = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if day:
        days_ahead = (["mon", "tue", "wed", "thu", "fri", "sat", "sun"].index(day.lower()[:3]) - now.weekday()) % 7
        moment += timedelta(days=days_ahead)
    while moment <= now:
        moment += timedelta(days=7 if day else 1)
    return moment
