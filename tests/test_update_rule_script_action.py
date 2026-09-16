import copy
import unittest
from unittest.mock import MagicMock

from openhab_mcp.openhab_client import OpenHABClient


class TestUpdateRuleScriptActionPreservesOtherActions(unittest.TestCase):
    """Regression test: update_rule_script_action must not drop sibling actions.

    update_rule()'s merge-by-id treats a passed `actions` list as the complete
    desired set and implicitly deletes any existing action whose id is absent
    from it. update_rule_script_action used to pass only the one action being
    edited, so every other action in the rule (e.g. a second ItemCommandAction)
    was silently deleted on every call.
    """

    def setUp(self):
        self.client = OpenHABClient(base_url="http://test.local", api_token="x")

        self.rule = {
            "uid": "0aefa706d6",
            "actions": [
                {
                    "id": "1",
                    "type": "script.ScriptAction",
                    "configuration": {"type": "application/javascript", "script": "old script"},
                },
                {
                    "id": "4",
                    "type": "core.ItemCommandAction",
                    "configuration": {"itemName": "doorbell_notification_expiry", "command": "ON"},
                },
            ],
            "triggers": [],
            "conditions": [],
        }
        # get_rule is called repeatedly (once by update_rule_script_action itself,
        # once inside update_rule, once more for the return value) and update_rule
        # mutates the dict it receives in place — return a fresh copy each time.
        self.client.get_rule = MagicMock(side_effect=lambda uid: copy.deepcopy(self.rule))
        self.client.session.put = MagicMock(return_value=MagicMock(raise_for_status=MagicMock()))

    def test_sibling_action_survives_script_update(self):
        self.client.update_rule_script_action(
            rule_uid="0aefa706d6",
            action_id="1",
            script_type="application/javascript",
            script_content="new script",
        )

        put_call = self.client.session.put.call_args
        sent_actions = put_call.kwargs["json"]["actions"]
        self.assertEqual(len(sent_actions), 2)

        by_id = {a["id"]: a for a in sent_actions}
        self.assertEqual(by_id["1"]["configuration"]["script"], "new script")
        self.assertEqual(by_id["4"]["type"], "core.ItemCommandAction")
        self.assertEqual(by_id["4"]["configuration"]["itemName"], "doorbell_notification_expiry")

    def test_unknown_action_id_raises(self):
        with self.assertRaises(ValueError):
            self.client.update_rule_script_action(
                rule_uid="0aefa706d6",
                action_id="does-not-exist",
                script_type="application/javascript",
                script_content="new script",
            )


if __name__ == "__main__":
    unittest.main()
