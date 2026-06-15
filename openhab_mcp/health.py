"""Model health analysis — statistical anomaly detection for the openHAB item model.

Two analyses:

1. group_membership_anomalies
   For each collection group, builds a feature profile from its members
   (item type, name tokens, semantic class, co-group memberships, metadata
   namespaces). Non-members with a high profile-match score are flagged as
   candidates — "this item looks like it should be here."

2. equipment_completeness
   Groups equipment items by semantic type (Equipment_Window,
   Equipment_Sensor_MultiSensor, …). For each type, derives the set of
   semantic points present in >60 % of instances. Equipment groups missing
   common points are reported as incomplete.

No external libraries required — pure Python Counter arithmetic.
"""

import math
from collections import Counter, defaultdict
from typing import Any, Dict, List, Set, Tuple

from .inventory import AdminInventory

# ── tunables ────────────────────────────────────────────────────────────────
_MIN_GROUP_MEMBERS = 3      # groups smaller than this are skipped
_MIN_SCORE = 0.30           # minimum match score to report a candidate
_MAX_CANDIDATES = 8         # max candidates per group
_MAX_GROUPS = 25            # max groups in anomaly report (highest-candidate first)
_COMPLETENESS_THRESHOLD = 0.6  # fraction of equipment that must have a point for it to be "expected"
_RARITY_MAX_COUNT = 1          # points present in ≤ this many instances are "rare"
_MIN_EQUIPMENT_INSTANCES = 2   # need at least this many to derive a pattern


# ── feature extraction ───────────────────────────────────────────────────────

def _features(
    item: dict,
    thing_uids: Set[str] = frozenset(),
    location_types: Set[str] = frozenset(),
) -> Set[str]:
    """Return the feature set for one item."""
    out: Set[str] = set()

    itype = item.get("type", "")
    if itype:
        out.add(f"type:{itype}")
        if ":" in itype:
            out.add(f"dim:{itype.split(':', 1)[1]}")

    for g in item.get("groupNames", []):
        out.add(f"grp:{g}")

    sem = item.get("metadata", {}).get("semantics", {}).get("value", "")
    if sem:
        out.add(f"sem:{sem}")

    for ns in item.get("metadata", {}).keys():
        if ns != "semantics":
            out.add(f"ns:{ns}")

    cat = item.get("category", "")
    if cat:
        out.add(f"cat:{cat}")

    for uid in thing_uids:
        out.add(f"thing:{uid}")

    for loc in location_types:
        out.add(f"loc:{loc}")

    for tok in item.get("name", "").split("_"):
        if len(tok) > 2:
            out.add(f"tok:{tok}")

    for tok in item.get("label", "").split():
        if len(tok) > 1:
            out.add(f"lbl:{tok.lower()}")

    return out


def _build_profile(member_feature_sets: List[Set[str]]) -> Dict[str, float]:
    """Feature → relative frequency (TF) across members."""
    n = len(member_feature_sets)
    if not n:
        return {}
    counts: Counter = Counter()
    for fs in member_feature_sets:
        counts.update(fs)
    return {f: c / n for f, c in counts.items()}


def _apply_idf(profiles: Dict[str, Dict[str, float]], n_groups: int) -> Dict[str, Dict[str, float]]:
    """Down-weight features that appear in many groups (TF-IDF).

    Features ubiquitous across groups (e.g. tok:Switch appearing in 30+ groups)
    are poor discriminators. IDF = log(N / df) where df = number of groups
    containing this feature.
    """
    df: Counter = Counter()
    for profile in profiles.values():
        df.update(profile.keys())

    result = {}
    for group, profile in profiles.items():
        weighted = {}
        for f, tf in profile.items():
            idf = math.log(n_groups / df[f]) if df[f] < n_groups else 0.0
            weighted[f] = tf * idf
        result[group] = weighted
    return result


