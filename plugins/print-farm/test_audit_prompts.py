"""Print Farm shows no OrcaSlicer permission dialog in normal use.

OrcaSlicer 2.5 runs a Python audit hook: every audited action a plugin takes that
is not already granted opens a Yes/No dialog. These tests install a model of that
hook (``orca_audit_sim``) and run the real plugin against loopback emulators. The
host is started like OrcaSlicer starts it: the plugin loads and declares what it
needs (one combined dialog, answered Yes), then every later action runs under the
audit and must not ask anything.
"""

import base64
import gc
import hashlib
import http.client
import io
import http.server
import json
import os
import pathlib
import shutil
import socket
import ssl
import struct
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.parse
import urllib.request
from unittest import mock

import orca_audit_sim as audit
from test_connections_integration import _Base
from test_creality_adapter import _HttpServer, _UploadHandler, _WsHandler, _WsServer, frame
from test_creality_adapter import fixture as creality_fixture
from test_duet_adapter import Emu, PASSWORD as DUET_PASSWORD
from test_flashforge_adapter import CHECK as FF_CHECK, SERIAL as FF_SERIAL
from test_flashforge_adapter import _ApiHandler as _FfApiHandler, _ApiServer as _FfApiServer
from test_flashforge_adapter import fixture as ff_fixture
from test_printers_plugin import _MemoryCredentials, _load_plugin

def quiet_collector(testcase):
    """Stop the collector inside a test: a finalizer of another test's temporary folder
    could otherwise run while this test is being audited and be blamed on it."""
    gc.collect()
    gc.disable()
    testcase.addCleanup(gc.enable)


def urllib_open(url):
    with urllib.request.urlopen(url, timeout=5) as response:
        response.read()


GCODE = b"G28\nG1 X10\n"
PNG =b"\x89PNG\r\n\x1a\nthumbnail"


class _FarmServer:
    """Loopback Moonraker, OctoPrint and Repetier in one server; records every request."""

    def __init__(self):
        self.requests = []
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, value, code=200, raw=False):
                data = value if raw else json.dumps(value).encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def handle_any(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length)
                owner.requests.append((self.command, self.path, body))
                path = urllib.parse.urlsplit(self.path).path
                if self.command == "POST":
                    return self.reply({"result": "ok"}, 201)
                routes = {
                    "/printer/info": {"result": {"hostname": "voron"}},
                    "/printer/objects/list": {"result": {"objects": [
                        "extruder", "heater_bed", "print_stats", "display_status",
                        "virtual_sdcard", "toolhead"]}},
                    "/server/files/metadata": {"result": {"thumbnails": [
                        {"relative_path": ".thumbs/job-300x300.png", "size": 9}]}},
                    "/server/webcams/list": {"result": {"webcams": []}},
                    "/api/printer": {"state": {"text": "Operational"},
                                     "temperature": {"tool0": {"actual": 200, "target": 210},
                                                     "bed": {"actual": 60, "target": 60}}},
                    "/api/job": {"state": "Operational", "progress": {"completion": 42.0,
                                 "printTime": 60, "printTimeLeft": 90},
                                 "job": {"file": {"name": "job.gcode", "path": "job.gcode"}}},
                    "/api/files/local/job.gcode": {"thumbnail": "downloads/thumb.png"},
                    "/api/settings": {"webcam": {"webcamEnabled": False}},
                }
                if path == "/printer/objects/query":
                    return self.reply({"result": {"status": {
                        "extruder": {"temperature": 200.0, "target": 210.0},
                        "heater_bed": {"temperature": 60.0, "target": 60.0},
                        "print_stats": {"state": "standby", "filename": "job.gcode",
                                        "print_duration": 60.0},
                        "display_status": {"progress": 0.42},
                        "virtual_sdcard": {"progress": 0.42},
                        "toolhead": {"extruder": "extruder"}}}})
                if path.startswith("/server/files/gcodes/") or path == "/downloads/thumb.png":
                    return self.reply(PNG, raw=True)
                if path in routes:
                    return self.reply(routes[path])
                return self.reply({}, 404)

            do_GET = do_POST = handle_any

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.base = "http://127.0.0.1:%d" % self.port
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)


class _RepetierServer:
    """Loopback Repetier-Server bridge with two printers."""

    def __init__(self):
        self.requests = []
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def handle_any(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                owner.requests.append((self.command, self.path, body))
                route = urllib.parse.urlsplit(self.path).path.removeprefix("/proxy")
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                printers = [dict(slug=slug, name=name, active=True, online=1, job="none",
                                 paused=False, pauseState=0)
                            for slug, name in (("Alpha", "Workshop"), ("Beta", "Office"))]
                states = {slug: {"condition": 1, "activeExtruder": 0,
                                 "extruder": [{"tempRead": 21.5, "tempSet": 0}],
                                 "heatedBeds": [{"tempRead": 22, "tempSet": 0}]}
                          for slug in ("Alpha", "Beta")}
                if route == "/printer/info":
                    value = {"software": "Repetier-Server", "version": "1.4.10"}
                elif route == "/printer/list":
                    value = {"data": printers}
                elif route.startswith("/printer/api/") and self.command == "GET":
                    value = printers if query.get("a") == ["listPrinter"] else states
                else:
                    value = {}
                data = json.dumps(value).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = handle_any

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.base = "http://127.0.0.1:%d/proxy" % self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)


