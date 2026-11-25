import os
import unittest
from unittest import mock

from openhab_mcp.secrets import PLACEHOLDER, SecretMasker, is_secret_name

UNIFI_CONTROLLER_TYPE = {
    "configParameters": [
        {"name": "host"},
        {"name": "username"},
        {"name": "password", "context": "password"},
    ]
}


def thing(type_uid, config):
    return {"UID": f"{type_uid}:x", "thingTypeUID": type_uid, "configuration": dict(config)}


class TestSecretNames(unittest.TestCase):
    def test_patterns(self):
        for name in ["password", "apiKey", "api_key", "token", "clientSecret", "pin", "accessKey"]:
            self.assertTrue(is_secret_name(name), name)
        for name in ["host", "port", "username", "refresh", "pinNumber", "keyboardLayout"]:
            self.assertFalse(is_secret_name(name), name)

    def test_env_overrides(self):
        with mock.patch.dict(os.environ, {"OPENHAB_MCP_SECRET_PARAMS": "serial",
                                          "OPENHAB_MCP_NON_SECRET_PARAMS": "token"}):
            self.assertTrue(is_secret_name("serial"))
            self.assertFalse(is_secret_name("token"))


class TestSecretMasker(unittest.TestCase):
    def setUp(self):
        types = {"unifi:controller": UNIFI_CONTROLLER_TYPE}
        self.fetch = mock.Mock(side_effect=lambda uid: types.get(uid))
        self.masker = SecretMasker(self.fetch)

    def test_masks_password_and_token_but_not_flags(self):
        t = self.masker.mask_thing(thing("unifi:controller", {"host": "10.0.0.1", "password": "geheim"}))
        self.assertEqual(t["configuration"], {"host": "10.0.0.1", "password": PLACEHOLDER})
        nvr = self.masker.mask_thing(thing("unifi:nvr", {"token": "abc", "tokenAutoManaged": True}))
        self.assertEqual(nvr["configuration"], {"token": PLACEHOLDER, "tokenAutoManaged": True})

    def test_declared_password_context_with_unusual_name(self):
        masker = SecretMasker(lambda uid: {"configParameters": [{"name": "zugang", "context": "password"}]})
        t = masker.mask_thing(thing("x:y", {"zugang": "geheim"}))
        self.assertEqual(t["configuration"]["zugang"], PLACEHOLDER)

    def test_variable_reference_and_empty_stay_visible(self):
        t = self.masker.mask_thing(thing("unifi:controller", {"password": "${env:UNIFI_PW}", "apiKey": ""}))
        self.assertEqual(t["configuration"], {"password": "${env:UNIFI_PW}", "apiKey": ""})

    def test_disabled(self):
        with mock.patch.dict(os.environ, {"OPENHAB_MCP_REDACT_SECRETS": "false"}):
            t = self.masker.mask_thing(thing("unifi:controller", {"password": "geheim"}))
        self.assertEqual(t["configuration"]["password"], "geheim")

    def test_thing_type_fetched_once(self):
        self.masker.mask_thing(thing("unifi:controller", {"password": "a"}))
        self.masker.mask_thing(thing("unifi:controller", {"password": "b"}))
        self.assertEqual(self.fetch.call_count, 1)

    def test_restore_placeholders(self):
        stored = {"host": "10.0.0.1", "password": "geheim"}
        new = {"host": "10.0.0.2", "password": PLACEHOLDER, "token": PLACEHOLDER}
        self.assertEqual(SecretMasker.restore_placeholders(new, stored),
                         {"host": "10.0.0.2", "password": "geheim"})


if __name__ == "__main__":
    unittest.main()


class TestClientSecretsAndThings(unittest.TestCase):
    """Client-level behaviour with a mocked requests session."""

    def setUp(self):
        from openhab_mcp.openhab_client import OpenHABClient
        self.client = OpenHABClient("http://oh")
        self.session = mock.MagicMock()
        self.client.session = self.session
        self.stored = {
            "UID": "unifi:controller:c1", "thingTypeUID": "unifi:controller",
            "configuration": {"host": "10.0.0.1", "password": "geheim"},
        }

        def get(url, **kwargs):
            r = mock.MagicMock()
            r.status_code = 200
            if url.endswith("/rest/thing-types/unifi%3Acontroller"):
                r.json.return_value = UNIFI_CONTROLLER_TYPE
            elif "/rest/things/" in url:
                import copy
                r.json.return_value = copy.deepcopy(self.stored)
            elif url.endswith("/rest/links"):
                r.json.return_value = [
                    {"itemName": "Foo", "channelUID": "a:b:c:ch1"},
                    {"itemName": "Foo", "channelUID": "a:b:c:grp#ch2"},
                    {"itemName": "FooBar", "channelUID": "a:b:c:ch3"},
                ]
            else:
                r.status_code = 404
            return r

        self.session.get.side_effect = get
        ok = mock.MagicMock(status_code=200)
        ok.json.return_value = {"statusInfo": {"status": "UNINITIALIZED", "statusDetail": "DISABLED"}}
        self.session.put.return_value = ok
        self.session.delete.return_value = mock.MagicMock(status_code=200)

    def test_get_thing_is_masked(self):
        self.assertEqual(self.client.get_thing("unifi:controller:c1")["configuration"]["password"], PLACEHOLDER)

    def test_update_thing_keeps_stored_secret(self):
        from openhab_mcp.models import ThingUpdate
        self.client.update_thing(ThingUpdate(UID="unifi:controller:c1",
                                             configuration={"host": "10.0.0.2", "password": PLACEHOLDER}))
        sent = self.session.put.call_args.kwargs["json"]["configuration"]
        self.assertEqual(sent, {"host": "10.0.0.2", "password": "geheim"})

    def test_delete_item_removes_only_own_links(self):
        result = self.client.delete_item("Foo")
        self.assertEqual(result["links_removed"], ["a:b:c:ch1", "a:b:c:grp#ch2"])
        urls = [c.args[0] for c in self.session.delete.call_args_list]
        self.assertIn("http://oh/rest/links/Foo/a:b:c:grp%23ch2", urls)
        self.assertNotIn("http://oh/rest/links/FooBar/a:b:c:ch3", urls)

    def test_delete_item_keep_links(self):
        result = self.client.delete_item("Foo", delete_links=False)
        self.assertEqual(result["links_removed"], [])
        self.assertEqual(self.session.delete.call_count, 1)

    def test_set_thing_enabled(self):
        result = self.client.set_thing_enabled("unifi:controller:c1", False)
        call = self.session.put.call_args
        self.assertTrue(call.args[0].endswith("/rest/things/unifi%3Acontroller%3Ac1/enable"))
        self.assertEqual(call.kwargs["data"], "false")
        self.assertEqual(result["statusInfo"]["statusDetail"], "DISABLED")
