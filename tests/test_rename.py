import unittest
from unittest.mock import MagicMock, patch

from openhab_mcp.rename import _build_member_regroup_payload, _direct_members, rename_item

EMPTY_DIAGNOSIS = {
    "channel_links": [],
    "referenced_in_rules": [],
    "referenced_in_ui": [],
    "referenced_in_sitemaps": [],
}


class TestDirectMembers(unittest.TestCase):
    """Pure unit tests for the helpers, no client/HTTP involved."""

    def test_direct_members_filters_by_own_groupnames(self):
        raw = {
            "name": "old_group",
            "members": [
                {"name": "child_a", "type": "Switch", "groupNames": ["old_group"]},
                {"name": "child_b", "type": "Switch", "groupNames": ["old_group", "other_group"]},
                # Nested grandchild that happens to be flattened into "members" by the
                # REST API but does NOT itself list old_group — must not be touched.
                {"name": "grandchild", "type": "Switch", "groupNames": ["some_subgroup"]},
            ],
        }
        members = _direct_members(raw, "old_group")
        self.assertEqual({m["name"] for m in members}, {"child_a", "child_b"})

    def test_build_member_regroup_payload_swaps_only_old_name(self):
        member = {
            "name": "child_b",
            "type": "Switch",
            "label": "Child B",
            "category": "Switch",
            "tags": ["Switch"],
            "groupNames": ["old_group", "other_group"],
        }
        payload = _build_member_regroup_payload(member, "old_group", "new_group")
        self.assertEqual(payload["groupNames"], ["new_group", "other_group"])
        self.assertEqual(payload["name"], "child_b")
        self.assertEqual(payload["label"], "Child B")
        self.assertEqual(payload["tags"], ["Switch"])


class TestRenameItemRegroupsMembers(unittest.TestCase):
    """Regression test: renaming a Group must repoint direct members' groupNames
    instead of leaving them referencing the now-deleted old group name."""

    def setUp(self):
        self.client = MagicMock()
        self.old_raw = {
            "name": "upperfloor_chime",
            "type": "Group",
            "label": "Gong Obergeschoss",
            "category": "soundvolume",
            "tags": ["Speaker"],
            "groupNames": ["loc_uppercorridor"],
            "metadata": {},
            "members": [
                {
                    "name": "upperfloor_chime_playchime",
                    "type": "Switch",
                    "label": "Gong abspielen",
                    "category": "SoundVolume",
                    "tags": ["Switch", "Enabled"],
                    "groupNames": ["upperfloor_chime"],
                },
                {
                    "name": "upperfloor_chime_volume",
                    "type": "Dimmer",
                    "label": "Lautstärke",
                    "category": "SoundVolume",
                    "tags": ["Control", "SoundVolume"],
                    "groupNames": ["upperfloor_chime"],
                },
            ],
        }

        def get_item_raw(name):
            if name == "upperfloor_chime":
                return self.old_raw
            raise ValueError(f"Item with name '{name}' not found")

        self.client.get_item_raw.side_effect = get_item_raw
        self.client.get_all_rules_raw.return_value = []
        self.client.base_url = "http://test.local"
        self.client.session.put.return_value = MagicMock(raise_for_status=MagicMock())

    @patch("openhab_mcp.rename.diagnose_item", return_value=EMPTY_DIAGNOSIS)
    def test_dry_run_plan_lists_members_to_regroup(self, _mock_diagnose):
        result = rename_item("upperfloor_chime", "chime_uppercorridor", self.client, dry_run=True)
        self.assertEqual(
            set(result["plan"]["regroup_members"]),
            {"upperfloor_chime_playchime", "upperfloor_chime_volume"},
        )
        self.client.session.put.assert_not_called()

    @patch("openhab_mcp.rename.diagnose_item", return_value=EMPTY_DIAGNOSIS)
    def test_execute_repoints_member_groupnames(self, _mock_diagnose):
        result = rename_item("upperfloor_chime", "chime_uppercorridor", self.client, dry_run=False)

        self.assertEqual(result["errors"], [])

        put_calls = {
            call.args[0]: call.kwargs["json"]
            for call in self.client.session.put.call_args_list
        }

        member_a_url = "http://test.local/rest/items/upperfloor_chime_playchime"
        member_b_url = "http://test.local/rest/items/upperfloor_chime_volume"
        self.assertIn(member_a_url, put_calls)
        self.assertIn(member_b_url, put_calls)
        self.assertEqual(put_calls[member_a_url]["groupNames"], ["chime_uppercorridor"])
        self.assertEqual(put_calls[member_b_url]["groupNames"], ["chime_uppercorridor"])

        self.assertTrue(
            any("regrouped 'upperfloor_chime_playchime'" in s for s in result["steps_completed"])
        )
        self.client.delete_item.assert_called_once_with("upperfloor_chime")


if __name__ == "__main__":
    unittest.main()
