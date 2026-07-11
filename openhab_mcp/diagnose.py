"""Impact analysis for openHAB items.

diagnose_item() answers: "What breaks or needs updating if I modify/rename/delete this item?"
It mirrors the openHAB Developer Sidebar's cross-entity search but returns structured data
suited for LLM consumption and as prerequisite for the rename_item workflow.
"""

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List

from .openhab_client import OpenHABClient


def _search_json(obj: Any, needle: str) -> bool:
    """True if needle appears as a standalone word in the JSON serialization of obj."""
    return f'"{needle}"' in json.dumps(obj)


def _rule_reference_types(rule: dict, item_name: str) -> List[str]:
    """Return which parts of a rule reference item_name."""
    refs = []
    if any(_search_json(t, item_name) for t in rule.get("triggers", [])):
        refs.append("trigger")
    if any(_search_json(a, item_name) for a in rule.get("actions", [])):
        refs.append("action")
    if any(_search_json(c, item_name) for c in rule.get("conditions", [])):
        refs.append("condition")
    return refs


_GROUP_TRIGGER_TYPES = {
    "core.GroupStateChangeTrigger",
    "core.GroupStateUpdateTrigger",
    "core.GroupItemStateChangeTrigger",
    "core.GroupItemStateUpdateTrigger",
    "core.GroupCommandTrigger",
}


def _group_trigger_matches(rule: dict, item_groups: List[str]) -> List[str]:
    """Return group names that are both a group trigger in the rule AND a
    membership of the diagnosed item.

    All four openHAB group trigger types (State/ItemState × Change/Update) fire
    when any member item changes, so all are relevant for impact analysis. Text-
    based scanning misses these entirely when the script uses dynamic name
    construction — this structural check catches them all.
    """
    if not item_groups:
        return []
    matches = []
    for trigger in rule.get("triggers", []):
        if trigger.get("type") in _GROUP_TRIGGER_TYPES:
            group_name = trigger.get("configuration", {}).get("groupName")
            if group_name and group_name in item_groups:
                matches.append(group_name)
    return matches


def _script_excerpts(rule: dict, item_name: str) -> List[Dict[str, Any]]:
    """Extract ±5-line context windows where item_name appears in script action bodies."""
    excerpts = []
    for action in rule.get("actions", []):
        if action.get("type") != "script.ScriptAction":
            continue
        script = action.get("configuration", {}).get("script", "")
        if not script or item_name not in script:
            continue
        lines = script.splitlines()
        hit_indices = [i for i, line in enumerate(lines) if item_name in line]
        # Merge overlapping ±5 windows
        windows: List[tuple] = []
        for idx in hit_indices:
            start, end = max(0, idx - 5), min(len(lines) - 1, idx + 5)
            if windows and start <= windows[-1][1] + 1:
                windows[-1] = (windows[-1][0], end)
            else:
                windows.append((start, end))
        for start, end in windows:
            excerpts.append(
                {
                    "action_id": action.get("id"),
                    "script_type": action.get("configuration", {}).get("type", "unknown"),
                    "start_line": start + 1,
                    "excerpt": "\n".join(lines[start : end + 1]),
                }
            )
    return excerpts


