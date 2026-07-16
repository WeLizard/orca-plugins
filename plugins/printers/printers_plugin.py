# /// script
# requires-python = ">=3.12"
# dependencies = []
#
# [tool.orcaslicer.plugin]
# id = "printers"
# name = "Printers"
# description = "Cross-vendor dashboard of your networked 3D printers: live status, temperatures, print progress and thumbnail, one click to each printer's web interface, and send a G-code to several printers at once."
# author = "FilamentHub"
# version = "0.0.6"
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

Backends: Moonraker (Klipper), OctoPrint, Bambu Lab in LAN mode, or a generic
reachability ping. Bambu talks MQTT over TLS on its LAN port (user "bblp", the
printer's access code); a self-contained stdlib MQTT client reads one status
report, so no third-party dependency and no cloud round-trip. Everything is
stdlib-only; host calls stay on the UI thread and all network / disk work is
offloaded to worker threads.

What is intentionally NOT done here, because it belongs to the host and would be
a good plugin-capability to expose (flagged for PR #14530, not worked around):
  * "Open" switches this tab to an iframe of the printer's web UI; a plugin
    cannot programmatically open the native Device tab for a specific printer.
  * Batch print uploads a file the user picks; a plugin cannot read the file the
    slicer just produced (that is the g-code capability's territory, not script).
"""

import base64
import ftplib
import io
import json
import os
import shutil
import socket
import ssl
import struct
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import orca

BAMBU_MQTT_PORT = 8883

def resolve_plugin_dir():
    """The plugin's install dir (orca_plugins/<name>), stable across package
    formats. A wheel runs from __whl_extracted__/<pkg>/ INSIDE the install dir
    and that cache is wiped on update — sidecar state (the printers store, the
    icon) must live in the install dir, not wherever __file__ happens to be."""
    here = os.path.dirname(os.path.abspath(__file__)).replace("\\", "/")
    parts = here.split("/")
    if "__whl_extracted__" in parts:
        return "/".join(parts[: parts.index("__whl_extracted__")])
    return here


PLUGIN_DIR = resolve_plugin_dir()
PRINTERS_FILE = os.path.join(PLUGIN_DIR, "printers.json")
# Tab icon. Embedded here rather than shipped as a sibling file so it survives a
# single-file install: OrcaSlicer copies only the .py, not adjacent assets. It is
# materialized next to the plugin on first use and handed to create_panel by path.
ICON_PATH = os.path.join(PLUGIN_DIR, "printers.svg")
ICON_SVG = r'''<?xml version="1.0" encoding="UTF-8"?><svg id="a" xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 20 20"><line x1="4.5" y1="14.5" x2="10.5" y2="14.5" style="fill:none; stroke:#fff; stroke-linecap:square; stroke-miterlimit:10;"/><polyline points="8.5 10.5 7.5 11.5 6.5 10.5" style="fill:none; stroke:#fff; stroke-linecap:round; stroke-linejoin:round;"/><line x1="4.5" y1="9.5" x2="6" y2="9.5" style="fill:none; stroke:#fff; stroke-linecap:square; stroke-miterlimit:10;"/><line x1="9.5" y1="9.5" x2="10.5" y2="9.5" style="fill:none; stroke:#fff; stroke-linecap:square; stroke-miterlimit:10;"/><rect x="6.5" y="8.5" width="2" height="2" rx=".27" ry=".27" style="fill:none; stroke:#fff; stroke-miterlimit:10;"/><rect x="1.5" y="6.5" width="12" height="11" rx="1" ry="1" style="fill:none; stroke:#fff; stroke-miterlimit:10;"/><path d="M5.5,5v-1.5c0-.55.45-1,1-1h10c.55,0,1,.45,1,1v9c0,.55-.45,1-1,1h-1.5" style="fill:none; stroke:#fff; stroke-miterlimit:10;"/></svg>'''


def ensure_icon():
    """Write the embedded tab icon next to the plugin if it's absent, and return
    its path — or "" if it can't be written, so the host uses its default icon."""
    try:
        if not os.path.exists(ICON_PATH):
            with open(ICON_PATH, "w", encoding="utf-8") as fh:
                fh.write(ICON_SVG)
        return ICON_PATH
    except OSError:
        return ""
HTTP_TIMEOUT = 6
UPLOAD_TIMEOUT = 120


def resolve_data_dir():
    here = os.path.abspath(__file__).replace("\\", "/")
    parts = here.split("/")
    if "orca_plugins" in parts:
        return "/".join(parts[: parts.index("orca_plugins")])
    return os.path.dirname(os.path.dirname(here))


DATA_DIR = resolve_data_dir()

# TLS is verified by default. A printer may opt out ("insecure": true) for a
# self-signed LAN certificate — an explicit, per-printer choice, not a blanket
# disable. Plain-HTTP LAN hosts (the common Moonraker/OctoPrint case) don't reach
# this code path at all. Two contexts, picked per request by the printer's flag.
_SSL_VERIFY = ssl.create_default_context()
_SSL_INSECURE = ssl.create_default_context()
_SSL_INSECURE.check_hostname = False
_SSL_INSECURE.verify_mode = ssl.CERT_NONE


def ssl_ctx_for(printer):
    return _SSL_INSECURE if (printer or {}).get("insecure") else _SSL_VERIFY


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
    # Atomic write: a crash mid-write must not corrupt the store.
    tmp = PRINTERS_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(printers, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, PRINTERS_FILE)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass


def normalize_url(url):
    url = (url or "").strip().rstrip("/")
    if url and not url.startswith("http://") and not url.startswith("https://"):
        url = "http://" + url
    return url


# OrcaSlicer exposes many print_host types. We keep the real type for display and
# map it to the status backend we actually implement. OctoPrint-compatible hosts
# (PrusaLink, Repetier) speak the OctoPrint API; Klipper stacks speak Moonraker.
# Everything else falls back to a reachability ping (still shows online/offline and
# opens its web UI). Cloud-only hosts (obico, simplyprint, prusaconnect,
# 3dprinteros) can't be polled over the LAN and stay generic.
_MOONRAKER_COMPATIBLE = {"moonraker", "klipper", "mainsail", "fluidd"}
_OCTOPRINT_COMPATIBLE = {"octoprint", "prusalink", "repetier"}


def normalize_host_type(host_type):
    # Keep OrcaSlicer's real host_type for the tile badge; default empty to generic.
    return (host_type or "").strip().lower() or "generic"


def backend_for_type(host_type):
    t = (host_type or "").lower()
    if t in _MOONRAKER_COMPATIBLE:
        return "moonraker"
    if t in _OCTOPRINT_COMPATIBLE:
        return "octoprint"
    if t == "bambu":
        return "bambu"
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
                "type": normalize_host_type(preset.config_value("host_type")),
                "apikey": preset.config_value("printhost_apikey") or "",
                "source": "orca",
            })
        except Exception:
            continue
    return found


def discover_bambu_printers():
    # Bambu printers don't expose a print_host, so preset discovery misses them.
    # OrcaSlicer records the ones you've connected to in OrcaSlicer.conf: a
    # local_machines entry keyed by serial (not "ip:port"), with the LAN access
    # code in user_access_code/access_code. Surface those for one-click adopt with
    # IP + serial + access code pre-filled. The file has a trailing MD5 line, so
    # decode just the JSON prefix.
    conf = os.path.join(DATA_DIR, "OrcaSlicer.conf")
    try:
        with open(conf, "r", encoding="utf-8-sig") as fh:
            obj, _ = json.JSONDecoder().raw_decode(fh.read().lstrip())
    except (OSError, ValueError):
        return []
    machines = obj.get("local_machines") or {}
    codes = obj.get("user_access_code") or {}
    codes_fallback = obj.get("access_code") or {}
    found = []
    for key, m in machines.items():
        ip = (m or {}).get("dev_ip") or ""
        # Bambu entries are serial-keyed; Moonraker/OctoPrint use "ip:port" == dev_ip.
        if not key or ":" in key or not ip or key == ip:
            continue
        found.append({
            "id": "bambu:" + key,
            "name": (m or {}).get("dev_name") or key,
            "url": normalize_url(ip),
            "type": "bambu",
            "serial": key,
            "access_code": codes.get(key) or codes_fallback.get(key) or "",
            "source": "orca",
        })
    return found


def dashboard_printers():
    # Only the printers the user chose to add (manual entries + printers adopted
    # from OrcaSlicer). Discovery never auto-populates the dashboard.
    result = []
    for p in load_manual_printers():
        p["url"] = normalize_url(p.get("url"))
        p.setdefault("source", "manual")
        result.append(p)
    return result


def available_orca_printers():
    # OrcaSlicer-configured printers not yet added to the dashboard, offered for
    # the user to adopt one by one — network presets (Moonraker/OctoPrint) plus
    # Bambu machines pulled from OrcaSlicer.conf.
    manual = load_manual_printers()
    added_urls = {normalize_url(p.get("url")) for p in manual}
    added_serials = {p.get("serial") for p in manual if p.get("serial")}
    out = []
    for p in discover_orca_printers() + discover_bambu_printers():
        if p.get("url") in added_urls:
            continue
        if p.get("serial") and p["serial"] in added_serials:
            continue
        out.append(p)
    return out


# --------------------------------------------------------------------------- #
# HTTP helpers (stdlib only).
# --------------------------------------------------------------------------- #
def _get_json(url, ctx, headers=None, timeout=HTTP_TIMEOUT):
    req = urllib.request.Request(url, headers=headers or {"Accept": "application/json"}, method="GET")
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _get_bytes(url, ctx, headers=None, timeout=HTTP_TIMEOUT):
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        return resp.read()


def _multipart_upload(url, ctx, headers, filename, file_bytes, extra_fields=None):
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
    with urllib.request.urlopen(req, timeout=UPLOAD_TIMEOUT, context=ctx) as resp:
        return resp.getcode()


# --------------------------------------------------------------------------- #
# Per-backend status polling. Common shape:
# {online, state, nozzle, target_nozzle, bed, target_bed, progress, filename, thumbnail}
# --------------------------------------------------------------------------- #
# Thumbnails are cached by (base, filename): they don't change during a print,
# so we fetch a print's thumbnail once instead of on every status poll.
_thumb_cache = {}


def moonraker_thumbnail(base, ctx, filename):
    if not filename:
        return ""
    key = base + "|" + filename
    if key in _thumb_cache:
        return _thumb_cache[key]
    result = ""
    try:
        meta = _get_json(base + "/server/files/metadata?filename=" + urllib.parse.quote(filename), ctx)
        thumbs = meta.get("result", {}).get("thumbnails", [])
        if thumbs:
            best = max(thumbs, key=lambda t: t.get("size", 0))
            rel = best.get("relative_path") or best.get("thumbnail_path")
            if rel:
                raw = _get_bytes(base + "/server/files/gcodes/" + urllib.parse.quote(rel), ctx)
                result = "data:image/png;base64," + base64.b64encode(raw).decode("ascii")
    except Exception:
        result = ""
    _thumb_cache[key] = result
    if len(_thumb_cache) > 64:  # bound the cache
        _thumb_cache.pop(next(iter(_thumb_cache)))
    return result


def poll_moonraker(base, ctx):
    query = "/printer/objects/query?extruder&heater_bed&print_stats&display_status"
    data = _get_json(base + query, ctx)
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
        # Show the loaded file's thumbnail whenever one is loaded (printing,
        # paused or just finished) — not only while actively printing.
        "thumbnail": moonraker_thumbnail(base, ctx, filename) if filename else "",
    }


