"""Admin inventory for openHAB items.

Indexes ALL items (not just semantic Points) and extracts semantic
attributes where available — enabling both admin queries
('all Switch items without semantic tag') and semantic queries
('all LightSource equipment in living room').
"""

from collections import defaultdict
from threading import RLock
from typing import Any, Dict, List, Optional, Set


def _sem(item: dict) -> dict:
    """Extract metadata.semantics block from a raw openHAB item dict."""
    return item.get("metadata", {}).get("semantics", {})


def _hierarchy_prefixes(value: str) -> List[str]:
    """'Indoor_Room_LivingRoom' → ['Indoor', 'Indoor_Room', 'Indoor_Room_LivingRoom']"""
    parts = value.split("_")
    return ["_".join(parts[: i + 1]) for i in range(len(parts))]


class AdminInventory:
    """Thread-safe inventory of all openHAB items with semantic-aware indexing."""

    def __init__(self):
        self._items: Dict[str, dict] = {}

        # Semantic indexes (mirror semantic MCP, same filter API)
        self._location_index: Dict[str, Set[str]] = defaultdict(set)
        self._equipment_index: Dict[str, Set[str]] = defaultdict(set)
        self._point_index: Dict[str, Set[str]] = defaultdict(set)
        self._property_index: Dict[str, Set[str]] = defaultdict(set)

        # Admin-specific indexes
        self._type_index: Dict[str, Set[str]] = defaultdict(set)
        self._group_index: Dict[str, Set[str]] = defaultdict(set)  # group → members
        self._tag_index: Dict[str, Set[str]] = defaultdict(set)
        self._state_index: Dict[str, Set[str]] = defaultdict(set)
        self._category_index: Dict[str, Set[str]] = defaultdict(set)
        self._metadata_ns_index: Dict[str, Set[str]] = defaultdict(set)  # namespace → items
        self._has_semantic: Set[str] = set()  # items with any semantics metadata
        self._editable: Set[str] = set()  # items editable via REST API
        self._link_index: Dict[str, Set[str]] = defaultdict(set)  # item → thing UIDs

        self._lock = RLock()

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def build(self, raw_items: List[Dict[str, Any]]) -> None:
        """(Re-)build all indexes from a raw openHAB items list."""
        items: Dict[str, dict] = {}
        location_idx: Dict[str, Set[str]] = defaultdict(set)
        equipment_idx: Dict[str, Set[str]] = defaultdict(set)
        point_idx: Dict[str, Set[str]] = defaultdict(set)
        property_idx: Dict[str, Set[str]] = defaultdict(set)
        type_idx: Dict[str, Set[str]] = defaultdict(set)
        group_idx: Dict[str, Set[str]] = defaultdict(set)
        tag_idx: Dict[str, Set[str]] = defaultdict(set)
        state_idx: Dict[str, Set[str]] = defaultdict(set)
        category_idx: Dict[str, Set[str]] = defaultdict(set)
        metadata_ns_idx: Dict[str, Set[str]] = defaultdict(set)
        has_semantic: Set[str] = set()
        editable: Set[str] = set()

        for item in raw_items:
            name = item.get("name")
            if not name:
                continue
            items[name] = item

            # type
            itype = item.get("type", "")
            if itype:
                type_idx[itype].add(name)

            # state
            state = item.get("state")
            if state and state not in ("NULL", "UNDEF"):
                state_idx[state].add(name)

            # group memberships
            for grp in item.get("groupNames", []):
                group_idx[grp].add(name)

            # tags
            for tag in item.get("tags", []):
                tag_idx[tag].add(name)

            # category (UI icon key)
            cat = item.get("category", "")
            if cat:
                category_idx[cat].add(name)

            # metadata namespaces
            for ns in item.get("metadata", {}).keys():
                metadata_ns_idx[ns].add(name)

            # editable flag
            if item.get("editable", False):
                editable.add(name)

            # semantics
            sem = _sem(item)
            sem_value = sem.get("value", "")
            sem_config = sem.get("config", {}) if sem else {}

            if sem_value:
                has_semantic.add(name)

            if sem_value.startswith("Location_"):
                loc_type = sem_value.removeprefix("Location_")
                for prefix in _hierarchy_prefixes(loc_type):
                    location_idx[prefix].add(name)

            elif sem_value.startswith("Equipment_"):
                eq_type = sem_value.removeprefix("Equipment_")
                for prefix in _hierarchy_prefixes(eq_type):
                    equipment_idx[prefix].add(name)

            elif sem_value.startswith("Point_"):
                point_type = sem_value.removeprefix("Point_")
                for prefix in _hierarchy_prefixes(point_type):
                    point_idx[prefix].add(name)

                prop = sem_config.get("relatesTo", "")
                if prop:
                    prop_type = prop.removeprefix("Property_")
                    for prefix in _hierarchy_prefixes(prop_type):
                        property_idx[prefix].add(name)

        # Second pass: transitive membership resolution.
        # For each Location/Equipment group, BFS through group_idx to find all
        # transitive members and add them to the corresponding semantic index.
        # This enables queries like location="GroundFloor" to return not just the
        # Location item itself but all Equipment and Points within it.
        def _transitive_members(group_name: str) -> Set[str]:
            visited: Set[str] = set()
            queue = [group_name]
            while queue:
                current = queue.pop()
                for member in group_idx.get(current, set()):
                    if member not in visited:
                        visited.add(member)
                        queue.append(member)
            return visited

        for name, item in items.items():
            sem_value = _sem(item).get("value", "")
            if sem_value.startswith("Location_"):
                loc_type = sem_value.removeprefix("Location_")
                prefixes = _hierarchy_prefixes(loc_type)
                for member in _transitive_members(name):
                    for prefix in prefixes:
                        location_idx[prefix].add(member)
            elif sem_value.startswith("Equipment_"):
                eq_type = sem_value.removeprefix("Equipment_")
                prefixes = _hierarchy_prefixes(eq_type)
                for member in _transitive_members(name):
                    for prefix in prefixes:
                        equipment_idx[prefix].add(member)

        with self._lock:
            self._items = items
            self._location_index = location_idx
            self._equipment_index = equipment_idx
            self._point_index = point_idx
            self._property_index = property_idx
            self._type_index = type_idx
            self._group_index = group_idx
            self._tag_index = tag_idx
            self._state_index = state_idx
            self._category_index = category_idx
            self._metadata_ns_index = metadata_ns_idx
            self._has_semantic = has_semantic
            self._editable = editable

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def get(
        self,
        *,
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
    ) -> Set[str]:
        """Return item names matching ALL specified criteria (intersection).

        Admin-specific filters:
            group: items that are members of this group
            tag: items carrying this tag
            category: items with this UI icon category (e.g. 'window', 'light')
            has_metadata_ns: items that have this metadata namespace
            missing_metadata_ns: items that do NOT have this metadata namespace
            has_semantic: True = only items with any semantics metadata
            missing_semantic: True = only items WITHOUT semantics metadata
            editable: True = only items editable via REST API (not from .items files)
        """
        with self._lock:
            all_names = set(self._items.keys())
            filters: List[Set[str]] = []

            if location:
                filters.append(self._location_index.get(location, set()))
            if equipment:
                filters.append(self._equipment_index.get(equipment, set()))
            if point:
                filters.append(self._point_index.get(point, set()))
            if item_property:
                filters.append(self._property_index.get(item_property, set()))
            if item_type:
                filters.append(self._type_index.get(item_type, set()))
            if group:
                filters.append(self._group_index.get(group, set()))
            if tag:
                filters.append(self._tag_index.get(tag, set()))
            if state:
                filters.append(self._state_index.get(state, set()))
            if category:
                filters.append(self._category_index.get(category, set()))
            if has_metadata_ns:
                filters.append(self._metadata_ns_index.get(has_metadata_ns, set()))
            if missing_metadata_ns:
                filters.append(all_names - self._metadata_ns_index.get(missing_metadata_ns, set()))
            if has_semantic is True:
                filters.append(self._has_semantic.copy())
            if missing_semantic is True:
                filters.append(all_names - self._has_semantic)
            if editable is True:
                filters.append(self._editable.copy())
            if editable is False:
                filters.append(all_names - self._editable)
            if item_names:
                filters.append(set(item_names) & all_names)

            if not filters:
                return all_names.copy()

            result = filters[0].copy()
            for f in filters[1:]:
                result &= f
            return result

    def get_item(self, name: str) -> Optional[dict]:
        with self._lock:
            return self._items.get(name)

    def all_items(self) -> List[dict]:
        with self._lock:
            return list(self._items.values())

    # ------------------------------------------------------------------
    # Discovery helpers (mirrors semantic MCP's get_available_* pattern)
    # ------------------------------------------------------------------

    def get_available_locations(self) -> List[str]:
        with self._lock:
            return sorted(self._location_index.keys())

    def get_available_equipment(self) -> List[str]:
        with self._lock:
            return sorted(self._equipment_index.keys())

    def get_available_points(self) -> List[str]:
        with self._lock:
            return sorted(self._point_index.keys())

    def get_available_properties(self) -> List[str]:
        with self._lock:
            return sorted(self._property_index.keys())

    def get_available_types(self) -> List[str]:
        with self._lock:
            return sorted(self._type_index.keys())

    def get_available_groups(self) -> List[str]:
        with self._lock:
            return sorted(self._group_index.keys())

    def get_available_categories(self) -> List[str]:
        with self._lock:
            return sorted(self._category_index.keys())

    def get_available_metadata_namespaces(self) -> List[str]:
        with self._lock:
            return sorted(self._metadata_ns_index.keys())

    def get_direct_members(self, group_name: str) -> Set[str]:
        """Direct members of a group (items whose groupNames contains group_name)."""
        with self._lock:
            return self._group_index.get(group_name, set()).copy()

    def get_root_groups(self) -> List[str]:
        """Group-type items with no group memberships — natural tree roots."""
        with self._lock:
            return sorted(
                name for name, item in self._items.items()
                if item.get("type", "").startswith("Group") and not item.get("groupNames")
            )

    def get_ungrouped_items(self) -> List[str]:
        """Non-Group items with no group memberships — the 'no group' node."""
        with self._lock:
            return sorted(
                name for name, item in self._items.items()
                if not item.get("type", "").startswith("Group") and not item.get("groupNames")
            )

    def build_links(self, raw_links: List[Dict[str, Any]]) -> None:
        """Index channel links: item → set of thing UIDs.

        thing UID is extracted from channelUID by dropping the last ':'-segment.
        E.g. 'zwave:device:ctrl:node5:switch_binary' → 'zwave:device:ctrl:node5'
        """
        link_idx: Dict[str, Set[str]] = defaultdict(set)
        for link in raw_links:
            item_name = link.get("itemName", "")
            channel_uid = link.get("channelUID", "")
            if item_name and ":" in channel_uid:
                thing_uid = channel_uid.rsplit(":", 1)[0]
                link_idx[item_name].add(thing_uid)
        with self._lock:
            self._link_index = link_idx

    def get_thing_uids(self, item_name: str) -> Set[str]:
        """Return the thing UIDs of all channels linked to this item."""
        with self._lock:
            return self._link_index.get(item_name, set()).copy()

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._items)