def _score(item_features: Set[str], profile: Dict[str, float]) -> Tuple[float, float, List[str]]:
    """Return (total_score, structural_score, top matching features).

    total_score     — all features including name tokens (tok:*)
    structural_score — only type:, sem:, grp:, ns:, cat: features
                       A high structural_score means the match is driven by
                       item type / semantics / group co-membership, not just
                       name token overlap. Use this to filter trivial findings.
    """
    total = sum(profile.values())
    if not total:
        return 0.0, 0.0, []

    matched = [(f, profile[f]) for f in item_features if f in profile and profile[f] > 0]
    matched.sort(key=lambda x: -x[1])

    total_score = sum(w for _, w in matched) / total

    structural_matched = [(f, w) for f, w in matched if not f.startswith(("tok:", "lbl:"))]
    structural_total = sum(w for f, w in profile.items() if not f.startswith(("tok:", "lbl:")))
    structural_score = (
        sum(w for _, w in structural_matched) / structural_total
        if structural_total > 0 else 0.0
    )

    return total_score, structural_score, [f for f, _ in matched[:6]]


# ── analysis 1: group membership anomalies ──────────────────────────────────

def _group_anomalies(inventory: AdminInventory) -> Dict[str, Any]:
    all_items = inventory.all_items()
    by_name = {i["name"]: i for i in all_items}

    # Pre-build per-item feature context (thing UIDs + direct location)
    item_things = {i["name"]: inventory.get_thing_uids(i["name"]) for i in all_items}

    # Direct location: walk 1-2 levels up from item to find the Location group ancestor.
    # Using transitive location index gives too many hierarchy levels (Floor, Building, …)
    # and causes cross-room false positives between rooms on the same floor.
    def _direct_locs(item: dict) -> Set[str]:
        locs: Set[str] = set()
        for parent_name in item.get("groupNames", []):
            parent = inventory.get_item(parent_name)
            if not parent:
                continue
            sem = parent.get("metadata", {}).get("semantics", {}).get("value", "")
            if sem.startswith("Location_"):
                locs.add(sem.removeprefix("Location_"))
                continue
            for gp_name in parent.get("groupNames", []):
                gp = inventory.get_item(gp_name)
                if gp:
                    gp_sem = gp.get("metadata", {}).get("semantics", {}).get("value", "")
                    if gp_sem.startswith("Location_"):
                        locs.add(gp_sem.removeprefix("Location_"))
        return locs

    item_locs = {i["name"]: _direct_locs(i) for i in all_items}

    def feat(item: dict) -> Set[str]:
        n = item["name"]
        return _features(item, item_things.get(n, frozenset()), item_locs.get(n, frozenset()))

    # First pass: build raw TF profiles for all qualifying groups
    eligible: Dict[str, Set[str]] = {}  # group → member names
    raw_profiles: Dict[str, Dict[str, float]] = {}

    for group_name in inventory.get_available_groups():
        members = inventory.get_direct_members(group_name)
        if len(members) < _MIN_GROUP_MEMBERS:
            continue
        member_items = [by_name[n] for n in members if n in by_name]
        profile = _build_profile([feat(i) for i in member_items])
        eligible[group_name] = members
        raw_profiles[group_name] = profile

    # Apply IDF across all groups to down-weight ubiquitous features
    tfidf_profiles = _apply_idf(raw_profiles, len(raw_profiles))

    findings = []

    for group_name, members in eligible.items():
        profile = tfidf_profiles[group_name]

        # Skip groups with no discriminating signal after IDF
        if not any(v > 0.01 for v in profile.values()):
            continue

        candidates = []
        for item in all_items:
            name = item["name"]
            if name in members or name == group_name:
                continue
            score, struct_score, top_feats = _score(feat(item), profile)
            if score >= _MIN_SCORE and struct_score >= 0.20:
                candidates.append({
                    "item": name,
                    "label": item.get("label", ""),
                    "score": round(score, 3),
                    "structural_score": round(struct_score, 3),
                    "matching_features": top_feats,
                })

        if not candidates:
            continue

        candidates.sort(key=lambda x: -x["score"])
        group_item = by_name.get(group_name)
        findings.append({
            "group": group_name,
            "group_label": group_item.get("label", "") if group_item else "",
            "member_count": len(members),
            "candidates": candidates[:_MAX_CANDIDATES],
        })

    findings.sort(key=lambda x: -len(x["candidates"]))
    return {
        "description": (
            "Items that statistically resemble group members but are not in the group. "
            "Score uses TF-IDF weighting: features rare across groups but common in this "
            "group contribute most to the score."
        ),
        "groups_analysed": len(findings),
        "findings": findings[:_MAX_GROUPS],
    }