def octoprint_thumbnail(base, headers, ctx, filepath):
    # OctoPrint's thumbnail plugins (PrusaSlicer/UFP) attach a "thumbnail" URL to
    # the file resource; fetch it once and inline it. Absent if no such plugin.
    if not filepath:
        return ""
    key = base + "|" + filepath
    if key in _thumb_cache:
        return _thumb_cache[key]
    result = ""
    try:
        meta = _get_json(base + "/api/files/local/" + urllib.parse.quote(filepath), ctx, headers=headers)
        rel = meta.get("thumbnail")
        if rel:
            raw = _get_bytes(base + "/" + rel.lstrip("/"), ctx, headers=headers)
            result = "data:image/png;base64," + base64.b64encode(raw).decode("ascii")
    except Exception:
        result = ""
    _thumb_cache[key] = result
    if len(_thumb_cache) > 64:
        _thumb_cache.pop(next(iter(_thumb_cache)))
    return result


def poll_octoprint(base, ctx, apikey):
    headers = {"Accept": "application/json"}
    if apikey:
        headers["X-Api-Key"] = apikey
    printer = _get_json(base + "/api/printer", ctx, headers=headers)
    temps = printer.get("temperature", {})
    tool0 = temps.get("tool0", {})
    bed = temps.get("bed", {})
    state = printer.get("state", {}).get("text", "Operational")
    progress, filename, filepath = 0, "", ""
    try:
        job = _get_json(base + "/api/job", ctx, headers=headers)
        progress = round(job.get("progress", {}).get("completion") or 0)
        job_file = job.get("job", {}).get("file", {})
        filename = job_file.get("name", "") or ""
        filepath = job_file.get("path", "") or filename
    except Exception:
        pass
    return {
        "online": True, "state": state,
        "nozzle": tool0.get("actual"), "target_nozzle": tool0.get("target"),
        "bed": bed.get("actual"), "target_bed": bed.get("target"),
        "progress": progress, "filename": filename,
        "thumbnail": octoprint_thumbnail(base, headers, ctx, filepath) if filepath else "",
    }


def poll_generic(base, ctx):
    req = urllib.request.Request(base, method="GET")
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT, context=ctx):
        return {"online": True, "state": "reachable", "nozzle": None, "target_nozzle": None,
                "bed": None, "target_bed": None, "progress": None, "filename": "", "thumbnail": ""}


# --------------------------------------------------------------------------- #
# Bambu Lab (LAN mode): a self-contained MQTT-over-TLS client. Bambu printers do
# not speak Moonraker/OctoPrint — in LAN mode they expose an MQTT broker on 8883
# (username "bblp", password = the printer's LAN access code, self-signed cert).
# We connect, subscribe to device/<serial>/report, ask for a full push, read one
# status report and disconnect. Just enough MQTT 3.1.1 to avoid a paho dependency.
# --------------------------------------------------------------------------- #
_BAMBU_STATES = {
    "RUNNING": "printing", "PAUSE": "paused", "IDLE": "idle", "FINISH": "finished",
    "FAILED": "failed", "PREPARE": "preparing", "SLICING": "slicing",
}


