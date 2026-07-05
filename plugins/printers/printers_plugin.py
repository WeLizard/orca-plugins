# /// script
# requires-python = ">=3.12"
# dependencies = []
#
# [tool.orcaslicer.plugin]
# id = "printers"
# name = "Printers"
# description = "Cross-vendor dashboard of your networked 3D printers: live status, temperatures, print progress and thumbnail, one click to each printer's web interface, and send a G-code to several printers at once."
# author = "FilamentHub"
# version = "0.2.0"
#
# # Printers live on user-chosen LAN addresses no static allow-list can enumerate,
# # so this declares intent for a future "local-network" permission class
# # (see PR #14530 feedback). Ignored by the current host.
# network = ["local-network"]
# ///
"""Printers — a cross-vendor printer dashboard plugin for OrcaSlicer (PR #14530).

Docks its own main-window tab (via orca.host.ui.create_panel) with a tile per
printer: live status, nozzle/bed temperatures, print progress and the current
print's thumbnail, plus one click to the printer's web UI. It also sends a
G-code file to several printers at once ("send to printers" / batch print).

Printers come from two sources, merged and de-duplicated by URL:
  * OrcaSlicer's own printer presets that have a network address configured
    (read via orca.host.preset_bundle() on the UI thread) — no manual entry.
  * Manually added entries, stored in printers.json next to the plugin.

Backends: Moonraker (Klipper), OctoPrint, or a generic reachability ping.
Everything is stdlib-only; host calls stay on the UI thread and all network /
disk work is offloaded to worker threads.

What is intentionally NOT done here, because it belongs to the host and would be
a good plugin-capability to expose (flagged for PR #14530, not worked around):
  * "Open" switches this tab to an iframe of the printer's web UI; a plugin
    cannot programmatically open the native Device tab for a specific printer.
  * Batch print uploads a file the user picks; a plugin cannot read the file the
    slicer just produced (that is the g-code capability's territory, not script).
"""

import base64
import json
import os
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request

import orca

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
PRINTERS_FILE = os.path.join(PLUGIN_DIR, "printers.json")
ICON_PATH = os.path.join(PLUGIN_DIR, "printers.svg")
HTTP_TIMEOUT = 6
UPLOAD_TIMEOUT = 120
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE  # LAN printers routinely use self-signed / plain HTTP


