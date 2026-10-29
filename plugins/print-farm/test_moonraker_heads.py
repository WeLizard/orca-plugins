"""Local read-only Moonraker telemetry and browser-only head selection."""
import http.server
import json
import pathlib
import shutil
import subprocess
import threading
import urllib.error
from unittest import mock

import pytest
from test_printers_plugin import _load_plugin


@pytest.fixture
def plugin():
    return _load_plugin()


@pytest.fixture
def server():
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            self.server.requests.append((self.path, dict(self.headers)))
            if self.path.endswith("/printer/objects/list"):
                code, value = self.server.discovery
            else:
                code, value = 200, self.server.telemetry
            body = json.dumps(value).encode()
            self.send_response(code)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.base = "http://127.0.0.1:%d" % srv.server_port
    srv.requests = []
    srv.discovery = 200, {"result": {"objects": ["extruder", "extruder1", "extruder2", "extruder3"]}}
    srv.telemetry = {"result": {"status": {
        "extruder": {"temperature": 23, "target": 0},
        "extruder1": {"temperature": 24, "target": 0},
        "extruder2": {"temperature": 215.5, "target": 220},
        "extruder3": {"temperature": 25, "target": 0},
        "toolhead": {"extruder": "extruder2"}, "heater_bed": {"temperature": 50, "target": 60},
        "print_stats": {"state": "standby"}, "display_status": {"progress": 0}}}}
    thread = threading.Thread(target=srv.serve_forever, daemon=True); thread.start()
    try:
        yield srv
    finally:
        srv.shutdown(); srv.server_close(); thread.join()


def poll(plugin, server):
    return plugin.poll_moonraker(server.base, None, {"X-Api-Key": "synthetic-key"})


def test_four_heads_use_the_actual_active_extruder_without_commands(plugin, server):
    value = poll(plugin, server)
    assert [tool["index"] for tool in value["tools"]] == [0, 1, 2, 3]
    assert value["active_tool"] == 2 and value["nozzle"] == 215.5 and value["target_nozzle"] == 220
    assert [t["active"] for t in value["tools"]] == [False, False, True, False]
    assert len(server.requests) == 2
    assert "toolhead" in server.requests[1][0] and "extruder3" in server.requests[1][0]
    assert all(headers["X-Api-Key"] == "synthetic-key" and "synthetic-key" not in path
               for path, headers in server.requests)


def test_sparse_heads_keep_their_numbers_and_numeric_order(plugin, server):
    server.discovery = 200, {"result": {"objects": ["extruder10", "extruder3", "extruder1"]}}
    value = poll(plugin, server)
    assert [t["index"] for t in value["tools"]] == [1, 3, 10]
    assert value["active_tool"] is None and value["nozzle"] is None
    assert value["tools"][2]["current"] is None


@pytest.mark.parametrize("toolhead", [None, {}, {"extruder": "extruder9"}, {"extruder": 0}])
def test_missing_or_unlisted_active_tool_is_never_t0(plugin, server, toolhead):
    server.telemetry["result"]["status"]["toolhead"] = toolhead
    value = poll(plugin, server)
    assert value["active_tool"] is None and value["nozzle"] is None and value["target_nozzle"] is None
    assert not any(t["active"] for t in value["tools"])


def test_partial_heaters_and_nonfinite_values_stay_unknown(plugin, server):
    values = server.telemetry["result"]["status"]
    values["extruder"] = {"temperature": float("nan"), "target": True}
    values["extruder1"] = {"temperature": float("inf"), "target": "200"}
    values["extruder2"] = "missing"
    del values["extruder3"]
    value = poll(plugin, server)
    assert len(value["tools"]) == 4
    assert all(t["current"] is None and t["target"] is None for t in value["tools"])
    assert value["nozzle"] is None
    assert plugin.public_status(value)["tools"] == value["tools"]


def test_discovery_is_whitelisted_deduplicated_and_bounded(plugin, server):
    objects = ["extruder"] + ["extruder%d" % i for i in range(1, 30)]
    objects += ["extruder", "extruder1;G28", "extruder01", "extruder0", "heater_generic chamber", "gcode_macro T0", "toolhead"]
    server.discovery = 200, {"result": {"objects": objects}}
    value = poll(plugin, server)
    assert [t["index"] for t in value["tools"]] == list(range(16))
    assert "extruder16" not in server.requests[1][0]
    assert "macro" not in server.requests[1][0] and ";" not in server.requests[1][0]
    assert len(server.requests[1][0]) < 512


