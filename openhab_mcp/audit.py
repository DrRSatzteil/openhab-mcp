"""Audit patterns — persistent assertions about the openHAB item model.

A pattern asserts: "all items matching filter F should be members of group G."
The audit computes two findings per pattern:
  - missing:    items matching F that are NOT in G
  - unexpected: members of G that do NOT match F

Patterns are stored as JSON at OPENHAB_AUDIT_PATTERNS_PATH
(default: /app/audit_patterns.json).
"""

import json
import os
from typing import Any, Dict, List, Optional

from .inventory import AdminInventory

_FILTER_KEYS = (
    "location", "equipment", "point", "item_property", "item_type",
    "group", "tag", "state", "category", "has_metadata_ns",
    "missing_metadata_ns", "has_semantic", "editable",
)


def _patterns_path() -> str:
    return os.environ.get("OPENHAB_AUDIT_PATTERNS_PATH", "/app/audit_patterns.json")


def _load(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _save(path: str, data: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def save_pattern(
    pattern_id: str,
    description: str,
    expect_in_group: str,
    filter_kwargs: Dict[str, Any],
) -> Dict[str, Any]:
    """Persist a pattern. Overwrites if id already exists."""
    path = _patterns_path()
    data = _load(path)
    data[pattern_id] = {
        "id": pattern_id,
        "description": description,
        "expect_in_group": expect_in_group,
        "filter": {k: v for k, v in filter_kwargs.items() if v is not None},
    }
    _save(path, data)
    return data[pattern_id]


def delete_pattern(pattern_id: str) -> bool:
    """Remove a pattern by id. Returns True if it existed."""
    path = _patterns_path()
    data = _load(path)
    if pattern_id not in data:
        return False
    del data[pattern_id]
    _save(path, data)
    return True


def list_patterns() -> List[Dict[str, Any]]:
    """Return all saved patterns."""
    return list(_load(_patterns_path()).values())


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

def run_audit(
    inventory: AdminInventory,
    pattern_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Test patterns against the current inventory state.

    Parameters
    ----------
    inventory:
        Must be populated (refresh_inventory called first).
    pattern_id:
        Run only this pattern. If None, run all.
    """
    if inventory.size == 0:
        return {"error": "Inventory empty — call refresh_inventory first.", "findings": []}

    all_patterns = _load(_patterns_path())
    if not all_patterns:
        return {"error": "No patterns saved yet. Use save_audit_pattern first.", "findings": []}

    if pattern_id:
        if pattern_id not in all_patterns:
            return {"error": f"Pattern '{pattern_id}' not found.", "findings": []}
        patterns_to_run = {pattern_id: all_patterns[pattern_id]}
    else:
        patterns_to_run = all_patterns

    findings = []

    for pid, pattern in patterns_to_run.items():
        group_name = pattern["expect_in_group"]
        flt = pattern.get("filter", {})

        # Items matching the filter
        expected: set = inventory.get(**flt)

        # Current members of the target group (direct only)
        actual: set = inventory.get_direct_members(group_name)

        missing = sorted(expected - actual)
        unexpected = sorted(actual - expected)

        findings.append({
            "pattern_id": pid,
            "description": pattern["description"],
            "expect_in_group": group_name,
            "filter": flt,
            "status": "ok" if not missing and not unexpected else "fail",
            "expected_count": len(expected),
            "actual_count": len(actual),
            "missing": missing,       # in filter but not in group
            "unexpected": unexpected, # in group but not matching filter
        })

    passed = sum(1 for f in findings if f["status"] == "ok")
    failed = len(findings) - passed

    return {
        "patterns_run": len(findings),
        "passed": passed,
        "failed": failed,
        "findings": findings,
    }
