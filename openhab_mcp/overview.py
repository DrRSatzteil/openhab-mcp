"""Home overview — group hierarchy with semantic annotations and item summaries.

build_home_overview() traverses the openHAB group tree starting from root groups
(Group items with no parent group memberships) and produces a compact nested
structure suitable for LLM consumption.

Each node represents a Group item. Leaf-level non-Group members are not listed
individually but summarised by semantic point type and item type. Items with no
group membership at all appear in a separate 'no_group' node.
"""

from typing import Any, Dict, List, Optional, Set

from .inventory import AdminInventory


def _sem_value(item: dict) -> str:
    return item.get("metadata", {}).get("semantics", {}).get("value", "")


def _item_summary(items_data: List[dict]) -> Dict[str, Any]:
    """Aggregate non-Group items into a compact summary dict."""
    semantic: Dict[str, int] = {}
    non_semantic: Dict[str, int] = {}
    for item in items_data:
        sv = _sem_value(item)
        itype = item.get("type", "?")
        if sv.startswith("Point_"):
            key = sv.removeprefix("Point_") or "Point"
            semantic[key] = semantic.get(key, 0) + 1
        elif sv.startswith("Equipment_"):
            # Equipment group that is a direct leaf (no children) — treat as semantic
            key = "Equipment_" + (sv.removeprefix("Equipment_") or "?")
            semantic[key] = semantic.get(key, 0) + 1
        else:
            non_semantic[itype] = non_semantic.get(itype, 0) + 1
    result: Dict[str, Any] = {}
    if semantic:
        result["semantic_points"] = semantic
    if non_semantic:
        result["non_semantic"] = non_semantic
    return result


def _build_node(
    group_name: str,
    inventory: AdminInventory,
    visited: Set[str],
) -> Dict[str, Any]:
    """Recursively build a tree node for a Group item."""
    item = inventory.get_item(group_name)
    sv = _sem_value(item) if item else ""

    node: Dict[str, Any] = {
        "name": group_name,
        "label": (item.get("label") or "") if item else "",
    }
    if sv:
        node["semantic"] = sv

    visited = visited | {group_name}

    child_groups: List[Dict[str, Any]] = []
    leaf_items: List[dict] = []

    for member_name in sorted(inventory.get_direct_members(group_name)):
        if member_name in visited:
            continue
        member = inventory.get_item(member_name)
        if not member:
            continue
        if member.get("type", "").startswith("Group"):
            child_nodes = _build_node(member_name, inventory, visited)
            child_groups.append(child_nodes)
        else:
            leaf_items.append(member)

    if child_groups:
        node["groups"] = child_groups
    summary = _item_summary(leaf_items)
    if summary:
        node["items"] = summary

    return node


def build_home_overview(inventory: AdminInventory) -> Dict[str, Any]:
    """Build a compact home overview from the current inventory.

    Returns
    -------
    dict with:
        tree        — list of root group nodes (recursive)
        no_group    — summary of items with no group membership at all
        stats       — total counts
    """
    if inventory.size == 0:
        return {"error": "Inventory empty — call refresh_inventory first."}

    # Root nodes: Group items without any group membership
    root_groups = inventory.get_root_groups()
    tree = [_build_node(name, inventory, set()) for name in root_groups]

    # No-group node: non-Group items without any group membership
    ungrouped = inventory.get_ungrouped_items()
    no_group: Optional[Dict[str, Any]] = None
    if ungrouped:
        ungrouped_items = [inventory.get_item(n) for n in ungrouped if inventory.get_item(n)]
        no_group = {
            "count": len(ungrouped),
            "summary": _item_summary(ungrouped_items),
            "names": ungrouped,
        }

    # Stats
    all_items = inventory.all_items()
    sem_count = sum(1 for i in all_items if _sem_value(i))
    group_count = sum(1 for i in all_items if i.get("type", "").startswith("Group"))

    return {
        "tree": tree,
        "no_group": no_group,
        "stats": {
            "total_items": inventory.size,
            "groups": group_count,
            "with_semantics": sem_count,
            "without_semantics": inventory.size - sem_count,
            "root_groups": len(root_groups),
            "ungrouped_items": len(ungrouped),
        },
    }
