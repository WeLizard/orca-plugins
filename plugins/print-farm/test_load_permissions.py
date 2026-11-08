"""Load-time permission request covers normal use without breaking registration."""
import json
import tempfile
import types
import unittest
from unittest import mock

from test_printers_plugin import _load_plugin


class LoadPermissionsTest(unittest.TestCase):
    def setUp(self):
        self.plugin = _load_plugin()
        self.storage = tempfile.TemporaryDirectory(prefix="print-farm-permissions-")
        self.addCleanup(self.storage.cleanup)
        self.calls = []
        self.plugin.orca.host.plugin = types.SimpleNamespace(storage=lambda: self.storage.name)
        self.discovery = [str(self.plugin.BAMBU_DISCOVERY_ADDRESS)]
        # An older install folder is only looked at when the test makes one.
        self.plugin.PLUGIN_DIR = self.storage.name + "-no-older-install"

    def save(self, cards):
        with open(self.storage.name + "/printers.json", "w", encoding="utf-8") as fh:
            json.dump(cards, fh)

    def host(self, refuse_network=None, refuse_socket=None):
        def request(**kwargs):
            self.calls.append(kwargs)
            if refuse_network and "network" in kwargs:
                raise refuse_network
            if refuse_socket and "network_socket" in kwargs:
                raise refuse_socket
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
            "network_socket": self.discovery,
            "threading": True,
        }])

    def test_host_without_network_grants_still_gets_the_thread_request(self):
        self.save([{"url": "http://192.168.1.20"}])
        self.host(refuse_network=TypeError("unexpected keyword argument 'network'"))
        self.plugin.declare_host_permissions()
        self.assertEqual(self.calls[-1], {"network_socket": self.discovery, "threading": True})

    def test_a_rejected_address_never_blocks_loading(self):
        self.save([{"url": "http://192.168.1.20"}])
        self.host(refuse_network=ValueError("requires an http:// or https:// URL with a host"))
        self.plugin.declare_host_permissions()
        self.assertEqual(self.calls[-1], {"network_socket": self.discovery, "threading": True})

    def test_a_host_without_socket_grants_still_gets_the_thread_request(self):
        self.save([{"url": "http://192.168.1.20"}])
        self.host(refuse_network=TypeError("unexpected keyword argument"),
                  refuse_socket=TypeError("unexpected keyword argument 'network_socket'"))
        self.plugin.declare_host_permissions()
        self.assertEqual(self.calls[-1], {"threading": True})

    def test_no_store_still_declares_discovery_and_threads(self):
        self.host()
        self.plugin.declare_host_permissions()
        self.assertEqual(self.calls, [{"network_socket": self.discovery, "threading": True}])

    def test_printers_waiting_in_an_older_folder_are_declared_too(self):
        legacy = tempfile.TemporaryDirectory(prefix="print-farm-legacy-")
        self.addCleanup(legacy.cleanup)
        with open(legacy.name + "/printers.json", "w", encoding="utf-8") as fh:
            json.dump([{"url": "http://10.0.0.8:7125"}, {"url": "http://192.168.1.20"}], fh)
        self.save([{"url": "http://192.168.1.20"}])
        self.plugin.PLUGIN_DIR = legacy.name
        self.host()
        self.plugin.declare_host_permissions()
        self.assertEqual(self.calls[0]["network"], ["http://192.168.1.20", "http://10.0.0.8"])

    def test_the_declared_discovery_address_is_the_one_the_listener_binds(self):
        sock = mock.MagicMock()
        sock.recvfrom.side_effect = OSError()
        with mock.patch.object(self.plugin.socket, "socket", return_value=sock):
            self.plugin.ssdp_bambu_names(0.1)
        (address,), _ = sock.bind.call_args
        self.assertEqual(str(address), self.discovery[0])


if __name__ == "__main__":
    unittest.main()
