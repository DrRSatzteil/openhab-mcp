"""Health analysis suppression list.

Persists known false positives as (analysis, item, characteristic) triples
so they are silently skipped on every subsequent run.

The file path defaults to health_suppressions.json in the working directory
and can be overridden via the HEALTH_SUPPRESSIONS_PATH environment variable.
"""

import json
import os
from threading import RLock
from typing import Any, Dict, List

_PATH = os.environ.get("HEALTH_SUPPRESSIONS_PATH", "health_suppressions.json")
_lock = RLock()
_entries: List[Dict[str, Any]] = []


def load() -> None:
    """Load suppressions from disk. Called once at server startup."""
    global _entries
    with _lock:
        try:
            with open(_PATH) as f:
                _entries = json.load(f)
        except FileNotFoundError:
            _entries = []


def _save() -> None:
    with open(_PATH, "w") as f:
        json.dump(_entries, f, indent=2)


def is_suppressed(analysis: str, item: str, characteristic: str) -> bool:
    with _lock:
        return any(
            e["analysis"] == analysis
            and e["item"] == item
            and e["characteristic"] == characteristic
            for e in _entries
        )


def add(analysis: str, item: str, characteristic: str, reason: str = "") -> Dict[str, Any]:
    """Add a suppression. No-op if the triple already exists. Returns the entry."""
    with _lock:
        for e in _entries:
            if e["analysis"] == analysis and e["item"] == item and e["characteristic"] == characteristic:
                return e
        entry: Dict[str, Any] = {"analysis": analysis, "item": item, "characteristic": characteristic}
        if reason:
            entry["reason"] = reason
        _entries.append(entry)
        _save()
        return entry


def remove(analysis: str, item: str, characteristic: str) -> bool:
    """Remove a suppression. Returns True if something was removed."""
    with _lock:
        before = len(_entries)
        _entries[:] = [
            e for e in _entries
            if not (e["analysis"] == analysis and e["item"] == item and e["characteristic"] == characteristic)
        ]
        if len(_entries) < before:
            _save()
            return True
        return False


def list_all() -> List[Dict[str, Any]]:
    with _lock:
        return list(_entries)
