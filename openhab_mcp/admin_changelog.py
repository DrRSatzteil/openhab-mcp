"""Admin changelog — append-only audit trail for mutating admin tools.

Every call to a tool decorated with @audit_log gets one JSON-lines entry:
timestamp, tool name, arguments, and outcome (success/error). Read-only tools
(get_*, list_*, query_*, diagnose_item, analyze_model_health, ...) are not
decorated — logging them would only add noise, not audit value.

This is intentionally just a flat, append-only log of "what changed and when" —
not a snapshot/diff of before/after state. Correlating it against openHAB's
own error log (e.g. "did error X start right after change Y") is a separate,
heuristic problem left to a future tool; this module only guarantees the
changes themselves are never silently lost.

The file path defaults to admin_changelog.jsonl in the working directory and
can be overridden via the ADMIN_CHANGELOG_PATH environment variable.
"""

import functools
import inspect
import json
import os
from datetime import datetime, timezone
from threading import RLock
from typing import Any, Callable, Dict, List, Optional

_PATH = os.environ.get("ADMIN_CHANGELOG_PATH", "admin_changelog.jsonl")
_lock = RLock()


def _safe_value(value: Any) -> Any:
    """Best-effort JSON-safe rendering of a single argument value."""
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)


def _append(entry: Dict[str, Any]) -> None:
    with _lock:
        with open(_PATH, "a") as f:
            f.write(json.dumps(entry) + "\n")


def audit_log(func: Callable) -> Callable:
    """Decorator: append a changelog entry after each call to a mutating tool.

    Logs both successful calls and exceptions (with the error message), then
    re-raises so normal error handling is unaffected.
    """
    sig = inspect.signature(func)

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        bound = sig.bind_partial(*args, **kwargs)
        bound.apply_defaults()
        params = {k: _safe_value(v) for k, v in bound.arguments.items()}

        entry: Dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "tool": func.__name__,
            "params": params,
        }
        try:
            result = func(*args, **kwargs)
            entry["outcome"] = "success"
            _append(entry)
            return result
        except Exception as exc:
            entry["outcome"] = "error"
            entry["error"] = str(exc)
            _append(entry)
            raise

    return wrapper


def read_entries(
    since: Optional[str] = None,
    until: Optional[str] = None,
    tool: Optional[str] = None,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """Read changelog entries, most recent first, with optional filters.

    since/until are ISO-8601 timestamps (compared as strings, which works
    because all entries use the same zero-padded ISO format).
    """
    try:
        with open(_PATH) as f:
            lines = f.readlines()
    except FileNotFoundError:
        return []

    entries = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    if since:
        entries = [e for e in entries if e.get("timestamp", "") >= since]
    if until:
        entries = [e for e in entries if e.get("timestamp", "") <= until]
    if tool:
        entries = [e for e in entries if e.get("tool") == tool]

    entries.reverse()
    return entries[:limit]
