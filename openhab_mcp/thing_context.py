"""Thing context — complete picture of a thing's role in the openHAB model.

Returns:
  thing             — basic thing info
  thing_channels    — all channels (linked and unlinked) with item types
  linked_channels   — channels that have items, with full item profiles
  equipment_context — group hierarchy from Location anchor down to the items
"""

from typing import Any, Dict, List, Optional, Set, Tuple

from .inventory import AdminInventory


def _sem_value(item: dict) -> str:
    return item.get("metadata", {}).get("semantics", {}).get("value", "")


def _collect_ancestor_chain(
    group_name: str,
    inventory: AdminInventory,
    visited: Set[str],
) -> List[dict]:
    """Walk upward from group_name through parent groupNames.

    Returns list of group dicts from root (topmost ancestor) down to group_name.
    Stops after adding a Location_ semantic group — that's the anchor point,
    we don't go higher.
    """
    chain: List[dict] = []
    current = group_name
    seen = set(visited)

    while current and current not in seen:
        seen.add(current)
        item = inventory.get_item(current)
        if not item:
            break

        sv = _sem_value(item)
        node: dict = {
            "name": current,
            "label": item.get("label", ""),
            "semantic": sv,
            "type": item.get("type", ""),
        }
        gtype = item.get("groupType")
        if gtype:
            func_name = (item.get("function") or {}).get("name")
            node["group_function"] = f"{gtype}:{func_name}" if func_name else gtype

        chain.append(node)

        if sv.startswith("Location_"):
            break  # anchor found — stop traversal

        parents = item.get("groupNames", [])
        if not parents:
            break
        current = parents[0]  # follow first parent (single-parent Equipment chains are standard)

    chain.reverse()  # root/anchor first
    return chain


def _merge_chains_into_tree(chains: List[List[dict]]) -> List[dict]:
    """Merge flat ancestor chains into a nested tree, deduplicating shared nodes."""
    node_map: Dict[str, dict] = {}
    root_names: List[str] = []

    def _ensure(d: dict) -> dict:
        n = d["name"]
        if n not in node_map:
            node = dict(d)
            node["children"] = []
            node_map[n] = node
        return node_map[n]

    for chain in chains:
        if not chain:
            continue
        _ensure(chain[0])
        if chain[0]["name"] not in root_names:
            root_names.append(chain[0]["name"])
        for i in range(len(chain) - 1):
            parent = _ensure(chain[i])
            child = _ensure(chain[i + 1])
            if chain[i + 1]["name"] not in [c["name"] for c in parent["children"]]:
                parent["children"].append(child)

    return [node_map[n] for n in root_names if n in node_map]


def get_thing_context(
    thing_uid: str,
    client: Any,
    inventory: AdminInventory,
) -> Dict[str, Any]:
    """Return complete structural context of a thing.

    Parameters
    ----------
    thing_uid:
        UID of the thing (e.g. 'mqtt:topic:broker:EsszimmerRollladen').
    client:
        OpenHABClient instance.
    inventory:
        Populated AdminInventory instance (refresh_inventory must be called first).
    """
    if inventory.size == 0:
        return {"error": "Inventory empty — call refresh_inventory first."}

    # Thing basic info
    try:
        thing = client.get_thing(thing_uid)
    except Exception as e:
        return {"error": f"Thing not found: {e}"}

    # All channels (linked and unlinked)
    try:
        all_channels = client.get_thing_channels(thing_uid)
    except Exception as e:
        return {"error": f"Could not fetch channels: {e}"}

    # All links — filter for this thing
    try:
        resp = client.session.get(f"{client.base_url}/rest/links")
        resp.raise_for_status()
        all_links: List[dict] = resp.json()
    except Exception as e:
        return {"error": f"Could not fetch links: {e}"}

    prefix = thing_uid + ":"
    thing_links = [lnk for lnk in all_links if lnk.get("channelUID", "").startswith(prefix)]

    # Index link configs by (channel_id, item_name)
    link_cfg: Dict[Tuple[str, str], dict] = {}
    for lnk in thing_links:
        ch_id = lnk["channelUID"][len(prefix):]
        link_cfg[(ch_id, lnk["itemName"])] = lnk.get("configuration", {})

    # Channel summary (all channels, mark which are linked)
    linked_ch_ids = {lnk["channelUID"][len(prefix):] for lnk in thing_links}

    def _ch_id(ch: dict) -> str:
        uid = ch.get("uid", "")
        return uid.split(":")[-1] if uid else ch.get("id", "")

    thing_channels_out = [
        {
            "channel_id": _ch_id(ch),
            "label": ch.get("label", ""),
            "item_type": ch.get("itemType", ""),
            "channel_type_uid": ch.get("channelTypeUID", ""),
            "linked": _ch_id(ch) in linked_ch_ids,
        }
        for ch in all_channels
    ]

    # Group links by channel_id
    ch_items: Dict[str, List[str]] = {}
    for lnk in thing_links:
        ch_id = lnk["channelUID"][len(prefix):]
        ch_items.setdefault(ch_id, []).append(lnk["itemName"])

    # Build linked channel list with full item profiles
    linked_channels_out = []
    ancestor_chains: List[List[dict]] = []

    for ch_id in sorted(ch_items):
        items_out = []
        for item_name in sorted(ch_items[ch_id]):
            inv_item = inventory.get_item(item_name)
            if not inv_item:
                continue

            sv = _sem_value(inv_item)
            sem_config = inv_item.get("metadata", {}).get("semantics", {}).get("config", {})
            metadata = {
                ns: val
                for ns, val in inv_item.get("metadata", {}).items()
                if ns != "semantics"
            }

            items_out.append({
                "name": item_name,
                "type": inv_item.get("type", ""),
                "label": inv_item.get("label", ""),
                "category": inv_item.get("category", ""),
                "tags": inv_item.get("tags", []),
                "semantic": sv,
                "semantic_config": sem_config,
                "groups": inv_item.get("groupNames", []),
                "metadata": metadata,
                "link_configuration": link_cfg.get((ch_id, item_name), {}),
            })

            for grp in inv_item.get("groupNames", []):
                chain = _collect_ancestor_chain(grp, inventory, {item_name})
                if chain:
                    ancestor_chains.append(chain)

        if items_out:
            linked_channels_out.append({
                "channel_id": ch_id,
                "channel_uid": f"{thing_uid}:{ch_id}",
                "items": items_out,
            })

    equipment_context = _merge_chains_into_tree(ancestor_chains)

    return {
        "thing": {
            "uid": thing_uid,
            "label": thing.get("label", ""),
            "thing_type_uid": thing.get("thingTypeUID", ""),
            "status": thing.get("statusInfo", {}).get("status", ""),
            "configuration": thing.get("configuration", {}),
        },
        "thing_channels": thing_channels_out,
        "linked_channels": linked_channels_out,
        "equipment_context": equipment_context,
    }
