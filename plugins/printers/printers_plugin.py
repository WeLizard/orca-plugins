# /// script
# requires-python = ">=3.12"
# dependencies = []
#
# [tool.orcaslicer.plugin]
# id = "printers"
# name = "Print Farm"
# description = "Cross-vendor print farm dashboard: live status, temperatures, print progress and thumbnails, one click to each printer's web interface, and G-code delivery to several printers at once."
# author = "FilamentHub"
# version = "0.0.5"
#
# # Printers live on user-chosen LAN addresses no static allow-list can enumerate,
# # so this declares intent for a future "local-network" permission class.
# # It is ignored by the current host.
# network = ["local-network"]
# ///
"""Print Farm — a cross-vendor printer dashboard plugin for OrcaSlicer.

Opens a host-managed dashboard window with a tile per printer: live status,
nozzle/bed temperatures, print progress and the current print's thumbnail,
plus one click to the printer's web UI. It also sends a G-code file to several
printers at once ("send to printers" / batch print).

Printers come from two sources, merged and de-duplicated by URL:
  * OrcaSlicer's own printer presets that have a network address configured
    (read via orca.host.preset_bundle() on the UI thread) — no manual entry.
  * Manually added entries, stored in Orca's private plugin storage when
    available, with a stable Orca data-root directory as an old-host fallback.

Active backends: Moonraker (Klipper), OctoPrint, and Bambu Lab in LAN mode.
Other OrcaSlicer host types remain visible as planned integrations, but are not
treated as working until their status, control, and upload transports exist.
Bambu talks MQTT over TLS on its LAN port (user "bblp", the
printer's access code); a self-contained stdlib MQTT client reads one status
report, so no third-party dependency and no cloud round-trip. Everything is
stdlib-only; host calls stay on the UI thread and all network / disk work is
offloaded to worker threads.

What is intentionally NOT done here, because it belongs to the host and would be
a good plugin capability to expose rather than work around:
  * "Open" uses a confined host window for the printer's web UI; a plugin cannot
    programmatically open the native Device tab for a specific printer.
  * The outbox uses the slicing-pipeline G-code export seam, which currently
    runs on file export and print-host upload rather than on Slice alone.
"""

import base64
import ftplib
import html
import io
import json
import os
import queue
import shutil
import socket
import ssl
import struct
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import orca


class BackgroundJob:
    def __init__(self):
        self._done = threading.Event()
        self._result = None
        self._error = None

    def finish(self, result=None, error=None):
        self._result = result
        self._error = error
        self._done.set()

    def wait(self, timeout=None):
        if not self._done.wait(timeout):
            raise TimeoutError("background job did not finish")
        if self._error is not None:
            raise self._error
        return self._result


class PollBatch:
    """Run one completion callback after every printer in a poll batch finishes."""

    def __init__(self, count, on_complete):
        self._remaining = count
        self._on_complete = on_complete
        self._lock = threading.Lock()

    def finish_one(self):
        callback = None
        with self._lock:
            self._remaining -= 1
            if self._remaining == 0:
                callback, self._on_complete = self._on_complete, None
        if callback is not None:
            callback()


_POOL_STOP = object()


class ReusableDaemonPool:
    """A lifecycle-aware bounded pool of reusable daemon workers."""

    def __init__(self, name, max_workers, idle_timeout=45.0):
        self._name = name
        self._max_workers = max(1, int(max_workers))
        self._idle_timeout = idle_timeout
        self._jobs = queue.Queue()
        self._lock = threading.Lock()
        self._workers = set()
        self._busy = 0
        self._sequence = 0
        self._generation = 0
        self._stopping = False
        self._local = threading.local()

    def activate(self):
        """Accept work for a new plugin lifecycle generation."""
        with self._lock:
            if self._stopping:
                self._generation += 1
                self._stopping = False

    def current_job_is_active(self):
        """Whether the calling job still belongs to the loaded plugin."""
        generation = getattr(self._local, "generation", None)
        if generation is None:
            return True
        with self._lock:
            return not self._stopping and generation == self._generation

    def submit(self, function, *args, **kwargs):
        job = BackgroundJob()
        with self._lock:
            if self._stopping:
                job.finish(error=RuntimeError("background worker is stopped"))
                return job
            generation = self._generation
            self._jobs.put((generation, job, function, args, kwargs))
            desired = min(self._max_workers, max(1, self._busy + self._jobs.qsize()))
            while len(self._workers) < desired:
                self._sequence += 1
                worker = threading.Thread(
                    target=self._run,
                    name="%s-%d" % (self._name, self._sequence),
                    daemon=True,
                )
                self._workers.add(worker)
                worker.start()
        return job

    def shutdown(self, wait_timeout=0.25):
        """Reject new work and discard every queued job from this generation."""
        with self._lock:
            if not self._stopping:
                self._stopping = True
                self._generation += 1
            workers = list(self._workers)
            while True:
                try:
                    marker, job, _function, _args, _kwargs = self._jobs.get_nowait()
                except queue.Empty:
                    break
                else:
                    if marker is not _POOL_STOP:
                        job.finish(error=RuntimeError("background job cancelled"))
                    self._jobs.task_done()
            for _worker in workers:
                self._jobs.put((_POOL_STOP, self._generation, None, None, None))
        deadline = time.monotonic() + max(0.0, wait_timeout)
        for worker in workers:
            if worker is threading.current_thread():
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            worker.join(remaining)

    def _run(self):
        current = threading.current_thread()
        idle_deadline = time.monotonic() + self._idle_timeout
        while True:
            remaining = idle_deadline - time.monotonic()
            if remaining <= 0:
                remaining = 0.001
            try:
                generation, job, function, args, kwargs = self._jobs.get(timeout=remaining)
            except queue.Empty:
                with self._lock:
                    if self._stopping or self._jobs.empty():
                        self._workers.discard(current)
                        return
                idle_deadline = time.monotonic() + self._idle_timeout
                continue
            if generation is _POOL_STOP:
                with self._lock:
                    should_stop = self._stopping and job == self._generation
                    if should_stop:
                        self._workers.discard(current)
                self._jobs.task_done()
                if should_stop:
                    return
                idle_deadline = time.monotonic() + self._idle_timeout
                continue
            with self._lock:
                should_run = not self._stopping and generation == self._generation
                if not should_run:
                    job.finish(error=RuntimeError("background job cancelled"))
                    self._jobs.task_done()
                    continue
                self._busy += 1
            try:
                self._local.generation = generation
                job.finish(result=function(*args, **kwargs))
            except Exception as exc:
                job.finish(error=exc)
            finally:
                try:
                    del self._local.generation
                except AttributeError:
                    pass
                with self._lock:
                    self._busy -= 1
                self._jobs.task_done()
                idle_deadline = time.monotonic() + self._idle_timeout


# Host callbacks stay on the UI thread. One coordinator serializes state
# mutations; a bounded reusable pool preserves parallel LAN polling without
# creating a fresh set of threads on every 15-second refresh.
ACTION_WORKER = ReusableDaemonPool("printers-action", max_workers=1)
POLL_WORKERS = ReusableDaemonPool("printers-poll", max_workers=8)

_PLUGIN_RUNTIME_LOCK = threading.Lock()
_PLUGIN_RUNTIME_ACTIVE = False


def start_plugin_runtime():
    """Start process-wide resources from an audited lifecycle callback."""
    global _PLUGIN_RUNTIME_ACTIVE
    with _PLUGIN_RUNTIME_LOCK:
        if _PLUGIN_RUNTIME_ACTIVE:
            return False
        _PLUGIN_RUNTIME_ACTIVE = True
    ACTION_WORKER.activate()
    POLL_WORKERS.activate()
    refresh_ui_language()
    configure_plugin_storage()
    return True


def stop_plugin_runtime():
    """Stop accepting work and invalidate all jobs from the loaded generation."""
    global _PLUGIN_RUNTIME_ACTIVE
    with _PLUGIN_RUNTIME_LOCK:
        if not _PLUGIN_RUNTIME_ACTIVE:
            return False
        _PLUGIN_RUNTIME_ACTIVE = False
    ACTION_WORKER.shutdown()
    POLL_WORKERS.shutdown()
    return True


class _PluginRuntimeLifecycleMixin:
    def on_load(self):
        start_plugin_runtime()

    def on_cancelled(self):
        stop_plugin_runtime()

    def on_unload(self):
        stop_plugin_runtime()


def post_window(window, payload):
    for worker_name in ("ACTION_WORKER", "POLL_WORKERS"):
        worker = globals().get(worker_name)
        if worker is not None and not worker.current_job_is_active():
            return False
    try:
        post = getattr(window, "post", None)
        if window is None or not window.is_open() or not callable(post):
            return False
        post(payload)
        return True
    except Exception:
        return False

# Single source of truth for the version. pyproject.toml derives the wheel
# version from this (dynamic version), and build_package.py validates that the
# PEP 723 `# version` comment matches it — so the manifest and the wheel can't
# drift.
PLUGIN_VERSION = "0.0.5"

ORCA_UI_LOCALES = (
    "ca", "cs", "de", "en", "es", "eu", "fr", "hu", "it", "ja", "ko",
    "lt", "nl", "pl", "pt_BR", "ru", "sv", "th", "tr", "uk", "vi",
    "zh_CN", "zh_TW",
)
_CANONICAL_UI_LOCALES = {locale.lower(): locale for locale in ORCA_UI_LOCALES}
_UI_LOCALE_ALIASES = {
    "zh": "zh_CN",
    "zh_hans": "zh_CN",
    "zh_hans_cn": "zh_CN",
    "zh_hant": "zh_TW",
    "zh_hant_tw": "zh_TW",
}
_LOCALE_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "printers_locales",
)


def normalize_ui_language(language):
    if not language:
        return ""
    token = str(language).strip().replace("-", "_")
    lowered = token.lower()
    alias = _UI_LOCALE_ALIASES.get(lowered)
    if alias:
        return alias
    exact = _CANONICAL_UI_LOCALES.get(lowered)
    if exact:
        return exact
    base = lowered.split("_", 1)[0]
    return _CANONICAL_UI_LOCALES.get(base, "en")


def host_ui_language():
    app_language = getattr(getattr(orca, "host", None), "app_language", None)
    if not callable(app_language):
        return ""
    try:
        return normalize_ui_language(app_language())
    except (AttributeError, RuntimeError):
        return ""