def _mqtt_len(n):
    # MQTT "remaining length": 7 bits per byte, high bit = continuation.
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            b |= 0x80
        out.append(b)
        if not n:
            return bytes(out)


def _mqtt_field(data):
    return struct.pack("!H", len(data)) + data


def _recv_exact(sock, n, deadline):
    buf = b""
    while len(buf) < n:
        sock.settimeout(max(0.1, deadline - time.monotonic()))
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise IOError("connection closed")
        buf += chunk
    return buf


def _mqtt_read_packet(sock, deadline):
    header = _recv_exact(sock, 1, deadline)[0]
    length, mult = 0, 1
    while True:
        b = _recv_exact(sock, 1, deadline)[0]
        length += (b & 0x7F) * mult
        if not (b & 0x80):
            break
        mult *= 128
    body = _recv_exact(sock, length, deadline) if length else b""
    return header, body


def _bambu_offline(state):
    return {"online": False, "state": state, "nozzle": None, "target_nozzle": None,
            "bed": None, "target_bed": None, "progress": None, "filename": "", "thumbnail": ""}


def poll_bambu(printer, timeout=8):
    host = urllib.parse.urlparse(normalize_url(printer.get("url"))).hostname or ""
    serial = (printer.get("serial") or "").strip()
    code = (printer.get("access_code") or printer.get("apikey") or "").strip()
    if not host:
        return _bambu_offline("no address")
    if not code:
        return _bambu_offline("needs access code")

    deadline = time.monotonic() + timeout
    try:
        raw = socket.create_connection((host, BAMBU_MQTT_PORT), timeout=timeout)
    except Exception as exc:
        return _bambu_offline(str(exc))
    sock = None
    try:
        # Bambu presents a self-signed cert on the LAN broker.
        sock = _SSL_INSECURE.wrap_socket(raw, server_hostname=host)
        # CONNECT: protocol "MQTT" v4, flags user+pass+clean-session, 60s keepalive.
        var = _mqtt_field(b"MQTT") + bytes([4, 0xC2]) + struct.pack("!H", 60)
        payload = _mqtt_field(b"orca-printers") + _mqtt_field(b"bblp") + _mqtt_field(code.encode("utf-8"))
        body = var + payload
        sock.sendall(b"\x10" + _mqtt_len(len(body)) + body)
        header, _ = _mqtt_read_packet(sock, deadline)
        if (header & 0xF0) != 0x20:
            return _bambu_offline("connect rejected")

        # Without a serial, subscribe to the wildcard: the broker allows it, and
        # the first report's topic (device/<serial>/report) reveals the serial —
        # so IP + access code are enough and nobody retypes 15-char serials.
        report = ("device/%s/report" % serial if serial else "device/+/report").encode("utf-8")
        # SUBSCRIBE (packet id 1) to the report topic at QoS 0.
        sub = struct.pack("!H", 1) + _mqtt_field(report) + b"\x00"
        sock.sendall(b"\x82" + _mqtt_len(len(sub)) + sub)
        _mqtt_read_packet(sock, deadline)  # SUBACK

        def send_pushall(target_serial):
            # PUBLISH a "pushall" so the printer sends a full snapshot immediately.
            request = ("device/%s/request" % target_serial).encode("utf-8")
            push = json.dumps({"pushing": {"sequence_id": "0", "command": "pushall"}}).encode("utf-8")
            pub = _mqtt_field(request) + push
            sock.sendall(b"\x30" + _mqtt_len(len(pub)) + pub)

        if serial:
            send_pushall(serial)

        while time.monotonic() < deadline:
            header, packet = _mqtt_read_packet(sock, deadline)
            if (header & 0xF0) != 0x30 or len(packet) < 2:
                continue
            topic_len = struct.unpack("!H", packet[:2])[0]
            if not serial:
                parts = packet[2:2 + topic_len].decode("utf-8", "replace").split("/")
                if len(parts) == 3 and parts[0] == "device" and parts[1]:
                    serial = parts[1]
                    printer["serial"] = serial  # caller persists the discovery
                    send_pushall(serial)
            try:
                msg = json.loads(packet[2 + topic_len:].decode("utf-8", "replace"))
            except ValueError:
                continue
            info = msg.get("print")
            if not isinstance(info, dict) or ("nozzle_temper" not in info and "gcode_state" not in info):
                continue
            gstate = info.get("gcode_state", "")
            pct = info.get("mc_percent")
            return {
                "online": True,
                "state": _BAMBU_STATES.get(gstate, (gstate or "idle").lower()),
                "nozzle": info.get("nozzle_temper"), "target_nozzle": info.get("nozzle_target_temper"),
                "bed": info.get("bed_temper"), "target_bed": info.get("bed_target_temper"),
                "progress": pct if pct is not None else None,
                "filename": info.get("subtask_name") or info.get("gcode_file") or "",
                "thumbnail": "",
            }
        return {"online": True, "state": "connected", "nozzle": None, "target_nozzle": None,
                "bed": None, "target_bed": None, "progress": None, "filename": "", "thumbnail": ""}
    except Exception as exc:
        return _bambu_offline(str(exc))
    finally:
        try:
            (sock or raw).sendall(b"\xe0\x00")  # DISCONNECT
        except Exception:
            pass
        try:
            (sock or raw).close()
        except Exception:
            pass


_STORE_LOCK = threading.Lock()


def persist_discovered_serial(printer):
    """Write a wildcard-learned Bambu serial back into the stored record, so the
    next poll subscribes to the exact topic and serial-based dedup keeps working.
    Poll threads run concurrently — the store write is serialized."""
    serial = (printer.get("serial") or "").strip()
    url = normalize_url(printer.get("url"))
    if not serial or not url:
        return
    with _STORE_LOCK:
        manual = load_manual_printers()
        changed = False
        for entry in manual:
            if normalize_url(entry.get("url")) == url and not (entry.get("serial") or "").strip():
                entry["serial"] = serial
                changed = True
        if changed:
            save_manual_printers(manual)


# --------------------------------------------------------------------------- #
# Bambu Lab (LAN mode): sending a print. Upload goes over implicit FTPS on 990
# (same bblp / access-code credentials as MQTT); the print is then started with
# an MQTT command. Plain .gcode uses the firmware's gcode_file command; sliced
# .3mf projects use project_file, which is what Bambu's own clients send.
# --------------------------------------------------------------------------- #
class _ImplicitFTPS(ftplib.FTP_TLS):
    # Bambu's FTP server is implicit TLS (the socket is TLS from byte one), which
    # ftplib doesn't speak natively — wrap the control socket before the welcome.
    def connect(self, host="", port=990, timeout=UPLOAD_TIMEOUT):
        self.host = host or self.host
        self.port = port or self.port
        self.timeout = timeout
        raw = socket.create_connection((self.host, self.port), self.timeout)
        self.sock = self.context.wrap_socket(raw, server_hostname=self.host)
        self.af = self.sock.family
        self.file = self.sock.makefile("r", encoding=self.encoding)
        self.welcome = self.getresp()
        return self.welcome