# ── analysis 2: equipment completeness ──────────────────────────────────────

def _equipment_completeness(inventory: AdminInventory) -> Dict[str, Any]:
    all_items = inventory.all_items()

    # Collect all Equipment Group items by their semantic type
    by_type: Dict[str, List[dict]] = defaultdict(list)
    for item in all_items:
        sem = item.get("metadata", {}).get("semantics", {}).get("value", "")
        if sem.startswith("Equipment_") and item.get("type", "").startswith("Group"):
            by_type[sem.removeprefix("Equipment_")].append(item)

    findings = []

    for eq_type, eq_items in sorted(by_type.items()):
        if len(eq_items) < _MIN_EQUIPMENT_INSTANCES:
            continue

        # For each equipment, derive the set of semantic Point types among direct members
        # (non-semantic items are excluded — we only care about semantic completeness)
        profiles: List[Set[str]] = []
        for eq in eq_items:
            points: Set[str] = set()
            for m_name in inventory.get_direct_members(eq["name"]):
                m = inventory.get_item(m_name)
                if not m:
                    continue
                m_sem = m.get("metadata", {}).get("semantics", {}).get("value", "")
                if m_sem.startswith("Point_"):
                    points.add(m_sem)
                elif m_sem.startswith("Equipment_") and m.get("type", "").startswith("Group"):
                    # Sub-equipment: recurse one level to capture its points too
                    for sub_name in inventory.get_direct_members(m_name):
                        sub = inventory.get_item(sub_name)
                        if sub:
                            sub_sem = sub.get("metadata", {}).get("semantics", {}).get("value", "")
                            if sub_sem.startswith("Point_"):
                                points.add(sub_sem)
            profiles.append(points)

        n = len(profiles)
        all_points: Set[str] = set().union(*profiles)
        point_counts = {p: sum(1 for prof in profiles if p in prof) for p in all_points}

        expected = {p for p, c in point_counts.items() if c / n >= _COMPLETENESS_THRESHOLD}
        rare = {p for p, c in point_counts.items() if 0 < c <= _RARITY_MAX_COUNT}

        if not expected and not rare:
            continue

        incomplete = []
        if expected:
            for eq, profile in zip(eq_items, profiles):
                missing = expected - profile
                if missing:
                    incomplete.append({
                        "equipment": eq["name"],
                        "label": eq.get("label", ""),
                        "missing_points": sorted(missing),
                    })

        rare_points = []
        for point in sorted(rare):
            owners = [
                {"equipment": eq["name"], "label": eq.get("label", "")}
                for eq, profile in zip(eq_items, profiles) if point in profile
            ]
            rare_points.append({"point": point, "only_in": owners})

        if incomplete or rare_points:
            finding: Dict[str, Any] = {
                "equipment_type": eq_type,
                "instance_count": n,
            }
            if expected:
                finding["expected_points"] = sorted(expected)
            if incomplete:
                finding["incomplete"] = sorted(incomplete, key=lambda x: -len(x["missing_points"]))
            if rare_points:
                finding["rare_points"] = rare_points
            findings.append(finding)

    return {
        "description": (
            f"Equipment groups missing semantic points present in ≥{int(_COMPLETENESS_THRESHOLD*100)}% "
            f"of same-type equipment (incomplete), or having points present in ≤{_RARITY_MAX_COUNT} "
            "instance (rare — potential template or inconsistency)."
        ),
        "equipment_types_affected": len(findings),
        "findings": findings,
    }


# ── public entry point ───────────────────────────────────────────────────────

def analyze_model_health(inventory: AdminInventory) -> Dict[str, Any]:
    """Run all health analyses and return a combined report.

    Requires inventory to be populated (refresh_inventory called first).
    """
    if inventory.size == 0:
        return {"error": "Inventory empty — call refresh_inventory first."}

    return {
        "item_count": inventory.size,
        "group_membership_anomalies": _group_anomalies(inventory),
        "equipment_completeness": _equipment_completeness(inventory),
    }
