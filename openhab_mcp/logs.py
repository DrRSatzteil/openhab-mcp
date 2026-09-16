"""openHAB log file reader with timestamp and text filtering.

Requires OPENHAB_LOG_PATH env var pointing to the openHAB log directory,
accessible inside the container (e.g. via a volume mount).

Log line format:
  2024-01-15 10:23:45.123 [INFO ] [org.openhab.binding.zwave] - Message

openHAB writes these timestamps in its own host's local civil time (whatever
timezone the openHAB JVM runs in), with no offset marker on the line. This
module's "now" reference for relative windows ('1h', '30m') must be computed
in that SAME timezone, not this container's own — otherwise, if the two
differ (e.g. this container defaults to UTC while openHAB runs in
Europe/Berlin), every relative/absolute window silently shifts by the zone
offset with no error. Set OPENHAB_LOG_TZ to the IANA name of openHAB's
timezone (e.g. "Europe/Berlin") to fix this; it defaults to UTC.
"""

import os
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

LOG_LINE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+)\s+\[(\w+)\s*\]"
)

_RELATIVE_RE = re.compile(r"^(\d+)(h|m|d)$")


def _log_tz() -> ZoneInfo:
    return ZoneInfo(os.environ.get("OPENHAB_LOG_TZ", "UTC"))


def _now_in_log_tz() -> datetime:
    """Current time as a naive datetime in openHAB's log timezone, so it
    compares correctly against the naive (unmarked) timestamps in the log
    lines themselves."""
    return datetime.now(_log_tz()).replace(tzinfo=None)


def _parse_time(value: str) -> datetime:
    """Parse ISO datetime or relative string ('1h', '30m', '2d') to datetime.

    Both branches return a naive datetime in openHAB's log timezone
    (OPENHAB_LOG_TZ). An absolute ISO value is assumed to already be given
    in that timezone — pass it without a UTC 'Z'/offset suffix.
    """
    m = _RELATIVE_RE.match(value.strip())
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = {"h": timedelta(hours=n), "m": timedelta(minutes=n), "d": timedelta(days=n)}[unit]
        return _now_in_log_tz() - delta
    return datetime.fromisoformat(value.strip())


def _parse_line_ts(line: str) -> Optional[datetime]:
    m = LOG_LINE_RE.match(line)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S.%f")
    except ValueError:
        return None


def _parse_line_level(line: str) -> Optional[str]:
    m = LOG_LINE_RE.match(line)
    return m.group(2).strip() if m else None


def read_log(
    log_path: str,
    log_type: str = "openhab",
    since: Optional[str] = None,
    until: Optional[str] = None,
    query: Optional[str] = None,
    level: Optional[str] = None,
    max_lines: int = 200,
) -> Dict[str, Any]:
    """Read and filter openHAB log entries.

    Parameters
    ----------
    log_path : str
        Directory containing openhab.log / events.log.
    log_type : str
        "openhab" (default) or "events".
    since : str, optional
        Start time — ISO datetime or relative ("1h", "30m", "2d").
    until : str, optional
        End time — ISO datetime or relative. Defaults to now.
    query : str, optional
        Case-insensitive substring filter applied to the full line.
    level : str, optional
        Filter by log level: ERROR, WARN, INFO, DEBUG, TRACE.
    max_lines : int
        Maximum lines to return (default 200, max 1000).
    """
    filename = "events.log" if log_type == "events" else "openhab.log"
    filepath = os.path.join(log_path, filename)

    if not os.path.exists(filepath):
        return {
            "error": f"Log file not found: {filepath}",
            "hint": "Check OPENHAB_LOG_PATH and ensure the log directory is mounted.",
        }

    since_dt = _parse_time(since) if since else None
    until_dt = _parse_time(until) if until else None
    query_lower = query.lower() if query else None
    level_upper = level.upper() if level else None
    max_lines = min(max_lines, 1000)

    # Read file in reverse to find max_lines matching entries efficiently
    matched: List[str] = []
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError as exc:
        return {"error": f"Cannot read {filepath}: {exc}"}

    # Scan forward — collect all matching lines (timestamp filter is range-based)
    in_window: bool = not (since_dt or until_dt)  # True when no time filter
    current_level: Optional[str] = None
    for line in lines:
        line = line.rstrip("\n")
        if not line:
            continue

        ts = _parse_line_ts(line)
        if ts is not None:
            # Timestamped line: update window state and level
            current_level = _parse_line_level(line)
            if since_dt and ts < since_dt:
                in_window = False
                continue
            if until_dt and ts > until_dt:
                in_window = False
                continue
            in_window = True
        else:
            # Continuation line (stack trace etc.): inherit parent's window state
            if not in_window:
                continue

        if not in_window:
            continue

        if level_upper:
            lv = current_level if ts is None else _parse_line_level(line)
            if lv and lv != level_upper:
                continue

        if query_lower and query_lower not in line.lower():
            continue

        matched.append(line)

    # Return last max_lines entries so recent entries are at the end
    result_lines = matched[-max_lines:]
    return {
        "log_type": log_type,
        "file": filepath,
        "total_matched": len(matched),
        "returned": len(result_lines),
        "filters": {
            "since": since,
            "until": until,
            "query": query,
            "level": level,
        },
        "lines": result_lines,
    }