def _mqtt_packet(stream):
    first = stream.read(1)
    if not first:
        return None, b""
    length, shift = 0, 0
    while True:
        byte = stream.read(1)[0]
        length |= (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            break
    return first[0], stream.read(length)


class _BambuServer:
    """Loopback MQTT-over-TLS broker that answers one status request."""

    def __init__(self, serial="SERIAL"):
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(8)
        self.port = self.listener.getsockname()[1]
        self.connections = 0
        self.serial = serial
        self.logins = 0
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while True:
            try:
                connection, _ = self.listener.accept()
            except OSError:
                return
            self.connections += 1
            threading.Thread(target=self._talk, args=(connection,), daemon=True).start()

    def _talk(self, connection):
        try:
            connection = _server_tls().wrap_socket(connection, server_side=True)
        except OSError:
            connection.close()
            return
        stream = connection.makefile("rb")
        try:
            header, _ = _mqtt_packet(stream)
            if header is None:
                return
            self.logins += 1
            connection.sendall(b"\x20\x02\x00\x00")
            _mqtt_packet(stream)
            connection.sendall(b"\x90\x03\x00\x01\x00")
            topic = ("device/%s/report" % self.serial).encode()
            report = json.dumps({"print": {"gcode_state": "IDLE", "nozzle_temper": 25,
                                           "bed_temper": 24, "printer_type": "C11"}}).encode()
            body = struct.pack("!H", len(topic)) + topic + report
            while True:
                header, _ = _mqtt_packet(stream)
                if header is None or header == 0xE0:
                    return
                if (header & 0xF0) == 0x30:
                    size = len(body)
                    length = bytes([size]) if size < 128 else bytes([(size & 127) | 128, size >> 7])
                    connection.sendall(b"\x30" + length + body)
        except (OSError, IndexError):
            return
        finally:
            connection.close()

    def close(self):
        self.listener.close()


_TLS_FIXTURE = pathlib.Path(__file__).with_name("audit_fixtures") / "loopback_tls_test_only.pem"


def _server_tls():
    """Server side of the printers' TLS: a throw-away self-signed certificate, test only."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(_TLS_FIXTURE))
    return context


def _certificate_fingerprint():
    text = _TLS_FIXTURE.read_text(encoding="ascii")
    end = text.index("-----END CERTIFICATE-----") + len("-----END CERTIFICATE-----")
    return hashlib.sha256(ssl.PEM_cert_to_DER_cert(text[:end])).hexdigest()


def _loopback_ca_context():
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    pem = _TLS_FIXTURE.read_text(encoding="ascii").split("-----END CERTIFICATE-----", 1)[0]
    context.load_verify_locations(cadata=pem + "-----END CERTIFICATE-----\n")
    return context


class _FtpServer:
    """Loopback FTP server (passive mode, STOR only) that keeps what it is sent."""

    def __init__(self):
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(4)
        self.port = self.listener.getsockname()[1]
        self.stored = {}
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        try:
            connection, _ = self.listener.accept()
            tls = _server_tls()
            connection = tls.wrap_socket(connection, server_side=True)
        except OSError:
            return
        data_listener = None
        protected = False
        stream = connection.makefile("rwb")

        def say(line):
            stream.write(line.encode() + b"\r\n")
            stream.flush()
        say("220 ready")
        try:
            for raw in stream:
                command, _, argument = raw.decode().strip().partition(" ")
                if command == "USER":
                    say("331 password")
                elif command == "PROT":
                    protected = argument == "P"
                    say("200 ok")
                elif command == "PASV":
                    data_listener = socket.socket()
                    data_listener.bind(("127.0.0.1", 0))
                    data_listener.listen(1)
                    port = data_listener.getsockname()[1]
                    say("227 Entering Passive Mode (127,0,0,1,%d,%d)" % (port >> 8, port & 255))
                elif command == "STOR":
                    say("150 go")
                    data, _ = data_listener.accept()
                    if protected:
                        data = tls.wrap_socket(data, server_side=True)
                    body = b""
                    while True:
                        block = data.recv(65536)
                        if not block:
                            break
                        body += block
                    if protected:
                        try:
                            data.unwrap()  # answer the client's close_notify
                        except OSError:
                            pass
                    data.close()
                    self.stored[argument] = body
                    say("226 done")
                elif command == "QUIT":
                    say("221 bye")
                    return
                else:
                    say("230 ok")
        finally:
            for item in (data_listener, connection):
                if item is not None:
                    item.close()

    def close(self):
        self.listener.close()


class AuditBase(_Base):
    """Starts the plugin the way OrcaSlicer does and keeps a model of its audit hook."""

    def setUp(self):
        quiet_collector(self)
        self.closers = []
        self.addCleanup(self._close_all)
        self.data_dir = tempfile.mkdtemp(prefix="orca-data-")
        self.addCleanup(shutil.rmtree, self.data_dir, True)
        self.storage = os.path.join(self.data_dir, "orca_plugins", "plugin_data",
                                    "printers-9.9.9-py3-none-any")
        os.makedirs(self.storage)
        self.root = pathlib.Path(self.storage)
        self.sim = audit.AuditSimulator(self.data_dir)
        # The operating-system store outlives a restart of OrcaSlicer.
        self.keychain = _MemoryCredentials()
        self.legacy_dir = None
        self.plugin = self.dashboard = self.outbox = None
        self.addCleanup(self.stop_host)
        self.start_host()

    def _close_all(self):
        for closer in reversed(self.closers):
            closer()
        for plugin in (self.plugin,):
            for handle in list((getattr(plugin, "_STORAGE_OWNER_HANDLES", None) or {}).values()):
                handle.close()

    # -- host ---------------------------------------------------------------
    def start_host(self):
        """Load the plugin (one declaration dialog) and run its on_load callbacks."""
        plugin = _load_plugin(lifecycle=True, pages=True, initialize_test_storage=False)
        plugin._CREDENTIAL_VAULT = plugin._pf_credentials.Vault(lambda: self.keychain)
        plugin.orca.host.plugin = types.SimpleNamespace(storage=lambda: self.storage)
        plugin.orca.request_permissions = self.sim.request_permissions
        if self.legacy_dir:
            plugin.PLUGIN_DIR = self.legacy_dir
        self.sim.load(lambda: plugin.PrintersPlugin().register_capabilities())
        self.plugin = plugin
        self.dashboard = plugin.PrintersDashboard()
        self.dashboard._post = mock.Mock(return_value=True)
        self.outbox = plugin.PrintersOutbox()
        with self.sim.audited():
            self.dashboard.on_load()
            self.outbox.on_load()
        self.assertEqual(plugin._STORAGE_DENIED,
                         bool(plugin._LEGACY_IMPORT_SOURCE or plugin._LEGACY_IMPORT_BLOCKED))

    def stop_host(self):
        if self.plugin is None:
            return
        try:
            self.dashboard._stop_action_scheduler()
        except Exception:
            pass
        self.plugin.stop_plugin_runtime()
        self.plugin = None

    def restart(self):
        """Close and reopen OrcaSlicer: declarations are made again from what is saved."""
        self.stop_host()
        self.start_host()

    # -- helpers ------------------------------------------------------------
    def settle(self, timeout=15):
        """Wait until the poll, action and camera workers have nothing left to do."""
        deadline = time.monotonic() + timeout
        quiet = 0
        while time.monotonic() < deadline:
            pools = (self.plugin.ACTION_WORKER, self.plugin.POLL_WORKERS,
                     self.plugin.CAMERA_WORKER)
            idle = all(pool._busy == 0 and pool._jobs.empty() for pool in pools)
            quiet = quiet + 1 if idle else 0
            if quiet >= 5:
                return
            time.sleep(0.05)
        self.fail("workers did not settle")

    def posted(self, key):
        return [call.args[0][key] for call in self.dashboard._post.call_args_list
                if key in call.args[0]]

    def receive(self, name="job.gcode", body=GCODE):
        """Hand a file to the plugin the way the page does, in chunks."""
        d = self.dashboard
        with self.sim.audited():
            d._do_artifact_begin({"filename": name, "size": len(body)})
            token = self.posted("artifactBegin")[-1]["token"]
            d._do_artifact_chunk({"token": token, "sequence": 0,
                                  "dataB64": base64.b64encode(body).decode(),
                                  "sha256": hashlib.sha256(body).hexdigest()})
            d._do_artifact_finish({"token": token})
        self.assertEqual(self.posted("artifactReady")[-1]["token"], token)
        return token

    def submit(self, printer, token, start=False):
        message = {"artifactToken": token, "plate": None, "use_ams": False,
                   "start": start, "targets": [self.identity(printer)]}
        with self.sim.audited():
            self.dashboard._submit_mass_print(message)
            self.assertTrue(self.dashboard._action_scheduler.wait_for_idle(15))

    def refresh_and_poll(self):
        with self.sim.audited():
            self.dashboard.on_message({"type": "ready"})
            self.settle()
            self.dashboard.on_message({"type": "poll"})
            self.settle()
            self.dashboard.on_message({"type": "poll"})
            self.settle()

    def statuses_of(self, backend):
        return [item["status"] for item in self.posted("printerStatus")
                if item["status"].get("backend", backend) == backend]

    def assert_quiet(self):
        self.assertEqual(self.sim.prompts, [], "\n" + self.sim.report())
        self.assertEqual(self.sim.denied, [])


class LoadTest(AuditBase):
    def test_a_fresh_install_asks_once_and_starts_silently(self):
        self.assertLessEqual(len(self.sim.declaration_dialogs), 1)
        self.assert_quiet()

    def test_saved_printers_are_declared_in_that_one_dialog(self):
        farm = _FarmServer()
        self.closers.append(farm.close)
        self.add_printer(type="moonraker", url=farm.base)
        self.add_printer(type="octoprint", url=farm.base, apikey="key")
        self.sim.declaration_dialogs.clear()
        self.sim.grants["network_http"].clear()
        self.sim.grants["network_socket"].clear()
        self.sim.grants["threading"].clear()
        self.restart()
        self.assertEqual(len(self.sim.declaration_dialogs), 1)
        dialog = self.sim.declaration_dialogs[0]
        self.assertEqual(dialog["network_http"], ["http://127.0.0.1"])
        self.assertEqual(dialog["threading"], ["thread"])
        self.assert_quiet()


class PollAndUploadTest(AuditBase):
    def serve(self):
        farm = _FarmServer()
        self.closers.append(farm.close)
        return farm

    def test_moonraker_poll_thumbnail_and_upload(self):
        farm = self.serve()
        printer = self.add_printer(type="moonraker", url=farm.base)
        self.restart()
        self.refresh_and_poll()
        status = self.statuses_of("moonraker")[-1]
        self.assertTrue(status["online"])
        self.assertTrue(farm.requests)
        paths = {urllib.parse.urlsplit(p).path for _, p, _ in farm.requests}
        self.assertIn("/printer/objects/query", paths)
        self.assertIn("/server/files/metadata", paths)
        token = self.receive()
        self.submit(self.current(printer), token)
        self.assertTrue([r for r in farm.requests if r[1] == "/server/files/upload"])
        self.assert_quiet()

    def test_octoprint_poll_thumbnail_and_upload(self):
        farm = self.serve()
        printer = self.add_printer(type="octoprint", url=farm.base, apikey="key")
        self.restart()
        self.refresh_and_poll()
        self.assertTrue(self.statuses_of("octoprint")[-1]["online"])
        token = self.receive()
        self.submit(self.current(printer), token)
        self.assertTrue([r for r in farm.requests if r[1] == "/api/files/local"])
        self.assert_quiet()

    def test_duet_poll_and_upload(self):
        emu = Emu("rrf", "model_idle_single", password=DUET_PASSWORD)
        self.closers.append(emu.close)
        printer = self.add_printer(type="duet", url=emu.base, apikey=DUET_PASSWORD)
        self.restart()
        self.refresh_and_poll()
        token = self.receive()
        self.submit(self.current(printer), token)
        self.assertEqual(emu.files.get("job.gcode"), GCODE)
        self.assert_quiet()


    def test_repetier_poll_and_upload(self):
        server = _RepetierServer()
        self.closers.append(server.close)
        printer = self.add_printer(type="repetier", url=server.base, repetier_slug="Alpha",
                                   apikey="test-key")
        self.restart()
        self.refresh_and_poll()
        self.assertTrue(self.statuses_of("repetier")[-1]["online"])
        token = self.receive()
        self.submit(self.current(printer), token)
        self.assertTrue([r for r in server.requests if r[0] == "POST"])
        self.assert_quiet()

    def test_creality_status_over_the_websocket(self):
        config = {"handshakes": [], "received": []}
        payload = json.dumps(creality_fixture("idle")).encode()
        config["reply"] = lambda _body: [frame(0x1, payload)]
        ws = _WsServer(("127.0.0.1", 0), _WsHandler)
        ws.cfg = config
        up = _HttpServer(("127.0.0.1", 0), _UploadHandler)
        up.cfg = {"posts": []}
        for server in (ws, up):
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.closers.extend([server.server_close, server.shutdown])
        printer = self.add_printer(type="crealityprint", ws_port=ws.server_address[1],
                                   url="http://127.0.0.1:%d/upload" % up.server_address[1])
        self.restart()
        self.refresh_and_poll()
        self.assertTrue(self.statuses_of("crealityprint")[-1]["online"])
        self.assertTrue(config["handshakes"])
        del printer
        self.assert_quiet()

    def test_creality_status_by_host_name(self):
        config = {"handshakes": [], "received": []}
        payload = json.dumps(creality_fixture("idle")).encode()
        config["reply"] = lambda _body: [frame(0x1, payload)]
        ws = _WsServer(("127.0.0.1", 0), _WsHandler)
        ws.cfg = config
        up = _HttpServer(("127.0.0.1", 0), _UploadHandler)
        up.cfg = {"posts": []}
        for server in (ws, up):
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.closers.extend([server.server_close, server.shutdown])
        self.add_printer(type="crealityprint", ws_port=ws.server_address[1],
                         url="http://localhost:%d/upload" % up.server_address[1])
        self.restart()
        self.refresh_and_poll()
        self.assertTrue(self.statuses_of("crealityprint")[-1]["online"])
        self.assertTrue(config["handshakes"])
        self.assert_quiet()

    def test_flashforge_status(self):
        config = {"requests": [], "mode": {}, "serial": FF_SERIAL, "check": FF_CHECK,
                  "replies": {"/detail": ff_fixture("detail_5m_ready")}}
        server = _FfApiServer(("127.0.0.1", 0), _FfApiHandler)
        server.cfg = config
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.closers.extend([server.server_close, server.shutdown])
        self.add_printer(type="flashforge", serial=FF_SERIAL, apikey=FF_CHECK,
                         url="http://127.0.0.1:%d" % server.server_address[1])
        self.restart()
        self.refresh_and_poll()
        self.assertTrue(self.statuses_of("flashforge")[-1]["online"])
        self.assertTrue(config["requests"])
        self.assert_quiet()


class BambuTest(AuditBase):
    def test_vendor_ca_status_without_a_manual_pin_checks_the_serial_before_login(self):
        serial = "print-farm-loopback-test"
        broker = _BambuServer(serial)
        self.closers.append(broker.close)
        printer = self.add_printer(type="bambu", url="127.0.0.1", serial=serial,
                                   access_code="12345678")
        self.restart()
        self.plugin.BAMBU_MQTT_PORT = broker.port
        self.plugin._BAMBU_TLS_PORTS = {"mqtt": broker.port}
        self.plugin._SSL_BAMBU_CA = _loopback_ca_context()
        with self.sim.audited():
            status = self.plugin.poll_printer(self.current(printer))
            self.assertTrue(status["online"], status)
            self.assertEqual(broker.logins, 1)
            wrong = self.plugin.load_manual_printers()[0]
            wrong["serial"] = "WRONGSERIAL"
            self.assertTrue(self.plugin.save_manual_printers([wrong]))
            status = self.plugin.poll_printer(self.current(printer))
            with self.assertRaises(self.plugin.BambuTlsError) as raised:
                self.plugin._probe_bambu_certificate(self.current(printer), "mqtt")
        self.assertEqual(status["error_code"], "tls_identity_mismatch")
        self.assertEqual(raised.exception.code, "tls_identity_mismatch")
        self.assertEqual(broker.logins, 1)
        with self.sim.audited():
            wrong["serial"] = ""
            self.assertTrue(self.plugin.save_manual_printers([wrong]))
            status = self.plugin.poll_printer(self.current(printer))
            self.assertTrue(status["online"], status)
            self.assertEqual(self.plugin.load_manual_printers()[0]["serial"], serial)
        self.assert_quiet()

    def check_status_and_certificate(self, host):
        broker = _BambuServer()
        self.closers.append(broker.close)
        card = self.add_printer(type="bambu", url=host, serial="SERIAL", access_code="12345678")
        digest = _certificate_fingerprint()
        stored = self.plugin.load_manual_printers()[0]
        stored.update(tls_pins={"mqtt": digest, "ftps": digest}, tls_trust_revision=1)
        self.assertTrue(self.plugin.save_manual_printers([stored]))
        self.restart()
        self.plugin.BAMBU_MQTT_PORT = broker.port
        self.plugin._BAMBU_TLS_PORTS = {"mqtt": broker.port, "ftps": broker.port}
        printer = self.current(card)
        with self.sim.audited():
            status = self.plugin.poll_printer(dict(printer))
            fingerprint = self.plugin._probe_bambu_certificate(printer, "mqtt")
        self.assertTrue(status["online"], status)
        self.assertEqual(status["state"], "idle")
        self.assertEqual(fingerprint, _certificate_fingerprint())
        deadline = time.monotonic() + 5
        while broker.connections < 3 and time.monotonic() < deadline:
            time.sleep(0.01)  # the broker counts a connection when its thread accepts it
        self.assertEqual(broker.connections, 3)  # status, CA check, explicit legacy read
        self.assert_quiet()

    def test_status_and_certificate_read(self):
        self.check_status_and_certificate("127.0.0.1")

    def test_status_and_certificate_read_by_host_name(self):
        self.check_status_and_certificate("localhost")

    def check_ftps_upload(self, host, vendor_ca=False):
        server = _FtpServer()
        self.closers.append(server.close)
        serial = "print-farm-loopback-test" if vendor_ca else "SERIAL"
        card = self.add_printer(type="bambu", url=host, serial=serial, access_code="12345678")
        if not vendor_ca:
            digest = _certificate_fingerprint()
            stored = self.plugin.load_manual_printers()[0]
            stored.update(tls_pins={"mqtt": digest, "ftps": digest}, tls_trust_revision=1)
            self.assertTrue(self.plugin.save_manual_printers([stored]))
        self.restart()
        if vendor_ca:
            self.plugin._SSL_BAMBU_CA = _loopback_ca_context()
        printer = self.current(card)
        context = self.plugin._bambu_tls_context(printer, "ftps")
        ftp = self.plugin._implicit_ftps_class()(context=context)
        ftp._bambu_printer = printer
        with self.sim.audited():
            ftp.connect(host, server.port, timeout=5)
            ftp.login("bblp", "12345678")
            ftp.prot_p()
            ftp.storbinary("STOR job.gcode", io.BytesIO(GCODE))
            ftp.quit()
        self.assertEqual(server.stored, {"job.gcode": GCODE})
        self.assert_quiet()

    def test_ftps_upload(self):
        self.check_ftps_upload("127.0.0.1")

    def test_ftps_upload_by_host_name(self):
        self.check_ftps_upload("localhost")

    def test_ftps_upload_with_vendor_ca_and_no_pin(self):
        self.check_ftps_upload("127.0.0.1", vendor_ca=True)

    def test_lan_discovery_listens_on_the_declared_socket(self):
        with self.sim.audited():
            self.plugin.ssdp_bambu_names(0.1)
        self.assert_quiet()


class StorageTest(AuditBase):
    def test_outbox_history_and_credentials_stay_inside_the_data_folder(self):
        gcode_dir = tempfile.mkdtemp(prefix="orca-gcode-")
        self.addCleanup(shutil.rmtree, gcode_dir, True)
        gcode = os.path.join(gcode_dir, "fresh.gcode")
        with open(gcode, "w", encoding="utf-8") as fh:
            fh.write("; printer_settings_id = Voron 2.4 350\nG28\n")
        ctx = types.SimpleNamespace(step="post-process", gcode_path=gcode,
                                    output_name="fresh.gcode", host="File")
        # While a post-process step runs, the host adds the G-code folder as a root.
        self.sim.roots.append(gcode_dir)
        try:
            with self.sim.audited():
                result = self.outbox.execute(ctx)
        finally:
            self.sim.roots.remove(gcode_dir)
        self.assertEqual(result[0], "success")
        with self.sim.audited():
            self.assertTrue(self.plugin.load_outbox_index())
            self.plugin.load_print_history()
            printer = self.plugin._ensure_printer_identity(
                {"name": "P", "type": "octoprint", "url": "http://127.0.0.1:1", "apikey": "k"})
            self.plugin.save_manual_printers([printer])
            self.plugin.hydrate_credentials(self.plugin.load_manual_printers()[0])
        self.assert_quiet()

    def test_a_printer_added_after_load_asks_once_for_its_host(self):
        """Declarations are only made while loading, so a host first seen later cannot be
        declared. Its answer is one question about the host, not one per URL."""
        farm = _FarmServer()
        self.closers.append(farm.close)
        self.add_printer(type="moonraker", url=farm.base)
        self.assertEqual(self.sim.prompts, [])
        with self.sim.audited():
            for _ in range(3):
                self.dashboard.on_message({"type": "poll"})
                self.settle()
        self.assertGreater(len({p for _, p, _ in farm.requests}), 3)
        self.assertEqual([(p.event, p.targets) for p in self.sim.prompts],
                         [("http.client.connect", ["127.0.0.1"])], self.sim.report())

    def test_background_polls_wait_for_user_after_one_network_prompt(self):
        farm = _FarmServer()
        self.closers.append(farm.close)
        printer = self.add_printer(type="moonraker", url=farm.base)
        self.add_printer(type="octoprint", url=farm.base, apikey="key")
        other = farm.base.replace("127.0.0.1", "localhost")
        self.add_printer(type="moonraker", url=other)
        self.sim.block_pending_network = True
        with self.sim.audited():
            for _ in range(3):
                self.dashboard.on_message({"type": "poll"})
                self.settle()
        targets = [["127.0.0.1"], ["localhost"]]
        self.assertCountEqual([p.targets for p in self.sim.prompts], targets,
                              self.sim.report())
        self.assertEqual(self.statuses_of("moonraker")[-1]["error_code"],
                         "network_permission_denied")
        self.assertEqual(self.statuses_of("octoprint")[-1]["error_code"],
                         "network_permission_denied")
        self.assertFalse(farm.requests)

        self.sim.grants["network_http"].append("127.0.0.1")
        with self.sim.audited():
            self.dashboard.on_message({"type": "refresh"})
            self.settle()
            self.assertFalse(farm.requests)
            self.dashboard.on_message({"type": "retry-network", **self.identity(printer)})
            self.settle()
            self.dashboard.on_message({"type": "poll"})
            self.settle()
        statuses = self.posted("printerStatus")
        self.assertTrue([p["status"] for p in statuses if p["url"] == farm.base][-1]["online"])
        self.assertEqual([p["status"] for p in statuses if p["url"] == other][-1]["error_code"],
                         "network_permission_denied")
        self.assertTrue(self.statuses_of("octoprint")[-1]["online"])
        self.assertGreater(len(farm.requests), 3)
        self.assertCountEqual([p.targets for p in self.sim.prompts], targets,
                              self.sim.report())


class MoreFlowsTest(AuditBase):
    def farm_printer(self, **entry):
        farm = _FarmServer()
        self.closers.append(farm.close)
        entry.setdefault("type", "moonraker")
        return farm, self.add_printer(url=farm.base, **entry)

    def test_queued_slice_is_sent_from_the_outbox(self):
        farm, printer = self.farm_printer()
        self.restart()
        gcode_dir = tempfile.mkdtemp(prefix="orca-gcode-")
        self.addCleanup(shutil.rmtree, gcode_dir, True)
        gcode = os.path.join(gcode_dir, "fresh.gcode")
        with open(gcode, "w", encoding="utf-8") as fh:
            fh.write("; printer_settings_id = Voron\nG28\n")
        # While a post-process step runs, the host adds the G-code folder as a root.
        self.sim.roots.append(gcode_dir)
        with self.sim.audited():
            self.outbox.execute(types.SimpleNamespace(
                step="post-process", gcode_path=gcode, output_name="fresh.gcode", host="File"))
        self.sim.roots.remove(gcode_dir)
        stored = self.plugin.load_outbox_index()[0]["file"]
        target = self.identity(self.current(printer))
        with self.sim.audited():
            self.dashboard._prepare_outbox_start(
                {**target, "file": stored, "unassigned": True, "start": False})
            approval = self.posted("outboxStartApproval")[-1]
            self.dashboard._do_outbox_send(
                {**target, "file": stored, "unassigned": True, "start": False,
                 "approvalNonce": approval["nonce"], "use_ams": False,
                 "operatorChecked": True, "bedClear": False})
        self.assertTrue([r for r in farm.requests if r[1] == "/server/files/upload"])
        self.assert_quiet()

    def test_camera_address_lookup(self):
        farm, printer = self.farm_printer()
        self.restart()
        with self.sim.audited():
            self.plugin.camera_lookup(self.current(printer))
        self.assertTrue([r for r in farm.requests if r[1] == "/server/webcams/list"])
        self.assert_quiet()

    def test_orca_events_and_cleanup_timers(self):
        names = ("GCodeExportStarted", "GCodeExportFinished", "SendJobStarted", "SendJobFinished")
        enum = types.SimpleNamespace(**{n: types.SimpleNamespace(name=n) for n in names})
        self.plugin.orca.LifecycleEvent = enum
        self.plugin.ORCA_HINT_DELAY_SECONDS = 0.01
        with self.sim.audited():
            self.outbox.on_lifecycle_event(enum.GCodeExportStarted, types.SimpleNamespace(
                code=types.SimpleNamespace(name="Ok"), name="cube", path="a.gcode"))
            self.outbox.on_lifecycle_event(enum.SendJobFinished, types.SimpleNamespace(
                code=types.SimpleNamespace(name="Ok"), name="cube", device_id="X",
                source="send_job"))
            time.sleep(0.2)
        self.assert_quiet()

    def test_importing_an_older_storage_folder_does_not_start_questions(self):
        farm = _FarmServer()
        self.closers.append(farm.close)
        self.legacy_dir = os.path.join(self.data_dir, "orca_plugins", "printers")
        os.makedirs(self.legacy_dir)
        legacy_card = self.plugin._ensure_printer_identity(
            {"name": "Old", "type": "moonraker", "url": farm.base, "source": "manual"})
        with open(os.path.join(self.legacy_dir, "printers.json"), "w", encoding="utf-8") as fh:
            json.dump([legacy_card], fh)
        self.sim.declaration_dialogs.clear()
        self.sim.grants["network_http"].clear()
        self.restart()
        self.assertEqual(self.sim.declaration_dialogs[0]["network_http"], ["http://127.0.0.1"])
        with self.sim.audited():
            preview = self.plugin.legacy_import_preview()
            self.assertTrue(preview["available"], preview)
            self.assertTrue(self.plugin.import_legacy_storage(preview["confirmation"]))
        self.assertEqual(len(self.plugin.load_manual_printers()), 1)
        self.refresh_and_poll()
        self.assertTrue(farm.requests)
        self.assert_quiet()

    def test_a_printer_known_by_name_is_covered_for_the_http_backends(self):
        farm = _FarmServer()
        self.closers.append(farm.close)
        printer = self.add_printer(type="moonraker", url="http://localhost:%d" % farm.port)
        self.restart()
        self.assertEqual(self.sim.declaration_dialogs[-1]["network_http"], ["http://localhost"])
        self.refresh_and_poll()
        token = self.receive()
        self.submit(self.current(printer), token)
        self.assertTrue([r for r in farm.requests if r[1] == "/server/files/upload"])
        self.assert_quiet()


class NamedHostConnectionTest(unittest.TestCase):
    """A raw TCP connection to a host name is attributed to that name, not to its address."""

    def setUp(self):
        quiet_collector(self)
        self.data = tempfile.mkdtemp(prefix="orca-data-")
        self.addCleanup(shutil.rmtree, self.data, True)
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(4)
        self.addCleanup(self.listener.close)
        self.port = self.listener.getsockname()[1]

    def connect(self, module, host):
        sim = audit.AuditSimulator(self.data, extra_roots=[], plugin_files={module.__file__})
        sim.grants["network_http"].append("http://localhost")
        sim.grants["network_socket"].append("('0.0.0.0', 2021)")
        with sim.audited():
            sock = module._connect_tcp(host, self.port, 5)
        sock.close()
        return sim

    def test_every_module_connects_a_name_under_the_hosts_grant(self):
        import creality_adapter
        import u1_camera
        plugin = _load_plugin()
        for module in (creality_adapter, u1_camera, plugin):
            with self.subTest(module=module.__name__):
                self.assertEqual(self.connect(module, "localhost").prompts, [])

    def test_a_literal_address_connects_directly(self):
        import u1_camera
        sim = audit.AuditSimulator(self.data, extra_roots=[], plugin_files={u1_camera.__file__})
        sim.grants["network_http"].append("http://127.0.0.1")
        with sim.audited():
            u1_camera._connect_tcp("127.0.0.1", self.port, 5).close()
        self.assertEqual(sim.prompts, [])
        with mock.patch.object(u1_camera.socket, "create_connection") as connect:
            u1_camera._connect_tcp("192.0.2.9", 80, 3)
        connect.assert_called_once_with(("192.0.2.9", 80), timeout=3)


class ArtifactFilesTest(unittest.TestCase):
    """Staged files are opened by path, so the host can name every file it is asked about."""

    def setUp(self):
        quiet_collector(self)
        import artifact_transfer
        import print_artifact
        self.transfer, self.artifacts = artifact_transfer, print_artifact
        self.data = tempfile.mkdtemp(prefix="orca-data-")
        self.addCleanup(shutil.rmtree, self.data, True)
        self.incoming = os.path.join(self.data, "orca_plugins", "plugin_data", "pf", "incoming")
        self.sim = audit.AuditSimulator(self.data, extra_roots=[], plugin_files={
            artifact_transfer.__file__, print_artifact.__file__})

    def receive(self, name, body):
        manager = self.transfer.ArtifactTransferManager(self.incoming)
        token = manager.begin("owner", name, len(body))
        manager.append("owner", token, 0, body)
        return manager.finish("owner", token)

    def test_a_chunked_upload_never_leaves_a_file_unnamed(self):
        with self.sim.audited():
            received = self.receive("job.gcode", GCODE)
            self.assertEqual(pathlib.Path(received.path).read_bytes(), GCODE)
        self.assertEqual(self.sim.prompts, [], self.sim.report())

    def test_extracting_a_plate_never_leaves_a_file_unnamed(self):
        from test_print_artifact import make_3mf
        source = pathlib.Path(self.data) / "source.3mf"
        make_3mf(source)
        body = source.read_bytes()
        with self.sim.audited():
            received = self.receive("job.gcode.3mf", body)
            artifact = self.artifacts.inspect_staged_path(
                received.path, received.filename, self.incoming,
                provenance="manual_import", expected_sha256=received.sha256)
            plate = self.artifacts.stage_selected_plate(
                artifact, artifact.plate_entries[0], self.incoming)
            self.assertTrue(plate.path.exists())
        self.assertEqual(self.sim.prompts, [], self.sim.report())

    def test_staging_a_copy_never_leaves_a_file_unnamed(self):
        source = pathlib.Path(self.data) / "source.gcode"
        source.write_bytes(GCODE)
        with self.sim.audited():
            staged = self.artifacts.stage_artifact(source, self.incoming)
            self.assertTrue(staged.path.exists())
        self.assertEqual(self.sim.prompts, [], self.sim.report())


_PROVIDER_PROBE = """
import json, sys, tempfile
sys.path.insert(0, %r)
import orca_audit_sim as audit
import credential_store
sim = audit.AuditSimulator(tempfile.mkdtemp(), extra_roots=[],
                           plugin_files={credential_store.__file__})
if %r:
    credential_store.preload()  # what loading the plugin does: nothing is audited yet
with sim.audited():
    credential_store.system_backend()
print(json.dumps(len(sim.prompts)))
"""


class CredentialProviderTest(unittest.TestCase):
    def run_probe(self, preload):
        """Prompts raised by building the provider, or None without a usable provider."""
        import subprocess
        done = subprocess.run(
            [sys.executable, "-I", "-c", _PROVIDER_PROBE % (str(audit.PLUGIN_DIR), preload)],
            capture_output=True, text=True, timeout=120, check=False)
        return json.loads(done.stdout.strip().splitlines()[-1]) if done.returncode == 0 else None

    def test_importing_the_provider_at_load_keeps_the_first_credential_use_silent(self):
        loaded = self.run_probe(True)
        if loaded is None:
            self.skipTest("no usable system credential provider on this machine")
        if not self.run_probe(False):
            self.skipTest("the provider's files are inside the allowed folders here")
        self.assertEqual(loaded, 0)

    def test_preload_imports_the_provider_and_never_blocks_loading(self):
        plugin = _load_plugin()
        names = {"win32": "keyring.backends.Windows", "darwin": "keyring.backends.macOS"}
        module = names.get(sys.platform, "keyring.backends.SecretService")
        fake = types.ModuleType(module)
        fake.WinVaultKeyring = fake.Keyring = object
        with mock.patch.dict(sys.modules, {"keyring": types.ModuleType("keyring"),
                                           "keyring.backends": types.ModuleType("keyring.backends"),
                                           module: fake}):
            plugin._pf_credentials.preload()
        with mock.patch.object(plugin._pf_credentials, "preload", side_effect=ImportError("none")):
            plugin.preload_credential_provider()
        registered = []
        plugin = _load_plugin(registered=registered)
        with mock.patch.object(plugin, "preload_credential_provider") as preload:
            plugin.PrintersPlugin().register_capabilities()
        preload.assert_called_once_with()


class SimulatorModelTest(unittest.TestCase):
    """The model must itself behave like the host, or green flows prove nothing."""

    def setUp(self):
        quiet_collector(self)
        self.data = tempfile.mkdtemp(prefix="orca-data-")
        self.outside = tempfile.mkdtemp(prefix="orca-outside-")
        self.addCleanup(shutil.rmtree, self.data, True)
        self.addCleanup(shutil.rmtree, self.outside, True)
        # This file stands in for the plugin: events raised below it are judged.
        self.sim = audit.AuditSimulator(self.data, extra_roots=[],
                                        plugin_files={type(self).setUp.__code__.co_filename})
        self.farm = _FarmServer()
        self.addCleanup(self.farm.close)

    def grant(self, **buckets):
        for bucket, targets in buckets.items():
            self.sim.grants[bucket].extend(targets)

    def prompted(self):
        return [(prompt.event, prompt.targets) for prompt in self.sim.prompts]

    def test_files_inside_the_data_folder_are_free_and_outside_ask(self):
        inside = os.path.join(self.data, "orca_plugins", "x.json")
        os.makedirs(os.path.dirname(inside))
        outside = os.path.join(self.outside, "x.json")
        with self.sim.audited():
            with open(inside, "w", encoding="utf-8") as fh:
                fh.write("{}")
            os.replace(inside, inside + ".1")
            self.assertEqual(self.prompted(), [])
            with open(outside, "w", encoding="utf-8") as fh:
                fh.write("{}")
        self.assertEqual(self.prompted(), [("open", [outside])])

    def test_a_file_tree_grant_covers_everything_below_it(self):
        nested = os.path.join(self.outside, "a", "b")
        os.makedirs(nested)
        self.grant(fs_readwrite=[os.path.join(self.outside, "*")])
        with self.sim.audited():
            with open(os.path.join(nested, "x"), "w", encoding="utf-8") as fh:
                fh.write("x")
        self.assertEqual(self.prompted(), [])

    def test_a_file_opened_by_descriptor_names_no_target(self):
        path = os.path.join(self.data, "fd.bin")
        with self.sim.audited():
            fd = os.open(path, os.O_WRONLY | os.O_CREAT)
            with os.fdopen(fd, "wb"):
                pass
            with open(path, "wb", opener=lambda opened, _flags: os.open(opened, os.O_WRONLY)):
                pass
        self.assertEqual([(p.event, p.by_event, p.key) for p in self.sim.prompts],
                         [("open", True, "FsReadWrite:open")])

    def test_denied_names_are_refused_not_asked(self):
        with self.sim.audited(), self.assertRaises(PermissionError):
            open(os.path.join(self.data, "OrcaSlicer.conf"), "w")
        with self.sim.audited(), self.assertRaises(PermissionError):
            open(os.path.join(self.data, "certs", "x"), "w")
        self.assertEqual(self.prompted(), [])
        self.assertEqual(len(self.sim.denied), 2)

    def test_a_full_url_is_asked_per_url_but_an_origin_covers_the_host(self):
        first = self.farm.base + "/printer/info"
        second = self.farm.base + "/printer/objects/list"
        with self.sim.audited():
            urllib_open(first)
            urllib_open(second)
        self.assertEqual([p.targets for p in self.sim.prompts if p.event == "urllib.Request"],
                         [[first], [second]])
        self.sim.reset()
        self.grant(network_http=["http://127.0.0.1"])
        with self.sim.audited():
            urllib_open(self.farm.base + "/printer/info?x=1")
        self.assertEqual(self.prompted(), [])

    def test_http_client_names_only_the_host_so_one_answer_covers_every_path(self):
        with self.sim.audited():
            for path in ("/printer/info", "/printer/objects/list"):
                connection = http.client.HTTPConnection("127.0.0.1", self.farm.port, timeout=5)
                connection.request("GET", path)
                connection.getresponse().read()
                connection.close()
        self.assertEqual(self.prompted(), [("http.client.connect", ["127.0.0.1"])])

    def test_targetless_socket_events_need_any_network_grant(self):
        with self.sim.audited():
            socket.socket().close()
        self.assertEqual(self.prompted(), [("socket.__new__", [])])
        self.sim.reset()
        self.grant(network_socket=["('0.0.0.0', 2021)"])
        with self.sim.audited():
            socket.socket().close()
        self.assertEqual(self.prompted(), [])

    def test_a_bound_address_is_granted_by_its_text(self):
        self.grant(network_socket=["('0.0.0.0', 0)"])
        with self.sim.audited(), socket.socket() as listener:
            listener.bind(("0.0.0.0", 0))
        self.assertEqual(self.prompted(), [])
        with self.sim.audited(), socket.socket() as listener:
            listener.bind(("", 0))
        self.assertEqual(self.prompted(), [("socket.bind", ["('', 0)"])])

    def test_threads_need_the_declared_thread_grant(self):
        def run():
            with self.sim.audited():
                thread = threading.Thread(target=lambda: None)
                thread.start()
                thread.join()
        run()
        self.assertEqual(self.prompted(), [("_thread.start_new_thread", ["thread"])])
        self.sim.reset()
        self.grant(threading=["thread"])
        run()
        self.assertEqual(self.prompted(), [])

    def test_create_and_delete_outside_the_data_folder_always_ask(self):
        target = os.path.join(self.outside, "new")
        with self.sim.audited():
            os.mkdir(target)
            os.rmdir(target)
            os.mkdir(target)
            os.rmdir(target)
        self.assertEqual([p.event for p in self.sim.prompts],
                         ["os.mkdir", "os.rmdir", "os.mkdir", "os.rmdir"])

    def test_network_matching_follows_the_host_rules(self):
        match = audit.network_permission_matches
        granted = ["http://192.168.1.20", "https://voron.local", "http://[fe80::1]"]
        self.assertTrue(match(granted, "http://192.168.1.20:7125/printer/info?x=1"))
        self.assertTrue(match(granted, "192.168.1.20"))
        self.assertTrue(match(granted, "('192.168.1.20', 7125)"))
        self.assertTrue(match(granted, "('fe80::1', 7125, 0, 0)"))
        self.assertTrue(match(granted, "VORON.local"))
        self.assertFalse(match(granted, "https://other.local/"))
        self.assertFalse(match(granted, "('', 2021)"))
        self.assertFalse(match(["http://192.168.1.20/exact"], "http://192.168.1.20/other"))
        self.assertFalse(match(["http://192.168.1.20/exact"], "192.168.1.20"))
        self.assertTrue(match(["192.168.1.20"], "192.168.1.20"))

    def test_declarations_follow_the_host_binding(self):
        with self.assertRaises(ValueError):
            self.sim.request_permissions(network=["http://ok"])  # outside plugin loading

        def declare():
            self.sim.request_permissions(network=["http://10.0.0.5:7125/x", "http://10.0.0.5"],
                                         network_socket=["('0.0.0.0', 2021)"], threading=True)
        shown = self.sim.load(declare)
        self.assertEqual(shown, {"network_http": ["http://10.0.0.5"],
                                 "network_socket": ["('0.0.0.0', 2021)"],
                                 "threading": ["thread"]})
        self.assertEqual(self.sim.load(declare), {})  # already granted: no second dialog
        for bad in ({"network": ["ftp://host"]}, {"network": ["10.0.0.5"]}, {"fs_read": [""]}):
            with self.assertRaises(ValueError):
                self.sim.load(lambda bad=bad: self.sim.request_permissions(**bad))


class HttpTransportTest(unittest.TestCase):
    def setUp(self):
        self.plugin = _load_plugin()
        self.farm = _FarmServer()
        self.addCleanup(self.farm.close)

    def request(self, path, **kwargs):
        return self.plugin._urlopen_no_redirect(
            self.plugin.urllib.request.Request(self.farm.base + path, **kwargs),
            timeout=5, context=None)

    def test_success_reads_body_and_status(self):
        with self.request("/printer/info") as response:
            self.assertEqual(response.getcode(), 200)
            self.assertEqual(json.loads(response.read())["result"]["hostname"], "voron")

    def test_post_sends_the_body_and_headers(self):
        request = {"data": b"payload", "method": "POST", "headers": {"X-Api-Key": "k"}}
        with self.request("/server/files/upload", **request) as response:
            self.assertEqual(response.status, 201)
        self.assertEqual(self.farm.requests[-1], ("POST", "/server/files/upload", b"payload"))

    def test_other_statuses_raise_http_error_with_the_code(self):
        with self.assertRaises(self.plugin.urllib.error.HTTPError) as caught:
            self.request("/missing")
        self.assertEqual(caught.exception.code, 404)
        self.assertEqual(self.plugin.transport_error_code(caught.exception), "endpoint_not_found")

    def test_a_refused_connection_is_a_url_error(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        request = self.plugin.urllib.request.Request("http://127.0.0.1:%d/" % port)
        with self.assertRaises(self.plugin.urllib.error.URLError) as caught:
            self.plugin._urlopen_no_redirect(request, timeout=2)
        # Windows lets a refused connect run into the timeout instead of failing at once.
        self.assertIn(self.plugin.transport_error_code(caught.exception),
                      ("network_error", "timeout"))

    def test_unsupported_scheme_is_refused(self):
        request = self.plugin.urllib.request.Request("file:///etc/passwd")
        with self.assertRaises(self.plugin.urllib.error.URLError):
            self.plugin._urlopen_no_redirect(request, timeout=2)


if __name__ == "__main__":
    unittest.main()