def diagnose_item(item_name: str, client: OpenHABClient) -> Dict[str, Any]:
    """
    Collect everything that references item_name across items, rules, and UI.

    Returns a structured impact report. Use dry_run=True on rename/delete tools
    to show this to the user before making changes.
    """
    # Parallel fetch — mirrors the Developer Sidebar's network calls
    with ThreadPoolExecutor(max_workers=5) as pool:
        f_item = pool.submit(client.get_item, item_name)
        f_links = pool.submit(client.get_item_links_raw, item_name)
        f_rules = pool.submit(client.get_all_rules_raw)
        f_pages = pool.submit(client.get_ui_components, "ui:page")
        f_widgets = pool.submit(client.get_ui_components, "ui:widget")
        f_sitemaps = pool.submit(client.get_sitemaps)

        item = f_item.result()
        links = f_links.result()
        all_rules = f_rules.result()
        pages = f_pages.result()
        widgets = f_widgets.result()
        sitemap_summaries = f_sitemaps.result()

    # Classic sitemaps (BasicUI) are a separate subsystem from ui:page/ui:widget —
    # fetching the full sitemap (not the per-page endpoint) returns the whole
    # widget tree already expanded (linked pages included), so one request per
    # sitemap is enough to search it completely.
    referencing_sitemaps = []
    if sitemap_summaries:
        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = {
                pool.submit(client.get_sitemap, sm.get("name")): sm for sm in sitemap_summaries
            }
            for future in as_completed(futures):
                sm = futures[future]
                full_sitemap = future.result()
                if full_sitemap and _search_json(full_sitemap, item_name):
                    referencing_sitemaps.append(
                        {"name": sm.get("name"), "label": sm.get("label")}
                    )

    # Rules referencing this item
    # ref_types only catches the item name double-quoted in structured trigger/
    # condition/action fields (e.g. itemName: "x"). Script bodies often reference
    # items differently — single-quoted strings (getItem('x')), no quotes at all
    # (items.x.sendCommand(...) via the items proxy), or just a mention in a
    # comment. _script_excerpts does a plain substring scan that catches all of
    # these, so a rule counts as referencing the item if *either* check hits —
    # otherwise script-only references (which are common and exactly the ones
    # worth a manual look before renaming/deleting) would be silently missed.
    #
    # Additionally: GroupStateChangeTrigger rules are fired when ANY member of a
    # group changes — so if this item is in group G and a rule has a
    # GroupStateChangeTrigger on G, that rule IS triggered by this item even if
    # the rule script never mentions the item by name. These are detected
    # structurally via _group_trigger_matches and reported as "group_trigger"
    # references. The trigger itself does not need patching on rename (the item
    # keeps its group memberships), but any dynamic name construction in the
    # script body (e.g. items[event.itemName + '_status']) cannot be
    # auto-patched and needs manual review.
    item_groups = item.get("groupNames", [])
    referencing_rules = []
    for rule in all_rules:
        ref_types = _rule_reference_types(rule, item_name)
        group_via = _group_trigger_matches(rule, item_groups)
        if group_via:
            ref_types.append("group_trigger")
        script_actions = [
            a for a in rule.get("actions", []) if a.get("type") == "script.ScriptAction"
        ]
        excerpts = _script_excerpts(rule, item_name) if script_actions else []
        if not ref_types and not excerpts:
            continue
        referencing_rules.append(
            {
                "uid": rule.get("uid"),
                "name": rule.get("name"),
                "reference_in": ref_types,
                "has_script": bool(script_actions),
                "script_excerpts": excerpts,
                "group_triggers": group_via,
            }
        )

    # UI components referencing this item
    # Components don't carry their own "namespace" field in the API response —
    # tag each by which namespace it was actually fetched from.
    referencing_ui = []
    for namespace, components in (("ui:page", pages), ("ui:widget", widgets)):
        for component in components:
            if _search_json(component, item_name):
                referencing_ui.append(
                    {
                        "uid": component.get("uid"),
                        "label": component.get("label"),
                        "namespace": namespace,
                    }
                )

    # Persistence: check if item has persistence metadata or known config
    persistence_namespaces = [
        ns
        for ns in item.get("metadata", {}).keys()
        if ns in ("rrd4j", "influxdb", "mapdb", "jdbc", "mongodb")
    ]

    # Impact summary
    script_rules = [r for r in referencing_rules if r["script_excerpts"]]
    group_trigger_rules = [r for r in referencing_rules if r.get("group_triggers")]
    # Group-trigger rules fire when this item changes but don't reference it by
    # name — the trigger itself survives a rename, but any dynamic script logic
    # (e.g. event.itemName + '_suffix') may need manual follow-up.
    blocking = len(links) + len(referencing_rules) + len(referencing_ui) + len(referencing_sitemaps)

    return {
        "item": {
            "name": item.get("name"),
            "label": item.get("label"),
            "type": item.get("type"),
            "state": item.get("state"),
            "group_memberships": item.get("groupNames", []),
            "tags": item.get("tags", []),
        },
        "channel_links": [
            {"channel_uid": lnk.get("channelUID"), "thing_uid": lnk.get("thing_uid")}
            for lnk in links
        ],
        "referenced_in_rules": referencing_rules,
        "referenced_in_ui": referencing_ui,
        "referenced_in_sitemaps": referencing_sitemaps,
        "persistence": {
            "configured_services": persistence_namespaces,
        },
        "impact_summary": {
            "total_references": blocking,
            "channel_links": len(links),
            "rules": len(referencing_rules),
            "ui_components": len(referencing_ui),
            "sitemaps": len(referencing_sitemaps),
            "script_rules_need_manual_review": [r["name"] for r in script_rules],
            "group_trigger_rules": [
                {"name": r["name"], "via_groups": r["group_triggers"]}
                for r in group_trigger_rules
            ],
            "safe_to_delete": blocking == 0,
            "rename_auto_updatable": [
                r["name"]
                for r in referencing_rules
                if r["reference_in"] == ["trigger"] and not r["script_excerpts"]
                and not r.get("group_triggers")
            ],
        },
    }