def load_ui_catalogs(directory=None):
    root = directory or _LOCALE_DIR
    catalogs = {}
    try:
        names = sorted(os.listdir(root))
    except OSError:
        names = []
    for name in names:
        if not name.endswith(".json"):
            continue
        locale = name[:-5]
        if locale not in ORCA_UI_LOCALES:
            continue
        try:
            with open(os.path.join(root, name), "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in data.items()
        ):
            continue
        catalogs[locale] = data
    catalogs.setdefault("en", {})
    return catalogs


UI_COPY = load_ui_catalogs()
_CACHED_UI_LANGUAGE = ""


def resolved_ui_catalog(language):
    locale = normalize_ui_language(language) or "en"
    resolved = dict(UI_COPY.get("en", {}))
    base = locale.split("_", 1)[0]
    if base != "en":
        resolved.update(UI_COPY.get(base, {}))
    if locale not in {"en", base}:
        resolved.update(UI_COPY.get(locale, {}))
    return resolved


def refresh_ui_language():
    global _CACHED_UI_LANGUAGE
    language = host_ui_language()
    if language:
        _CACHED_UI_LANGUAGE = language
    return language


def ui_text(key, **values):
    template = resolved_ui_catalog(_CACHED_UI_LANGUAGE or "en").get(key, key)
    return template.format(**values)


def ui_state(value):
    key = {
        "online": "stateOnline",
        "offline": "stateOffline",
        "checking": "stateChecking",
        "not available yet": "stateUnavailable",
        "idle": "stateIdle",
        "standby": "stateStandby",
        "printing": "statePrinting",
        "running": "statePrinting",
        "paused": "statePaused",
        "complete": "stateComplete",
        "completed": "stateComplete",
        "error": "stateError",
    }.get(str(value or "").lower())
    return ui_text(key) if key else str(value or "")


def operation_error(key, **values):
    return {"ok": False, "error_key": key, "error_values": values}


def operation_error_text(result):
    key = (result or {}).get("error_key")
    if key:
        return ui_text(key, **((result or {}).get("error_values") or {}))
    return (result or {}).get("error") or ui_text("genericError")

BAMBU_MQTT_PORT = 8883

def resolve_plugin_dir():
    """The plugin's install dir (orca_plugins/<name>), stable across package
    formats. A wheel runs from __whl_extracted__/<pkg>/ INSIDE the install dir
    and that cache is wiped on update. This locates packaged assets and mutable
    state left by releases that predate private plugin storage."""
    here = os.path.dirname(os.path.abspath(__file__)).replace("\\", "/")
    parts = here.split("/")
    if "__whl_extracted__" in parts:
        return "/".join(parts[: parts.index("__whl_extracted__")])
    return here


PLUGIN_DIR = resolve_plugin_dir()
PLUGIN_STORAGE_DIR = PLUGIN_DIR
PRINTERS_FILE = os.path.join(PLUGIN_DIR, "printers.json")
PRINT_HISTORY_FILE = os.path.join(PLUGIN_DIR, "print_history.json")
# Use the adjacent source/package icon when present. Cloud wheel and single-file
# installs fall back to Orca's default icon rather than writing an asset on load.
ICON_PATH = os.path.join(PLUGIN_DIR, "printers.svg")


def ensure_icon():
    return ICON_PATH if os.path.isfile(ICON_PATH) else ""


def fallback_plugin_storage_dir():
    """Stable state root for hosts without ``orca.host.plugin.storage()``."""
    plugins_root = os.path.dirname(os.path.abspath(PLUGIN_DIR))
    if os.path.basename(plugins_root).lower() != "orca_plugins":
        return ""
    data_root = os.path.dirname(plugins_root)
    return os.path.join(data_root, ".print-farm", "orca-plugin")


def configure_plugin_storage():
    """Move mutable state to native storage or a durable old-host fallback."""
    global PLUGIN_STORAGE_DIR, PRINTERS_FILE, PRINT_HISTORY_FILE, OUTBOX_DIR, OUTBOX_INDEX

    fallback_root = fallback_plugin_storage_dir()
    target_root = ""
    plugin_host = getattr(getattr(orca, "host", None), "plugin", None)
    storage = getattr(plugin_host, "storage", None)
    if callable(storage):
        try:
            target_root = os.path.abspath(storage())
        except (OSError, RuntimeError, TypeError, ValueError):
            target_root = ""
    if not target_root:
        target_root = fallback_root
    if not target_root:
        return False
    try:
        os.makedirs(target_root, mode=0o700, exist_ok=True)
        try:
            os.chmod(target_root, 0o700)
        except OSError:
            pass
    except OSError:
        return False

    source_roots = []
    for candidate in (fallback_root, PLUGIN_DIR):
        if candidate and os.path.abspath(candidate) != os.path.abspath(target_root):
            source_roots.append(candidate)
    for name in ("printers.json", "print_history.json", "outbox_index.json"):
        target = os.path.join(target_root, name)
        if os.path.exists(target):
            continue
        for source_root in source_roots:
            source = os.path.join(source_root, name)
            if not os.path.isfile(source):
                continue
            try:
                shutil.copy2(source, target)
            except OSError:
                pass
            break

    target_outbox = os.path.join(target_root, "outbox")
    if not os.path.exists(target_outbox):
        for source_root in source_roots:
            source_outbox = os.path.join(source_root, "outbox")
            if not os.path.isdir(source_outbox):
                continue
            try:
                shutil.copytree(source_outbox, target_outbox)
            except OSError:
                pass
            break

    PLUGIN_STORAGE_DIR = target_root
    PRINTERS_FILE = os.path.join(target_root, "printers.json")
    PRINT_HISTORY_FILE = os.path.join(target_root, "print_history.json")
    OUTBOX_DIR = target_outbox
    OUTBOX_INDEX = os.path.join(target_root, "outbox_index.json")
    return True


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
# Manual printer store (redirected to durable storage during plugin lifecycle)
# --------------------------------------------------------------------------- #
def load_manual_printers():
    if not os.path.isfile(PRINTERS_FILE):
        return []
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


# OrcaSlicer exposes many print_host types. Keep the real type for display, but
# only map transports that this plugin implements end-to-end: status, controls,
# and upload. The remaining types stay in the product target list and UI as
# disabled choices instead of being misrepresented by a reachability-only ping.
_MOONRAKER_COMPATIBLE = {"moonraker", "klipper", "mainsail", "fluidd"}
_OCTOPRINT_COMPATIBLE = {"octoprint"}
_ACTIVE_BACKENDS = {"moonraker", "octoprint", "bambu"}


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
    return "unavailable"


def backend_is_active(host_type):
    return backend_for_type(host_type) in _ACTIVE_BACKENDS


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


def dashboard_printers():
    # Only the printers the user chose to add (manual entries + printers adopted
    # from OrcaSlicer). Discovery never auto-populates the dashboard.
    result = []
    for p in load_manual_printers():
        p["url"] = normalize_url(p.get("url"))
        p.setdefault("source", "manual")
        result.append(p)
    return result


def available_orca_printers(discovered=None):
    # OrcaSlicer network presets (Moonraker/OctoPrint) not yet on the dashboard.
    # Bambu comes via SSDP now — the audit blocks OrcaSlicer.conf.
    manual = load_manual_printers()
    added_urls = {normalize_url(p.get("url")) for p in manual}
    added_serials = {p.get("serial") for p in manual if p.get("serial")}
    out = []
    for p in discovered if discovered is not None else discover_orca_printers():
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
        "elapsed_seconds": print_stats.get("total_duration") or print_stats.get("print_duration"),
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
    progress, elapsed_seconds, filename, filepath = 0, None, "", ""
    try:
        job = _get_json(base + "/api/job", ctx, headers=headers)
        progress = round(job.get("progress", {}).get("completion") or 0)
        elapsed_seconds = job.get("progress", {}).get("printTime")
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
        "elapsed_seconds": elapsed_seconds,
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

    def ntransfercmd(self, cmd, rest=None):
        # Bambu's vsftpd requires the data connection to reuse the control
        # channel's TLS session. FTP_TLS wraps a new session by default.
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            kwargs = {"server_hostname": self.host}
            session = getattr(self.sock, "session", None)
            if session is not None:
                kwargs["session"] = session
            conn = self.context.wrap_socket(conn, **kwargs)
        return conn, size


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
    """Last-resort STOR via system curl, streaming bytes over stdin.

    The native ftplib path now reuses the control TLS session. Curl remains a
    compatibility fallback for older firmware quirks without creating a
    temporary file in OrcaSlicer's data directory.
    """
    curl = shutil.which("curl")
    if not curl:
        return "curl not available"
    try:
        cmd = [curl, "--insecure", "--ftp-pasv", "--silent", "--show-error",
                "--connect-timeout", "15", "--max-time", str(UPLOAD_TIMEOUT),
                "-u", "bblp:" + access_code, "-T", "-",
                "ftps://%s:990/%s" % (host, remote)]
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.run(cmd, input=file_bytes, capture_output=True,
                               timeout=UPLOAD_TIMEOUT + 30, creationflags=flags)
        if proc.returncode == 0:
            return ""
        stderr = (
            proc.stderr.decode("utf-8", "replace")
            if isinstance(proc.stderr, bytes)
            else proc.stderr
        )
        return (stderr or "curl exit %d" % proc.returncode).strip()
    except Exception as exc:
        return str(exc)


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
        return operation_error("errorNoAddress")
    if not code:
        return operation_error("errorNeedsAccessCode")
    try:
        remote_path = bambu_upload(host, code, filename, file_bytes)
    except Exception as exc:
        return operation_error("errorUpload", error=exc)
    if not start:
        return {"ok": True}
    serial = (printer.get("serial") or "").strip()
    if not serial:
        poll_bambu(printer)  # the wildcard subscription learns the serial
        serial = (printer.get("serial") or "").strip()
        if serial:
            persist_discovered_serial(printer)
    if not serial:
        return operation_error("errorUnknownSerial")
    err = bambu_start_print(host, code, serial, remote_path, use_ams)
    if err:
        return operation_error("errorPrintCommand", error=err)
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
            updates[(p.get("id"), normalize_url(p.get("url")))] = name
    if not updates:
        return False
    with _STORE_LOCK:
        manual = load_manual_printers()
        changed = False
        for entry in manual:
            key = (entry.get("id"), normalize_url(entry.get("url")))
            if key in updates and entry.get("name") != updates[key]:
                entry["name"] = updates[key]
                changed = True
        if changed:
            save_manual_printers(manual)
    return True