def test_unsupported_discovery_uses_legacy_single_head(plugin, server):
    server.discovery = 404, {"error": {"message": "not found"}}
    value = poll(plugin, server)
    assert len(value["tools"]) == 1 and value["nozzle"] == 23
    assert not value["tools"][0]["active"] and value["active_tool"] is None
    assert "extruder1" not in server.requests[1][0]


@pytest.mark.parametrize("code", [401, 403, 405, 500])
def test_denied_or_failed_discovery_never_falls_back(plugin, server, code):
    server.discovery = code, {"error": "unavailable"}
    with pytest.raises(urllib.error.HTTPError) as error:
        poll(plugin, server)
    assert error.value.code == code
    assert len(server.requests) == 1


@pytest.mark.parametrize("reply", [{}, {"result": {}}, {"result": {"objects": None}},
                                    {"result": {"objects": ["extruder", None]}}])
def test_malformed_discovery_is_not_single_head_evidence(plugin, server, reply):
    server.discovery = 200, reply
    with pytest.raises(ValueError):
        poll(plugin, server)
    assert len(server.requests) == 1


def test_missing_print_state_stays_unknown(plugin, server):
    del server.telemetry["result"]["status"]["print_stats"]
    assert poll(plugin, server)["state"] == "unknown"


def test_oversized_discovery_stops_before_query(plugin, server):
    server.discovery = 200, {"result": {"objects": ["x" * (256 * 1024)]}}
    with pytest.raises(ValueError, match="exceeds limit"):
        poll(plugin, server)
    assert len(server.requests) == 1


@pytest.mark.skipif(not shutil.which("node"), reason="node unavailable")
def test_browser_selection_is_local_and_separate_from_actual_active(plugin, tmp_path):
    page = plugin.PAGE
    block = page[page.index("// <tool-details>"):page.index("// </tool-details>")]
    script = r'''
var posted = [], orca = {postMessage:function(m){posted.push(m);}};
function esc(x){return String(x);}
function temp(x){return x == null ? '—' : String(x) + '°C';}
function t(k,v){return k === 'toolDetails' ? v.tool + '|current:' + v.current + '|target:' + v.target + '|' + v.state : k;}
'''+block+r'''
var p={target_uuid:'target',binding_revision:1};
var tools=[{index:0,name:'extruder',current:20,target:0,active:false},
  {index:2,name:'extruder2',current:210,target:220,active:true}];
var initial=toolDetailsHtml(p,tools);
function button(i){return {attrs:{'data-tool':String(i)},listeners:{},
  getAttribute:function(k){return this.attrs[k];},setAttribute:function(k,v){this.attrs[k]=v;},
  addEventListener:function(k,f){this.listeners[k]=f;}};}
var buttons=[button(0),button(2)],detail={textContent:''};
var tile={querySelectorAll:function(){return buttons;},querySelector:function(){return detail;}};
bindToolDetails(tile,p,tools);buttons[0].listeners.click();
var selected=toolDetailsHtml(p,tools);
var afterPoll=toolDetailsHtml(p,tools);
var rev={target_uuid:'target',binding_revision:2};pruneToolDetails([rev]);
var next=toolDetailsHtml(rev,tools);
console.log(JSON.stringify({initial:initial,selected:selected,detail:detail.textContent,
  afterPoll:afterPoll,next:next,posted:posted,keys:Object.keys(toolDetailSelections),
  duplicate:toolDetailsText({index:2,name:'T2',current:30,target:0,active:false},[])}));
'''
    path = tmp_path / "head-selection.js"; path.write_text(script, encoding="utf-8")
    done = subprocess.run(["node", str(path)], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert done.returncode == 0, done.stderr
    value = json.loads(done.stdout)
    assert 'data-tool="2" aria-pressed="true"' in value["initial"]
    assert 'data-tool="0" aria-pressed="true"' in value["selected"]
    assert 'actual-active" data-tool="2" aria-pressed="false"' in value["selected"]
    assert value["selected"] == value["afterPoll"]
    assert 'data-tool="2" aria-pressed="true"' in value["next"]
    assert value["keys"] == ["target:2"] and value["posted"] == []
    assert value["detail"] == "T0 (extruder)|current:20°C|target:0°C|toolInactive"
    assert value["duplicate"] == "T2|current:30°C|target:0°C|stateUnknown"
