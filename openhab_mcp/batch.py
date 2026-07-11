"""Batch update for openHAB items using JSON Merge Patch semantics (RFC 7396).

update_items() combines inventory filters with a patch object:
  - Present field  → replace
  - null value     → delete / clear
  - Absent field   → unchanged

Metadata is shallow-merged at namespace level: only listed namespaces
are touched, others remain. metadata.semantics raises an error (change
semantic class via tags/group membership instead).
"""

from typing import Any, Dict, List, Optional, Set
from urllib.parse import quote

from .inventory import AdminInventory
from .openhab_client import OpenHABClient

_ITEM_WRITABLE_FIELDS = {"label", "category", "tags", "groupNames", "tags_remove", "groups_remove", "function", "groupType", "unitSymbol"}
_SEMANTIC_PREFIXES = ("Location_", "Equipment_", "Point_", "Property_")


def _is_semantic_tag(tag: str) -> bool:
    return any(tag.startswith(p) for p in _SEMANTIC_PREFIXES)


def _apply_item_patch(
    current: Dict[str, Any], patch: Dict[str, Any], merge: bool = True
) -> Dict[str, Any]:
    """Merge patch fields into a PUT-ready item payload.

    With merge=True (default):
      - tags / groupNames: union of existing + patch values
      - null: always clears the field regardless of merge mode
    With merge=False:
      - tags / groupNames: exact replacement with patch values

    Returns (payload, changed) where payload is ready for PUT /rest/items/{name}
    and changed is True if any writable field differs from current.
    """
    payload: Dict[str, Any] = {
        "type": current["type"],
        "name": current["name"],
        "label": current.get("label", ""),
        "category": current.get("category", ""),
        "tags": list(current.get("tags", [])),
        "groupNames": list(current.get("groupNames", [])),
    }
    for opt in ("function", "groupType", "unitSymbol"):
        if current.get(opt):
            payload[opt] = current[opt]

    changed = False

    # Scalar fields: always replace if present
    for field in ("label", "category"):
        if field not in patch:
            continue
        new_val = "" if patch[field] is None else patch[field]
        if payload[field] != new_val:
            payload[field] = new_val
            changed = True

    # List fields: merge (union) or replace depending on mode
    for field in ("tags", "groupNames"):
        if field not in patch:
            continue
        val = patch[field]
        if val is None:
            new_val = []  # null always clears, regardless of merge mode
        elif merge:
            new_val = sorted(set(payload[field]) | set(val))
        else:
            new_val = val
        if sorted(payload[field]) != sorted(new_val):
            payload[field] = new_val
            changed = True

    # Explicit removals — remove specific entries without touching others
    for patch_key, field in (("tags_remove", "tags"), ("groups_remove", "groupNames")):
        if patch_key not in patch or not patch[patch_key]:
            continue
        to_remove = set(patch[patch_key])
        new_val = [v for v in payload[field] if v not in to_remove]
        if new_val != payload[field]:
            payload[field] = new_val
            changed = True

    return payload, changed


def _metadata_ops(patch_metadata: Dict[str, Any]) -> tuple:
    """Split metadata patch into (upserts, deletes).

    upserts: {ns: {value, config}}
    deletes: [ns, ...]
    Raises ValueError if 'semantics' namespace is in the patch.
    """
    if "semantics" in patch_metadata:
        raise ValueError(
            "Patching 'semantics' metadata directly is not supported. "
            "Change the semantic class via tags or group membership instead."
        )
    upserts: Dict[str, Any] = {}
    deletes: List[str] = []
    for ns, val in patch_metadata.items():
        if val is None:
            deletes.append(ns)
        else:
            upserts[ns] = val
    return upserts, deletes