# --------------------------------------------------------------------------- #
# Manual printer store (printers.json next to the plugin)
# --------------------------------------------------------------------------- #
def load_manual_printers():
    try:
        with open(PRINTERS_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def save_manual_printers(printers):
    try:
        with open(PRINTERS_FILE, "w", encoding="utf-8") as fh:
            json.dump(printers, fh, ensure_ascii=False, indent=2)
    except OSError:
        pass


def normalize_url(url):
    url = (url or "").strip().rstrip("/")
    if url and not url.startswith("http://") and not url.startswith("https://"):
        url = "http://" + url
    return url


def kind_from_host_type(host_type):
    host_type = (host_type or "").lower()
    if "octo" in host_type:
        return "octoprint"
    if "moonraker" in host_type or "klipper" in host_type or "mainsail" in host_type or "fluidd" in host_type:
        return "moonraker"
    return "generic"


# --------------------------------------------------------------------------- #
# Discover printers from OrcaSlicer's own printer presets. MUST run on the UI
# thread (reads host state); call it from on_message, not from a worker.
# --------------------------------------------------------------------------- #
def discover_orca_printers():
    found = []
    try:
        printers = orca.host.preset_bundle().printers
        count = printers.size()
    except Exception:
        return found
    for i in range(count):
        try:
            preset = printers.preset(i)
            host = preset.config_value("print_host")
            if not host:
                continue
            found.append({
                "id": "orca:" + preset.name,
                "name": preset.name,
                "url": normalize_url(host),
                "type": kind_from_host_type(preset.config_value("host_type")),
                "apikey": preset.config_value("printhost_apikey") or "",
                "source": "orca",
            })
        except Exception:
            continue
    return found


def merged_printers():
    # Discovered (Orca) first, then manual entries whose URL is not already present.
    result = discover_orca_printers()
    seen = {p["url"] for p in result if p.get("url")}
    for p in load_manual_printers():
        url = normalize_url(p.get("url"))
        if url and url not in seen:
            p["url"] = url
            p["source"] = "manual"
            result.append(p)
            seen.add(url)
    return result


# --------------------------------------------------------------------------- #
# HTTP helpers (stdlib only).
# --------------------------------------------------------------------------- #
def _get_json(url, headers=None, timeout=HTTP_TIMEOUT):
    req = urllib.request.Request(url, headers=headers or {"Accept": "application/json"}, method="GET")
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _get_bytes(url, headers=None, timeout=HTTP_TIMEOUT):
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
        return resp.read()


def _multipart_upload(url, headers, filename, file_bytes, extra_fields=None):
    boundary = "----OrcaPrintersBoundary7MA4YWxkTrZu0gW"
    parts = []
    for key, value in (extra_fields or {}).items():
        parts.append(("--" + boundary + "\r\n"
                      "Content-Disposition: form-data; name=\"" + key + "\"\r\n\r\n"
                      + str(value) + "\r\n").encode("utf-8"))
    parts.append(("--" + boundary + "\r\n"
                  "Content-Disposition: form-data; name=\"file\"; filename=\"" + filename + "\"\r\n"
                  "Content-Type: application/octet-stream\r\n\r\n").encode("utf-8"))
    body = b"".join(parts) + file_bytes + ("\r\n--" + boundary + "--\r\n").encode("utf-8")
    hdrs = dict(headers or {})
    hdrs["Content-Type"] = "multipart/form-data; boundary=" + boundary
    req = urllib.request.Request(url, data=body, headers=hdrs, method="POST")
    with urllib.request.urlopen(req, timeout=UPLOAD_TIMEOUT, context=_SSL_CTX) as resp:
        return resp.getcode()


# --------------------------------------------------------------------------- #
# Per-backend status polling. Common shape:
# {online, state, nozzle, target_nozzle, bed, target_bed, progress, filename, thumbnail}
# --------------------------------------------------------------------------- #
def moonraker_thumbnail(base, filename):
    if not filename:
        return ""
    try:
        meta = _get_json(base + "/server/files/metadata?filename=" + urllib.parse.quote(filename))
        thumbs = meta.get("result", {}).get("thumbnails", [])
        if not thumbs:
            return ""
        best = max(thumbs, key=lambda t: t.get("size", 0))
        rel = best.get("relative_path") or best.get("thumbnail_path")
        if not rel:
            return ""
        raw = _get_bytes(base + "/server/files/gcodes/" + urllib.parse.quote(rel))
        return "data:image/png;base64," + base64.b64encode(raw).decode("ascii")
    except Exception:
        return ""


def poll_moonraker(base):
    query = "/printer/objects/query?extruder&heater_bed&print_stats&display_status"
    data = _get_json(base + query)
    status = data.get("result", {}).get("status", {})
    extruder = status.get("extruder", {})
    bed = status.get("heater_bed", {})
    print_stats = status.get("print_stats", {})
    display = status.get("display_status", {})
    state = print_stats.get("state", "standby")
    filename = print_stats.get("filename", "")
    return {
        "online": True,
        "state": state,
        "nozzle": extruder.get("temperature"),
        "target_nozzle": extruder.get("target"),
        "bed": bed.get("temperature"),
        "target_bed": bed.get("target"),
        "progress": round((display.get("progress") or 0.0) * 100),
        "filename": filename,
        "thumbnail": moonraker_thumbnail(base, filename) if state == "printing" else "",
    }


def poll_octoprint(base, apikey):
    headers = {"Accept": "application/json"}
    if apikey:
        headers["X-Api-Key"] = apikey
    printer = _get_json(base + "/api/printer", headers=headers)
    temps = printer.get("temperature", {})
    tool0 = temps.get("tool0", {})
    bed = temps.get("bed", {})
    state = printer.get("state", {}).get("text", "Operational")
    progress, filename = 0, ""
    try:
        job = _get_json(base + "/api/job", headers=headers)
        progress = round(job.get("progress", {}).get("completion") or 0)
        filename = job.get("job", {}).get("file", {}).get("name", "") or ""
    except Exception:
        pass
    return {
        "online": True, "state": state,
        "nozzle": tool0.get("actual"), "target_nozzle": tool0.get("target"),
        "bed": bed.get("actual"), "target_bed": bed.get("target"),
        "progress": progress, "filename": filename, "thumbnail": "",
    }


def poll_generic(base):
    req = urllib.request.Request(base, method="GET")
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT, context=_SSL_CTX):
        return {"online": True, "state": "reachable", "nozzle": None, "target_nozzle": None,
                "bed": None, "target_bed": None, "progress": None, "filename": "", "thumbnail": ""}


