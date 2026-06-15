"""Rename an openHAB item and update all its references.

rename_item() is a three-phase operation:
  1. diagnose_item() — collect every reference (rules, links, UI)
  2. build a plan — describe what will change without touching openHAB
  3. execute — create new item, migrate links/rules/UI, delete old item

Always call with dry_run=True first to review the plan, then confirm.
Script bodies are patched automatically unless the rule UID is in
skip_script_rule_uids; skipped rules are flagged for manual follow-up
via update_rule_script_action.
"""

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Set

from .diagnose import diagnose_item
from .openhab_client import OpenHABClient


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _replace_exact(obj: Any, old: str, new: str) -> Any:
    """Recursively replace string values that exactly equal old with new.

    Leaves substrings untouched — only whole-value matches are renamed.
    """
    if isinstance(obj, dict):
        return {k: _replace_exact(v, old, new) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_replace_exact(item, old, new) for item in obj]
    if isinstance(obj, str) and obj == old:
        return new
    return obj


def _patch_script(text: str, old: str, new: str) -> str:
    """Replace word-boundary occurrences of old in script source with new."""
    return re.sub(r"\b" + re.escape(old) + r"\b", new, text)


def _build_updated_rule(
    rule: Dict[str, Any], old_name: str, new_name: str, skip: Set[str]
) -> Dict[str, Any]:
    """Return a copy of rule with old_name → new_name applied throughout."""
    uid = rule.get("uid", "")
    updated = dict(rule)
    updated["triggers"] = _replace_exact(rule.get("triggers", []), old_name, new_name)
    updated["conditions"] = _replace_exact(rule.get("conditions", []), old_name, new_name)

    new_actions = []
    for action in rule.get("actions", []):
        if action.get("type") == "script.ScriptAction":
            action = dict(action)
            if uid not in skip:
                cfg = dict(action.get("configuration", {}))
                cfg["script"] = _patch_script(cfg.get("script", ""), old_name, new_name)
                action["configuration"] = cfg
            # else: leave script body untouched
        else:
            action = _replace_exact(action, old_name, new_name)
        new_actions.append(action)

    updated["actions"] = new_actions
    return updated


