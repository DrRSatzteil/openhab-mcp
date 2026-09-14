import unittest
from unittest.mock import MagicMock

from openhab_mcp.batch import update_items


class TestUpdateItemsLiveFetch(unittest.TestCase):
    """Regression test: update_items must merge against the item's live state,
    not the (possibly stale) cached AdminInventory snapshot — otherwise a
    group/tag added by an earlier write since the last refresh_inventory()
    gets silently dropped by the merge."""

    def setUp(self):
        self.inventory = MagicMock()
        self.inventory.size = 1
        self.inventory.get.return_value = {"camera_carport_snapshot"}

        # The cache is stale: it still thinks groupNames is empty, even though
        # the item was already moved into "camera_carport" by an earlier write
        # that never triggered a refresh_inventory().
        self.stale_cached_item = {
            "name": "camera_carport_snapshot",
            "type": "Image",
            "label": "Snapshot Kamera Carport",
            "category": "camera",
            "tags": [],
            "groupNames": [],
            "metadata": {},
        }
        self.inventory.get_item.return_value = self.stale_cached_item

        # The live item (fetched fresh from openHAB) reflects the real current
        # state: already a member of camera_carport.
        self.live_item = {
            "name": "camera_carport_snapshot",
            "type": "Image",
            "label": "Snapshot Kamera Carport",
            "category": "camera",
            "tags": [],
            "groupNames": ["camera_carport"],
            "metadata": {},
        }
        self.client = MagicMock()
        self.client.get_item_raw.return_value = self.live_item
        self.client.base_url = "http://test.local"
        self.client.session.put.return_value = MagicMock(raise_for_status=MagicMock())

    def test_dry_run_merges_against_live_state_not_stale_cache(self):
        result = update_items(
            patch={"groupNames": ["nohistory"]},
            inventory=self.inventory,
            client=self.client,
            dry_run=True,
            merge=True,
        )

        self.assertEqual(result["will_change"], 1)
        entry = result["plan"][0]
        # Must be the union of the LIVE groupNames + the patch, i.e. both
        # camera_carport (only visible live) and nohistory (from the patch).
        self.assertEqual(sorted(entry["new_payload"]["groupNames"]), ["camera_carport", "nohistory"])
        self.client.get_item_raw.assert_called_with("camera_carport_snapshot")

    def test_execute_uses_live_fetched_type_not_cache(self):
        result = update_items(
            patch={"groupNames": ["nohistory"]},
            inventory=self.inventory,
            client=self.client,
            dry_run=False,
            merge=True,
        )

        self.assertEqual(result["errors"], [])
        put_call = self.client.session.put.call_args_list[0]
        self.assertEqual(put_call.kwargs["json"]["type"], "Image")
        self.assertEqual(
            sorted(put_call.kwargs["json"]["groupNames"]), ["camera_carport", "nohistory"]
        )
        # The stale cache's get_item must never be consulted for the merge baseline.
        self.inventory.get_item.assert_not_called()


if __name__ == "__main__":
    unittest.main()