def poll_printer(printer):
    base = normalize_url(printer.get("url"))
    kind = (printer.get("type") or "moonraker").lower()
    try:
        if kind == "moonraker":
            return poll_moonraker(base)
        if kind == "octoprint":
            return poll_octoprint(base, printer.get("apikey", ""))
        return poll_generic(base)
    except Exception as exc:
        return {"online": False, "state": "offline", "error": str(exc), "nozzle": None,
                "target_nozzle": None, "bed": None, "target_bed": None, "progress": None,
                "filename": "", "thumbnail": ""}


def send_gcode(printer, filename, file_bytes, start):
    base = normalize_url(printer.get("url"))
    kind = (printer.get("type") or "moonraker").lower()
    try:
        if kind == "moonraker":
            fields = {"print": "true"} if start else {}
            _multipart_upload(base + "/server/files/upload", {}, filename, file_bytes, fields)
        elif kind == "octoprint":
            headers = {}
            if printer.get("apikey"):
                headers["X-Api-Key"] = printer["apikey"]
            fields = {"select": "true", "print": "true"} if start else {}
            _multipart_upload(base + "/api/files/local", headers, filename, file_bytes, fields)
        else:
            return {"ok": False, "error": "unsupported backend"}
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# --------------------------------------------------------------------------- #
# The page — native, host-themed. Tiles, thumbnail preview, per-tile select for
# batch print, and a "Send G-code to selected" action.
# --------------------------------------------------------------------------- #
PAGE = r"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8">
<style>
  html, body { margin:0; height:100%; }
  body { display:flex; flex-direction:column; background:var(--orca-bg,#1e1e2e);
         color:var(--orca-fg,#e0e0e0); font-family:var(--orca-font,sans-serif); }
  #bar { flex:0 0 auto; display:flex; align-items:center; gap:8px; padding:8px 12px;
         border-bottom:1px solid var(--orca-border,#3c3c4c); flex-wrap:wrap; }
  #bar h1 { margin:0; margin-right:auto; font-size:14px; font-weight:600; }
  #bar input, #bar select { font:inherit; font-size:12px; padding:4px 8px;
         background:var(--orca-bg,#1e1e2e); color:var(--orca-fg,#e0e0e0);
         border:1px solid var(--orca-border,#3c3c4c); border-radius:4px; }
  button { font:inherit; font-size:12px; cursor:pointer; padding:4px 12px; border-radius:4px;
           background:var(--orca-accent,#009688); color:var(--orca-accent-fg,#fff);
           border:1px solid var(--orca-accent,#009688); }
  button.ghost { background:transparent; color:var(--orca-fg,#e0e0e0); border-color:var(--orca-border,#3c3c4c); }
  button:disabled { opacity:.5; cursor:default; }
  #grid { flex:1 1 auto; overflow:auto; padding:12px;
          display:grid; grid-template-columns:repeat(auto-fill,minmax(250px,1fr)); gap:12px; align-content:start; }
  .tile { border:1px solid var(--orca-border,#3c3c4c); border-radius:8px; padding:12px;
          display:flex; flex-direction:column; gap:8px; }
  .tile .top { display:flex; align-items:center; gap:8px; }
  .dot { width:9px; height:9px; border-radius:50%; background:#888; flex:0 0 auto; }
  .dot.on { background:#3fbf6f; } .dot.off { background:#c0504d; }
  .tile .name { font-weight:600; margin-right:auto; }
  .src { font-size:10px; padding:1px 5px; border-radius:3px; border:1px solid var(--orca-border,#3c3c4c);
         color:var(--orca-muted,#a0a0a0); }
  .tile .state { font-size:11px; color:var(--orca-muted,#a0a0a0); text-transform:capitalize; }
  .body { display:flex; gap:10px; }
  .thumb { width:64px; height:64px; flex:0 0 auto; border:1px solid var(--orca-border,#3c3c4c);
           border-radius:6px; object-fit:contain; background:rgba(255,255,255,.04); display:none; }
  .thumb.show { display:block; }
  .meta { flex:1 1 auto; display:flex; flex-direction:column; gap:6px; min-width:0; }
  .temps { display:flex; gap:14px; font-size:12px; } .temps b { font-weight:600; }
  .fname { font-size:11px; color:var(--orca-muted,#a0a0a0); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .bar { height:6px; border-radius:3px; background:var(--orca-border,#3c3c4c); overflow:hidden; }
  .bar > i { display:block; height:100%; background:var(--orca-accent,#009688); }
  .row { display:flex; gap:6px; align-items:center; }
  .row label { display:flex; align-items:center; gap:4px; font-size:11px; margin-right:auto; }
  .empty { color:var(--orca-muted,#a0a0a0); padding:24px; text-align:center; }
  #viewer { flex:1 1 auto; display:none; flex-direction:column; }
  #viewer .vbar { display:flex; align-items:center; gap:8px; padding:6px 12px; border-bottom:1px solid var(--orca-border,#3c3c4c); }
  #viewer iframe { flex:1 1 auto; border:0; width:100%; }
  #status { font-size:11px; color:var(--orca-muted,#a0a0a0); }
</style></head>
<body>
  <div id="bar">
    <h1>Printers</h1>
    <span id="status"></span>
    <button id="send" class="ghost" disabled>Send G-code to selected…</button>
    <input id="f-name" placeholder="Name" size="8">
    <input id="f-url" placeholder="192.168.0.42" size="14">
    <select id="f-type">
      <option value="moonraker">Moonraker</option>
      <option value="octoprint">OctoPrint</option>
      <option value="generic">Other</option>
    </select>
    <button id="add">Add</button>
    <button id="refresh" class="ghost">Refresh</button>
    <input id="file" type="file" accept=".gcode,.gco,.g,.gz" style="display:none">
  </div>
  <div id="grid"><div class="empty">Loading…</div></div>
  <div id="viewer">
    <div class="vbar"><button id="back" class="ghost">&larr; Back</button><span id="vtitle"></span></div>
    <iframe id="vframe"></iframe>
  </div>
<script>
'use strict';
var grid = document.getElementById('grid');
var viewer = document.getElementById('viewer');
var sendBtn = document.getElementById('send');
var fileInput = document.getElementById('file');
var current = [];
var selected = {};   // url -> true

function esc(s){ return String(s == null ? '' : s).replace(/[&<>"]/g, function(c){
  return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]; }); }
function temp(v){ return v == null ? '—' : Math.round(v) + '°'; }

function updateSendBtn(){
  var n = Object.keys(selected).filter(function(k){ return selected[k]; }).length;
  sendBtn.disabled = n === 0;
  sendBtn.textContent = n ? ('Send G-code to ' + n + ' selected…') : 'Send G-code to selected…';
}

function render(printers){
  current = printers;
  grid.innerHTML = '';
  if (!printers.length){ grid.innerHTML = '<div class="empty">No printers found. Configure a printer host in OrcaSlicer, or add one above.</div>'; return; }
  printers.forEach(function(p){
    var s = p.status || {};
    var tile = document.createElement('div'); tile.className = 'tile';
    var prog = (s.progress == null) ? '' : '<div class="bar"><i style="width:' + s.progress + '%"></i></div>';
    var thumb = s.thumbnail ? '<img class="thumb show" src="' + s.thumbnail + '">' : '<div class="thumb"></div>';
    var fname = s.filename ? '<div class="fname" title="' + esc(s.filename) + '">' + esc(s.filename) + '</div>' : '';
    tile.innerHTML =
      '<div class="top"><span class="dot ' + (s.online ? 'on' : 'off') + '"></span>' +
      '<span class="name">' + esc(p.name || p.url) + '</span>' +
      '<span class="src">' + (p.source === 'orca' ? 'Orca' : 'manual') + '</span>' +
      '<span class="state">' + esc(s.state || (s.online ? 'online' : 'offline')) + '</span></div>' +
      '<div class="body">' + thumb + '<div class="meta">' +
      '<div class="temps"><span>Nozzle <b>' + temp(s.nozzle) + '</b>' + (s.target_nozzle ? '/' + temp(s.target_nozzle) : '') + '</span>' +
      '<span>Bed <b>' + temp(s.bed) + '</b>' + (s.target_bed ? '/' + temp(s.target_bed) : '') + '</span></div>' +
      fname + prog + '</div></div>' +
      '<div class="row"><label><input type="checkbox" class="sel"' + (selected[p.url] ? ' checked' : '') + '> select</label>' +
      '<button class="open ghost">Open</button>' +
      (p.source === 'manual' ? '<button class="remove ghost">Remove</button>' : '') + '</div>';
    tile.querySelector('.sel').addEventListener('change', function(e){
      selected[p.url] = e.target.checked; updateSendBtn(); });
    tile.querySelector('.open').addEventListener('click', function(){ openPrinter(p); });
    var rm = tile.querySelector('.remove');
    if (rm) rm.addEventListener('click', function(){ orca.postMessage({ type:'remove', id:p.id }); });
    grid.appendChild(tile);
  });
  updateSendBtn();
}

function openPrinter(p){
  document.getElementById('vtitle').textContent = p.name || p.url;
  document.getElementById('vframe').src = p.url;
  viewer.style.display = 'flex';
}
document.getElementById('back').addEventListener('click', function(){
  viewer.style.display = 'none'; document.getElementById('vframe').src = 'about:blank'; });

document.getElementById('add').addEventListener('click', function(){
  var name = document.getElementById('f-name').value.trim();
  var url = document.getElementById('f-url').value.trim();
  if (!url) return;
  orca.postMessage({ type:'add', name:name, url:url, kind:document.getElementById('f-type').value });
  document.getElementById('f-name').value = ''; document.getElementById('f-url').value = '';
});
document.getElementById('refresh').addEventListener('click', function(){ orca.postMessage({ type:'refresh' }); });

// Batch print: pick a file, then hand it to Python with the selected printer urls.
sendBtn.addEventListener('click', function(){ fileInput.click(); });
fileInput.addEventListener('change', function(){
  var file = fileInput.files && fileInput.files[0];
  if (!file) return;
  var urls = Object.keys(selected).filter(function(k){ return selected[k]; });
  var reader = new FileReader();
  reader.onload = function(){
    var b64 = String(reader.result).split(',')[1] || '';
    document.getElementById('status').textContent = 'Sending ' + file.name + ' to ' + urls.length + '…';
    orca.postMessage({ type:'mass-print', filename:file.name, dataB64:b64, urls:urls, start:true });
  };
  reader.readAsDataURL(file);
  fileInput.value = '';
});

orca.onMessage(function(data){
  if (!data) return;
  if (data.printers) render(data.printers);
  if (data.status !== undefined) document.getElementById('status').textContent = data.status;
});
orca.postMessage({ type:'ready' });
</script>
</body>
</html>
"""


class PrintersDashboard(orca.script.ScriptPluginCapabilityBase):
    win = None

    def get_name(self):
        return "Printers"

    def _supports_panel(self):
        return getattr(orca.host.ui, "create_panel", None)

    def _open(self):
        if self.win is not None and self.win.is_open():
            return False
        create_panel = self._supports_panel()
        if create_panel is not None:
            self.win = create_panel(
                title="Printers", html=PAGE, on_message=self.on_message, on_close=self.on_close,
                icon=ICON_PATH if os.path.exists(ICON_PATH) else "",
            )
        else:
            self.win = orca.host.ui.create_window(
                title="Printers", html=PAGE, width=1000, height=700,
                on_message=self.on_message, on_close=self.on_close,
            )
        return True

    def on_load(self):
        if self._supports_panel() is not None:
            try:
                self._open()
            except Exception:
                pass

    def execute(self):
        self._open()
        return orca.ExecutionResult.success("Printers dashboard opened.")

    def on_close(self):
        self.win = None

    # on_message runs on the UI thread — host reads happen here, network/disk on workers.
    def on_message(self, msg):
        msg = msg or {}
        kind = msg.get("type")
        if kind in ("ready", "refresh"):
            self._refresh_async()  # merged_printers() reads host state on the UI thread
        elif kind == "add":
            manual = load_manual_printers()
            new_id = max([p.get("id", 0) for p in manual if isinstance(p.get("id"), int)], default=0) + 1
            manual.append({"id": new_id, "name": msg.get("name") or msg.get("url"),
                           "url": normalize_url(msg.get("url")), "type": msg.get("kind") or "moonraker"})
            save_manual_printers(manual)
            self._refresh_async()
        elif kind == "remove":
            manual = [p for p in load_manual_printers() if p.get("id") != msg.get("id")]
            save_manual_printers(manual)
            self._refresh_async()
        elif kind == "mass-print":
            printers = merged_printers()  # host read on the UI thread
            threading.Thread(target=self._do_mass_print, args=(msg, printers), daemon=True).start()

    def _refresh_async(self):
        printers = merged_printers()  # UI thread
        threading.Thread(target=self._push_status, args=(printers,), daemon=True).start()

    def _push_status(self, printers):
        for p in printers:
            p["status"] = poll_printer(p)
        if self.win is not None and self.win.is_open():
            self.win.post({"printers": printers})

    def _do_mass_print(self, msg, printers):
        try:
            file_bytes = base64.b64decode(msg.get("dataB64") or "")
        except Exception:
            self._post_status("Send failed: could not read the file.")
            return
        filename = msg.get("filename") or "print.gcode"
        start = bool(msg.get("start"))
        targets = {normalize_url(u) for u in (msg.get("urls") or [])}
        selected = [p for p in printers if normalize_url(p.get("url")) in targets]
        ok, fail = 0, 0
        for p in selected:
            if send_gcode(p, filename, file_bytes, start).get("ok"):
                ok += 1
            else:
                fail += 1
        self._post_status("Sent to %d printer(s)%s." % (ok, (", %d failed" % fail) if fail else ""))
        self._refresh_async()

    def _post_status(self, text):
        if self.win is not None and self.win.is_open():
            self.win.post({"status": text})


@orca.plugin
class PrintersPlugin(orca.base):
    def register_capabilities(self):
        orca.register_capability(PrintersDashboard)
