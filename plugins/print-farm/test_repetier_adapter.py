"""Local HTTP emulation of the documented Repetier-Server bridge contract."""
import http.server
import json
import threading
import urllib.parse
from unittest import mock

import pytest
import repetier_adapter as adapter


@pytest.fixture
def server():
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def handle_request(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.server.requests.append((self.command, self.path, dict(self.headers), body))
            path = urllib.parse.urlsplit(self.path)
            route = path.path.removeprefix("/proxy")
            if route in self.server.overrides:
                code, value = self.server.overrides[route]
            elif route == "/printer/info":
                code, value = 200, self.server.info
            elif route == "/printer/list":
                code, value = 200, {"data": self.server.printers}
            elif route.startswith("/printer/api/") and self.command == "GET":
                query = urllib.parse.parse_qs(path.query)
                code, value = 200, self.server.printers if query.get("a") == ["listPrinter"] else self.server.states
            else:
                code, value = 200, {}
            data = value if isinstance(value, bytes) else json.dumps(value).encode()
            self.send_response(code)
            if 300 <= code < 400:
                self.send_header("Location", self.server.base + "/stolen")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        do_GET = do_POST = handle_request

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.base = "http://127.0.0.1:%d/proxy" % srv.server_port
    srv.requests, srv.overrides = [], {}
    srv.info = {"software": "Repetier-Server", "version": "1.4.10", "apikey": "DO-NOT-LEAK"}
    srv.printers = [dict(slug=slug, name=name, active=True, online=1, job="none", paused=False, pauseState=0)
                    for slug, name in (("Alpha", "Workshop"), ("Beta", "Office"))]
    srv.states = {slug: {"condition": 1, "activeExtruder": 0,
                        "extruder": [{"tempRead": 21.5, "tempSet": 0}],
                        "heatedBeds": [{"tempRead": 22, "tempSet": 0}]}
                  for slug in ("Alpha", "Beta")}
    worker = threading.Thread(target=srv.serve_forever, daemon=True)
    worker.start()
    try:
        yield srv
    finally:
        srv.shutdown()
        srv.server_close()
        worker.join()


def test_selection_whitelist_and_header_only_auth(server):
    result = adapter.list_printers(server.base, None, "test-key")
    assert result == {"ok": True, "printers": [
        {"slug": "Alpha", "name": "Workshop", "active": True, "online": 1},
        {"slug": "Beta", "name": "Office", "active": True, "online": 1}]}
    assert "DO-NOT-LEAK" not in json.dumps(result)
    assert all(r[2]["X-Api-Key"] == "test-key" and "test-key" not in r[1] for r in server.requests)


def test_documented_custom_name_without_software_is_supported(server):
    server.info = {"name": "Rolands Server", "version": "1.0.3", "printers": server.printers,
                   "apikey": "DO-NOT-LEAK"}
    assert adapter.list_printers(server.base, None)["ok"]
    assert adapter.poll(server.base, None, slug="Alpha")["online"]


@pytest.mark.parametrize("info,code", [
    ({"software": "Another-server", "version": "1.4.0"}, "unsupported_server"),
    ({"name": "Custom", "version": "1.4.0"}, "malformed_response"),
    ({"name": "Custom", "printers": []}, "malformed_response"),
    ({"name": "Custom", "version": "1.4.0", "printers": [{}]}, "malformed_response")])
def test_explicit_software_mismatch_and_malformed_info_rejected(server, info, code):
    server.info = info
    assert adapter.list_printers(server.base, None)["error_code"] == code
    assert not any(r[0] == "POST" for r in server.requests)


def test_poll_selected_once_and_exact_temperature_map(server):
    result = adapter.poll(server.base, None, "key", slug="Beta")
    assert result["state"] == "idle" and result["can_start"] is True
    assert result["nozzle"] == 21.5 and result["bed"] == 22
    assert len(server.requests) == 3
    route = urllib.parse.urlsplit(server.requests[-1][1])
    assert route.path == "/proxy/printer/api/Beta"
    assert urllib.parse.parse_qs(route.query) == {"a": ["stateList"], "data": ['{"includeHistory":false}']}


@pytest.mark.parametrize("slug", ["", "Missing", "../Alpha", "Alpha/Beta", "Alpha\r\n", None])
def test_invalid_or_disappeared_selection_never_chooses_first(server, slug):
    result = adapter.poll(server.base, None, slug=slug)
    assert not result["online"] and not result["upload_ready"]
    assert not any(r[0] == "POST" for r in server.requests)


def test_duplicate_slug_is_not_a_target(server):
    server.printers.append(dict(server.printers[0]))
    assert adapter.list_printers(server.base, None)["ok"] is False
    assert adapter.poll(server.base, None, slug="Alpha")["error_code"] == "malformed_response"


@pytest.mark.parametrize("field,value", [("paused", None), ("pauseState", None), ("job", None),
                                          ("paused", True), ("pauseState", 1)])
def test_partial_or_contradictory_job_is_unknown(server, field, value):
    server.printers[0][field] = value
    result = adapter.poll(server.base, None, slug="Alpha")
    assert result["state"] == "unknown" and result["progress"] is None
    assert not result["can_start"] and not result["upload_ready"]


@pytest.mark.parametrize("condition", [None, 0, 2, 3, 4, 5, True, "1"])
def test_nonready_condition_forbids_every_write(server, condition, tmp_path):
    server.states["Alpha"]["condition"] = condition
    result = adapter.poll(server.base, None, slug="Alpha")
    assert result["online"] and not result["upload_ready"] and not result["can_control"]
    file = tmp_path / "job.gcode"
    file.write_bytes(b"G28\n")
    assert not adapter.upload(server.base, None, "", file, file.name, slug="Alpha")["ok"]
    assert not any(r[0] == "POST" for r in server.requests)


@pytest.mark.parametrize("version", ["1.5.0", "2.0.0", "unknown"])
def test_unknown_connector_version_readonly(server, version):
    server.info["version"] = version
    result = adapter.poll(server.base, None, slug="Alpha")
    assert result["online"] and result["nozzle"] == 21.5
    assert not result["can_start"] and not result["can_control"]
    assert result["upload_reason"] == "repetierModeUnsupported"


@pytest.mark.parametrize("code,value,error", [(401, {}, "authorization_denied"), (403, {}, "authorization_denied"),
    (302, {}, "redirect_rejected"), (200, b"<html>login</html>", "malformed_response"),
    (200, 42, "malformed_response"), (200, {"error": "Authorization required"}, "authorization_denied")])
def test_read_failure_is_closed(server, code, value, error):
    server.overrides["/printer/api/Alpha"] = code, value
    result = adapter.poll(server.base, None, slug="Alpha")
    assert result["error_code"] == error and not result["upload_ready"]
    assert not any("/stolen" in r[1] for r in server.requests)


@pytest.mark.parametrize("start", [False, True])
def test_upload_streams_exact_bytes_one_mutation_and_no_verification(server, tmp_path, start):
    file = tmp_path / "job.gcode"
    payload = b"; exact bytes\r\n" + bytes(range(256)) * 1024
    file.write_bytes(payload)
    result = adapter.upload(server.base, None, "key", file, file.name, slug="Beta", start=start)
    assert result["ok"] and result["verified"] is False
    posts = [r for r in server.requests if r[0] == "POST"]
    assert len(posts) == 1
    _, path, headers, body = posts[0]
    assert path == "/proxy/printer/%s/Beta" % ("job" if start else "model")
    assert b'name="filename"; filename="job.gcode"' in body
    assert body.count(payload) == 1 and len(body) == int(headers["Content-Length"])
    assert (b'name="autostart"\r\n\r\ntrue' in body) is start
    assert b"overwrite" not in body and b"copyModel" not in body


@pytest.mark.parametrize("reply", [b"<html>accepted</html>", b"{bad json", b"[]"])
def test_ambiguous_mutation_body_is_unknown_no_retry(server, tmp_path, reply):
    file = tmp_path / "job.gcode"; file.write_bytes(b"G28")
    server.overrides["/printer/job/Alpha"] = 200, reply
    result = adapter.upload(server.base, None, "", file, file.name, slug="Alpha", start=True)
    assert result["error_code"] == "unknown_outcome" and result["outcome"] == "unknown"
    assert len([r for r in server.requests if r[0] == "POST"]) == 1


def test_timeout_after_dispatch_is_unknown_no_retry(server, tmp_path):
    file = tmp_path / "job.gcode"; file.write_bytes(b"G28")
    with mock.patch.object(adapter.http.client.HTTPConnection, "getresponse", side_effect=TimeoutError()):
        result = adapter._request
        with pytest.raises(adapter.AdapterError) as error:
            result(server.base, None, "", "/printer/job/Alpha", mutation=True,
                   file_path=file, filename=file.name, fields={"a": "upload", "autostart": "true"})
    assert error.value.code == "unknown_outcome"


@pytest.mark.parametrize("action,paused,command,data", [("pause", False, "send", {"cmd": "@pause User requested pause."}),
                    ("resume", True, "continueJob", {}), ("cancel", True, "stopJob", {})])
def test_control_only_selected_slug_fixed_command(server, action, paused, command, data):
    server.printers[1].update(job="part.gcode", jobid=9, paused=paused, pauseState=1 if paused else 0, done=42)
    result = adapter.control(server.base, None, "key", action, slug="Beta")
    assert result["ok"]
    posts = [r for r in server.requests if r[0] == "POST"]
    assert len(posts) == 1
    parsed = urllib.parse.urlsplit(posts[0][1])
    assert parsed.path == "/proxy/printer/api/Beta"
    query = urllib.parse.parse_qs(parsed.query)
    assert query["a"] == [command] and json.loads(query["data"][0]) == data


@pytest.mark.parametrize("base", ["http://user:pass@localhost", "http://localhost/a/../b", "http://localhost/%2e%2e/b",
                                  "http://localhost/?key=secret", "http://localhost/#fragment", "http://localhost/\r\n"])
def test_unsafe_address_no_request(base):
    with mock.patch.object(adapter.http.client.HTTPConnection, "connect") as connect:
        assert not adapter.list_printers(base, None)["ok"]
        connect.assert_not_called()


def test_header_injection_is_rejected_before_network():
    with mock.patch.object(adapter.http.client.HTTPConnection, "connect") as connect:
        assert adapter.list_printers("http://localhost", None, "key\r\nInjected: x")["error_code"] == "invalid_api_key"
        connect.assert_not_called()