def update_items(
    patch: Dict[str, Any],
    inventory: AdminInventory,
    client: OpenHABClient,
    dry_run: bool = True,
    merge: bool = True,
    # --- inventory filters (mirrors query_inventory) ---
    location: Optional[str] = None,
    equipment: Optional[str] = None,
    point: Optional[str] = None,
    item_property: Optional[str] = None,
    item_type: Optional[str] = None,
    group: Optional[str] = None,
    tag: Optional[str] = None,
    state: Optional[str] = None,
    category: Optional[str] = None,
    has_metadata_ns: Optional[str] = None,
    missing_metadata_ns: Optional[str] = None,
    has_semantic: Optional[bool] = None,
    missing_semantic: Optional[bool] = None,
    editable: Optional[bool] = None,
    item_names: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Apply a JSON Merge Patch to all items matching the given filters.

    Parameters
    ----------
    patch : dict
        JSON Merge Patch object. Supported fields:
        - label, category, tags, groupNames  →  direct replace if present, null clears
        - metadata: {ns: {value, config}}    →  add/update namespace; null deletes it
        metadata.semantics raises an error — use tags/groups to change semantic class.
    dry_run : bool
        If True (default), return the plan without making changes.
    All other parameters mirror query_inventory filters.
    """
    if inventory.size == 0:
        return {"error": "Inventory empty — call refresh_inventory first.", "matched": 0}

    # Validate patch before touching anything
    patch_metadata: Dict[str, Any] = patch.get("metadata", {})
    try:
        meta_upserts, meta_deletes = _metadata_ops(patch_metadata)
    except ValueError as exc:
        return {"error": str(exc), "matched": 0}

    has_item_field_changes = any(f in patch for f in _ITEM_WRITABLE_FIELDS)
    has_meta_changes = bool(meta_upserts or meta_deletes)

    if not has_item_field_changes and not has_meta_changes:
        return {"error": "Patch is empty — nothing to change.", "matched": 0}

    # Resolve matching items
    names = inventory.get(
        location=location,
        equipment=equipment,
        point=point,
        item_property=item_property,
        item_type=item_type,
        group=group,
        tag=tag,
        state=state,
        category=category,
        has_metadata_ns=has_metadata_ns,
        missing_metadata_ns=missing_metadata_ns,
        has_semantic=has_semantic,
        missing_semantic=missing_semantic,
        editable=editable,
        item_names=item_names,
    )

    if not names:
        return {"matched": 0, "plan": [], "executed": False, "errors": []}

    # Build plan
    plan = []
    for name in sorted(names):
        current = inventory.get_item(name)
        if not current:
            continue

        item_payload, item_changed = _apply_item_patch(current, patch, merge=merge)

        meta_plan = []
        for ns, val in meta_upserts.items():
            current_meta = current.get("metadata", {}).get(ns)
            if current_meta != val:
                meta_plan.append({"op": "upsert", "namespace": ns, "value": val})
        for ns in meta_deletes:
            if ns in current.get("metadata", {}):
                meta_plan.append({"op": "delete", "namespace": ns})

        if item_changed or meta_plan:
            plan.append({
                "name": name,
                "item_fields_changed": item_changed,
                "new_payload": {k: v for k, v in item_payload.items()
                                if k not in ("type", "name")} if item_changed else {},
                "metadata_ops": meta_plan,
            })

    if dry_run:
        return {
            "matched": len(names),
            "will_change": len(plan),
            "plan": plan,
            "executed": False,
            "errors": [],
        }

    # Execute
    completed: List[str] = []
    errors: List[str] = []

    for entry in plan:
        name = entry["name"]
        encoded = quote(name, safe="")

        if entry["item_fields_changed"]:
            try:
                client.session.put(
                    f"{client.base_url}/rest/items/{encoded}",
                    json=entry["new_payload"] | {"type": inventory.get_item(name)["type"], "name": name},
                ).raise_for_status()
                completed.append(f"{name}: item fields updated")
            except Exception as exc:
                errors.append(f"{name}: item PUT failed — {exc}")
                continue

        for op in entry["metadata_ops"]:
            ns = op["namespace"]
            try:
                if op["op"] == "upsert":
                    client.session.put(
                        f"{client.base_url}/rest/items/{encoded}/metadata/{ns}",
                        json=op["value"],
                    ).raise_for_status()
                    completed.append(f"{name}: metadata.{ns} updated")
                else:
                    client.session.delete(
                        f"{client.base_url}/rest/items/{encoded}/metadata/{ns}"
                    ).raise_for_status()
                    completed.append(f"{name}: metadata.{ns} deleted")
            except Exception as exc:
                errors.append(f"{name}: metadata.{ns} {op['op']} failed — {exc}")

    return {
        "matched": len(names),
        "changed": len(plan),
        "executed": True,
        "steps_completed": completed,
        "errors": errors,
    }