def _ftps_stor(host, access_code, remote, file_bytes):
    """One STOR attempt over ftplib. Returns "" on success or an error string."""
    ftp = _ImplicitFTPS(context=_SSL_INSECURE)
    try:
        ftp.connect(host, 990, timeout=UPLOAD_TIMEOUT)
        ftp.login("bblp", access_code)
        ftp.prot_p()
        ftp.storbinary("STOR " + remote, io.BytesIO(file_bytes))
        return ""
    except Exception as exc:
        return str(exc) or type(exc).__name__
    finally:
        try:
            ftp.quit()
        except Exception:
            try:
                ftp.close()
            except Exception:
                pass


def _curl_stor(host, access_code, remote, file_bytes):
    """Fallback STOR via the system curl (ships with Windows 10+/macOS; Bambu
    Studio itself uses curl). Unlike ftplib, curl handles vsftpd's mandatory TLS
    session reuse on the data channel. Returns "" on success or an error."""
    curl = shutil.which("curl")
    if not curl:
        return "curl not available"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".gcode")
    try:
        tmp.write(file_bytes)
        tmp.close()
        cmd = [curl, "--insecure", "--ftp-pasv", "--silent", "--show-error",
               "--connect-timeout", "15", "--max-time", str(UPLOAD_TIMEOUT),
               "-u", "bblp:" + access_code, "-T", tmp.name,
               "ftps://%s:990/%s" % (host, remote)]
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=UPLOAD_TIMEOUT + 30, creationflags=flags)
        if proc.returncode == 0:
            return ""
        return (proc.stderr or "curl exit %d" % proc.returncode).strip()
    except Exception as exc:
        return str(exc)
    finally:
        try:
            os.remove(tmp.name)
        except OSError:
            pass


def bambu_upload(host, access_code, filename, file_bytes):
    """Upload to the printer's sdcard; returns the sdcard-relative path the file
    landed at (root or cache/). Tries ftplib first, then curl — vsftpd on the
    printer demands TLS session reuse on the data channel, which ftplib can't
    always deliver. Raises with an actionable message when the firmware refuses
    writes altogether (LAN lockdown: Developer Mode off)."""
    errors = []
    refused = 0
    for remote in (filename, "cache/" + filename):
        for stor in (_ftps_stor, _curl_stor):
            err = stor(host, access_code, remote, file_bytes)
            if not err:
                return remote
            errors.append("%s %s: %s" % (stor.__name__.lstrip("_"), remote, err))
            if "553" in err or "550" in err:
                refused += 1
                break  # permission problem — retrying the other transport won't help
    if refused >= 2:
        raise RuntimeError("printer refused the upload — enable Developer Mode (LAN) in the printer settings")
    raise RuntimeError("; ".join(errors[-2:]))


def _bambu_publish(host, code, topic, payload, timeout=10):
    """Connect to the printer's MQTT broker, publish one message, disconnect.
    Returns "" on success or an error string."""
    deadline = time.monotonic() + timeout
    try:
        raw = socket.create_connection((host, BAMBU_MQTT_PORT), timeout=timeout)
    except Exception as exc:
        return str(exc)
    sock = None
    try:
        sock = _SSL_INSECURE.wrap_socket(raw, server_hostname=host)
        var = _mqtt_field(b"MQTT") + bytes([4, 0xC2]) + struct.pack("!H", 60)
        creds = _mqtt_field(b"orca-printers-pub") + _mqtt_field(b"bblp") + _mqtt_field(code.encode("utf-8"))
        body = var + creds
        sock.sendall(b"\x10" + _mqtt_len(len(body)) + body)
        header, _ = _mqtt_read_packet(sock, deadline)
        if (header & 0xF0) != 0x20:
            return "connect rejected"
        pub = _mqtt_field(topic.encode("utf-8")) + json.dumps(payload).encode("utf-8")
        sock.sendall(b"\x30" + _mqtt_len(len(pub)) + pub)
        return ""
    except Exception as exc:
        return str(exc)
    finally:
        try:
            (sock or raw).sendall(b"\xe0\x00")  # DISCONNECT
        except Exception:
            pass
        try:
            (sock or raw).close()
        except Exception:
            pass


def bambu_start_print(host, code, serial, remote_path, use_ams):
    filename = remote_path.rsplit("/", 1)[-1]
    if remote_path.lower().endswith(".3mf"):
        payload = {"print": {
            "sequence_id": "0", "command": "project_file",
            "param": "Metadata/plate_1.gcode",
            "url": "file:///sdcard/" + remote_path,
            "subtask_name": filename,
            "use_ams": bool(use_ams),
            "timelapse": False, "bed_leveling": True,
            "flow_cali": False, "vibration_cali": False, "layer_inspect": False,
            "project_id": "0", "profile_id": "0", "task_id": "0", "subtask_id": "0",
        }}
    else:
        payload = {"print": {"sequence_id": "0", "command": "gcode_file",
                             "param": "/sdcard/" + remote_path}}
    return _bambu_publish(host, code, "device/%s/request" % serial, payload)


def bambu_send(printer, filename, file_bytes, start, use_ams):
    host = urllib.parse.urlparse(normalize_url(printer.get("url"))).hostname or ""
    code = (printer.get("access_code") or printer.get("apikey") or "").strip()
    if not host:
        return {"ok": False, "error": "no address"}
    if not code:
        return {"ok": False, "error": "needs access code"}
    try:
        remote_path = bambu_upload(host, code, filename, file_bytes)
    except Exception as exc:
        return {"ok": False, "error": "upload: %s" % exc}
    if not start:
        return {"ok": True}
    serial = (printer.get("serial") or "").strip()
    if not serial:
        poll_bambu(printer)  # the wildcard subscription learns the serial
        serial = (printer.get("serial") or "").strip()
        if serial:
            persist_discovered_serial(printer)
    if not serial:
        return {"ok": False, "error": "uploaded, but the serial is still unknown — start it from the printer"}
    err = bambu_start_print(host, code, serial, remote_path, use_ams)
    if err:
        return {"ok": False, "error": "print command: %s" % err}
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Auto-naming: nobody should stare at bare IPs. Bambu printers announce
# themselves over SSDP (UDP 2021) with a DevName header; Moonraker exposes the
# Klipper host name. Placeholder names get replaced once and persisted.
# --------------------------------------------------------------------------- #
def ssdp_bambu_names(duration=3.0):
    """Passively collect Bambu SSDP announcements: ip -> {name, serial}.
    Printers NOTIFY every few seconds, so a short listen window is enough."""
    names = {}
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # OrcaSlicer's own Bambu discovery may hold the port; share it.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", 2021))
    except OSError:
        return names
    try:
        sock.settimeout(0.5)
        end = time.monotonic() + duration
        while time.monotonic() < end:
            try:
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            info = {}
            for line in data.decode("utf-8", "replace").splitlines():
                key, _, value = line.partition(":")
                info[key.strip().lower()] = value.strip()
            name = info.get("devname.bambu.com")
            if name:
                ip = info.get("location") or addr[0]
                names[ip] = {"name": name, "serial": info.get("usn") or ""}
    finally:
        try:
            sock.close()
        except OSError:
            pass
    return names