# --------------------------------------------------------------------------- #
# Outbox: every sliced G-code lands next to its printer on the dashboard.
# A SlicingPipeline capability (psGCodePostProcess) copies the produced file
# into the plugin's outbox and records which machine preset it was sliced for;
# the dashboard shows each file on the matching printer's tile with one-click
# send. The pipeline hook only fires for process presets that select this
# capability through OrcaSlicer's own Slicing pipeline plugin field. A plugin
# cannot safely synthesize that manifest entry: cloud installs are keyed by a
# host-owned UUID, so an all-process trigger must come from the host.
# --------------------------------------------------------------------------- #
OUTBOX_DIR = os.path.join(PLUGIN_DIR, "outbox")
OUTBOX_INDEX = os.path.join(PLUGIN_DIR, "outbox_index.json")
OUTBOX_CAP = "Printers Outbox"
_OUTBOX_LOCK = threading.Lock()


def load_outbox_index():
    if not os.path.isfile(OUTBOX_INDEX):
        return []
    try:
        with open(OUTBOX_INDEX, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def save_outbox_index(index):
    tmp = OUTBOX_INDEX + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(index, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, OUTBOX_INDEX)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass


def parse_gcode_printer(path):
    """Read the machine identity Orca writes into the G-code config block
    (at the file's tail): printer_settings_id and printer_model."""
    sid = model = ""
    try:
        size = os.path.getsize(path)
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            fh.seek(max(0, size - 200000))
            for line in fh:
                s = line.strip()
                if not s.startswith(";"):
                    continue
                if "printer_settings_id =" in s:
                    sid = s.split("=", 1)[1].strip().strip('"')
                elif "printer_model =" in s:
                    model = s.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return sid, model


def outbox_add(gcode_path, output_name="", host=""):
    """Copy a produced G-code into the outbox and index it. Returns the record."""
    original = os.path.basename(output_name or gcode_path) or "print.gcode"
    sid, model = parse_gcode_printer(gcode_path)
    with _OUTBOX_LOCK:
        os.makedirs(OUTBOX_DIR, exist_ok=True)
        stored = "%d__%s" % (int(time.time() * 1000), original)
        target = os.path.join(OUTBOX_DIR, stored)
        shutil.copyfile(gcode_path, target)
        normalized_host = str(host or "").strip()
        record = {"file": stored, "name": original,
                  "printer_settings_id": sid, "printer_model": model,
                  "size": os.path.getsize(target), "ts": int(time.time()),
                  "host": normalized_host,
                  "host_handled": bool(normalized_host and normalized_host.casefold() != "file")}
        index = load_outbox_index()
        index.append(record)
        save_outbox_index(index)
    return record


def outbox_remove(stored_name):
    with _OUTBOX_LOCK:
        index = [r for r in load_outbox_index() if r.get("file") != stored_name]
        save_outbox_index(index)
        try:
            os.remove(os.path.join(OUTBOX_DIR, stored_name))
        except OSError:
            pass


def attach_outbox(printers):
    """Group queued files onto their printers (by the machine preset name the
    file was sliced for). Returns the count that matched no printer."""
    index = load_outbox_index()
    for p in printers:
        p["queued"] = []
    unmatched = 0
    for record in index:
        sid = (record.get("printer_settings_id") or "").strip()
        model = (record.get("printer_model") or "").strip()
        target = None
        for p in printers:
            name = (p.get("name") or "").strip()
            if sid and name and (name == sid or name in sid or sid in name):
                target = p
                break
        if target is None and model:
            for p in printers:
                name = (p.get("name") or "").strip()
                if name and (model in name or name in model):
                    target = p
                    break
        if target is not None:
            target["queued"].append(record)
        else:
            unmatched += 1
    return unmatched


class PrintersOutbox(
    _PluginRuntimeLifecycleMixin,
    orca.slicing.SlicingPipelineCapabilityBase,
):
    def get_name(self):
        return OUTBOX_CAP

    def execute(self, ctx):
        step = getattr(ctx, "step", None)
        step_enum = getattr(orca.slicing, "Step", None)
        post = getattr(step_enum, "psGCodePostProcess", None)
        if post is None:
            post = getattr(orca.slicing, "psGCodePostProcess", None)
        if step is not None and post is not None and step != post:
            return orca.ExecutionResult.skipped(ui_text("outboxWrongStep"))
        path = getattr(ctx, "gcode_path", "") or ""
        if not path or not os.path.exists(path):
            return orca.ExecutionResult.skipped(ui_text("outboxNotReady"))
        try:
            record = outbox_add(path, getattr(ctx, "output_name", "") or "",
                                getattr(ctx, "host", "") or "")
        except Exception as exc:
            return orca.ExecutionResult.failure(orca.PluginResult.RecoverableError,
                                                "%s: %s" % (ui_text("outboxWriteFailed"), exc))
        return orca.ExecutionResult.success(ui_text(
            "outboxQueued",
            name=record["name"],
            target=record.get("printer_settings_id") or ui_text("other"),
        ))


def _poll_backend(kind, printer, base, ctx):
    if kind == "moonraker":
        return poll_moonraker(base, ctx)
    if kind == "octoprint":
        return poll_octoprint(base, ctx, printer.get("apikey", ""))
    return poll_generic(base, ctx)


def poll_printer(printer):
    base = normalize_url(printer.get("url"))
    kind = (printer.get("type") or "moonraker").lower()
    backend = backend_for_type(kind)
    if backend == "unavailable":
        return {
            "online": None,
            "state": "not available yet",
            "supported": False,
            "backend": kind,
            "error": "Printer backend is not implemented yet",
            "nozzle": None,
            "target_nozzle": None,
            "bed": None,
            "target_bed": None,
            "progress": None,
            "filename": "",
            "thumbnail": "",
        }
    ctx = ssl_ctx_for(printer)
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


def status_is_busy(status):
    state = str((status or {}).get("state") or "").lower()
    return "paus" in state or "print" in state or state == "running"


# --------------------------------------------------------------------------- #
# Local observed print history. Providers expose incompatible history APIs, so
# this store records only jobs Print Farm starts or actually sees while polling.
# It never claims to reconstruct work performed while OrcaSlicer was offline.
# --------------------------------------------------------------------------- #
PRINT_HISTORY_MAX = 500
PRINT_HISTORY_START_GRACE_SECONDS = 30
PRINT_HISTORY_MAX_DURATION_SECONDS = 10 * 365 * 24 * 60 * 60
PRINT_HISTORY_PROGRESS_FLUSH_PERCENT = 5
PRINT_HISTORY_DURATION_FLUSH_SECONDS = 5 * 60
_HISTORY_LOCK = threading.Lock()
_HISTORY_ACTIVE_STATES = {"printing", "paused"}
_HISTORY_TERMINAL_STATES = {"completed", "failed", "cancelled", "ended"}


def printer_history_key(printer):
    serial = str((printer or {}).get("serial") or "").strip()
    if serial:
        return "serial:%s" % serial.casefold()
    url = normalize_url((printer or {}).get("url")).casefold()
    if url:
        return "url:%s" % url
    printer_id = (printer or {}).get("id")
    if isinstance(printer_id, int) and not isinstance(printer_id, bool):
        return "printer:%d" % printer_id
    return "printer:unknown"


def _history_state(status):
    raw = str((status or {}).get("state") or "").strip().lower()
    if "cancel" in raw or "cancell" in raw:
        return "cancelled"
    if "fail" in raw or "error" in raw:
        return "failed"
    if raw in {"complete", "completed", "finish", "finished"}:
        return "completed"
    if "paus" in raw:
        return "paused"
    if "print" in raw or raw == "running":
        return "printing"
    if raw in {"idle", "standby", "operational", "ready", "connected"}:
        return "idle"
    return None


def _history_filename(value):
    return os.path.basename(str(value or "").strip().replace("\\", "/"))[:300]


def _history_int(value, default=0, minimum=None, maximum=None):
    if isinstance(value, bool):
        return default
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if minimum is not None:
        result = max(minimum, result)
    if maximum is not None:
        result = min(maximum, result)
    return result


def _normalized_history_record(item):
    record = dict(item)
    record["id"] = str(record.get("id") or "")[:80]
    record["printer_key"] = str(record.get("printer_key") or "")[:500]
    record["printer_name"] = str(record.get("printer_name") or "")[:160]
    record["backend"] = str(record.get("backend") or "")[:40].lower()
    record["filename"] = _history_filename(record.get("filename"))
    state = str(record.get("state") or "").lower()
    record["state"] = (
        state if state in _HISTORY_ACTIVE_STATES | _HISTORY_TERMINAL_STATES else "ended"
    )
    record["source"] = (
        "print_farm" if record.get("source") == "print_farm" else "observed"
    )
    record["started_at"] = _history_int(record.get("started_at"), minimum=0)
    record["updated_at"] = _history_int(
        record.get("updated_at"), record["started_at"], minimum=0
    )
    if record.get("finished_at") is not None:
        record["finished_at"] = _history_int(
            record.get("finished_at"), record["updated_at"], minimum=0
        )
    record["duration_seconds"] = _history_int(
        record.get("duration_seconds"),
        minimum=0,
        maximum=PRINT_HISTORY_MAX_DURATION_SECONDS,
    )
    progress = record.get("progress")
    record["progress"] = (
        None
        if progress is None
        else _history_int(progress, None, minimum=0, maximum=100)
    )
    return record


def load_print_history():
    if not os.path.isfile(PRINT_HISTORY_FILE):
        return []
    try:
        with open(PRINT_HISTORY_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    records = [
        _normalized_history_record(item)
        for item in data
        if isinstance(item, dict) and item.get("id")
    ]
    records.sort(key=lambda item: _history_int(item.get("started_at")), reverse=True)
    return records[:PRINT_HISTORY_MAX]


def save_print_history(records):
    records = [dict(item) for item in records if isinstance(item, dict) and item.get("id")]
    records.sort(key=lambda item: _history_int(item.get("started_at")), reverse=True)
    records = records[:PRINT_HISTORY_MAX]
    tmp = PRINT_HISTORY_FILE + ".tmp"
    try:
        parent = os.path.dirname(PRINT_HISTORY_FILE)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(records, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, PRINT_HISTORY_FILE)
        return True
    except (OSError, TypeError, ValueError):
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


def _history_elapsed(status):
    value = (status or {}).get("elapsed_seconds")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return _history_int(value, minimum=0, maximum=PRINT_HISTORY_MAX_DURATION_SECONDS)


def _new_history_record(printer, filename, now, source, elapsed_seconds=None):
    started_at = now - elapsed_seconds if elapsed_seconds is not None else now
    return {
        "id": str(time.time_ns()),
        "printer_key": printer_history_key(printer),
        "printer_name": str((printer or {}).get("name") or (printer or {}).get("url") or "")[:160],
        "backend": str((printer or {}).get("type") or "")[:40].lower(),
        "filename": _history_filename(filename),
        "state": "printing",
        "source": source,
        "started_at": started_at,
        "updated_at": now,
        "finished_at": None,
        "duration_seconds": elapsed_seconds or 0,
        "progress": None,
    }


def _update_history_record(record, status, state, now):
    changed = False
    filename = _history_filename((status or {}).get("filename"))
    if filename and not record.get("filename"):
        record["filename"] = filename
        changed = True
    progress = (status or {}).get("progress")
    if isinstance(progress, (int, float)) and not isinstance(progress, bool):
        progress = max(0, min(100, int(progress)))
        previous_progress = _history_int(
            record.get("progress"), None, minimum=0, maximum=100
        )
        progress = max(previous_progress or 0, progress)
        if previous_progress != progress:
            record["progress"] = progress
            if (
                previous_progress is None
                or progress == 100
                or progress - previous_progress >= PRINT_HISTORY_PROGRESS_FLUSH_PERCENT
            ):
                changed = True
    elapsed = _history_elapsed(status)
    started_at = _history_int(record.get("started_at"), now, minimum=0)
    observed_duration = max(0, now - started_at)
    duration = min(
        PRINT_HISTORY_MAX_DURATION_SECONDS,
        max(
            _history_int(record.get("duration_seconds"), minimum=0),
            elapsed or 0,
            observed_duration,
        ),
    )
    previous_duration = _history_int(record.get("duration_seconds"), minimum=0)
    if previous_duration != duration:
        record["duration_seconds"] = duration
        if (
            state in _HISTORY_TERMINAL_STATES
            or duration - previous_duration >= PRINT_HISTORY_DURATION_FLUSH_SECONDS
        ):
            changed = True
    if record.get("state") != state:
        record["state"] = state
        changed = True
    finished_at = now if state in _HISTORY_TERMINAL_STATES else None
    if record.get("finished_at") != finished_at:
        record["finished_at"] = finished_at
        changed = True
    if changed:
        record["updated_at"] = now
    return changed


def record_print_started(printer, filename, now=None):
    """Persist a job immediately after a backend accepts a start request."""
    now = int(time.time()) if now is None else int(now)
    key = printer_history_key(printer)
    with _HISTORY_LOCK:
        records = load_print_history()
        for item in records:
            if item.get("printer_key") == key and item.get("state") in _HISTORY_ACTIVE_STATES:
                _update_history_record(item, {"filename": filename}, "ended", now)
        records.insert(0, _new_history_record(printer, filename, now, "print_farm"))
        save_print_history(records)
    return records[0]


def record_print_observation(printer, status, now=None):
    """Reconcile one live status into the bounded local history store."""
    state = _history_state(status)
    if state is None:
        return False
    now = int(time.time()) if now is None else int(now)
    key = printer_history_key(printer)
    filename = _history_filename((status or {}).get("filename"))
    elapsed = _history_elapsed(status)
    with _HISTORY_LOCK:
        records = load_print_history()
        active = next((
            item for item in records
            if item.get("printer_key") == key and item.get("state") in _HISTORY_ACTIVE_STATES
        ), None)
        changed = False
        if state in _HISTORY_ACTIVE_STATES:
            if active is None:
                active = _new_history_record(printer, filename, now, "observed", elapsed)
                records.insert(0, active)
                changed = True
            changed = _update_history_record(active, status, state, now) or changed
        elif active is not None:
            if (
                state == "idle"
                and active.get("source") == "print_farm"
                and now - _history_int(active.get("started_at"), now) < PRINT_HISTORY_START_GRACE_SECONDS
            ):
                return False
            terminal = state
            if state == "idle":
                progress = max(
                    _history_int(active.get("progress"), minimum=0, maximum=100),
                    _history_int((status or {}).get("progress"), minimum=0, maximum=100),
                )
                terminal = "completed" if progress >= 99 else "ended"
            changed = _update_history_record(active, status, terminal, now) or changed
        elif state in {"completed", "failed", "cancelled"} or (
            state == "idle"
            and filename
            and _history_int((status or {}).get("progress"), minimum=0, maximum=100) >= 99
        ):
            terminal = "completed" if state == "idle" else state
            latest = next((
                item for item in records
                if item.get("printer_key") == key
                and item.get("filename") == filename
                and item.get("state") in _HISTORY_TERMINAL_STATES
            ), None)
            if latest is None:
                latest = _new_history_record(printer, filename, now, "observed", elapsed)
                records.insert(0, latest)
                changed = True
            changed = _update_history_record(latest, status, terminal, now) or changed
        if changed:
            save_print_history(records)
        return changed


def print_history_for_printer(printer):
    key = printer_history_key(printer)
    return [item for item in load_print_history() if item.get("printer_key") == key]


def _post_json(url, ctx, headers=None, payload=None, timeout=HTTP_TIMEOUT):
    body = json.dumps(payload if payload is not None else {}).encode("utf-8")
    hdrs = {"Content-Type": "application/json"}
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=body, headers=hdrs, method="POST")
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        return resp.getcode()


# Bambu names the cancel command "stop"; the two HTTP backends call it cancel.
_BAMBU_CONTROL = {"pause": "pause", "resume": "resume", "cancel": "stop"}
# Moonraker holds the HTTP response until the PAUSE/CANCEL macro finishes, and
# Klipper macros (Happy Hare parks and may home first) easily outlive a normal
# request timeout — don't report a command failed while it is still running.
CONTROL_TIMEOUT = 60


def printer_command(printer, action):
    """Pause/resume/cancel the current print on whichever backend the printer
    speaks. Returns {ok} or {ok: False, error}."""
    if action not in _BAMBU_CONTROL:
        return operation_error("errorUnknownAction")
    base = normalize_url(printer.get("url"))
    ctx = ssl_ctx_for(printer)
    kind = backend_for_type(printer.get("type") or "moonraker")
    try:
        if kind == "moonraker":
            _post_json(base + "/printer/print/" + ("cancel" if action == "cancel" else action), ctx,
                       timeout=CONTROL_TIMEOUT)
        elif kind == "octoprint":
            headers = {}
            if printer.get("apikey"):
                headers["X-Api-Key"] = printer["apikey"]
            payload = {"command": "cancel"} if action == "cancel" else {"command": "pause", "action": action}
            _post_json(base + "/api/job", ctx, headers, payload, timeout=CONTROL_TIMEOUT)
        elif kind == "bambu":
            host = urllib.parse.urlparse(base).hostname or ""
            code = (printer.get("access_code") or printer.get("apikey") or "").strip()
            serial = (printer.get("serial") or "").strip()
            if not host or not code or not serial:
                return operation_error("errorNeedsBambuCredentials")
            err = _bambu_publish(host, code, "device/%s/request" % serial,
                                 {"print": {"sequence_id": "0", "command": _BAMBU_CONTROL[action]}})
            if err:
                return {"ok": False, "error": err}
        else:
            return operation_error("errorUnsupportedBackend")
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
            return operation_error("errorUnsupportedBackend")
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
  /* Keep print controls and ordinary tile actions on separate fixed rows. A
     printing tile can expose two extra controls without overflowing into the
     next grid column or moving the ordinary actions. */
  .tile-actions { display:flex; flex-direction:column; gap:6px; margin-top:auto; min-width:0; }
  .ctl-row { display:flex; justify-content:space-between; align-items:center; gap:6px; min-height:28px; }
  .row { display:flex; gap:6px; align-items:center; min-width:0; }
  .row label { display:flex; align-items:center; gap:4px; font-size:11px; margin-right:auto; }
  .row button { white-space:nowrap; padding-left:4px; padding-right:4px; }
  .row .print, .row .open { width:60px; }
  .row .remove { width:68px; }
  .ctl-slot { display:flex; gap:6px; width:126px; justify-content:flex-end; }
  .ctl-slot button { width:60px; padding-left:4px; padding-right:4px; }
  .spacer { visibility:hidden; pointer-events:none; }
  .empty { color:var(--orca-muted,#a0a0a0); padding:24px; text-align:center; }
  #status { font-size:11px; color:var(--orca-muted,#a0a0a0); }
  .queue { display:flex; flex-direction:column; gap:4px; }
  .qrow { display:flex; align-items:center; gap:6px; font-size:11px;
          border:1px dashed var(--orca-border,#3c3c4c); border-radius:5px; padding:3px 6px; }
  .qrow .qname { margin-right:auto; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .qrow .qstatus { color:var(--orca-muted,#a0a0a0); white-space:nowrap; }
  .qrow button { padding:1px 8px; font-size:11px; }
  .history-modal { display:none; position:fixed; inset:0; background:rgba(0,0,0,.55);
                   align-items:center; justify-content:center; z-index:25; padding:18px; }
  .history-card { display:flex; flex-direction:column; width:min(680px,100%); max-height:85%;
                  background:var(--orca-bg,#1e1e2e); border:1px solid var(--orca-border,#3c3c4c);
                  border-radius:10px; box-shadow:0 12px 38px rgba(0,0,0,.35); }
  .history-head { display:flex; align-items:center; gap:10px; padding:12px 14px;
                  border-bottom:1px solid var(--orca-border,#3c3c4c); }
  .history-head strong { margin-right:auto; min-width:0; overflow:hidden; text-overflow:ellipsis;
                         white-space:nowrap; }
  .history-note { padding:10px 14px; font-size:11px; line-height:1.45;
                  color:var(--orca-muted,#a0a0a0); }
  .history-list { overflow:auto; padding:0 14px 14px; display:flex; flex-direction:column; gap:7px; }
  .history-item { border:1px solid var(--orca-border,#3c3c4c); border-radius:7px;
                  padding:8px 10px; display:flex; flex-direction:column; gap:5px; }
  .history-item-top { display:flex; align-items:center; gap:8px; }
  .history-file { margin-right:auto; min-width:0; overflow:hidden; text-overflow:ellipsis;
                  white-space:nowrap; font-size:12px; font-weight:600; }
  .history-state { font-size:10px; padding:1px 6px; border-radius:999px;
                   border:1px solid var(--orca-border,#3c3c4c); white-space:nowrap; }
  .history-meta { color:var(--orca-muted,#a0a0a0); font-size:10px; display:flex;
                  flex-wrap:wrap; gap:5px 12px; }
</style></head>
<body>
  <div id="bar">
    <label id="sel-all-l" title="Select or deselect every printer"><input id="sel-all" type="checkbox"> <span id="sel-all-text">all</span></label>
    <span id="status"></span>
    <button id="send" class="ghost" disabled>Send G-code to selected…</button>
    <select id="f-adopt" title="Add a printer configured in OrcaSlicer" style="display:none"></select>
    <input id="f-name" placeholder="Name" size="8">
    <input id="f-url" placeholder="192.168.0.42" size="14">
    <select id="f-type" title="Connection type. Active integrations provide live status, controls and upload. Disabled entries remain planned targets.">
      <option value="moonraker">Moonraker / Klipper</option>
      <option value="octoprint">OctoPrint</option>
      <option value="bambu">Bambu (LAN)</option>
      <option value="prusalink" disabled>PrusaLink — coming later</option>
      <option value="repetier" disabled>Repetier — coming later</option>
      <option value="duet" disabled>Duet — coming later</option>
      <option value="mks" disabled>MKS — coming later</option>
      <option value="flashair" disabled>FlashAir — coming later</option>
      <option value="astrobox" disabled>AstroBox — coming later</option>
      <option value="esp3d" disabled>ESP3D — coming later</option>
      <option value="crealityprint" disabled>Creality — coming later</option>
      <option value="flashforge" disabled>FlashForge — coming later</option>
      <option value="elegoolink" disabled>Elegoo — coming later</option>
      <option value="generic" disabled>Other — coming later</option>
    </select>
    <input id="f-serial" placeholder="Serial (auto)" size="10" style="display:none" title="Leave empty — the serial is discovered from the printer automatically. Fill only to pin a specific printer. MQTT port 8883 is used.">
    <input id="f-access" placeholder="Access code" size="10" style="display:none">
    <label id="f-insecure-l" title="Check only if the printer is served over HTTPS with a self-signed certificate — this skips TLS verification for it. Plain http:// LAN printers don't need this." style="font-size:11px;display:flex;align-items:center;gap:4px">
      <input id="f-insecure" type="checkbox"> <span id="f-insecure-text">self-signed</span></label>
    <button id="add">Add</button>
    <button id="refresh" class="ghost">Refresh</button>
    <input id="file" type="file" accept=".gcode,.gco,.g,.gz,.3mf" style="display:none">
  </div>
  <div id="confirm" style="display:none;position:absolute;inset:0;background:rgba(0,0,0,.55);
       align-items:center;justify-content:center;z-index:20">
    <div style="background:var(--orca-bg,#1e1e2e);border:1px solid var(--orca-border,#3c3c4c);
         border-radius:10px;padding:18px;max-width:420px;width:90%">
      <div id="confirm-title" style="font-weight:600;margin-bottom:8px">Start print on multiple printers?</div>
      <div id="confirm-body" style="font-size:12px;color:var(--orca-muted,#a0a0a0);margin-bottom:12px"></div>
      <label style="display:flex;align-items:center;gap:6px;font-size:12px;margin-bottom:12px">
        <input id="confirm-start" type="checkbox" checked> <span id="confirm-start-text">Start printing immediately after upload</span></label>
      <label id="confirm-ams-l" style="display:none;align-items:center;gap:6px;font-size:12px;margin-bottom:12px">
        <input id="confirm-ams" type="checkbox" checked> <span id="confirm-ams-text">Use AMS (Bambu)</span></label>
      <div style="display:flex;gap:8px;justify-content:flex-end">
        <button id="confirm-cancel" class="ghost">Cancel</button>
        <button id="confirm-ok">Send</button>
      </div>
    </div>
  </div>
  <div id="history-modal" class="history-modal">
    <div class="history-card">
      <div class="history-head">
        <strong id="history-title">Print history</strong>
        <button id="history-close" class="ghost">Close</button>
      </div>
      <div id="history-note" class="history-note"></div>
      <div id="history-list" class="history-list"><div class="empty">Loading…</div></div>
    </div>
  </div>
  <div id="grid"><div class="empty">Loading…</div></div>
<script>
'use strict';
var UI_COPY = __UI_COPY__;
var hostLanguage = '__HOST_UI_LANGUAGE__';
function normalizeUiLocale(value) {
  var token = String(value || '').replace(/-/g, '_');
  var lowered = token.toLowerCase();
  var aliases = { zh: 'zh_CN', zh_hans: 'zh_CN', zh_hans_cn: 'zh_CN',
                  zh_hant: 'zh_TW', zh_hant_tw: 'zh_TW' };
  if (aliases[lowered]) return aliases[lowered];
  var exact = Object.keys(UI_COPY).find(function(key) {
    return key.toLowerCase() === lowered;
  });
  if (exact) return exact;
  var base = lowered.split('_', 1)[0];
  return Object.prototype.hasOwnProperty.call(UI_COPY, base) ? base : 'en';
}
function resolveUiCopy(value) {
  var locale = normalizeUiLocale(value);
  var base = locale.split('_', 1)[0];
  return Object.assign({}, UI_COPY.en || {}, UI_COPY[base] || {}, UI_COPY[locale] || {});
}
var uiLocale = normalizeUiLocale(hostLanguage || navigator.language || 'en');
var uiCopy = resolveUiCopy(uiLocale);
document.documentElement.lang = uiLocale;
function t(key, values) {
  var template = uiCopy[key] || (UI_COPY.en || {})[key] || key;
  return template.replace(/\{([A-Za-z0-9_]+)\}/g, function(match, name) {
    return values && Object.prototype.hasOwnProperty.call(values, name)
      ? String(values[name]) : match;
  });
}
function applyStaticCopy() {
  function text(id, key) {
    var node = document.getElementById(id); if (node) node.textContent = t(key);
  }
  function title(id, key) {
    var node = document.getElementById(id); if (node) node.title = t(key);
  }
  text('sel-all-text', 'all'); title('sel-all-l', 'selectAllTitle');
  text('send', 'sendSelected'); title('f-adopt', 'addConfiguredTitle');
  document.getElementById('f-name').placeholder = t('namePlaceholder');
  title('f-type', 'connectionTypeTitle');
  document.getElementById('f-serial').placeholder = t('serialAuto');
  title('f-serial', 'serialTitle');
  document.getElementById('f-access').placeholder = t('accessCode');
  text('f-insecure-text', 'selfSigned');
  title('f-insecure-l', 'selfSignedTitle');
  text('add', 'add'); text('refresh', 'refresh');
  text('confirm-title', 'confirmTitle'); text('confirm-start-text', 'startImmediately');
  text('confirm-ams-text', 'useAms'); text('confirm-cancel', 'cancel');
  text('confirm-ok', 'send');
  text('history-close', 'close'); text('history-note', 'historyCoverage');
  var empty = document.querySelector('#grid .empty'); if (empty) empty.textContent = t('loading');
  Array.prototype.forEach.call(document.getElementById('f-type').options, function(option) {
    if (!option.disabled) return;
    option.textContent = option.textContent.split(' — ')[0] + ' — ' + t('comingLater');
  });
}
applyStaticCopy();
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
    bambu:'Bambu Lab', generic:t('other') }[(t || '').toLowerCase()] || (t || t('other'));
}

// Normalize backend state strings ("printing", "RUNNING", "Printing from SD"…)
// into the three kinds the control buttons care about.
function stateKind(s){
  var t = String((s && s.state) || '').toLowerCase();
  if (t.indexOf('paus') >= 0) return 'paused';
  if (t.indexOf('print') >= 0 || t === 'running') return 'printing';
  return 'idle';
}

function backendReady(t){
  return ['moonraker','klipper','mainsail','fluidd','octoprint','bambu']
    .indexOf(String(t || '').toLowerCase()) >= 0;
}

function printerSelectable(p){
  var s = p.status || {};
  var kind = stateKind(s);
  return backendReady(p.type) && s.supported !== false && s.online === true &&
    kind !== 'printing' && kind !== 'paused';
}

function updateSendBtn(){
  var eligible = current.filter(printerSelectable);
  var n = eligible.filter(function(p){ return selected[p.url]; }).length;
  sendBtn.disabled = n === 0;
  sendBtn.textContent = n ? t('sendSelectedCount', {count:n}) : t('sendSelected');
  var all = document.getElementById('sel-all');
  all.disabled = eligible.length === 0;
  all.checked = eligible.length > 0 && eligible.every(function(p){ return selected[p.url]; });
}

document.getElementById('sel-all').addEventListener('change', function(e){
  current.forEach(function(p){ selected[p.url] = printerSelectable(p) && e.target.checked; });
  render(current);
});

function render(printers){
  current = printers;
  grid.innerHTML = '';
  if (!printers.length){ grid.innerHTML = '<div class="empty">' + esc(t('noPrinters')) + '</div>'; return; }
  printers.forEach(function(p){
    var s = p.status || {};
    var supported = backendReady(p.type) && s.supported !== false;
    var dotClass = supported ? (s.online === true ? 'on' : (s.online === false ? 'off' : '')) : '';
    var rawState = supported
      ? (s.state || (s.online === true ? 'online' : (s.online === false ? 'offline' : 'checking')))
      : 'not available yet';
    var stateLabels = {
      online:'stateOnline', offline:'stateOffline', checking:'stateChecking',
      'not available yet':'stateUnavailable', idle:'stateIdle', standby:'stateStandby',
      printing:'statePrinting', running:'statePrinting', paused:'statePaused',
      complete:'stateComplete', completed:'stateComplete', error:'stateError'
    };
    var stateKey = stateLabels[String(rawState).toLowerCase()];
    var stateText = stateKey ? t(stateKey) : rawState;
    var tile = document.createElement('div'); tile.className = 'tile';
    var prog = (s.progress == null) ? '' : '<div class="bar"><i style="width:' + s.progress + '%"></i></div>';
    var thumb = s.thumbnail ? '<img class="thumb show" src="' + s.thumbnail + '">' : '<div class="thumb"></div>';
    var fname = s.filename ? '<div class="fname" title="' + esc(s.filename) + '">' + esc(s.filename) + '</div>' : '';
    tile.innerHTML =
      '<div class="top"><span class="dot ' + dotClass + '"></span>' +
      '<span class="name">' + esc(p.name || p.url) + '</span>' +
      '<span class="src">' + esc(typeLabel(p.type)) + '</span>' +
      '<span class="state">' + esc(stateText) + '</span></div>' +
      '<div class="body">' + thumb + '<div class="meta">' +
      '<div class="temps"><span>' + esc(t('nozzle')) + ' <b>' + temp(s.nozzle) + '</b>' + (s.target_nozzle ? '/' + temp(s.target_nozzle) : '') + '</span>' +
      '<span>' + esc(t('bed')) + ' <b>' + temp(s.bed) + '</b>' + (s.target_bed ? '/' + temp(s.target_bed) : '') + '</span></div>' +
      fname + prog + '</div></div>';
    var kind = stateKind(s);
    var busy = supported && s.online === true && (kind === 'printing' || kind === 'paused');
    var selectable = printerSelectable(p);
    if (!selectable) selected[p.url] = false;
    var ctl = '<span class="ctl-slot">';
    if (s.online && kind === 'printing')
      ctl += '<button class="ctl ghost" data-a="pause">' + esc(t('pause')) + '</button><button class="ctl danger" data-a="cancel">' + esc(t('stop')) + '</button>';
    else if (s.online && kind === 'paused')
      ctl += '<button class="ctl ghost" data-a="resume">' + esc(t('resume')) + '</button><button class="ctl danger" data-a="cancel">' + esc(t('stop')) + '</button>';
    else
      ctl += '<button class="ctl spacer" disabled></button><button class="ctl spacer" disabled></button>';
    ctl += '</span>';
    var printDisabledReason = !supported
      ? t('integrationUnavailable')
      : (busy ? t('printerBusy') : t('printerNotOnline'));
    var printAttrs = selectable ? '' : ' disabled title="' + esc(printDisabledReason) + '"';
    tile.innerHTML +=
      '<div class="tile-actions"><div class="ctl-row"><button class="history ghost">' + esc(t('history')) + '</button>' + ctl + '</div>' +
      '<div class="row"><label><input type="checkbox" class="sel"' +
      (selected[p.url] ? ' checked' : '') + (selectable ? '' : ' disabled') + '> ' + esc(t('select')) + '</label>' +
      '<button class="print ghost"' + printAttrs + '>' + esc(t('print')) + '</button>' +
      // Bambu has no LAN web page to open; its full UI lives in OrcaSlicer's Device tab.
      (p.type === 'bambu'
        ? '<button class="open ghost spacer" disabled></button>'
        : '<button class="open ghost">' + esc(t('open')) + '</button>') +
      '<button class="remove ghost">' + esc(t('remove')) + '</button></div></div>';
    tile.querySelector('.sel').addEventListener('change', function(e){
      selected[p.url] = e.target.checked; updateSendBtn(); });
    // Stopping a print is destructive — the button arms on the first click and
    // fires only when confirmed within 3 seconds.
    Array.prototype.forEach.call(tile.querySelectorAll('.ctl'), function(btn){
      btn.addEventListener('click', function(){
        var a = btn.getAttribute('data-a');
        if (a === 'cancel' && !btn._armed) {
          btn._armed = true; btn.textContent = t('sure');
          setTimeout(function(){ btn._armed = false; btn.textContent = t('stop'); }, 3000);
          return;
        }
        btn.disabled = true;
        orca.postMessage({ type:'printer-cmd', url:p.url, action:a });
      });
    });
    tile.querySelector('.print').addEventListener('click', function(){ pickAndSend([p.url]); });
    tile.querySelector('.history').addEventListener('click', function(){ openPrintHistory(p); });
    // Outbox records may be local exports waiting for an action, or copies of a
    // file Orca has already handed to a print host. Never offer a duplicate send
    // for the latter. Print arms on first click; × drops the retained copy.
    if (p.queued && p.queued.length) {
      var q = document.createElement('div');
      q.className = 'queue';
      // The plugin can't know whether Orca (or anything else) already sent a
      // file to a printer — the pipeline "host" signal is inconsistent across
      // vendors — so we don't pretend to. The queue is just sliced files you
      // can push to a dashboard printer: one Print action, gated on the
      // printer's current state. No "Send"/"sent by Orca" state claims.
      p.queued.forEach(function(r){
        var row = document.createElement('div');
        row.className = 'qrow';
        var printBtn = !selectable
          ? '<button class="qprint" disabled title="' + esc(printDisabledReason) + '">' + esc(t('printShort')) + '</button>'
          : '<button class="qprint" title="' + esc(t('sendToPrinterTitle')) + '">' + esc(t('printShort')) + '</button>';
        row.innerHTML = '<span class="qname" title="' + esc(r.name) + '">' + esc(r.name) + '</span>' +
          printBtn + '<button class="qdrop ghost" title="' + esc(t('removeRetainedTitle')) + '">&times;</button>';
        var pb = row.querySelector('.qprint');
        if (selectable) {
          pb.addEventListener('click', function(){
            if (!pb._armed) {
              pb._armed = true; pb.textContent = t('sure');
              setTimeout(function(){ pb._armed = false; pb.textContent = t('printShort'); }, 3000);
              return;
            }
            pb.disabled = true;
            orca.postMessage({ type:'outbox-send', file:r.file, url:p.url, start:true });
          });
        }
        row.querySelector('.qdrop').addEventListener('click', function(){
          orca.postMessage({ type:'outbox-drop', file:r.file });
        });
        q.appendChild(row);
      });
      tile.appendChild(q);
    }
    var openBtn = tile.querySelector('.open');
    if (openBtn && !openBtn.disabled) openBtn.addEventListener('click', function(){
      orca.postMessage({ type:'open', url:p.url, name:p.name || p.url }); });
    tile.querySelector('.remove').addEventListener('click', function(){ orca.postMessage({ type:'remove', id:p.id }); });
    grid.appendChild(tile);
  });
  updateSendBtn();
}


var historyExpected = null;
function historyStateLabel(state){
  return t({ printing:'historyStatePrinting', paused:'historyStatePaused',
    completed:'historyStateCompleted', failed:'historyStateFailed',
    cancelled:'historyStateCancelled', ended:'historyStateEnded' }[state] || 'historyStateEnded');
}
function historyDuration(seconds){
  var value = Math.max(0, Number(seconds) || 0);
  var hours = Math.floor(value / 3600);
  var minutes = Math.floor((value % 3600) / 60);
  return hours
    ? t('historyDurationHoursMinutes', {hours:hours, minutes:minutes})
    : t('historyDurationMinutes', {minutes:minutes});
}
function openPrintHistory(printer){
  historyExpected = { id:printer.id, url:printer.url };
  document.getElementById('history-title').textContent = t('historyTitle', {
    printer:printer.name || printer.url
  });
  document.getElementById('history-list').innerHTML = '<div class="empty">' + esc(t('historyLoading')) + '</div>';
  document.getElementById('history-modal').style.display = 'flex';
  orca.postMessage({ type:'print-history', id:printer.id, url:printer.url });
}
function closePrintHistory(){
  historyExpected = null;
  document.getElementById('history-modal').style.display = 'none';
}
function renderPrintHistory(payload){
  if (!historyExpected || !payload) return;
  if (historyExpected.id != null && payload.printerId != null && historyExpected.id !== payload.printerId) return;
  if (historyExpected.id == null && historyExpected.url !== payload.printerUrl) return;
  document.getElementById('history-title').textContent = t('historyTitle', {
    printer:payload.printerName || historyExpected.url
  });
  var list = document.getElementById('history-list');
  var items = payload.items || [];
  if (!items.length) {
    list.innerHTML = '<div class="empty">' + esc(t('historyEmpty')) + '</div>';
    return;
  }
  list.innerHTML = '';
  items.forEach(function(item){
    var row = document.createElement('div'); row.className = 'history-item';
    var source = item.source === 'print_farm' ? t('historyStartedHere') : t('historyObserved');
    var started = item.started_at ? new Date(item.started_at * 1000).toLocaleString() : '—';
    var progress = item.progress == null ? '' : t('historyProgress', {progress:item.progress});
    row.innerHTML = '<div class="history-item-top"><span class="history-file" title="' +
      esc(item.filename || t('historyUnknownFile')) + '">' + esc(item.filename || t('historyUnknownFile')) +
      '</span><span class="history-state">' + esc(historyStateLabel(item.state)) + '</span></div>' +
      '<div class="history-meta"><span>' + esc(started) + '</span><span>' +
      esc(historyDuration(item.duration_seconds)) + '</span><span>' + esc(source) + '</span>' +
      (progress ? '<span>' + esc(progress) + '</span>' : '') + '</div>';
    list.appendChild(row);
  });
}
document.getElementById('history-close').addEventListener('click', closePrintHistory);
document.getElementById('history-modal').addEventListener('click', function(event){
  if (event.target === this) closePrintHistory();
});


// Bambu (LAN) needs a serial + access code instead of the self-signed toggle;
// swap the relevant fields when the backend type changes.
var typeSel = document.getElementById('f-type');
function syncTypeFields(){
  var bambu = typeSel.value === 'bambu';
  // The serial is never typed: the MQTT wildcard learns it on first poll and
  // SSDP discovery prefills it. The hidden input only carries those values.
  document.getElementById('f-access').style.display = bambu ? '' : 'none';
  document.getElementById('f-insecure-l').style.display = bambu ? 'none' : '';
  document.getElementById('f-url').placeholder = bambu ? t('printerIpPlaceholder') : '192.168.0.42';
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
    ? '<br><span style="color:#c0504d">' + esc(t('file3mfWarning')) + '</span>' : '';
  document.getElementById('confirm-ams-l').style.display = hasBambu ? 'flex' : 'none';
  document.getElementById('confirm-body').innerHTML =
    t('uploadToPrinters', {name:'<b>' + esc(file.name) + '</b>', count:urls.length}) + '<br>' + names.join(', ') + warn;
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
    document.getElementById('status').textContent = t('sendingFile', {name:file.name, count:urls.length});
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
  var opts = '<option value="">' + esc(t('addPrinter')) + '</option>';
  items.forEach(function(p, i){
    var label = (p.source === 'network') ? (p.name + ' — ' + t('foundOnNetwork')) : (p.name || p.url);
    var ready = backendReady(p.type);
    opts += '<option value="' + i + '"' + (ready ? '' : ' disabled') + '>' +
      esc(label + (ready ? '' : ' — ' + t('unavailableSuffix'))) + '</option>';
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
  if (data.printerStatus) {
    current.forEach(function(p){
      if (p.url === data.printerStatus.url) p.status = data.printerStatus.status;
    });
    render(current);
  }
  if (data.available) { lastAvailable = data.available; renderAvailable(); }
  if (data.discovered) { lastDiscovered = data.discovered; renderAvailable(); }
  if (data.printHistory) renderPrintHistory(data.printHistory);
  if (data.unmatched !== undefined)
    document.getElementById('status').textContent = data.unmatched
      ? t('unmatchedQueued', {count:data.unmatched})
      : '';
  if (data.status !== undefined) document.getElementById('status').textContent = data.status;
});
orca.postMessage({ type:'ready' });
// Live dashboard: refresh statuses while the tab is open. Status-only tick —
// the store and the picker are rebuilt only on explicit Refresh/actions.
setInterval(function(){
  if (document.hidden) return;
  try { orca.postMessage({ type:'poll' }); } catch (e) { /* bridge not ready */ }
}, 15000);
</script>
</body>
</html>
"""


def render_page():
    language = refresh_ui_language()
    return PAGE.replace(
        "__UI_COPY__",
        json.dumps(UI_COPY, ensure_ascii=False).replace("</", "<\\/"),
    ).replace("__HOST_UI_LANGUAGE__", language)


# A printer UI stays inside a separate host window. The parent document permits
# only the selected printer origin, and the iframe cannot open popups or replace
# Orca's top-level page.
VIEWER_PAGE = """<!DOCTYPE html><html><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; frame-src __ORIGIN__">
<style>html,body{margin:0;height:100%}iframe{border:0;width:100%;height:100%;display:block}</style>
</head><body><iframe src="__URL__" sandbox="allow-same-origin allow-scripts allow-forms allow-downloads allow-modals allow-pointer-lock" allow="fullscreen" referrerpolicy="no-referrer"></iframe></body></html>"""


def validated_printer_web_url(value):
    candidate = str(value or "").strip().rstrip("/")
    initial = urllib.parse.urlsplit(candidate)
    if initial.scheme and initial.scheme.lower() not in {"http", "https"}:
        port_token = candidate.split(":", 1)[1].split("/", 1)[0]
        if not port_token.isdigit():
            return ""
    raw = normalize_url(candidate)
    if not raw or any(ord(char) < 32 or ord(char) == 127 for char in raw):
        return ""
    if any(char in raw for char in "\"'<>\\"):
        return ""
    try:
        parsed = urllib.parse.urlsplit(raw)
        port = parsed.port
    except ValueError:
        return ""
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return ""
    host = parsed.hostname
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        return ""
    if not all(char.isalnum() or char in ".-_:" for char in host):
        return ""
    if ":" in host:
        host = "[" + host + "]"
    netloc = host + ((":" + str(port)) if port is not None else "")
    return urllib.parse.urlunsplit((
        parsed.scheme.lower(),
        netloc,
        parsed.path,
        parsed.query,
        parsed.fragment,
    ))


def render_viewer_page(url):
    safe_url = validated_printer_web_url(url)
    if not safe_url:
        return ""
    parsed = urllib.parse.urlsplit(safe_url)
    host = parsed.hostname
    if ":" in host:
        host = "[" + host + "]"
    origin = "%s://%s%s" % (
        parsed.scheme,
        host,
        (":" + str(parsed.port)) if parsed.port is not None else "",
    )
    return VIEWER_PAGE.replace(
        "__ORIGIN__",
        html.escape(origin, quote=True),
    ).replace(
        "__URL__",
        html.escape(safe_url, quote=True),
    )


_PAGES = getattr(orca, "pages", None)
_PAGE_CAPABILITY_BASE = getattr(_PAGES, "PagesPluginCapabilityBase", None)
_SCRIPT_CAPABILITY_BASE = orca.script.ScriptPluginCapabilityBase
_DASHBOARD_CAPABILITY_BASE = _PAGE_CAPABILITY_BASE or _SCRIPT_CAPABILITY_BASE


class _PageWindowProxy:
    """Adapt Pages.post_message() to the existing window push contract."""

    def __init__(self, page):
        self._page = page

    def is_open(self):
        return self._page is not None

    def post(self, payload):
        self._page.post_message(payload)


class PrintersDashboard(_PluginRuntimeLifecycleMixin, _DASHBOARD_CAPABILITY_BASE):

    def __init__(self):
        super().__init__()
        self._status_cache = {}
        self._status_lock = threading.Lock()
        self._list_generation = 0
        self._viewers = []
        self.win = _PageWindowProxy(self) if _PAGE_CAPABILITY_BASE is not None else None

    def get_name(self):
        return "Print Farm"

    def get_icon(self):
        return ensure_icon()

    def get_ui(self):
        return render_page()

    def _open(self):
        if _PAGE_CAPABILITY_BASE is not None:
            return False
        if self.win is not None and self.win.is_open():
            return False
        self.win = orca.host.ui.create_window(
            title="Print Farm", html=render_page(), width=1000, height=700,
            on_message=self.on_message, on_close=self.on_close,
        )
        return True

    def execute(self):
        self._open()
        return orca.ExecutionResult.success(ui_text("dashboardOpened"))

    def on_close(self):
        self.win = None

    def _close_owned_windows(self):
        if _PAGE_CAPABILITY_BASE is None:
            window = self.win
            self.win = None
            try:
                if window is not None and window.is_open():
                    window.close()
            except Exception:
                pass
        for viewer in self._viewers:
            try:
                if viewer.is_open():
                    viewer.close()
            except Exception:
                pass
        self._viewers = []

    def on_cancelled(self):
        self._close_owned_windows()
        stop_plugin_runtime()

    def on_unload(self):
        self._close_owned_windows()
        stop_plugin_runtime()

    def _add_manual(self, entry):
        with _STORE_LOCK:
            manual = load_manual_printers()
            new_id = max(
                [p.get("id", 0) for p in manual if isinstance(p.get("id"), int)],
                default=0,
            ) + 1
            entry["id"] = new_id
            manual.append(entry)
            save_manual_printers(manual)

    def _remove_manual(self, printer_id):
        with _STORE_LOCK:
            manual = [p for p in load_manual_printers() if p.get("id") != printer_id]
            save_manual_printers(manual)

    def _post(self, payload):
        return post_window(self.win, payload)

    # on_message runs on the UI thread — host reads happen here, network/disk on workers.
    def on_message(self, msg):
        msg = msg or {}
        kind = msg.get("type")
        if kind == "ready":
            self._refresh_async(discover_network=False)
        elif kind == "refresh":
            self._refresh_async(discover_network=True)
        elif kind == "poll":
            # Periodic status tick from the page: statuses only, no store/picker
            # rebuild, and never more than one poll in flight.
            if not getattr(self, "_polling", False):
                self._polling = True
                ACTION_WORKER.submit(self._poll_dashboard)
        elif kind == "printer-cmd":
            ACTION_WORKER.submit(self._do_printer_cmd, msg)
        elif kind == "print-history":
            ACTION_WORKER.submit(self._post_print_history, msg)
        elif kind == "outbox-send":
            ACTION_WORKER.submit(self._do_outbox_send, msg)
        elif kind == "outbox-drop":
            stored = msg.get("file") or ""
            if stored:
                ACTION_WORKER.submit(
                    self._drop_and_refresh,
                    stored,
                    discover_orca_printers(),
                )
        elif kind == "add":
            entry = {
                "name": msg.get("name") or msg.get("url"),
                "url": normalize_url(msg.get("url")),
                "type": msg.get("kind") or "moonraker",
                "serial": msg.get("serial") or "",
                "access_code": msg.get("access_code") or "",
                "insecure": bool(msg.get("insecure")),
                "source": "manual",
            }
            if not backend_is_active(entry["type"]):
                self._post_status(
                    ui_text("supportPlanned", type=entry["type"] or ui_text("other"))
                )
                return
            ACTION_WORKER.submit(
                self._add_and_refresh,
                entry,
                discover_orca_printers(),
            )
        elif kind == "adopt":
            # User chose an OrcaSlicer-configured printer to put on the dashboard.
            p = msg.get("printer") or {}
            if p.get("url"):
                entry = {
                    "name": p.get("name") or p.get("url"),
                    "url": normalize_url(p.get("url")),
                    "type": p.get("type") or "moonraker",
                    "apikey": p.get("apikey", ""),
                    "serial": p.get("serial") or "",
                    "access_code": p.get("access_code") or "",
                    "insecure": bool(p.get("insecure")),
                    "source": "orca",
                }
                if not backend_is_active(entry["type"]):
                    self._post_status(
                        ui_text("supportPlanned", type=entry["type"] or ui_text("other"))
                    )
                    return
                ACTION_WORKER.submit(
                    self._add_and_refresh,
                    entry,
                    discover_orca_printers(),
                )
        elif kind == "remove":
            ACTION_WORKER.submit(
                self._remove_and_refresh,
                msg.get("id"),
                discover_orca_printers(),
            )
        elif kind == "open":
            url = validated_printer_web_url(msg.get("url"))
            if url:
                self._open_viewer(url, msg.get("name") or url)
        elif kind == "mass-print":
            ACTION_WORKER.submit(self._do_mass_print, msg)

    def _open_viewer(self, url, name):
        # Printer web UI in its own host window — inside Orca, no external browser
        # and without hijacking the dashboard tab. Keep a reference so it lives on.
        page = render_viewer_page(url)
        if not page:
            return False
        try:
            win = orca.host.ui.create_window(
                title=(name or url)[:80], html=page,
                width=1100, height=760, on_message=lambda m: None, on_close=lambda: None)
            self._viewers = [
                viewer for viewer in self._viewers
                if viewer is not None and viewer.is_open()
            ]
            self._viewers.append(win)
            return True
        except Exception:
            return False

    def _refresh_async(self, discover_network=False):
        # Capture host-owned preset state on the UI thread. Disk and LAN work
        # continue on the reusable coordinator.
        discovered = discover_orca_printers()
        ACTION_WORKER.submit(self._refresh_worker, discovered, discover_network)

    def _refresh_worker(self, discovered, discover_network=False):
        if not ACTION_WORKER.current_job_is_active():
            return
        printers = dashboard_printers()
        unmatched = attach_outbox(printers)
        active_urls = {printer.get("url") for printer in printers}
        with self._status_lock:
            self._list_generation += 1
            generation = self._list_generation
            for stale in set(self._status_cache) - active_urls:
                self._status_cache.pop(stale, None)
            for printer in printers:
                cached = self._status_cache.get(printer.get("url"))
                printer["status"] = (
                    dict(cached)
                    if cached is not None
                    else {"online": None, "state": "checking"}
                )
        payload = {
            "printers": printers,
            "unmatched": unmatched,
        }
        if discovered is not None:
            payload["available"] = available_orca_printers(discovered)
        self._post(payload)
        self._poll_and_push(printers, discover_network, generation=generation)

    def _add_and_refresh(self, entry, discovered):
        if not ACTION_WORKER.current_job_is_active():
            return
        self._add_manual(entry)
        self._refresh_worker(discovered)

    def _remove_and_refresh(self, printer_id, discovered):
        if not ACTION_WORKER.current_job_is_active():
            return
        self._remove_manual(printer_id)
        self._refresh_worker(discovered)

    def _drop_and_refresh(self, stored, discovered):
        if not ACTION_WORKER.current_job_is_active():
            return
        outbox_remove(stored)
        self._refresh_worker(discovered)

    def _poll_dashboard(self):
        if not ACTION_WORKER.current_job_is_active():
            return
        with self._status_lock:
            generation = self._list_generation
        self._poll_and_push(
            dashboard_printers(),
            on_complete=self._finish_dashboard_poll,
            generation=generation,
        )

    def _finish_dashboard_poll(self):
        self._polling = False

    def _poll_into(self, printer, batch=None, generation=None):
        try:
            status = poll_printer(printer)
            if not POLL_WORKERS.current_job_is_active():
                return
            printer["status"] = status
            with self._status_lock:
                if generation is not None and generation != self._list_generation:
                    return
                self._status_cache[printer.get("url")] = dict(status)
            history_changed = record_print_observation(printer, status)
            payload = {"printerStatus": {"url": printer.get("url"), "status": status}}
            if history_changed:
                payload["printHistory"] = {
                    "printerKey": printer_history_key(printer),
                    "printerId": printer.get("id"),
                    "printerUrl": printer.get("url") or "",
                    "printerName": printer.get("name") or printer.get("url") or "",
                    "items": print_history_for_printer(printer),
                }
            self._post(payload)
        finally:
            if batch is not None:
                batch.finish_one()

    def _finish_poll_batch(self, printers, on_complete=None, generation=None):
        try:
            if not POLL_WORKERS.current_job_is_active():
                return
            with self._status_lock:
                if generation is not None and generation != self._list_generation:
                    return
            # Placeholder names (bare IPs) get their real names once statuses are in.
            try:
                autoname_printers(printers)
            except Exception:
                pass
            with self._status_lock:
                if generation is not None and generation != self._list_generation:
                    return
            try:
                attach_outbox(printers)
            except Exception:
                pass
            self._post({"printers": printers})
        finally:
            if on_complete is not None:
                on_complete()

    def _discover_and_push(self, printers, generation=None):
        # Bambu printers announce themselves over SSDP — offer ones that aren't
        # on the dashboard yet (explicit Refresh only).
        if not POLL_WORKERS.current_job_is_active():
            return
        try:
            known_hosts = {
                urllib.parse.urlparse(normalize_url(p.get("url"))).hostname
                for p in printers
            }
            known_serials = {
                (p.get("serial") or "").strip()
                for p in printers
                if (p.get("serial") or "").strip()
            }
            found = []
            for ip, info in ssdp_bambu_names(2.0).items():
                if not POLL_WORKERS.current_job_is_active():
                    return
                if ip in known_hosts or (
                    info.get("serial") and info["serial"] in known_serials
                ):
                    continue
                found.append({
                    "name": info.get("name") or ip,
                    "url": ip,
                    "serial": info.get("serial") or "",
                    "type": "bambu",
                    "source": "network",
                })
            with self._status_lock:
                is_current = generation is None or generation == self._list_generation
            if found and is_current:
                self._post({"discovered": found})
        except Exception:
            pass

    def _poll_and_push(
        self,
        printers,
        discover=False,
        on_complete=None,
        generation=None,
    ):
        # Poll all printers concurrently — one offline/slow host would otherwise
        # block additions or the rest of the dashboard. The action worker only
        # schedules the batch; reusable poll workers push individual results.
        if (
            not ACTION_WORKER.current_job_is_active()
            or not POLL_WORKERS.current_job_is_active()
        ):
            if on_complete is not None:
                on_complete()
            return
        if not printers:
            self._finish_poll_batch(printers, on_complete, generation)
        else:
            batch = PollBatch(
                len(printers),
                lambda: self._finish_poll_batch(printers, on_complete, generation),
            )
            for printer in printers:
                POLL_WORKERS.submit(self._poll_into, printer, batch, generation)
        if not discover:
            return
        POLL_WORKERS.submit(
            self._discover_and_push,
            list(printers),
            generation,
        )

    def _do_outbox_send(self, msg):
        if not ACTION_WORKER.current_job_is_active():
            return
        printers = dashboard_printers()
        stored = msg.get("file") or ""
        target = normalize_url(msg.get("url") or "")
        record = next((r for r in load_outbox_index() if r.get("file") == stored), None)
        printer = next((p for p in printers if normalize_url(p.get("url")) == target), None)
        if record is None or printer is None:
            return
        try:
            with open(os.path.join(OUTBOX_DIR, stored), "rb") as fh:
                file_bytes = fh.read()
        except OSError:
            outbox_remove(stored)  # file vanished — drop the dangling record
            self._refresh_worker(None)
            return
        start = bool(msg.get("start", True))
        label = printer.get("name") or target
        if start:
            # Re-poll live before starting: the dashboard status can be up to a
            # poll interval stale, and we must not queue a job onto a printer
            # that is already printing or paused. Vendor-agnostic via the
            # normalized "state" field (Moonraker/OctoPrint/Bambu).
            live = poll_printer(printer) or {}
            if not ACTION_WORKER.current_job_is_active():
                return
            if not live.get("online"):
                self._post_status(ui_text("notReachableNotStarted", printer=label))
                self._refresh_worker(None)
                return
            if status_is_busy(live):
                self._post_status(ui_text(
                    "busyNotStarted",
                    printer=label,
                    state=ui_state(live.get("state") or "printing"),
                ))
                self._refresh_worker(None)
                return
        result = send_gcode(printer, record.get("name") or stored, file_bytes,
                            start=start, use_ams=bool(msg.get("use_ams", True)))
        if not ACTION_WORKER.current_job_is_active():
            return
        if result.get("ok"):
            if start:
                record_print_started(printer, record.get("name") or stored)
            outbox_remove(stored)
            done = ui_text("sentToPrint" if start else "uploadedNotStarted")
            self._post_status(ui_text(
                "fileOperationDone",
                printer=label,
                name=record.get("name") or stored,
                result=done,
            ))
        else:
            self._post_status(ui_text(
                "sendFailed",
                printer=label,
                error=operation_error_text(result),
            ))
        self._refresh_worker(None)

    def _post_print_history(self, msg):
        target = normalize_url(msg.get("url") or "")
        printer_id = msg.get("id")
        printer = next((
            item for item in dashboard_printers()
            if (
                isinstance(printer_id, int)
                and not isinstance(printer_id, bool)
                and item.get("id") == printer_id
            ) or normalize_url(item.get("url")) == target
        ), None)
        if printer is None:
            return
        self._post({
            "printHistory": {
                "printerKey": printer_history_key(printer),
                "printerId": printer.get("id"),
                "printerUrl": printer.get("url") or "",
                "printerName": printer.get("name") or printer.get("url") or "",
                "items": print_history_for_printer(printer),
            }
        })

    def _do_printer_cmd(self, msg):
        if not ACTION_WORKER.current_job_is_active():
            return
        printers = dashboard_printers()
        target = normalize_url(msg.get("url") or "")
        action = msg.get("action") or ""
        printer = next((p for p in printers if normalize_url(p.get("url")) == target), None)
        if printer is None:
            return
        result = printer_command(printer, action)
        label = printer.get("name") or target
        action_label = ui_text({"pause": "pause", "resume": "resume", "cancel": "stop"}.get(
            action, "genericError"
        ))
        if result.get("ok"):
            self._post_status(ui_text("commandSent", printer=label, action=action_label))
        else:
            self._post_status(ui_text(
                "commandFailed",
                printer=label,
                action=action_label,
                error=operation_error_text(result),
            ))
        self._poll_and_push(printers)

    def _do_mass_print(self, msg):
        if not ACTION_WORKER.current_job_is_active():
            return
        printers = dashboard_printers()
        try:
            file_bytes = base64.b64decode(msg.get("dataB64") or "")
        except Exception:
            self._post_status(ui_text("fileReadFailed"))
            return
        filename = msg.get("filename") or "print.gcode"
        start = bool(msg.get("start"))
        use_ams = bool(msg.get("use_ams", True))
        targets = {normalize_url(u) for u in (msg.get("urls") or [])}
        selected = [p for p in printers if normalize_url(p.get("url")) in targets]
        ok, fail, errors = 0, 0, []
        for p in selected:
            if not ACTION_WORKER.current_job_is_active():
                return
            if start:
                live = poll_printer(p) or {}
                if not ACTION_WORKER.current_job_is_active():
                    return
                if not live.get("online"):
                    fail += 1
                    errors.append(ui_text(
                        "notReachable", printer=p.get("name") or p.get("url")
                    ))
                    continue
                if status_is_busy(live):
                    fail += 1
                    errors.append(ui_text(
                        "busy",
                        printer=p.get("name") or p.get("url"),
                        state=ui_state(live.get("state") or "printing"),
                    ))
                    continue
            result = send_gcode(p, filename, file_bytes, start, use_ams)
            if not ACTION_WORKER.current_job_is_active():
                return
            if result.get("ok"):
                ok += 1
                if start:
                    record_print_started(p, filename)
            else:
                fail += 1
                errors.append("%s: %s" % (
                    p.get("name") or p.get("url"), operation_error_text(result)
                ))
        failed_text = ui_text("massFailed", count=fail) if fail else ""
        text = ui_text("massResult", count=ok, failed=failed_text)
        if errors:
            text += " " + "; ".join(errors[:3])
        self._post_status(text)
        self._refresh_worker(None)

    def _post_status(self, text):
        self._post({"status": text})


def host_capability_lifecycle_available():
    """Whether every selected host base supplies load and unload callbacks."""
    bases = [
        _DASHBOARD_CAPABILITY_BASE,
        orca.slicing.SlicingPipelineCapabilityBase,
    ]
    return all(
        callable(getattr(base, "on_load", None))
        and callable(getattr(base, "on_unload", None))
        for base in bases
    )


@orca.plugin
class PrintersPlugin(orca.base):
    def register_capabilities(self):
        if not host_capability_lifecycle_available():
            start_plugin_runtime()
        orca.register_capability(PrintersDashboard)
        orca.register_capability(PrintersOutbox)