def _build_item_payload(raw: Dict[str, Any], new_name: str) -> Dict[str, Any]:
    """Build a PUT-ready item payload from a raw item, with a new name."""
    payload: Dict[str, Any] = {
        "type": raw["type"],
        "name": new_name,
        "label": raw.get("label", ""),
        "category": raw.get("category", ""),
        "tags": raw.get("tags", []),
        "groupNames": raw.get("groupNames", []),
    }
    for opt in ("groupType", "function", "unitSymbol"):
        if raw.get(opt):
            payload[opt] = raw[opt]
    return payload


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def rename_item(
    old_name: str,
    new_name: str,
    client: OpenHABClient,
    dry_run: bool = True,
    skip_script_rule_uids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Rename an item and update every reference across rules and UI.

    Parameters
    ----------
    old_name:
        Current item name (must exist).
    new_name:
        Target item name (must not exist yet).
    client:
        OpenHABClient instance.
    dry_run:
        If True (default), return the plan without making any changes.
    skip_script_rule_uids:
        Rule UIDs whose script bodies should NOT be auto-patched.
        These are added to manual_review_required in the result.

    Returns
    -------
    dict with keys:
        plan        — what will be / was changed
        executed    — False (dry_run) or True
        errors      — list of step errors (non-fatal, collected)
        manual_review_required — rules needing manual script follow-up
    """
    skip: Set[str] = set(skip_script_rule_uids or [])

    # --- 1. Gather data (parallel where possible) --------------------------
    diagnosis = diagnose_item(old_name, client)   # also validates old exists

    try:
        client.get_item_raw(new_name)
        raise ValueError(f"Item '{new_name}' already exists — choose a different name")
    except ValueError as exc:
        if "already exists" in str(exc):
            raise

    old_raw = client.get_item_raw(old_name)
    all_rules_raw = client.get_all_rules_raw()
    rule_by_uid = {r["uid"]: r for r in all_rules_raw}

    referencing_rules = diagnosis["referenced_in_rules"]
    referencing_ui = diagnosis["referenced_in_ui"]
    channel_links = diagnosis["channel_links"]

    # --- 2. Build plan -----------------------------------------------------
    rule_steps = []
    manual_review = []

    for rule_info in referencing_rules:
        uid = rule_info["uid"]
        has_script_hit = bool(rule_info.get("script_excerpts"))
        skipped = uid in skip

        rule_steps.append(
            {
                "uid": uid,
                "name": rule_info["name"],
                "reference_in": rule_info["reference_in"],
                "script_body_patched": has_script_hit and not skipped,
                "script_body_skipped": has_script_hit and skipped,
            }
        )

        if has_script_hit and skipped:
            manual_review.append(
                {
                    "rule_uid": uid,
                    "rule_name": rule_info["name"],
                    "action": "update script body manually via update_rule_script_action",
                    "excerpts": rule_info["script_excerpts"],
                }
            )

    plan = {
        "old_name": old_name,
        "new_name": new_name,
        "create_item": {
            "type": old_raw.get("type"),
            "label": old_raw.get("label"),
            "groups": old_raw.get("groupNames", []),
            "tags": old_raw.get("tags", []),
            "metadata_namespaces": [
                ns for ns in old_raw.get("metadata", {}) if ns != "semantics"
            ],
        },
        "copy_links": channel_links,
        "update_rules": rule_steps,
        "update_ui": [{"uid": c["uid"], "namespace": c["namespace"]} for c in referencing_ui],
        "delete_old_item": old_name,
    }

    if dry_run:
        return {
            "plan": plan,
            "executed": False,
            "errors": [],
            "manual_review_required": manual_review,
        }

    # --- 3. Execute --------------------------------------------------------
    errors: List[str] = []
    completed: List[str] = []

    # Step A: create new item
    try:
        payload = _build_item_payload(old_raw, new_name)
        resp = client.session.put(f"{client.base_url}/rest/items/{new_name}", json=payload)
        resp.raise_for_status()

        # Copy metadata (skip semantics — carried by tags/groups already)
        for ns, meta in old_raw.get("metadata", {}).items():
            if ns == "semantics":
                continue
            client.session.put(
                f"{client.base_url}/rest/items/{new_name}/metadata/{ns}",
                json={"value": meta.get("value", ""), "config": meta.get("config", {})},
            ).raise_for_status()

        completed.append(f"created item '{new_name}'")
    except Exception as exc:
        errors.append(f"create_item failed: {exc}")
        return {
            "plan": plan,
            "executed": False,
            "errors": errors,
            "manual_review_required": manual_review,
        }

    # Step B: copy channel links
    for link in channel_links:
        channel_uid = link["channel_uid"]
        if not channel_uid:
            continue
        try:
            old_link = client.session.get(
                f"{client.base_url}/rest/links/{old_name}/{channel_uid.replace('#', '%23')}"
            ).json()
            client.session.put(
                f"{client.base_url}/rest/links/{new_name}/{channel_uid.replace('#', '%23')}",
                json={
                    "itemName": new_name,
                    "channelUID": channel_uid,
                    "configuration": old_link.get("configuration", {}),
                },
            ).raise_for_status()
            completed.append(f"linked '{new_name}' → '{channel_uid}'")
        except Exception as exc:
            errors.append(f"copy_link {channel_uid} failed: {exc}")

    # Step C: update rules
    for rule_info in referencing_rules:
        uid = rule_info["uid"]
        try:
            full_rule = rule_by_uid.get(uid) or client.get_rule(uid)
            updated = _build_updated_rule(full_rule, old_name, new_name, skip)
            client.put_rule_raw(uid, updated)
            completed.append(f"updated rule '{rule_info['name']}'")
        except Exception as exc:
            errors.append(f"update_rule '{uid}' failed: {exc}")

    # Step D: update UI components
    for ui_ref in referencing_ui:
        ns = ui_ref.get("namespace", "ui:pages")
        uid = ui_ref["uid"]
        try:
            components = client.get_ui_components(ns)
            component = next((c for c in components if c.get("uid") == uid), None)
            if component:
                patched_json = json.loads(
                    json.dumps(component).replace(f'"{old_name}"', f'"{new_name}"')
                )
                client.put_ui_component(ns, uid, patched_json)
                completed.append(f"updated UI component '{uid}'")
        except Exception as exc:
            errors.append(f"update_ui '{uid}' failed: {exc}")

    # Step E: delete old item (also removes its links)
    try:
        client.delete_item(old_name)
        completed.append(f"deleted item '{old_name}'")
    except Exception as exc:
        errors.append(f"delete_item '{old_name}' failed: {exc}")

    return {
        "plan": plan,
        "executed": True,
        "steps_completed": completed,
        "errors": errors,
        "manual_review_required": manual_review,
    }