_hostname_cache = {}


def moonraker_hostname(base, ctx):
    if base in _hostname_cache:
        return _hostname_cache[base]
    name = ""
    try:
        info = _get_json(base + "/printer/info", ctx)
        name = (info.get("result") or {}).get("hostname") or ""
    except Exception:
        name = ""
    _hostname_cache[base] = name
    return name


def _needs_name(printer):
    name = (printer.get("name") or "").strip()
    if not name:
        return True
    url = normalize_url(printer.get("url"))
    host = urllib.parse.urlparse(url).hostname or ""
    return name in (host, url, (printer.get("url") or "").strip())


def autoname_printers(printers):
    """Give placeholder-named printers their real names. Runs on a worker thread
    after a poll; touches the store under the lock. Returns True if anything
    changed so the caller can re-push the list."""
    need = [p for p in printers if _needs_name(p)]
    if not need:
        return False
    ssdp = None
    updates = {}
    for p in need:
        kind = backend_for_type(p.get("type"))
        name = ""
        if kind == "bambu":
            if ssdp is None:
                ssdp = ssdp_bambu_names()
            host = urllib.parse.urlparse(normalize_url(p.get("url"))).hostname or ""
            hit = ssdp.get(host)
            if not hit and p.get("serial"):
                hit = next((v for v in ssdp.values() if v.get("serial") == p["serial"]), None)
            name = (hit or {}).get("name") or ""
        elif kind == "moonraker":
            status = p.get("status") or {}
            if status.get("online"):
                name = moonraker_hostname(normalize_url(p.get("url")), ssl_ctx_for(p))
        if name and name != p.get("name"):
            p["name"] = name
            updates[p.get("id")] = name
    if not updates:
        return False
    with _STORE_LOCK:
        manual = load_manual_printers()
        changed = False
        for entry in manual:
            if entry.get("id") in updates and entry.get("name") != updates[entry.get("id")]:
                entry["name"] = updates[entry.get("id")]
                changed = True
        if changed:
            save_manual_printers(manual)
    return True


def _poll_backend(kind, printer, base, ctx):
    if kind == "moonraker":
        return poll_moonraker(base, ctx)
    if kind == "octoprint":
        return poll_octoprint(base, ctx, printer.get("apikey", ""))
    return poll_generic(base, ctx)


def poll_printer(printer):
    base = normalize_url(printer.get("url"))
    kind = (printer.get("type") or "moonraker").lower()
    ctx = ssl_ctx_for(printer)
    backend = backend_for_type(kind)
    if backend == "bambu":
        had_serial = bool((printer.get("serial") or "").strip())
        status = poll_bambu(printer)
        status["backend"] = "bambu"
        if not had_serial and (printer.get("serial") or "").strip():
            persist_discovered_serial(printer)
        return status
    # Don't fully trust the declared type: OrcaSlicer often reports a Klipper host
    # as "octoprint". Try the mapped backend first, then the other HTTP one, so a
    # mislabeled printer still shows real status.
    order = ["octoprint", "moonraker"] if backend == "octoprint" else \
            ["moonraker", "octoprint"] if backend == "moonraker" else ["generic"]
    last_exc = ""
    for k in order:
        try:
            status = _poll_backend(k, printer, base, ctx)
            status["backend"] = k
            return status
        except Exception as exc:
            last_exc = str(exc)
    return {"online": False, "state": "offline", "error": last_exc, "backend": kind, "nozzle": None,
            "target_nozzle": None, "bed": None, "target_bed": None, "progress": None,
            "filename": "", "thumbnail": ""}


def _post_json(url, ctx, headers=None, payload=None, timeout=HTTP_TIMEOUT):
    body = json.dumps(payload if payload is not None else {}).encode("utf-8")
    hdrs = {"Content-Type": "application/json"}
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=body, headers=hdrs, method="POST")
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        return resp.getcode()


# Bambu names the cancel command "stop"; the two HTTP backends call it cancel.
_BAMBU_CONTROL = {"pause": "pause", "resume": "resume", "cancel": "stop"}


