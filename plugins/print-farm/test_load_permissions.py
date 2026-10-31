"""Load-time permission request covers saved printers without breaking registration."""
import json
import tempfile
import types
import unittest

from test_printers_plugin import _load_plugin


class LoadPermissionsTest(unittest.TestCase):
    def setUp(self):
        self.plugin = _load_plugin()
        self.storage = tempfile.TemporaryDirectory(prefix="print-farm-permissions-")
        self.addCleanup(self.storage.cleanup)
        self.calls = []
        self.plugin.orca.host.plugin = types.SimpleNamespace(storage=lambda: self.storage.name)

    def save(self, cards):
        with open(self.storage.name + "/printers.json", "w", encoding="utf-8") as fh:
            json.dump(cards, fh)

    def host(self, refuse_network=None):
        def request(**kwargs):
            self.calls.append(kwargs)
            if refuse_network and "network" in kwargs:
                raise refuse_network
        self.plugin.orca.request_permissions = request

    def test_saved_printers_are_requested_once_as_origins(self):
        self.save([
            {"url": "192.168.1.20:7125/"}, {"url": "http://192.168.1.20"},
            {"url": "https://voron.local"}, {"url": "http://[fe80::1]:80"},
            {"url": "ftp://bad.example"}, {"url": "http://bad host"}, {"url": 7}, "junk",
        ])
        self.host()
        self.plugin.declare_host_permissions()
        self.assertEqual(self.calls, [{
            "network": ["http://192.168.1.20", "https://voron.local", "http://[fe80::1]"],
            "threading": True,
        }])

    def test_host_without_network_grants_still_gets_the_thread_request(self):
        self.save([{"url": "http://192.168.1.20"}])
        self.host(refuse_network=TypeError("unexpected keyword argument 'network'"))
        self.plugin.declare_host_permissions()
        self.assertEqual(self.calls[-1], {"threading": True})

    def test_a_rejected_address_never_blocks_loading(self):
        self.save([{"url": "http://192.168.1.20"}])
        self.host(refuse_network=ValueError("requires an http:// or https:// URL with a host"))
        self.plugin.declare_host_permissions()
        self.assertEqual(self.calls[-1], {"threading": True})

    def test_no_store_means_threads_only(self):
        self.host()
        self.plugin.declare_host_permissions()
        self.assertEqual(self.calls, [{"threading": True}])


if __name__ == "__main__":
    unittest.main()
