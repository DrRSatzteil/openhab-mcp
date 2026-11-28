import unittest
from unittest import mock

from openhab_mcp.openhab_client import OpenHABClient, TEXT_PLAIN_UTF8


class TestTextEncoding(unittest.TestCase):
    def setUp(self):
        self.client = OpenHABClient("http://oh")
        self.session = mock.MagicMock()
        self.client.session = self.session
        ok = mock.MagicMock(status_code=200)
        self.session.post.return_value = ok
        self.session.put.return_value = ok

    def test_get_item_state_decodes_utf8(self):
        r = mock.MagicMock(status_code=200)
        r.content = "21.5 °C".encode("utf-8")
        r.text = r.content.decode("iso-8859-1")  # what requests would guess without charset
        self.session.get.return_value = r
        self.assertEqual(self.client.get_item_state("t"), "21.5 °C")

    def test_send_command_sends_utf8(self):
        self.client.send_command("s", "Küche 20 €")
        kwargs = self.session.post.call_args.kwargs
        self.assertEqual(kwargs["data"], "Küche 20 €".encode("utf-8"))
        self.assertEqual(kwargs["headers"]["Content-Type"], TEXT_PLAIN_UTF8)

    def test_update_item_state_sends_utf8(self):
        self.client.update_item_state("t", "21.5 °C")
        kwargs = self.session.put.call_args.kwargs
        self.assertEqual(kwargs["data"], "21.5 °C".encode("utf-8"))
        self.assertEqual(kwargs["headers"]["Content-Type"], TEXT_PLAIN_UTF8)


if __name__ == "__main__":
    unittest.main()