def printer_command(printer, action):
    """Pause/resume/cancel the current print on whichever backend the printer
    speaks. Returns {ok} or {ok: False, error}."""
    if action not in _BAMBU_CONTROL:
        return {"ok": False, "error": "unknown action"}
    base = normalize_url(printer.get("url"))
    ctx = ssl_ctx_for(printer)
    kind = backend_for_type(printer.get("type") or "moonraker")
    try:
        if kind == "moonraker":
            _post_json(base + "/printer/print/" + ("cancel" if action == "cancel" else action), ctx)
        elif kind == "octoprint":
            headers = {}
            if printer.get("apikey"):
                headers["X-Api-Key"] = printer["apikey"]
            payload = {"command": "cancel"} if action == "cancel" else {"command": "pause", "action": action}
            _post_json(base + "/api/job", ctx, headers, payload)
        elif kind == "bambu":
            host = urllib.parse.urlparse(base).hostname or ""
            code = (printer.get("access_code") or printer.get("apikey") or "").strip()
            serial = (printer.get("serial") or "").strip()
            if not host or not code or not serial:
                return {"ok": False, "error": "needs IP, access code and serial"}
            err = _bambu_publish(host, code, "device/%s/request" % serial,
                                 {"print": {"sequence_id": "0", "command": _BAMBU_CONTROL[action]}})
            if err:
                return {"ok": False, "error": err}
        else:
            return {"ok": False, "error": "unsupported backend"}
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def send_gcode(printer, filename, file_bytes, start, use_ams=True):
    base = normalize_url(printer.get("url"))
    kind = backend_for_type(printer.get("type") or "moonraker")
    ctx = ssl_ctx_for(printer)
    try:
        if kind == "moonraker":
            fields = {"print": "true"} if start else {}
            _multipart_upload(base + "/server/files/upload", ctx, {}, filename, file_bytes, fields)
        elif kind == "octoprint":
            headers = {}
            if printer.get("apikey"):
                headers["X-Api-Key"] = printer["apikey"]
            fields = {"select": "true", "print": "true"} if start else {}
            _multipart_upload(base + "/api/files/local", ctx, headers, filename, file_bytes, fields)
        elif kind == "bambu":
            return bambu_send(printer, filename, file_bytes, start, use_ams)
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
  button.danger { background:transparent; color:#c0504d; border-color:#c0504d; }
  button:disabled { opacity:.5; cursor:default; }
  #grid { flex:1 1 auto; overflow:auto; padding:12px;
          display:grid; grid-template-columns:repeat(auto-fill,minmax(310px,1fr)); gap:12px; align-content:start; }
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
  /* Pin the action row to the bottom so it lines up across cards regardless of
     how much status (thumbnail, filename, progress) each one has above it. */
  .row { display:flex; gap:6px; align-items:center; margin-top:auto; }
  .row label { display:flex; align-items:center; gap:4px; font-size:11px; margin-right:auto; }
  .row button { white-space:nowrap; }
  .empty { color:var(--orca-muted,#a0a0a0); padding:24px; text-align:center; }
  #status { font-size:11px; color:var(--orca-muted,#a0a0a0); }
</style></head>
<body>
  <div id="bar">
    <label id="sel-all-l" title="Select or deselect every printer"><input id="sel-all" type="checkbox"> all</label>
    <span id="status"></span>
    <button id="send" class="ghost" disabled>Send G-code to selected…</button>
    <select id="f-adopt" title="Add a printer configured in OrcaSlicer" style="display:none"></select>
    <input id="f-name" placeholder="Name" size="8">
    <input id="f-url" placeholder="192.168.0.42" size="14">
    <select id="f-type" title="Connection type. Klipper/Moonraker, OctoPrint and Bambu LAN report full status; PrusaLink/Repetier use the OctoPrint API; the rest show reachability and open their web UI.">
      <option value="moonraker">Moonraker / Klipper</option>
      <option value="octoprint">OctoPrint</option>
      <option value="bambu">Bambu (LAN)</option>
      <option value="prusalink">PrusaLink</option>
      <option value="repetier">Repetier</option>
      <option value="duet">Duet</option>
      <option value="mks">MKS</option>
      <option value="flashair">FlashAir</option>
      <option value="astrobox">AstroBox</option>
      <option value="esp3d">ESP3D</option>
      <option value="crealityprint">Creality</option>
      <option value="flashforge">FlashForge</option>
      <option value="elegoolink">Elegoo</option>
      <option value="generic">Other</option>
    </select>
    <input id="f-serial" placeholder="Serial (auto)" size="10" style="display:none" title="Leave empty — the serial is discovered from the printer automatically. Fill only to pin a specific printer. MQTT port 8883 is used.">
    <input id="f-access" placeholder="Access code" size="10" style="display:none">
    <label id="f-insecure-l" title="Check only if the printer is served over HTTPS with a self-signed certificate — this skips TLS verification for it. Plain http:// LAN printers don't need this." style="font-size:11px;display:flex;align-items:center;gap:4px">
      <input id="f-insecure" type="checkbox"> self-signed</label>
    <button id="add">Add</button>
    <button id="refresh" class="ghost">Refresh</button>
    <input id="file" type="file" accept=".gcode,.gco,.g,.gz,.3mf" style="display:none">
  </div>
  <div id="confirm" style="display:none;position:absolute;inset:0;background:rgba(0,0,0,.55);
       align-items:center;justify-content:center;z-index:20">
    <div style="background:var(--orca-bg,#1e1e2e);border:1px solid var(--orca-border,#3c3c4c);
         border-radius:10px;padding:18px;max-width:420px;width:90%">
      <div style="font-weight:600;margin-bottom:8px">Start print on multiple printers?</div>
      <div id="confirm-body" style="font-size:12px;color:var(--orca-muted,#a0a0a0);margin-bottom:12px"></div>
      <label style="display:flex;align-items:center;gap:6px;font-size:12px;margin-bottom:12px">
        <input id="confirm-start" type="checkbox" checked> Start printing immediately after upload</label>
      <label id="confirm-ams-l" style="display:none;align-items:center;gap:6px;font-size:12px;margin-bottom:12px">
        <input id="confirm-ams" type="checkbox" checked> Use AMS (Bambu)</label>
      <div style="display:flex;gap:8px;justify-content:flex-end">
        <button id="confirm-cancel" class="ghost">Cancel</button>
        <button id="confirm-ok">Send</button>
      </div>
    </div>
  </div>
  <div id="grid"><div class="empty">Loading…</div></div>
<script>
'use strict';
var grid = document.getElementById('grid');
var sendBtn = document.getElementById('send');
var fileInput = document.getElementById('file');
var current = [];
var selected = {};   // url -> true

function esc(s){ return String(s == null ? '' : s).replace(/[&<>"]/g, function(c){
  return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]; }); }
function temp(v){ return v == null ? '—' : Math.round(v) + '°'; }
// Friendly badge for the connection type — the ecosystem we actually detect,
// not the transport. (OctoPrint can front any firmware, so we don't claim Marlin.)
function typeLabel(t){
  return { moonraker:'Klipper', klipper:'Klipper', octoprint:'OctoPrint',
    prusalink:'PrusaLink', prusaconnect:'PrusaConnect', duet:'Duet', repetier:'Repetier',
    mks:'MKS', flashair:'FlashAir', astrobox:'AstroBox', esp3d:'ESP3D',
    crealityprint:'Creality', flashforge:'FlashForge', simplyprint:'SimplyPrint',
    elegoolink:'Elegoo', '3dprinteros':'3DPrinterOS', obico:'Obico',
    bambu:'Bambu Lab', generic:'Other' }[(t || '').toLowerCase()] || (t || 'Other');
}

// Normalize backend state strings ("printing", "RUNNING", "Printing from SD"…)
// into the three kinds the control buttons care about.
function stateKind(s){
  var t = String((s && s.state) || '').toLowerCase();
  if (t.indexOf('paus') >= 0) return 'paused';
  if (t.indexOf('print') >= 0 || t === 'running') return 'printing';
  return 'idle';
}

function updateSendBtn(){
  var n = Object.keys(selected).filter(function(k){ return selected[k]; }).length;
  sendBtn.disabled = n === 0;
  sendBtn.textContent = n ? ('Send G-code to ' + n + ' selected…') : 'Send G-code to selected…';
  var all = document.getElementById('sel-all');
  all.checked = current.length > 0 && current.every(function(p){ return selected[p.url]; });
}

document.getElementById('sel-all').addEventListener('change', function(e){
  current.forEach(function(p){ selected[p.url] = e.target.checked; });
  render(current);
});

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
      '<span class="src">' + esc(typeLabel(p.type)) + '</span>' +
      '<span class="state">' + esc(s.state || (s.online ? 'online' : 'offline')) + '</span></div>' +
      '<div class="body">' + thumb + '<div class="meta">' +
      '<div class="temps"><span>Nozzle <b>' + temp(s.nozzle) + '</b>' + (s.target_nozzle ? '/' + temp(s.target_nozzle) : '') + '</span>' +
      '<span>Bed <b>' + temp(s.bed) + '</b>' + (s.target_bed ? '/' + temp(s.target_bed) : '') + '</span></div>' +
      fname + prog + '</div></div>';
    var kind = stateKind(s);
    var ctl = '';
    if (s.online && kind === 'printing')
      ctl = '<button class="ctl ghost" data-a="pause">Pause</button><button class="ctl danger" data-a="cancel">Stop</button>';
    else if (s.online && kind === 'paused')
      ctl = '<button class="ctl ghost" data-a="resume">Resume</button><button class="ctl danger" data-a="cancel">Stop</button>';
    tile.innerHTML +=
      '<div class="row"><label><input type="checkbox" class="sel"' + (selected[p.url] ? ' checked' : '') + '> select</label>' +
      ctl +
      '<button class="print ghost">Print…</button>' +
      // Bambu has no LAN web page to open; its full UI lives in OrcaSlicer's Device tab.
      (p.type === 'bambu' ? '' : '<button class="open ghost">Open</button>') +
      '<button class="remove ghost">Remove</button></div>';
    tile.querySelector('.sel').addEventListener('change', function(e){
      selected[p.url] = e.target.checked; updateSendBtn(); });
    // Stopping a print is destructive — the button arms on the first click and
    // fires only when confirmed within 3 seconds.
    Array.prototype.forEach.call(tile.querySelectorAll('.ctl'), function(btn){
      btn.addEventListener('click', function(){
        var a = btn.getAttribute('data-a');
        if (a === 'cancel' && !btn._armed) {
          btn._armed = true; btn.textContent = 'Sure?';
          setTimeout(function(){ btn._armed = false; btn.textContent = 'Stop'; }, 3000);
          return;
        }
        btn.disabled = true;
        orca.postMessage({ type:'printer-cmd', url:p.url, action:a });
      });
    });
    tile.querySelector('.print').addEventListener('click', function(){ pickAndSend([p.url]); });
    var openBtn = tile.querySelector('.open');
    if (openBtn) openBtn.addEventListener('click', function(){
      orca.postMessage({ type:'open', url:p.url, name:p.name || p.url }); });
    tile.querySelector('.remove').addEventListener('click', function(){ orca.postMessage({ type:'remove', id:p.id }); });
    grid.appendChild(tile);
  });
  updateSendBtn();
}


// Bambu (LAN) needs a serial + access code instead of the self-signed toggle;
// swap the relevant fields when the backend type changes.
var typeSel = document.getElementById('f-type');
function syncTypeFields(){
  var bambu = typeSel.value === 'bambu';
  // The serial is never typed: the MQTT wildcard learns it on first poll and
  // SSDP discovery prefills it. The hidden input only carries those values.
  document.getElementById('f-access').style.display = bambu ? '' : 'none';
  document.getElementById('f-insecure-l').style.display = bambu ? 'none' : '';
  document.getElementById('f-url').placeholder = bambu ? '192.168.0.42 (printer IP)' : '192.168.0.42';
}
typeSel.addEventListener('change', syncTypeFields);
syncTypeFields();

document.getElementById('add').addEventListener('click', function(){
  var name = document.getElementById('f-name').value.trim();
  var url = document.getElementById('f-url').value.trim();
  if (!url) return;
  orca.postMessage({ type:'add', name:name, url:url,
                     kind:typeSel.value,
                     serial:document.getElementById('f-serial').value.trim(),
                     access_code:document.getElementById('f-access').value.trim(),
                     insecure:document.getElementById('f-insecure').checked });
  document.getElementById('f-name').value = ''; document.getElementById('f-url').value = '';
  document.getElementById('f-serial').value = ''; document.getElementById('f-access').value = '';
  document.getElementById('f-insecure').checked = false;
});
document.getElementById('refresh').addEventListener('click', function(){ orca.postMessage({ type:'refresh' }); });

// Print flow (per-printer or batch): pick a file → confirm (starting a physical
// print is not reversible) → hand it to Python with the target printer urls.
var pending = null;      // { file, urls }
var pendingUrls = null;  // urls chosen before opening the file dialog
function pickAndSend(urls){
  if (!urls || !urls.length) return;
  pendingUrls = urls;
  fileInput.click();
}
sendBtn.addEventListener('click', function(){
  pickAndSend(Object.keys(selected).filter(function(k){ return selected[k]; }));
});
fileInput.addEventListener('change', function(){
  var file = fileInput.files && fileInput.files[0];
  fileInput.value = '';
  var urls = pendingUrls; pendingUrls = null;
  if (!file || !urls || !urls.length) return;
  pending = { file:file, urls:urls };
  var targets = current.filter(function(p){ return urls.indexOf(p.url) >= 0; });
  var names = targets.map(function(p){ return esc(p.name || p.url); });
  var hasBambu = targets.some(function(p){ return (p.type || '').toLowerCase() === 'bambu'; });
  // .3mf project files start only on Bambu; Moonraker/OctoPrint queue G-code.
  var warn = (/\.3mf$/i.test(file.name) && targets.some(function(p){ return (p.type || '').toLowerCase() !== 'bambu'; }))
    ? '<br><span style="color:#c0504d">.3mf starts only on Bambu printers — the others expect G-code.</span>' : '';
  document.getElementById('confirm-ams-l').style.display = hasBambu ? 'flex' : 'none';
  document.getElementById('confirm-body').innerHTML =
    'Upload <b>' + esc(file.name) + '</b> to ' + urls.length + ' printer(s):<br>' + names.join(', ') + warn;
  document.getElementById('confirm').style.display = 'flex';
});
document.getElementById('confirm-cancel').addEventListener('click', function(){
  pending = null; document.getElementById('confirm').style.display = 'none'; });
document.getElementById('confirm-ok').addEventListener('click', function(){
  if (!pending) return;
  var start = document.getElementById('confirm-start').checked;
  var file = pending.file, urls = pending.urls;
  pending = null; document.getElementById('confirm').style.display = 'none';
  var reader = new FileReader();
  reader.onload = function(){
    var b64 = String(reader.result).split(',')[1] || '';
    document.getElementById('status').textContent = 'Sending ' + file.name + ' to ' + urls.length + '…';
    orca.postMessage({ type:'mass-print', filename:file.name, dataB64:b64, urls:urls, start:start,
                       use_ams:document.getElementById('confirm-ams').checked });
  };
  reader.readAsDataURL(file);
});

// "Add from OrcaSlicer": populate a picker with printers Orca knows about that
// are not on the dashboard yet; choosing one adopts it (user decides, no auto-add).
var lastAvailable = [];
var lastDiscovered = [];
function renderAvailable(){
  var sel = document.getElementById('f-adopt');
  var items = lastAvailable.concat(lastDiscovered);
  if (!items.length){ sel.style.display = 'none'; sel.innerHTML = ''; return; }
  var opts = '<option value="">Add a printer…</option>';
  items.forEach(function(p, i){
    var label = (p.source === 'network') ? (p.name + ' — found on network') : (p.name || p.url);
    opts += '<option value="' + i + '">' + esc(label) + '</option>';
  });
  sel.innerHTML = opts;
  sel.style.display = '';
  sel._items = items;
}
document.getElementById('f-adopt').addEventListener('change', function(e){
  var i = e.target.value;
  if (i === '') return;
  var p = (e.target._items || [])[i];
  e.target.value = '';
  if (!p) return;
  if (p.source === 'network') {
    // SSDP finds the printer but not its access code — prefill the form and
    // let the user finish the add with the code from the printer's screen.
    document.getElementById('f-name').value = p.name || '';
    document.getElementById('f-url').value = p.url || '';
    document.getElementById('f-type').value = 'bambu';
    syncTypeFields();
    document.getElementById('f-serial').value = p.serial || '';
    document.getElementById('f-access').focus();
    return;
  }
  orca.postMessage({ type:'adopt', printer:p });
});

orca.onMessage(function(data){
  if (!data) return;
  if (data.printers) render(data.printers);
  if (data.available) { lastAvailable = data.available; renderAvailable(); }
  if (data.discovered) { lastDiscovered = data.discovered; renderAvailable(); }
  if (data.status !== undefined) document.getElementById('status').textContent = data.status;
});
orca.postMessage({ type:'ready' });
// Live dashboard: refresh statuses while the tab is open. Status-only tick —
// the store and the picker are rebuilt only on explicit Refresh/actions.
setInterval(function(){
  try { orca.postMessage({ type:'poll' }); } catch (e) { /* bridge not ready */ }
}, 15000);
</script>
</body>
</html>
"""


# A plain full-window iframe of the printer's web UI — opened as its own host
# window (create_window) so it stays inside OrcaSlicer without hijacking the
# dashboard tab or launching an external browser.
VIEWER_PAGE = """<!DOCTYPE html><html><head><meta charset="utf-8"><style>
html,body{margin:0;height:100%}iframe{border:0;width:100%;height:100%;display:block}
</style></head><body><iframe src="__URL__" allow="fullscreen"></iframe></body></html>"""


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
                icon=ensure_icon(),
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

    def _add_manual(self, entry):
        manual = load_manual_printers()
        new_id = max([p.get("id", 0) for p in manual if isinstance(p.get("id"), int)], default=0) + 1
        entry["id"] = new_id
        manual.append(entry)
        save_manual_printers(manual)

    # on_message runs on the UI thread — host reads happen here, network/disk on workers.
    def on_message(self, msg):
        msg = msg or {}
        kind = msg.get("type")
        if kind in ("ready", "refresh"):
            self._refresh_async()  # host reads run on the UI thread
        elif kind == "poll":
            # Periodic status tick from the page: statuses only, no store/picker
            # rebuild, and never more than one poll in flight.
            if not getattr(self, "_polling", False):
                self._polling = True
                printers = dashboard_printers()
                def run():
                    try:
                        self._poll_and_push(printers)
                    finally:
                        self._polling = False
                threading.Thread(target=run, daemon=True).start()
        elif kind == "printer-cmd":
            printers = dashboard_printers()
            threading.Thread(target=self._do_printer_cmd, args=(msg, printers), daemon=True).start()
        elif kind == "add":
            self._add_manual({"name": msg.get("name") or msg.get("url"),
                              "url": normalize_url(msg.get("url")), "type": msg.get("kind") or "moonraker",
                              "serial": msg.get("serial") or "", "access_code": msg.get("access_code") or "",
                              "insecure": bool(msg.get("insecure")), "source": "manual"})
            self._refresh_async()
        elif kind == "adopt":
            # User chose an OrcaSlicer-configured printer to put on the dashboard.
            p = msg.get("printer") or {}
            if p.get("url"):
                self._add_manual({"name": p.get("name") or p.get("url"), "url": normalize_url(p.get("url")),
                                  "type": p.get("type") or "moonraker", "apikey": p.get("apikey", ""),
                                  "serial": p.get("serial") or "", "access_code": p.get("access_code") or "",
                                  "insecure": bool(p.get("insecure")), "source": "orca"})
            self._refresh_async()
        elif kind == "remove":
            manual = [p for p in load_manual_printers() if p.get("id") != msg.get("id")]
            save_manual_printers(manual)
            self._refresh_async()
        elif kind == "open":
            url = normalize_url(msg.get("url"))
            if url:
                self._open_viewer(url, msg.get("name") or url)
        elif kind == "mass-print":
            printers = dashboard_printers()  # host read on the UI thread
            threading.Thread(target=self._do_mass_print, args=(msg, printers), daemon=True).start()

    def _open_viewer(self, url, name):
        # Printer web UI in its own host window — inside Orca, no external browser
        # and without hijacking the dashboard tab. Keep a reference so it lives on.
        try:
            win = orca.host.ui.create_window(
                title=(name or url)[:80], html=VIEWER_PAGE.replace("__URL__", url),
                width=1100, height=760, on_message=lambda m: None, on_close=lambda: None)
            if not hasattr(self, "_viewers"):
                self._viewers = []
            self._viewers.append(win)
        except Exception:
            pass

    def _refresh_async(self):
        printers = dashboard_printers()       # UI thread: dashboard = user-chosen
        available = available_orca_printers()  # UI thread: Orca printers not yet added
        # Render the list right away so an add/remove shows instantly, then poll the
        # printers in the background and push their status once it arrives.
        if self.win is not None and self.win.is_open():
            self.win.post({"printers": printers, "available": available})
        threading.Thread(target=self._poll_and_push, args=(printers, True), daemon=True).start()

    @staticmethod
    def _poll_into(printer):
        printer["status"] = poll_printer(printer)

    def _poll_and_push(self, printers, discover=False):
        # Poll all printers concurrently — one offline/slow host would otherwise
        # block the rest for its full timeout, making add/remove feel sluggish.
        threads = [threading.Thread(target=self._poll_into, args=(p,), daemon=True) for p in printers]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # Placeholder names (bare IPs) get their real names once statuses are in.
        try:
            autoname_printers(printers)
        except Exception:
            pass
        if self.win is not None and self.win.is_open():
            self.win.post({"printers": printers})
        if not discover:
            return
        # Bambu printers announce themselves over SSDP — offer ones that aren't
        # on the dashboard yet (full refresh only, not every status tick).
        try:
            known_hosts = {urllib.parse.urlparse(normalize_url(p.get("url"))).hostname for p in printers}
            known_serials = {(p.get("serial") or "").strip() for p in printers if (p.get("serial") or "").strip()}
            found = []
            for ip, info in ssdp_bambu_names(2.0).items():
                if ip in known_hosts or (info.get("serial") and info["serial"] in known_serials):
                    continue
                found.append({"name": info.get("name") or ip, "url": ip,
                              "serial": info.get("serial") or "", "type": "bambu", "source": "network"})
            if found and self.win is not None and self.win.is_open():
                self.win.post({"discovered": found})
        except Exception:
            pass

    def _do_printer_cmd(self, msg, printers):
        target = normalize_url(msg.get("url") or "")
        action = msg.get("action") or ""
        printer = next((p for p in printers if normalize_url(p.get("url")) == target), None)
        if printer is None:
            return
        result = printer_command(printer, action)
        label = printer.get("name") or target
        if result.get("ok"):
            self._post_status("%s: %s sent." % (label, action))
        else:
            self._post_status("%s: %s failed — %s" % (label, action, result.get("error") or "error"))
        self._poll_and_push(printers)

    def _do_mass_print(self, msg, printers):
        try:
            file_bytes = base64.b64decode(msg.get("dataB64") or "")
        except Exception:
            self._post_status("Send failed: could not read the file.")
            return
        filename = msg.get("filename") or "print.gcode"
        start = bool(msg.get("start"))
        use_ams = bool(msg.get("use_ams", True))
        targets = {normalize_url(u) for u in (msg.get("urls") or [])}
        selected = [p for p in printers if normalize_url(p.get("url")) in targets]
        ok, fail, errors = 0, 0, []
        for p in selected:
            result = send_gcode(p, filename, file_bytes, start, use_ams)
            if result.get("ok"):
                ok += 1
            else:
                fail += 1
                errors.append("%s: %s" % (p.get("name") or p.get("url"), result.get("error") or "failed"))
        text = "Sent to %d printer(s)%s." % (ok, (", %d failed" % fail) if fail else "")
        if errors:
            text += " " + "; ".join(errors[:3])
        self._post_status(text)
        self._refresh_async()

    def _post_status(self, text):
        if self.win is not None and self.win.is_open():
            self.win.post({"status": text})


@orca.plugin
class PrintersPlugin(orca.base):
    def register_capabilities(self):
        orca.register_capability(PrintersDashboard)
