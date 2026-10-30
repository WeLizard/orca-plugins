# /// script
# requires-python = ">=3.12"
# dependencies = ["keyring==25.7.0"]
#
# [tool.orcaslicer.plugin]
# id = "printers"
# name = "Print Farm"
# description = "Cross-vendor print farm dashboard: live status, temperatures, print progress and thumbnails, one click to each printer's web interface, and G-code delivery to several printers at once."
# author = "FilamentHub"
# version = "0.1.1"
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

Printers come from two sources, explicitly chosen and persisted by target identity:
  * OrcaSlicer's own printer presets that have a network address configured
    (read via orca.host.preset_bundle() on the UI thread) — no manual entry.
  * Manually added entries, stored in Orca's private plugin storage when
    available, with a stable Orca data-root directory as an old-host fallback.

Active backends: Moonraker (Klipper), OctoPrint, and Bambu Lab in LAN mode.
Other OrcaSlicer host types remain visible as planned integrations, but are not
treated as working until their status, control, and upload transports exist.
Bambu talks MQTT over TLS on its LAN port (user "bblp", the
printer's access code); a self-contained stdlib MQTT client reads one status
report, with no cloud round-trip. System credentials use keyring; host calls stay on the UI thread and all network / disk work is
offloaded to worker threads.

What is intentionally NOT done here, because it belongs to the host and would be
a good plugin capability to expose rather than work around:
  * "Open" uses a confined host window for the printer's web UI; a plugin cannot
    programmatically open the native Device tab for a specific printer.
  * The outbox uses the slicing-pipeline G-code export seam, which currently
    runs on file export and print-host upload rather than on Slice alone.
"""

import base64
import hashlib
import hmac
import html
import ipaddress
import io
import json
import math
import os
import pathlib
import posixpath
import queue
import re
import shutil
import stat
import socket
import ssl
import struct
import threading
import time
import unicodedata
import uuid
import urllib.error
import urllib.parse
import urllib.request

import orca

# BEGIN PRINT_FARM_HELPERS
# Source modules stay separate for review and focused tests. The release builder
# embeds their exact contents into this single-file plugin at this marker.
import importlib.util as _pf_importlib_util
import sys as _pf_sys


def _pf_load_local_helper(name):
    module_name = __name__ + "_" + name
    path = pathlib.Path(__file__).with_name(name + ".py")
    spec = _pf_importlib_util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError("Print Farm helper is unavailable: " + name)
    module = _pf_importlib_util.module_from_spec(spec)
    _pf_sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


_pf_artifact = _pf_load_local_helper("print_artifact")
_pf_transfer = _pf_load_local_helper("artifact_transfer")
_pf_upload = _pf_load_local_helper("upload_transport")
_pf_journal = _pf_load_local_helper("operation_journal")
_pf_scheduler = _pf_load_local_helper("action_scheduler")
_pf_approval = _pf_load_local_helper("operation_approval")
_pf_duet = _pf_load_local_helper("duet_adapter")
_pf_prusalink = _pf_load_local_helper("prusalink_adapter")
_pf_repetier = _pf_load_local_helper("repetier_adapter")
_pf_u1_camera = _pf_load_local_helper("u1_camera")
_pf_creality = _pf_load_local_helper("creality_adapter")
_pf_flashforge = _pf_load_local_helper("flashforge_adapter")
_pf_settings = _pf_load_local_helper("plugin_settings")
_pf_credentials = _pf_load_local_helper("credential_store")
# END PRINT_FARM_HELPERS


# Orca asks the user before a plugin creates its first thread. Asked on the UI
# thread it is one modal in context; asked from the loader thread it is denied
# and shown later, once per attempt, so two attempts mean two pop-ups. Threads
# therefore start only from the UI thread until one has started.
_THREAD_GRANT_SEEN = False


def start_thread_if_allowed(thread):
    """Start a plugin thread without provoking a prompt from a loader thread."""
    global _THREAD_GRANT_SEEN
    if (threading.current_thread() is not threading.main_thread()
            and not _THREAD_GRANT_SEEN):
        return False
    try:
        thread.start()
    except (PermissionError, RuntimeError):
        return False
    _THREAD_GRANT_SEEN = True
    return True


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

    def __init__(self, name, max_workers, idle_timeout=45.0, initially_active=True):
        self._name = name
        self._max_workers = max(1, int(max_workers))
        self._idle_timeout = idle_timeout
        self._jobs = queue.Queue()
        self._lock = threading.Lock()
        self._workers = set()
        self._busy = 0
        self._sequence = 0
        self._generation = 0
        self._stopping = not initially_active
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

    def accepting_work(self):
        with self._lock:
            return not self._stopping

    def submit(self, function, *args, **kwargs):
        job = BackgroundJob()
        with self._lock:
            if self._stopping:
                raise RuntimeError("background worker is stopped")
            generation = self._generation
            desired = min(self._max_workers, max(1, self._busy + self._jobs.qsize() + 1))
            while len(self._workers) < desired:
                self._sequence += 1
                worker = threading.Thread(
                    target=self._run,
                    name="%s-%d" % (self._name, self._sequence),
                    daemon=True,
                )
                worker.start()
                self._workers.add(worker)
            self._jobs.put((generation, job, function, args, kwargs))
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
ACTION_WORKER = ReusableDaemonPool("printers-action", max_workers=1,
                                   initially_active=False)
POLL_WORKERS = ReusableDaemonPool("printers-poll", max_workers=8,
                                  initially_active=False)
CAMERA_WORKER = ReusableDaemonPool("printers-camera", max_workers=1,
                                   initially_active=False)
_U1_CAMERA_SLOT = threading.Lock()
_U1_CAMERA_CLIENTS = set()
_U1_CAMERA_CLIENTS_LOCK = threading.Lock()
_U1_CAMERA_OWNER = None

_PLUGIN_RUNTIME_LOCK = threading.Lock()
_PLUGIN_RUNTIME_ACTIVE = False
_PLUGIN_RUNTIME_EPOCH = 0
_SCHEDULER_ACTION_CONTEXT = threading.local()
_CREDENTIAL_WORK_CONTEXT = threading.local()
# Set by _journal_begin on the calling thread: True when an unresolved earlier
# operation (hold) is what stopped the new one.
_JOURNAL_CLAIM = threading.local()


_RECOVERY_LOCK = threading.Lock()
_RECOVERY_DONE = set()      # storage roots handled by this process
_STORAGE_OWNER_HANDLES = {}  # storage root -> open, OS-locked owner file


def _acquire_storage_owner(root):
    """Hold an OS lock for the whole process life: proof that no other Orca
    instance is dispatching against this storage (None when it is not ours)."""
    try:
        handle = open(os.path.join(root, "operations.owner"), "a+b")
    except OSError:
        return None
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, ImportError):
        handle.close()
        return None
    return handle


def _journal_cancel_plan(journal, operation_id):
    """Release a prepared operation whose dispatch claim failed (nothing was sent)."""
    try:
        journal.report(operation_id, "cancelled_before_dispatch", actor="dispatcher")
    except Exception:
        pass


def recover_interrupted_operations():
    """Turn operations a crashed run left in flight into ``unknown_outcome``.

    Runs once per process and storage, before any worker can dispatch. Nothing
    is sent or retried: an unknown operation only keeps its hold until the user
    releases it from the Print journal after looking at the printer.
    """
    if _STORAGE_DENIED or not PLUGIN_STORAGE_DIR:
        return 0
    root = os.path.realpath(PLUGIN_STORAGE_DIR)
    with _RECOVERY_LOCK:
        if root in _RECOVERY_DONE:
            return 0
        _RECOVERY_DONE.add(root)
        handle = _acquire_storage_owner(root)
        if handle is None:
            return 0
        _STORAGE_OWNER_HANDLES[root] = handle
        database = os.path.join(root, "operations.sqlite3")
        if not os.path.exists(database):
            return 0
        journal = None
        try:
            journal = _pf_journal.OperationJournal(database)
            return journal.recover_interrupted(
                hashlib.sha256(root.encode("utf-8")).hexdigest(),
                exclusive_session_owner=True)
        except Exception:
            return 0
        finally:
            if journal is not None:
                try:
                    journal.close()
                except Exception:
                    pass


def start_plugin_runtime():
    """Start process-wide resources from an audited lifecycle callback."""
    global _PLUGIN_RUNTIME_ACTIVE, _PLUGIN_RUNTIME_EPOCH
    with _PLUGIN_RUNTIME_LOCK:
        if _PLUGIN_RUNTIME_ACTIVE:
            if _PRUNE_STOP is not None and _PRUNE_STOP["timer"] is None:
                _schedule_prune(_PRUNE_STOP, _OUTBOX_PRUNE_DELAY)
            return False
        refresh_ui_language()
        if not configure_plugin_storage():
            return False
        recover_interrupted_operations()
        # Keep the state flag and both worker generations in one critical
        # section.  A page can become ready while a capability is being
        # cancelled; previously that race could leave the flag active while
        # both pools were stopped, so every refresh was silently rejected.
        ACTION_WORKER.activate()
        POLL_WORKERS.activate()
        CAMERA_WORKER.activate()
        _PLUGIN_RUNTIME_EPOCH += 1
        _PLUGIN_RUNTIME_ACTIVE = True
        start_outbox_pruning()
    return True


def stop_plugin_runtime():
    """Stop accepting work and invalidate all jobs from the loaded generation."""
    global _PLUGIN_RUNTIME_ACTIVE, _PLUGIN_RUNTIME_EPOCH
    clear_credential_access()
    with _PLUGIN_RUNTIME_LOCK:
        if not _PLUGIN_RUNTIME_ACTIVE:
            return False
        stop_outbox_pruning()
        CAMERA_WORKER.shutdown(wait_timeout=0)
        with _U1_CAMERA_CLIENTS_LOCK:
            clients = list(_U1_CAMERA_CLIENTS)
            camera_owner = _U1_CAMERA_OWNER
        if camera_owner:
            camera_owner[0]._cancel_u1_camera()
        for client in clients:
            client.cancel()
        ACTION_WORKER.shutdown()
        POLL_WORKERS.shutdown()
        CAMERA_WORKER.shutdown()
        _PLUGIN_RUNTIME_EPOCH += 1
        _PLUGIN_RUNTIME_ACTIVE = False
    return True


class _PluginRuntimeLifecycleMixin:
    def on_load(self):
        start_plugin_runtime()

    def on_cancelled(self):
        stop_plugin_runtime()

    def on_unload(self):
        stop_plugin_runtime()


def post_window(window, payload):
    dashboard = getattr(_SCHEDULER_ACTION_CONTEXT, "dashboard", None)
    if dashboard is not None and not dashboard._action_context_is_active():
        return False
    for worker_name in ("ACTION_WORKER", "POLL_WORKERS", "CAMERA_WORKER"):
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
PLUGIN_VERSION = "0.1.1"

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
# build_package.py replaces this empty mapping in release artifacts. The JSON
# files remain authoritative in the source tree while the installable wheel
# stays a single OrcaSlicer plugin module.
_EMBEDDED_UI_COPY = {}
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
    """Orca's UI language; reads host state, so call it on the UI thread.

    Newer hosts expose app_info()["language"]; the older app_language() stays
    as a fallback and anything unavailable yields an empty string."""
    host = getattr(orca, "host", None)
    app_info = getattr(host, "app_info", None)
    if callable(app_info):
        try:
            info = app_info()
            value = info.get("language") if hasattr(info, "get") else getattr(info, "language", "")
            if isinstance(value, str) and value.strip():
                return normalize_ui_language(value)
        except Exception:
            pass
    app_language = getattr(host, "app_language", None)
    if not callable(app_language):
        return ""
    try:
        return normalize_ui_language(app_language())
    except Exception:
        return ""


def load_ui_catalogs(directory=None):
    root = directory or _LOCALE_DIR
    catalogs = {
        locale: dict(data)
        for locale, data in _EMBEDDED_UI_COPY.items()
        if locale in ORCA_UI_LOCALES and isinstance(data, dict)
    }
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


def transport_operation_error(exc, key):
    code, safe_message = safe_transport_error(exc)
    return {
        "ok": False,
        "error_key": key,
        "error_values": {"error": safe_message},
        "error_code": code,
    }


def transport_error_code(exc):
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code in (401, 403):
            return "authorization_denied"
        if exc.code == 404:
            return "endpoint_not_found"
        if exc.code == 409:
            return "device_conflict"
        if exc.code == 429:
            return "rate_limited"
        if 500 <= exc.code <= 599:
            return "server_error"
        return "http_error"
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return "timeout"
    if isinstance(exc, urllib.error.URLError):
        reason = getattr(exc, "reason", None)
        return "timeout" if isinstance(reason, (TimeoutError, socket.timeout)) else "network_error"
    if isinstance(exc, (ValueError, json.JSONDecodeError)):
        return "invalid_response"
    if isinstance(exc, (ConnectionError, OSError)):
        return "network_error"
    return "request_failed"


def safe_transport_error(exc):
    code = transport_error_code(exc)
    labels = {
        "authorization_denied": "authentication rejected",
        "endpoint_not_found": "printer endpoint unavailable",
        "device_conflict": "printer is not ready for this request",
        "rate_limited": "printer is rate limiting requests",
        "server_error": "printer service failed",
        "http_error": "printer rejected the request",
        "timeout": "printer request timed out",
        "network_error": "printer could not be reached",
        "invalid_response": "printer returned incomplete or invalid data",
        "request_failed": "printer request failed",
    }
    if isinstance(exc, urllib.error.HTTPError) and code in {
        "authorization_denied", "endpoint_not_found", "device_conflict",
        "rate_limited", "server_error", "http_error",
    }:
        return code, "%s (HTTP %d)" % (labels[code], exc.code)
    return code, labels[code]


def operation_error_text(result):
    key = (result or {}).get("error_key")
    if key:
        return ui_text(key, **((result or {}).get("error_values") or {}))
    return ui_text("genericError")

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
_STORAGE_DENIED = True
_LEGACY_IMPORT_SOURCE = ""
_LEGACY_IMPORT_BLOCKED = False
_LEGACY_PREVIEW_TOKEN = ""
_LEGACY_PREVIEW_BINDING = None


def _optional_regular_file(path):
    """Return False only for a genuinely missing store; propagate denied I/O."""
    try:
        return stat.S_ISREG(os.stat(path, follow_symlinks=False).st_mode)
    except FileNotFoundError:
        return False
PRINTERS_FILE = os.path.join(PLUGIN_DIR, "printers.json")
PRINT_HISTORY_FILE = os.path.join(PLUGIN_DIR, "print_history.json")
ICON_PATH = os.path.join(PLUGIN_DIR, "printers.svg")
PACKAGED_ICON_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "printers.svg",
)
_ICON_SVG = b'''<?xml version="1.0" encoding="UTF-8"?><svg id="a" xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 20 20"><rect x="6.5" y="2.5" width="6" height="6" rx="1" ry="1" style="fill:none; stroke:#fff; stroke-miterlimit:10;"/><rect x="1.5" y="12.5" width="6" height="6" rx="1" ry="1" style="fill:none; stroke:#fff; stroke-miterlimit:10;"/><rect x="11.5" y="12.5" width="6" height="6" rx="1" ry="1" style="fill:none; stroke:#fff; stroke-miterlimit:10;"/><polyline points="4.5 12 4.5 10.5 14.5 10.5 14.5 12" style="fill:none; stroke:#fff; stroke-miterlimit:10;"/><line x1="9.5" y1="9" x2="9.5" y2="10.5" style="fill:none; stroke:#fff; stroke-miterlimit:10;"/><polygon points="8 3 11 3 11 5 10 5 10 6 9 6 9 5 8 5" style="fill:#fff;"/><polygon points="3 13 6 13 6 15 5 15 5 16 4 16 4 15 3 15" style="fill:#fff;"/><polygon points="13 13 16 13 16 15 15 15 15 16 14 16 14 15 13 15" style="fill:#fff;"/></svg>'''


def _temporary_path(path):
    return "%s.tmp.%d.%d" % (path, os.getpid(), threading.get_ident())


def write_bytes_atomic(path, payload):
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    temporary = _temporary_path(path)
    try:
        with open(temporary, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.remove(temporary)
        except OSError:
            pass
        raise


def ensure_icon():
    for candidate in (PACKAGED_ICON_PATH, ICON_PATH):
        if os.path.isfile(candidate):
            return candidate
    if _STORAGE_DENIED or not PLUGIN_STORAGE_DIR:
        return ""
    target = os.path.join(PLUGIN_STORAGE_DIR, "printers.svg")
    try:
        write_bytes_atomic(target, _ICON_SVG)
    except OSError:
        return ""
    return target if os.path.isfile(target) else ""


def fallback_plugin_storage_dir():
    """Stable state root for hosts without ``orca.host.plugin.storage()``."""
    plugins_root = os.path.dirname(os.path.abspath(PLUGIN_DIR))
    if os.path.basename(plugins_root).lower() != "orca_plugins":
        return ""
    data_root = os.path.dirname(plugins_root)
    return os.path.join(data_root, ".print-farm", "orca-plugin")


def configure_plugin_storage():
    """Select account storage without implicitly importing legacy state."""
    global PLUGIN_STORAGE_DIR, PRINTERS_FILE, PRINT_HISTORY_FILE, OUTBOX_DIR, OUTBOX_INDEX
    global _STORAGE_DENIED, _LEGACY_IMPORT_SOURCE, _LEGACY_IMPORT_BLOCKED
    clear_credential_access()
    _STORAGE_DENIED = True
    _LEGACY_IMPORT_SOURCE = ""
    _LEGACY_IMPORT_BLOCKED = False

    fallback_root = fallback_plugin_storage_dir()
    target_root = ""
    try:
        plugin_host = getattr(getattr(orca, "host", None), "plugin", None)
        storage = getattr(plugin_host, "storage", None)
    except Exception:
        return False
    if callable(storage):
        try:
            supplied_root = storage()
            if not isinstance(supplied_root, str) or not supplied_root:
                return False
            target_root = os.path.abspath(supplied_root)
        except Exception:
            # A denied native plugin store must not become a shared fallback.
            return False
    elif not target_root:
        # A missing API is the only old-host fallback condition.  Exceptions,
        # None and empty results from the native API are denied/signed-out.
        target_root = fallback_root
    if not target_root:
        return False
    target_state_exists = any(os.path.lexists(os.path.join(target_root, name)) for name in (
        "printers.json", "print_history.json", "outbox_index.json", "outbox",
    ))
    legacy_roots = []
    for candidate in (fallback_root, PLUGIN_DIR):
        if candidate and os.path.abspath(candidate) != os.path.abspath(target_root):
            if any(os.path.lexists(os.path.join(candidate, name)) for name in (
                "printers.json", "print_history.json", "outbox_index.json", "outbox",
            )):
                legacy_roots.append(os.path.abspath(candidate))
    if len(legacy_roots) > 1:
        _LEGACY_IMPORT_BLOCKED = True
    elif legacy_roots and not target_state_exists:
        _LEGACY_IMPORT_SOURCE = legacy_roots[0]
    try:
        if not _LEGACY_IMPORT_SOURCE:
            os.makedirs(target_root, mode=0o700, exist_ok=True)
            try:
                # Best effort only: on Windows this does not set or verify an
                # owner-only ACL. Do not treat chmod success as a security claim.
                os.chmod(target_root, 0o700)
            except OSError:
                pass
    except OSError:
        return False

    with _STORE_LOCK:
        PLUGIN_STORAGE_DIR = target_root
        PRINTERS_FILE = os.path.join(target_root, "printers.json")
        PRINT_HISTORY_FILE = os.path.join(target_root, "print_history.json")
        target_outbox = os.path.join(target_root, "outbox")
        OUTBOX_DIR = target_outbox
        OUTBOX_INDEX = os.path.join(target_root, "outbox_index.json")
        _STORAGE_DENIED = False
    return True


def legacy_import_preview(issue_token=True):
    """Return counts and readiness only; never disclose stored field values."""
    if not _LEGACY_IMPORT_SOURCE or _LEGACY_IMPORT_BLOCKED:
        return {"available": False, "blocked": bool(_LEGACY_IMPORT_BLOCKED)}
    try:
        summary = {"available": True, "printers": 0, "history": 0, "outbox": 0}
        for name, key in (("printers.json", "printers"), ("print_history.json", "history"),
                          ("outbox_index.json", "outbox")):
            path = os.path.join(_LEGACY_IMPORT_SOURCE, name)
            if not os.path.exists(path):
                continue
            with open(path, "r", encoding="utf-8") as stream:
                value = json.load(stream)
            if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
                raise ValueError("invalid legacy store")
            if key == "printers" and any(
                credential in item and private_credential(item[credential]) is None
                for item in value for credential in ("apikey", "access_code")
            ):
                raise ValueError("invalid legacy credential")
            if key == "history" and any(not item.get("id") for item in value):
                raise ValueError("invalid legacy history")
            if key == "outbox" and not all(isinstance(item.get("file"), str) for item in value):
                raise ValueError("invalid legacy outbox index")
            summary[key] = len(value)
        outbox = os.path.join(_LEGACY_IMPORT_SOURCE, "outbox")
        if os.path.lexists(outbox):
            if not os.path.isdir(outbox) or os.path.islink(outbox):
                raise ValueError("invalid legacy outbox")
            entries = list(os.scandir(outbox))
            if any(not entry.is_file(follow_symlinks=False)
                   or os.path.basename(entry.name) != entry.name for entry in entries):
                raise ValueError("unsafe legacy outbox entry")
            summary["outbox_files"] = len(entries)
        else:
            summary["outbox_files"] = 0
        if issue_token:
            global _LEGACY_PREVIEW_TOKEN, _LEGACY_PREVIEW_BINDING
            digest = _legacy_state_digest(_LEGACY_IMPORT_SOURCE,
                                          ("printers.json", "print_history.json",
                                           "outbox_index.json", "outbox"))
            _LEGACY_PREVIEW_TOKEN = uuid.uuid4().hex
            _LEGACY_PREVIEW_BINDING = (_LEGACY_IMPORT_SOURCE, PLUGIN_STORAGE_DIR, digest)
            summary["confirmation"] = _LEGACY_PREVIEW_TOKEN
        return summary
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        return {"available": False, "blocked": True}


def import_legacy_storage(confirmation):
    """Import one validated legacy snapshot into this plugin's empty local store."""
    global _LEGACY_IMPORT_SOURCE, _LEGACY_IMPORT_BLOCKED
    global _LEGACY_PREVIEW_TOKEN, _LEGACY_PREVIEW_BINDING
    binding = _LEGACY_PREVIEW_BINDING
    if (not isinstance(confirmation, str) or not confirmation
            or confirmation != _LEGACY_PREVIEW_TOKEN or binding is None
            or _STORAGE_DENIED or not _LEGACY_IMPORT_SOURCE
            or binding[:2] != (_LEGACY_IMPORT_SOURCE, PLUGIN_STORAGE_DIR)):
        return False
    _LEGACY_PREVIEW_TOKEN = ""
    _LEGACY_PREVIEW_BINDING = None
    preview = legacy_import_preview(issue_token=False)
    if (not preview.get("available") or preview.get("blocked")
            or _legacy_state_digest(_LEGACY_IMPORT_SOURCE,
                                    ("printers.json", "print_history.json",
                                     "outbox_index.json", "outbox")) != binding[2]):
        return False
    target = PLUGIN_STORAGE_DIR
    state_names = ("printers.json", "print_history.json", "outbox_index.json", "outbox")
    if any(os.path.lexists(os.path.join(target, name)) for name in state_names):
        return False
    if os.path.isdir(target) and os.listdir(target):
        return False
    parent = os.path.dirname(target)
    os.makedirs(parent, exist_ok=True)
    stage = target + ".import-stage-%d" % os.getpid()
    source_digest = binding[2]
    backup = target + ".legacy-backup-" + source_digest[:16]
    if os.path.lexists(stage):
        return False
    try:
        os.makedirs(stage, mode=0o700)
        for name in state_names:
            source = os.path.join(_LEGACY_IMPORT_SOURCE, name)
            if not os.path.lexists(source):
                continue
            destination = os.path.join(stage, name)
            if name == "outbox":
                shutil.copytree(source, destination, symlinks=True)
            else:
                shutil.copy2(source, destination)
        # The staged snapshot must exactly match the bytes shown at preview time.
        original_root = _LEGACY_IMPORT_SOURCE
        staged_preview = legacy_import_preview
        globals()["_LEGACY_IMPORT_SOURCE"] = stage
        try:
            if not staged_preview(issue_token=False).get("available"):
                raise ValueError("staged legacy data failed validation")
        finally:
            globals()["_LEGACY_IMPORT_SOURCE"] = original_root
        if _legacy_state_digest(stage, state_names) != source_digest:
            raise ValueError("staged legacy snapshot changed")
        if _legacy_state_digest(original_root, state_names) != source_digest:
            raise ValueError("legacy source changed during import")
        backup_stage = backup + ".stage-" + uuid.uuid4().hex
        try:
            os.makedirs(backup_stage, mode=0o700)
            for name in state_names:
                source = os.path.join(original_root, name)
                if not os.path.lexists(source):
                    continue
                destination = os.path.join(backup_stage, name)
                if name == "outbox":
                    shutil.copytree(source, destination, symlinks=True)
                else:
                    shutil.copy2(source, destination)
            if _legacy_state_digest(backup_stage, state_names) != source_digest:
                raise ValueError("legacy backup digest mismatch")
            if _legacy_state_digest(original_root, state_names) != source_digest:
                raise ValueError("legacy source changed during backup")
            if os.path.lexists(backup):
                if _legacy_state_digest(backup, state_names) != source_digest:
                    raise ValueError("existing recovery backup differs")
                shutil.rmtree(backup_stage)
            else:
                os.replace(backup_stage, backup)
        except Exception:
            if os.path.isdir(backup_stage):
                shutil.rmtree(backup_stage, ignore_errors=True)
            raise
        if any(os.path.lexists(os.path.join(target, name)) for name in state_names):
            raise FileExistsError("plugin storage is no longer empty")
        if os.path.isdir(target):
            if os.listdir(target):
                raise FileExistsError("plugin storage is not empty")
            os.rmdir(target)
        os.replace(stage, target)
        _LEGACY_IMPORT_SOURCE = ""
        _LEGACY_IMPORT_BLOCKED = False
        return True
    except (OSError, ValueError, shutil.Error):
        if os.path.isdir(stage):
            shutil.rmtree(stage, ignore_errors=True)
        return False


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
    printer = printer or {}
    # Bambu LAN brokers and FTPS servers always present a self-signed
    # certificate, so default CA verification can never succeed for them; this
    # is how 0.0.9 behaved. Other TLS endpoints stay verified unless the user
    # opts out per printer.
    if printer.get("insecure") or backend_for_type(printer.get("type")) == "bambu":
        return _SSL_INSECURE
    return _SSL_VERIFY


class BambuTlsError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


_BAMBU_TLS_ERRORS = {
    "tls_trust_required": "tlsTrustRequired",
    "tls_certificate_changed": "tlsCertificateChanged",
    "tls_probe_failed": "tlsProbeFailed",
    "tls_trust_stale": "tlsTrustStale",
    "tls_trust_save_failed": "tlsTrustSaveFailed",
    "tls_trust_busy": "tlsTrustBusy",
}
_BAMBU_TLS_PORTS = {"mqtt": 8883, "ftps": 990}
_BAMBU_TARGET_LOCKS = tuple(threading.RLock() for _ in range(64))


def _bambu_target_lock(printer):
    key = str((printer or {}).get("target_uuid") or "")
    return _BAMBU_TARGET_LOCKS[int(hashlib.sha256(key.encode()).hexdigest()[:8], 16) % 64]


def _bambu_tls_identity(printer):
    return tuple(printer.get(key) for key in (
        "owner_scope_uuid", "target_uuid", "target_selector", "binding_revision",
    )) + (_endpoint_key(printer.get("url")), backend_for_type(printer.get("type")))


def _stored_bambu_record(printer):
    identity = _bambu_tls_identity(printer)
    if not all(identity[:5]) or identity[5] != "bambu":
        return None
    with _STORE_LOCK:
        matches = [entry for entry in load_manual_printers()
                   if _bambu_tls_identity(entry) == identity]
    return matches[0] if len(matches) == 1 else None


def _bambu_pin(printer, protocol):
    record = _stored_bambu_record(printer)
    if record is None:
        raise BambuTlsError("tls_trust_stale")
    pins = record.get("tls_pins")
    pin = pins.get(protocol) if isinstance(pins, dict) else None
    if not isinstance(pin, str) or not re.fullmatch(r"[0-9a-f]{64}", pin):
        raise BambuTlsError("tls_trust_required")
    return pin


def _bambu_peer_fingerprint(sock):
    certificate = sock.getpeercert(binary_form=True)
    if not isinstance(certificate, bytes) or not certificate:
        raise BambuTlsError("tls_probe_failed")
    return hashlib.sha256(certificate).hexdigest()


def _verify_bambu_socket(printer, protocol, sock):
    expected = _bambu_pin(printer, protocol)
    if not hmac.compare_digest(expected, _bambu_peer_fingerprint(sock)):
        raise BambuTlsError("tls_certificate_changed")


def _probe_bambu_certificate(printer, protocol, timeout=5):
    if protocol not in _BAMBU_TLS_PORTS:
        raise BambuTlsError("tls_probe_failed")
    raw = None
    sock = None
    try:
        host = urllib.parse.urlparse(normalize_url(printer.get("url"))).hostname
        if not host:
            raise BambuTlsError("tls_probe_failed")
        raw = socket.create_connection((host, _BAMBU_TLS_PORTS[protocol]), timeout=timeout)
        raw.settimeout(timeout)
        sock = _SSL_INSECURE.wrap_socket(raw, server_hostname=host)
        return _bambu_peer_fingerprint(sock)
    except BambuTlsError:
        raise
    except (OSError, ssl.SSLError, ValueError) as exc:
        raise BambuTlsError("tls_probe_failed") from exc
    finally:
        if sock is not None:
            sock.close()
        elif raw is not None:
            raw.close()


def moonraker_headers(printer):
    key = (printer or {}).get("apikey")
    return {"X-Api-Key": key} if isinstance(key, str) and key else {}


# --------------------------------------------------------------------------- #
# Manual printer store (redirected to durable storage during plugin lifecycle)
# --------------------------------------------------------------------------- #
_PRINTERS_STORE_DAMAGED = False
_PRINTER_IDENTITY_SCHEMA = 1


def _ensure_printer_identity(printer, owner_scope=None):
    """Attach stable local identity without treating a route as identity."""
    owner_scope = owner_scope or printer.get("owner_scope_uuid") or str(uuid.uuid4())
    try:
        owner_scope = str(uuid.UUID(owner_scope))
    except (TypeError, ValueError, AttributeError):
        owner_scope = str(uuid.uuid4())
    printer["owner_scope_uuid"] = owner_scope
    target_uuid = printer.get("target_uuid")
    if not isinstance(target_uuid, str) or not target_uuid:
        target_uuid = str(uuid.uuid4())
        printer["target_uuid"] = target_uuid
    if printer.get("type") == "repetier":
        printer["target_selector"] = repetier_target_selector(printer)
    selector = printer.get("target_selector")
    if not isinstance(selector, str) or not selector:
        printer["target_selector"] = str(uuid.uuid4())
    server_uuid = printer.get("server_uuid")
    if not isinstance(server_uuid, str) or not server_uuid:
        printer["server_uuid"] = _server_uuid_for(printer, owner_scope)
    revision = printer.get("binding_revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        printer["binding_revision"] = 1
    printer["identity_schema"] = _PRINTER_IDENTITY_SCHEMA
    return printer


def printer_binding_key(printer):
    record = printer if isinstance(printer, dict) else {}
    return "%s:%s:%s" % (
        record.get("owner_scope_uuid") or "",
        record.get("target_uuid") or "",
        record.get("binding_revision") or "",
    )


def repetier_target_selector(printer):
    return "repetier:" + hashlib.sha256((normalize_url(printer.get("url")) + "\0" +
        str(printer.get("repetier_slug") or "")).encode("utf-8")).hexdigest()


def _endpoint_key(url):
    """"endpoint:scheme://host[:port]" for an address, or "" when it has none."""
    parsed = urllib.parse.urlsplit(normalize_url(url))
    if not parsed.hostname:
        return ""
    authority = parsed.hostname.casefold()
    try:
        port = parsed.port
    except ValueError:
        port = None
    if port:
        authority += ":%d" % port
    return "endpoint:%s://%s" % (parsed.scheme.casefold(), authority)


def _server_uuid_for(printer, owner_scope):
    serial = str((printer or {}).get("serial") or "").strip()
    if serial:
        key = "serial:" + serial.casefold()
    else:
        key = _endpoint_key((printer or {}).get("url"))
        if not key:
            return str(uuid.uuid4())
    return str(uuid.uuid5(uuid.UUID(owner_scope), "server:" + key))


def resolve_printer_target(printers, message):
    """Resolve an action only when it carries the exact persisted binding."""
    message = message if isinstance(message, dict) else {}
    target_uuid = message.get("targetUuid") or message.get("target_uuid")
    if not isinstance(target_uuid, str) or not target_uuid:
        return None
    matches = [p for p in printers if p.get("target_uuid") == target_uuid]
    if len(matches) != 1:
        return None
    printer = matches[0]
    if (printer.get("type") == "repetier" and
            printer.get("target_selector") != repetier_target_selector(printer)):
        return None
    if message.get("ownerScope") != printer.get("owner_scope_uuid"):
        return None
    if message.get("url") is None or normalize_url(message.get("url")) != normalize_url(printer.get("url")):
        return None
    if message.get("bindingRevision") != printer.get("binding_revision"):
        return None
    if message.get("targetSelector") != printer.get("target_selector"):
        return None
    if message.get("connectionType") != printer.get("type"):
        return None
    return printer


def load_manual_printers():
    global _PRINTERS_STORE_DAMAGED
    if _STORAGE_DENIED:
        _PRINTERS_STORE_DAMAGED = True
        return []
    try:
        present = _optional_regular_file(PRINTERS_FILE)
    except OSError:
        _PRINTERS_STORE_DAMAGED = True
        return []
    if not present:
        if os.path.lexists(PRINTERS_FILE):
            _PRINTERS_STORE_DAMAGED = True
            return []
        _PRINTERS_STORE_DAMAGED = False
        return []
    try:
        with open(PRINTERS_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, list):
            raise ValueError("invalid printers store")
        result = []
        owner_scope = next((
            raw.get("owner_scope_uuid") for raw in data
            if isinstance(raw, dict) and isinstance(raw.get("owner_scope_uuid"), str)
        ), None)
        migrated = False
        for raw in data:
            if not isinstance(raw, dict):
                raise ValueError("invalid printer record")
            printer = dict(raw)
            for credential_key in ("apikey", "access_code"):
                if credential_key in printer and private_credential(printer[credential_key]) is None:
                    raise ValueError("invalid stored credential")
            printer["url"] = normalize_url(printer.get("url"))
            before = dict(printer)
            _ensure_printer_identity(printer, owner_scope)
            owner_scope = printer["owner_scope_uuid"]
            migrated = migrated or printer != before
            result.append(printer)
        if migrated:
            # Atomic replacement preserves the original on failure; never create
            # another plaintext credential copy during identity migration.
            if not save_manual_printers(result):
                raise OSError("could not persist printer identity migration")
        _PRINTERS_STORE_DAMAGED = False
        return result
    except (OSError, ValueError):
        _PRINTERS_STORE_DAMAGED = True
        return []


_CREDENTIAL_VAULT = _pf_credentials.Vault()


def _credential_work_is_active():
    if not ACTION_WORKER.current_job_is_active() or not POLL_WORKERS.current_job_is_active():
        return False
    dashboard = getattr(_SCHEDULER_ACTION_CONTEXT, "dashboard", None)
    if dashboard is not None and (not dashboard._accept_actions
                                  or not dashboard._action_context_is_active()):
        return False
    guard = getattr(_CREDENTIAL_WORK_CONTEXT, "guard", None)
    return guard is None or guard()


def _run_credential_work(guard, function, *args, **kwargs):
    previous = getattr(_CREDENTIAL_WORK_CONTEXT, "guard", None)
    _CREDENTIAL_WORK_CONTEXT.guard = lambda: guard() and (previous is None or previous())
    try:
        return function(*args, **kwargs)
    finally:
        _CREDENTIAL_WORK_CONTEXT.guard = previous


def credential_operation_error():
    result = operation_error("credentialUnavailable")
    result.update(error_code="credential_unavailable", journal_state="rejected",
                  evidence_ref="credential:pre_dispatch")
    return result


def clear_credential_access():
    _CREDENTIAL_VAULT.clear()
    with _ORCA_KEYS_LOCK:
        _ORCA_KEYS.clear()


def credential_state(printer):
    if printer.get("credential_refs"):
        return "stored"
    if any(printer.get(field) for field in _pf_credentials.FIELDS):
        return "legacy"
    return "none"


def _credential_card_current(printer):
    if not printer.get("credential_refs"):
        return True
    with _STORE_LOCK:
        matches = [card for card in load_manual_printers()
                   if card.get("target_uuid") == printer.get("target_uuid")
                   and card.get("owner_scope_uuid") == printer.get("owner_scope_uuid")]
        return (len(matches) == 1
                and all(matches[0].get(key) == printer.get(key) for key in
                        ("url", "type", "target_selector", "binding_revision", "credential_refs", "repetier_slug")))


def hydrate_credentials(printer):
    """Resolve secrets only at worker network boundaries, never during store reads."""
    if (printer.get("type") == "repetier" and
            (not _pf_repetier.valid_slug(printer.get("repetier_slug")) or
             printer.get("target_selector") != repetier_target_selector(printer))):
        raise _pf_credentials.Unavailable()
    if (not _credential_work_is_active()
            or "credential_refs" in printer and not isinstance(printer["credential_refs"], dict)
            or not _credential_card_current(printer)):
        raise _pf_credentials.Unavailable()
    hydrated = dict(printer) if printer.get("credential_refs") else printer
    root, epoch = PLUGIN_STORAGE_DIR, _PLUGIN_RUNTIME_EPOCH
    for field, ref in (printer.get("credential_refs") or {}).items():
        if field not in _pf_credentials.FIELDS:
            raise _pf_credentials.Unavailable()
        hydrated[field] = _CREDENTIAL_VAULT.read(root, printer, field, ref,
            is_current=lambda: _credential_work_is_active() and root == PLUGIN_STORAGE_DIR
                               and epoch == _PLUGIN_RUNTIME_EPOCH and _credential_card_current(printer))
    if (not _credential_work_is_active() or root != PLUGIN_STORAGE_DIR
            or epoch != _PLUGIN_RUNTIME_EPOCH or not _credential_card_current(printer)):
        raise _pf_credentials.Unavailable()
    return _with_orca_key(hydrated)


def secure_card_credentials(card, typed=None, root=None, epoch=None):
    root = PLUGIN_STORAGE_DIR if root is None else root
    epoch = _PLUGIN_RUNTIME_EPOCH if epoch is None else epoch
    sanitized = dict(card)
    if "credential_refs" in sanitized and not isinstance(sanitized["credential_refs"], dict):
        raise _pf_credentials.Unavailable()
    refs = dict(sanitized.get("credential_refs") or {})
    for field in _pf_credentials.FIELDS:
        value = (card if typed is None else typed).get(field, "")
        if value:
            sanitized.pop(field, None)
        if value and not (field == "apikey" and _orca_key_for(card)):
            refs[field] = _CREDENTIAL_VAULT.write(root, card, field, value,
                is_current=lambda: _credential_work_is_active() and root == PLUGIN_STORAGE_DIR
                                   and epoch == _PLUGIN_RUNTIME_EPOCH)
    if refs:
        sanitized["credential_refs"] = refs
        for field in refs:
            sanitized.pop(field, None)
    for field in _pf_credentials.FIELDS:
        if not sanitized.get(field):
            sanitized.pop(field, None)
    if _orca_key_for(card):
        sanitized.pop("apikey", None)
    return sanitized


_CREDENTIAL_CONFIRM_KEY = os.urandom(32)


def credential_migration_preview():
    with _STORE_LOCK:
        cards = load_manual_printers()
        count = sum(any(card.get(field) for field in _pf_credentials.FIELDS)
                    for card in cards)
        snapshot = json.dumps([PLUGIN_STORAGE_DIR, _PLUGIN_RUNTIME_EPOCH, cards], sort_keys=True)
        token = hmac.new(_CREDENTIAL_CONFIRM_KEY, snapshot.encode(), hashlib.sha256).hexdigest()
        return {"count": count, "confirmation": token}


def migrate_credentials(confirmation):
    with _STORE_LOCK:
        preview = credential_migration_preview()
        if (not isinstance(confirmation, str) or not preview["count"]
                or not hmac.compare_digest(preview["confirmation"], confirmation)):
            return False
        cards = load_manual_printers()
        root, epoch = PLUGIN_STORAGE_DIR, _PLUGIN_RUNTIME_EPOCH
    # The OS may display an unlock dialog. Raw card reads and other metadata
    # operations must remain available while it waits.
    try:
        secured = [secure_card_credentials(card, root=root, epoch=epoch) for card in cards]
    except _pf_credentials.Unavailable:
        return False
    with _STORE_LOCK:
        current = credential_migration_preview()
        if (not _credential_work_is_active() or root != PLUGIN_STORAGE_DIR or epoch != _PLUGIN_RUNTIME_EPOCH
                or not hmac.compare_digest(current["confirmation"], confirmation)):
            return False
        return save_manual_printers(secured)


# API keys of printers adopted from OrcaSlicer presets live in the preset, not
# on disk. They are captured from preset_bundle() on the UI thread (see
# remember_orca_keys) and held in memory; a key already saved in printers.json
# is dropped from the file only once the preset is known to supply one.
_ORCA_KEYS = {}
_ORCA_KEYS_LOCK = threading.Lock()


def remember_orca_keys(discovered):
    with _ORCA_KEYS_LOCK:
        for item in discovered or ():
            ref = item.get("discovery_ref")
            key = item.get("apikey")
            if isinstance(ref, str) and isinstance(key, str) and key:
                _ORCA_KEYS[ref] = key


def _orca_key_for(printer):
    if not isinstance(printer, dict) or printer.get("source") != "orca":
        return ""
    with _ORCA_KEYS_LOCK:
        return _ORCA_KEYS.get(printer.get("discovery_ref")) or ""


def _with_orca_key(printer):
    key = _orca_key_for(printer)
    if key:
        printer["apikey"] = key
    return printer


def migrate_orca_keys():
    """Remove stored copies of preset-supplied keys (worker thread)."""
    with _STORE_LOCK:
        manual = load_manual_printers()
        if any(p.get("apikey") and _orca_key_for(p) for p in manual if isinstance(p, dict)):
            save_manual_printers(manual)


def save_manual_printers(printers):
    # Atomic write: a crash mid-write must not corrupt the store.
    if _STORAGE_DENIED or _PRINTERS_STORE_DAMAGED:
        return False
    printers = [
        {k: v for k, v in p.items() if k != "apikey"} if _orca_key_for(p) else p
        for p in printers
    ]
    printers = [
        {key: value for key, value in card.items()
         if key not in (card.get("credential_refs") or {})}
        for card in printers
    ]
    store_path = PRINTERS_FILE
    tmp = store_path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(printers, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, store_path)
        return True
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


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
# Connections served by a separate adapter module (poll/upload; see _poll_backend
# and send_file_path for the per-adapter call shapes).
# To enable another one: load its helper in the PRINT_FARM_HELPERS block, add one
# entry here, and its _CONNECTION_TYPES row becomes selectable and "ready" in the
# page. Starting prints stays closed for adapters until a start path is confirmed
# (only "duet" has one, see _START_CAPABLE_ADAPTERS).
_ADAPTER_BACKENDS = {"duet": _pf_duet, "prusalink": _pf_prusalink,
                     "crealityprint": _pf_creality, "flashforge": _pf_flashforge,
                     "repetier": _pf_repetier}
_START_CAPABLE_ADAPTERS = frozenset({"duet", "repetier"})
CREALITY_DEFAULT_WS_PORT = 9999  # unconfirmed default; the user may change it
_ACTIVE_BACKENDS = {"moonraker", "octoprint", "bambu"} | set(_ADAPTER_BACKENDS)
# Connection types whose form field is a typed secret (API key or password).
_CREDENTIAL_TYPES = frozenset({"octoprint", "duet", "prusalink", "flashforge", "repetier"})
# Entries of the "Add" form in display order. An entry is selectable exactly
# when backend_is_active() says so; the rest are shown as "coming later".
_CONNECTION_TYPES = (
    ("moonraker", "Moonraker / Klipper"), ("octoprint", "OctoPrint"),
    ("bambu", "Bambu (LAN)"), ("prusalink", "PrusaLink"),
    ("repetier", "Repetier"), ("duet", "Duet (RRF/DSF)"), ("mks", "MKS"),
    ("flashair", "FlashAir"), ("astrobox", "AstroBox"), ("esp3d", "ESP3D"),
    ("crealityprint", "Creality"), ("flashforge", "FlashForge"),
    ("elegoolink", "Elegoo"), ("generic", "Other"),
)


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
    if t in _ADAPTER_BACKENDS:
        return t
    return "unavailable"


def backend_is_active(host_type):
    return backend_for_type(host_type) in _ACTIVE_BACKENDS


def _orca_discovery_ref(preset_name, url, host_type, repetier_slug=""):
    if host_type == "repetier":
        return "orca:" + hashlib.sha256((str(preset_name) + "\0" + normalize_url(url) +
            "\0repetier\0" + str(repetier_slug)).encode("utf-8")).hexdigest()[:24]
    """Stable reference of an OrcaSlicer printer preset (name + address + type)."""
    return "orca:" + hashlib.sha256(
        (str(preset_name) + "\0" + normalize_url(url) + "\0" + str(host_type)).encode("utf-8")
    ).hexdigest()[:24]


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
            normalized_url = normalize_url(host)
            host_type = normalize_host_type(preset.config_value("host_type"))
            repetier_slug = (preset.config_value("printhost_port") or "") if host_type == "repetier" else ""
            discovery_ref = _orca_discovery_ref(preset.name, normalized_url, host_type, repetier_slug)
            found.append({
                "id": "orca:" + preset.name,
                "repetier_slug": repetier_slug,
                "discovery_ref": discovery_ref,
                "name": preset.name,
                "url": normalized_url,
                "type": host_type,
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
        result.append(_with_orca_key(p))
    return result


_PUBLIC_PRINTER_FIELDS = frozenset({
    "id", "target_uuid", "owner_scope_uuid", "server_uuid", "target_selector",
    "binding_revision", "name", "url", "type", "serial", "source", "discovery_ref", "repetier_slug",
    "status", "queued", "outbox_count", "outbox_bytes", "webui_url", "camera_mode",
})
_PUBLIC_STATUS_FIELDS = frozenset({
    "supported", "online", "state", "backend", "job_state", "error",
    "thumbnail", "filename", "nozzle", "target_nozzle", "bed",
    "target_bed", "progress", "transport_online", "device_health",
    "device_state", "authorization", "freshness", "observed_at",
    "error_code", "upload_ready", "can_control", "repetier_slug", "repetier_url",
    "tools", "active_tool", "protocol", "rr_emulation", "can_start",
    "upload_reason", "capabilities",
    "model", "model_family", "firmware", "identity_verified", "raw_state",
    "control_reason",
    "remaining_minutes", "remaining_seconds", "layer", "total_layers", "ams", "print_error",
})
_PUBLIC_TOOL_FIELDS = frozenset({"index", "name", "current", "target", "state", "active"})
_MAX_PUBLIC_TOOLS = 16
_PUBLIC_QUEUED_FIELDS = frozenset({"file", "name", "size", "ts"})


def private_credential(value):
    """Preserve credential bytes and reject values outside supported bounds."""
    if value is None or value == "":
        return ""
    if not isinstance(value, str):
        return None
    try:
        if len(value.encode("utf-8")) > 512:
            return None
    except UnicodeEncodeError:
        return None
    if any(unicodedata.category(char) == "Cc" for char in value):
        return None
    return value


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _urlopen_no_redirect(request, **kwargs):
    """Refuse every redirect so origin-bound credentials never migrate."""
    context = kwargs.pop("context", None)
    handlers = [_RejectRedirects()]
    if context is not None:
        handlers.append(urllib.request.HTTPSHandler(context=context))
    return urllib.request.build_opener(*handlers).open(request, **kwargs)


def _public_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if value == value and value not in (float("inf"), float("-inf")) else None


def _public_tools(tools):
    """Device-supplied tool rows reduced to plain, bounded display values."""
    result = []
    for tool in tools if isinstance(tools, list) else []:
        if not isinstance(tool, dict) or len(result) >= _MAX_PUBLIC_TOOLS:
            continue
        index = tool.get("index")
        if isinstance(index, bool) or not isinstance(index, int):
            continue
        name = tool.get("name")
        state = tool.get("state")
        result.append({
            "index": index,
            "name": name[:40] if isinstance(name, str) else "",
            "current": _public_number(tool.get("current")),
            "target": _public_number(tool.get("target")),
            "state": state[:16] if isinstance(state, str) else None,
            "active": tool.get("active") is True,
        })
    return result


def public_status(status):
    """Whitelisted, display-safe projection of a polled status."""
    result = {key: value for key, value in (status or {}).items()
              if key in _PUBLIC_STATUS_FIELDS}
    if "tools" in result:
        result["tools"] = _public_tools(result["tools"])
    if "capabilities" in result:
        caps = result["capabilities"]
        result["capabilities"] = {
            str(key)[:24]: value[:48] for key, value in caps.items()
            if isinstance(value, str)
        } if isinstance(caps, dict) else {}
    for key in ("protocol", "upload_reason", "model", "model_family", "firmware",
                "raw_state", "control_reason"):
        if key in result:
            result[key] = result[key][:64] if isinstance(result[key], str) else None
    if "identity_verified" in result:
        result["identity_verified"] = result["identity_verified"] is True
    if any(key in result for key in _BAMBU_STAT_KEYS):
        result.update(_public_bambu_stats(result))
    if "remaining_seconds" in result:
        seconds = _remaining_seconds(result["remaining_seconds"])
        result["remaining_seconds"] = seconds
        if seconds is not None and result.get("remaining_minutes") is None:
            result["remaining_minutes"] = math.ceil(seconds / 60)
    return result


def public_printer_record(printer):
    """Project a stored/discovered printer onto the dashboard DTO.

    Credentials remain in Python/private plugin storage.  A local plugin page
    may submit a newly typed secret once, but Python never reflects saved
    secrets or arbitrary legacy fields back into JavaScript.
    """
    record = printer if isinstance(printer, dict) else {}
    public = {
        key: value for key, value in record.items()
        if key in _PUBLIC_PRINTER_FIELDS and key not in {"status", "queued"}
    }
    status = record.get("status")
    if isinstance(status, dict):
        public["status"] = public_status(status)
    queued = record.get("queued")
    if isinstance(queued, list):
        public["queued"] = [
            {key: value for key, value in item.items() if key in _PUBLIC_QUEUED_FIELDS}
            for item in queued
            if isinstance(item, dict)
        ]
    return public


def available_orca_printers(discovered=None):
    # OrcaSlicer network presets (Moonraker/OctoPrint) not yet on the dashboard.
    # Bambu comes via SSDP now — the audit blocks OrcaSlicer.conf.
    manual = load_manual_printers()
    added_urls = {normalize_url(p.get("url")) for p in manual}
    added_serials = {p.get("serial") for p in manual if p.get("serial")}
    added_refs = {
        p.get("discovery_ref") for p in manual
        if p.get("source") == "orca" and p.get("discovery_ref")
    }
    out = []
    for p in discovered if discovered is not None else discover_orca_printers():
        if p.get("source") == "orca":
            # A server address can represent several distinct printer presets.
            # An edited preset gets a new reference and needs explicit adoption.
            if p.get("discovery_ref") in added_refs:
                continue
        else:
            if p.get("url") in added_urls:
                continue
            if p.get("serial") and p["serial"] in added_serials:
                continue
        out.append(public_printer_record(p))
    return out


# --------------------------------------------------------------------------- #
# HTTP helpers (stdlib only).
# --------------------------------------------------------------------------- #
def _get_json(url, ctx, headers=None, timeout=HTTP_TIMEOUT):
    req = urllib.request.Request(url, headers=headers or {"Accept": "application/json"}, method="GET")
    with _urlopen_no_redirect(req, timeout=timeout, context=ctx) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _get_bounded_json(url, ctx, headers=None, timeout=HTTP_TIMEOUT, max_bytes=256 * 1024):
    req = urllib.request.Request(url, headers=headers or {"Accept": "application/json"}, method="GET")
    with _urlopen_no_redirect(req, timeout=timeout, context=ctx) as response:
        body = response.read(max_bytes + 1)
    if len(body) > max_bytes:
        raise ValueError("JSON response exceeds limit")
    return json.loads(body.decode("utf-8"))


def _get_bytes(url, ctx, headers=None, timeout=HTTP_TIMEOUT):
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    with _urlopen_no_redirect(req, timeout=timeout, context=ctx) as resp:
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
    with _urlopen_no_redirect(req, timeout=UPLOAD_TIMEOUT, context=ctx) as resp:
        return resp.getcode()


# Per-backend status polling. Common shape:
# {online, state, nozzle, target_nozzle, bed, target_bed, progress, filename, thumbnail}
# --------------------------------------------------------------------------- #
# Thumbnails are cached by (base, filename): they don't change during a print,
# so we fetch a print's thumbnail once instead of on every status poll.
_thumb_cache = {}
_THUMB_RETRY_SECONDS = 60


def _thumb_lookup(key):
    """Cached thumbnail, or None when it should be fetched (again)."""
    entry = _thumb_cache.get(key)
    if entry is None:
        return None
    value, retry_at = entry
    if value or time.monotonic() < retry_at:
        return value
    return None  # an empty result is only remembered briefly: a failed or not-yet-ready read


def _thumb_store(key, value):
    _thumb_cache[key] = (value, time.monotonic() + _THUMB_RETRY_SECONDS)
    if len(_thumb_cache) > 64:  # bound the cache
        _thumb_cache.pop(next(iter(_thumb_cache)))


def moonraker_thumbnail(base, ctx, filename, headers=None):
    if not filename:
        return ""
    # An authenticated thumbnail must not be reused for another account.
    key = (base, filename, hashlib.sha256(
        (headers or {}).get("X-Api-Key", "").encode("utf-8")
    ).hexdigest())
    cached = _thumb_lookup(key)
    if cached is not None:
        return cached
    result = ""
    try:
        meta = _get_json(base + "/server/files/metadata?filename=" + urllib.parse.quote(filename), ctx, headers)
        thumbs = meta.get("result", {}).get("thumbnails", [])
        if thumbs:
            best = max(thumbs, key=lambda t: t.get("size", 0))
            rel = best.get("relative_path") or best.get("thumbnail_path")
            if rel:
                # Moonraker gives relative_path relative to the G-code's own directory.
                folder = posixpath.dirname(filename.replace("\\", "/"))
                full = posixpath.join(folder, rel) if folder else rel
                raw = _get_bytes(base + "/server/files/gcodes/" + urllib.parse.quote(full), ctx, headers)
                result = "data:image/png;base64," + base64.b64encode(raw).decode("ascii")
    except Exception:
        _thumb_store(key, "")
        return ""
    _thumb_store(key, result)
    return result


_MAX_REMAINING_SECONDS = 30 * 86400
_MAX_JOB_LAYERS = 100000


def _remaining_seconds(value):
    """Whole seconds from a finite non-negative JSON number within a sane ceiling."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value or not 0 <= value <= _MAX_REMAINING_SECONDS:
        return None
    return int(round(value))


def _job_layers(layer, total):
    """(layer, total) as plain ints, or (None, None) when absent or inconsistent."""
    def whole(value):
        if isinstance(value, bool):
            return None
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        return value if isinstance(value, int) else None
    layer, total = whole(layer), whole(total)
    if total is None or layer is None or not 1 <= total <= _MAX_JOB_LAYERS or not 0 <= layer <= total:
        return None, None
    return layer, total


def _moonraker_job_stats(state, print_stats, sdcard):
    """Remaining seconds and layers for an active job; read-only, from queried objects."""
    if state not in ("printing", "paused"):
        return None, None, None
    remaining = None
    progress = sdcard.get("progress") if isinstance(sdcard, dict) else None
    duration = print_stats.get("print_duration")
    if (isinstance(progress, (int, float)) and not isinstance(progress, bool)
            and isinstance(duration, (int, float)) and not isinstance(duration, bool)
            and duration >= 0 and 0.01 <= progress <= 1):
        remaining = _remaining_seconds(duration / progress - duration)
    info = print_stats.get("info")
    info = info if isinstance(info, dict) else {}
    layer, total = _job_layers(info.get("current_layer"), info.get("total_layer"))
    return remaining, layer, total


def _moonraker_extruders(base, ctx, headers):
    """Discover only Klipper extruder objects, with a bounded query inventory."""
    try:
        data = _get_bounded_json(base + "/printer/objects/list", ctx, headers)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return ["extruder"]  # Older servers without object discovery.
        raise
    result = data.get("result") if isinstance(data, dict) else None
    objects = result.get("objects") if isinstance(result, dict) else None
    if not isinstance(objects, list) or len(objects) > 4096:
        raise ValueError("invalid Moonraker object list")
    names = set()
    for name in objects:
        if not isinstance(name, str):
            raise ValueError("invalid Moonraker object name")
        if (len(name) <= 32 and re.fullmatch(r"extruder(?:[1-9][0-9]*)?", name)
                and int(name[8:] or "0") < 2**53):
            names.add(name)
    return sorted(names, key=lambda name: int(name[8:] or "0"))[:_MAX_PUBLIC_TOOLS]


def poll_moonraker(base, ctx, headers=None):
    names = _moonraker_extruders(base, ctx, headers)
    objects = names + ["heater_bed", "print_stats", "display_status", "virtual_sdcard", "toolhead"]
    query = "/printer/objects/query?" + "&".join(objects)
    data = _get_json(base + query, ctx, headers)
    result = data.get("result") if isinstance(data, dict) else None
    status = result.get("status") if isinstance(result, dict) else None
    if not isinstance(status, dict):
        raise ValueError("invalid Moonraker object status")
    toolhead = status.get("toolhead")
    active_name = toolhead.get("extruder") if isinstance(toolhead, dict) else None
    active_name = active_name if active_name in names else None
    tools = []
    for name in names:
        value = status.get(name)
        value = value if isinstance(value, dict) else {}
        tools.append({"index": int(name[8:] or "0"), "name": name,
                      "current": _public_number(value.get("temperature")),
                      "target": _public_number(value.get("target")),
                      "state": None, "active": name == active_name})
    active_tool = next((tool for tool in tools if tool["active"]), None)
    nozzle = active_tool or (tools[0] if len(tools) == 1 else {})
    bed = status.get("heater_bed")
    bed = bed if isinstance(bed, dict) else {}
    print_stats = status.get("print_stats")
    display = status.get("display_status")
    display = display if isinstance(display, dict) else {}
    print_stats = print_stats if isinstance(print_stats, dict) else {}
    state = print_stats.get("state")
    state = state if isinstance(state, str) and state else "unknown"
    remaining_seconds, layer, total_layers = _moonraker_job_stats(
        state, print_stats, status.get("virtual_sdcard"))
    filename = print_stats.get("filename", "")
    raw_progress = display.get("progress")
    progress = (round(raw_progress * 100)
                if isinstance(raw_progress, (int, float)) and not isinstance(raw_progress, bool)
                else None)
    return {
        "online": True,
        "state": state,
        "job_state": print_stats.get("state") if isinstance(print_stats, dict) else None,
        "nozzle": nozzle.get("current"),
        "target_nozzle": nozzle.get("target"),
        "tools": tools,
        "active_tool": active_tool["index"] if active_tool else None,
        "bed": _public_number(bed.get("temperature")),
        "target_bed": _public_number(bed.get("target")),
        "progress": progress,
        "filename": filename,
        "elapsed_seconds": print_stats.get("total_duration") or print_stats.get("print_duration"),
        "remaining_seconds": remaining_seconds,
        "layer": layer,
        "total_layers": total_layers,
        # Show the loaded file's thumbnail whenever one is loaded (printing,
        # paused or just finished) — not only while actively printing.
        "thumbnail": moonraker_thumbnail(base, ctx, filename, headers) if filename else "",
    }


def octoprint_thumbnail(base, headers, ctx, filepath):
    # OctoPrint's thumbnail plugins (PrusaSlicer/UFP) attach a "thumbnail" URL to
    # the file resource; fetch it once and inline it. Absent if no such plugin.
    if not filepath:
        return ""
    key = base + "|" + filepath
    cached = _thumb_lookup(key)
    if cached is not None:
        return cached
    result = ""
    try:
        meta = _get_json(base + "/api/files/local/" + urllib.parse.quote(filepath), ctx, headers=headers)
        rel = meta.get("thumbnail")
        if rel:
            raw = _get_bytes(base + "/" + rel.lstrip("/"), ctx, headers=headers)
            result = "data:image/png;base64," + base64.b64encode(raw).decode("ascii")
    except Exception:
        _thumb_store(key, "")
        return ""
    _thumb_store(key, result)
    return result


def poll_octoprint(base, ctx, apikey):
    headers = {"Accept": "application/json"}
    if apikey:
        headers["X-Api-Key"] = apikey
    try:
        printer = _get_json(base + "/api/printer", ctx, headers=headers)
        state = printer.get("state", {}).get("text") or "unknown"
    except urllib.error.HTTPError as exc:
        # OctoPrint exposes the server and file API while its serial printer is
        # disconnected, but the full printer-state endpoint is unavailable in
        # that state (409 by contract; some configurations answer 404).  The
        # connection endpoint remains authoritative for the live state.
        if exc.code not in {404, 409}:
            raise
        connection = _get_json(base + "/api/connection", ctx, headers=headers)
        printer = {}
        state = connection.get("current", {}).get("state") or "Disconnected"
    temps = printer.get("temperature", {})
    tool0 = temps.get("tool0", {})
    bed = temps.get("bed", {})
    progress, elapsed_seconds, filename, filepath = None, None, "", ""
    job_state = None
    remaining_seconds = None
    try:
        job = _get_json(base + "/api/job", ctx, headers=headers)
        if not isinstance(job, dict) or not isinstance(job.get("state"), str):
            raise ValueError("Incomplete OctoPrint job state")
        job_state = job["state"]
        raw_progress = job.get("progress", {}).get("completion")
        progress = (round(raw_progress)
                    if isinstance(raw_progress, (int, float)) and not isinstance(raw_progress, bool)
                    else None)
        elapsed_seconds = job.get("progress", {}).get("printTime")
        if job_state.lower() in ("printing", "paused", "pausing"):
            remaining_seconds = _remaining_seconds(job.get("progress", {}).get("printTimeLeft"))
        job_file = job.get("job", {}).get("file", {})
        filename = job_file.get("name", "") or ""
        filepath = job_file.get("path", "") or filename
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise
    except Exception:
        pass
    return {
        "online": True, "state": state,
        "job_state": job_state,
        "nozzle": tool0.get("actual"), "target_nozzle": tool0.get("target"),
        "bed": bed.get("actual"), "target_bed": bed.get("target"),
        "progress": progress, "filename": filename,
        "elapsed_seconds": elapsed_seconds,
        "remaining_seconds": remaining_seconds,
        "layer": None, "total_layers": None,
        "thumbnail": octoprint_thumbnail(base, headers, ctx, filepath) if filepath else "",
    }


def poll_generic(base, ctx):
    req = urllib.request.Request(base, method="GET")
    with _urlopen_no_redirect(req, timeout=HTTP_TIMEOUT, context=ctx):
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


_BAMBU_STAT_KEYS = ("remaining_minutes", "layer", "total_layers", "ams", "print_error")
_BAMBU_MAX_MINUTES = 100000
_BAMBU_MAX_LAYERS = 100000
_BAMBU_MAX_TRAYS = 16
_BAMBU_TYPE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 +\-]{0,15}")
_BAMBU_COLOR_RE = re.compile(r"#[0-9A-Fa-f]{6}")
_BAMBU_ERROR_RE = re.compile(r"[0-9A-F]{8}")


def _bambu_int(value, low, high):
    """Integer from a JSON number or digit string; anything else is None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, str) and len(value) <= 12 and value.strip().isdigit():
        value = int(value.strip())
    if not isinstance(value, int):
        return None
    return value if low <= value <= high else None


def _bambu_tray(raw):
    """One AMS tray as {type, color, remain}, or None for an empty/invalid slot."""
    if not isinstance(raw, dict) or "tray_slot_placeholder" in raw:
        return None
    kind = raw.get("tray_type")
    kind = kind.strip() if isinstance(kind, str) else ""
    if not _BAMBU_TYPE_RE.fullmatch(kind):
        return None
    color = raw.get("tray_color")
    # Bambu sends RRGGBBAA; alpha 00 (or "00000000") marks an unknown colour.
    if (isinstance(color, str) and len(color) in (6, 8) and color[:8] != "00000000"
            and not (len(color) == 8 and color[6:] == "00")
            and _BAMBU_COLOR_RE.fullmatch("#" + color[:6])):
        color = "#" + color[:6].upper()
    else:
        color = None
    return {"type": kind, "color": color, "remain": _bambu_int(raw.get("remain"), 0, 100)}


def _bambu_stats(info, state):
    """Statistics Bambu already puts in its report. Absent or invalid -> None."""
    stats = dict.fromkeys(_BAMBU_STAT_KEYS)
    if not isinstance(info, dict):
        return stats
    if state in ("printing", "paused"):
        stats["remaining_minutes"] = _bambu_int(info.get("mc_remaining_time"), 0, _BAMBU_MAX_MINUTES)
        total = _bambu_int(info.get("total_layer_num"), 1, _BAMBU_MAX_LAYERS)
        layer = _bambu_int(info.get("layer_num"), 0, _BAMBU_MAX_LAYERS)
        if total is not None and layer is not None and layer <= total:
            stats["layer"], stats["total_layers"] = layer, total
    trays = []
    ams = info.get("ams")
    units = ams.get("ams") if isinstance(ams, dict) else None
    for unit in (units[:8] if isinstance(units, list) else []):
        raw_trays = unit.get("tray") if isinstance(unit, dict) else None
        for raw in (raw_trays[:4] if isinstance(raw_trays, list) else []):
            tray = _bambu_tray(raw)
            if tray and len(trays) < _BAMBU_MAX_TRAYS:
                trays.append(tray)
    stats["ams"] = trays or None
    code = _bambu_int(info.get("print_error"), 1, 0xFFFFFFFF)
    stats["print_error"] = "%08X" % code if code else None
    return stats


def _public_bambu_stats(result):
    """Re-validate the statistics fields of a status before it reaches the page."""
    minutes = _bambu_int(result.get("remaining_minutes"), 0, _BAMBU_MAX_MINUTES)
    total = _bambu_int(result.get("total_layers"), 1, _BAMBU_MAX_LAYERS)
    layer = _bambu_int(result.get("layer"), 0, _BAMBU_MAX_LAYERS)
    if total is None or layer is None or layer > total:
        layer = total = None
    trays = []
    raw = result.get("ams")
    for tray in (raw if isinstance(raw, list) else [])[:_BAMBU_MAX_TRAYS]:
        if not isinstance(tray, dict):
            continue
        kind, color = tray.get("type"), tray.get("color")
        if not isinstance(kind, str) or not _BAMBU_TYPE_RE.fullmatch(kind):
            continue
        trays.append({
            "type": kind,
            "color": color.upper() if isinstance(color, str) and _BAMBU_COLOR_RE.fullmatch(color) else None,
            "remain": _bambu_int(tray.get("remain"), 0, 100),
        })
    code = result.get("print_error")
    return {"remaining_minutes": minutes, "layer": layer, "total_layers": total,
            "ams": trays or None,
            "print_error": code if isinstance(code, str) and _BAMBU_ERROR_RE.fullmatch(code)
            and code != "00000000" else None}


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


def _bambu_offline(error):
    text = str(error or "").casefold() if not isinstance(error, BaseException) else ""
    if isinstance(error, BambuTlsError):
        code = error.code
    elif "no address" in text:
        code = "missing_address"
    elif "needs access code" in text:
        code = "missing_credentials"
    elif "authentication" in text or "connect rejected" in text:
        code = "authorization_denied"
    elif isinstance(error, BaseException):
        code, _message = safe_transport_error(error)
    else:
        code = "request_failed"
    return {"online": False, "state": "offline", "error": ui_text(
                _BAMBU_TLS_ERRORS.get(code, "genericError")),
            "error_code": code, "authorization": "rejected" if code == "authorization_denied" else "unknown",
            "nozzle": None, "target_nozzle": None,
            "bed": None, "target_bed": None, "progress": None, "filename": "", "thumbnail": ""}


# The only vendor-specific table in the plugin: a Bambu preset carries no
# printer address, so a slice is matched to a Bambu card by its model code
# (printer_model -> model_id, as in OrcaSlicer's BBL machine_model profiles).
# Every other brand is matched by address/serial.
_BAMBU_MODEL_IDS = {
    "Bambu Lab X1 Carbon": "BL-P001",
    "Bambu Lab X1": "BL-P002",
    "Bambu Lab P1P": "C11",
    "Bambu Lab P1S": "C12",
    "Bambu Lab X1E": "C13",
    "Bambu Lab A1 mini": "N1",
    "Bambu Lab A1": "N2S",
    "Bambu Lab P2S": "N7",
    "Bambu Lab X2D": "N6",
    "Bambu Lab A2L": "N9",
    "Bambu Lab H2D": "O1D",
    "Bambu Lab H2D Pro": "O1E",
    "Bambu Lab H2S": "O1S",
    "Bambu Lab H2C": "O1C2",
}
_BAMBU_CODE_ALIASES = {"3dprinter-x1-carbon": "BL-P001", "3dprinter-x1": "BL-P002"}
_MODEL_CODE_RE = re.compile(r"^[A-Za-z0-9-]{1,32}$")


def _normalize_model_code(value):
    """Printer-reported model code (SSDP devmodel / MQTT printer_type) or ""."""
    if not isinstance(value, str):
        return ""
    value = value.strip()
    alias = _BAMBU_CODE_ALIASES.get(value.casefold())
    if alias:
        return alias
    return value.upper() if _MODEL_CODE_RE.match(value) else ""


def _slice_model_code(printer_model):
    return _BAMBU_MODEL_IDS.get(str(printer_model or "").strip(), "")


def _card_model_code(printer):
    """Use only a code observed over this card's configured MQTT connection."""
    serial = str(printer.get("serial") or "").strip().casefold()
    if (not serial or printer.get("model_code_source") != "mqtt"
            or str(printer.get("model_code_serial") or "").strip().casefold() != serial
            or printer.get("model_code_endpoint") != _endpoint_key(printer.get("url"))
            or printer.get("model_code_binding_revision") != printer.get("binding_revision")):
        return ""
    return _normalize_model_code(printer.get("model_code"))


def _verified_bambu_serial(printer):
    serial = str(printer.get("serial") or "").strip().casefold()
    return (bool(serial) and printer.get("serial_source") in ("mqtt", "user")
            and str(printer.get("serial_value") or "").strip().casefold() == serial
            and printer.get("serial_endpoint") == _endpoint_key(printer.get("url"))
            and printer.get("serial_binding_revision") == printer.get("binding_revision"))


def _copy_bambu_observations(source, target):
    for key in ("serial", "_seen_verified_serial", "_seen_model_code"):
        if key in source:
            target[key] = source[key]


def poll_bambu(printer, timeout=8):
    try:
        hydrated = hydrate_credentials(printer)
    except _pf_credentials.Unavailable:
        result = _bambu_offline(ui_text("credentialUnavailable"))
        result["error_code"] = "credential_unavailable"
        return result
    with _bambu_target_lock(printer):
        result = _poll_bambu_connection(hydrated, timeout)
        _copy_bambu_observations(hydrated, printer)
        return result


def _poll_bambu_connection(printer, timeout):
    host = urllib.parse.urlparse(normalize_url(printer.get("url"))).hostname or ""
    serial = (printer.get("serial") or "").strip()
    code = printer.get("access_code") or printer.get("apikey") or ""
    if not host:
        return _bambu_offline("no address")
    if not code:
        return _bambu_offline("needs access code")
    try:
        _bambu_pin(printer, "mqtt")
    except BambuTlsError as exc:
        return _bambu_offline(exc)

    deadline = time.monotonic() + timeout
    try:
        raw = socket.create_connection((host, BAMBU_MQTT_PORT), timeout=timeout)
    except Exception as exc:
        return _bambu_offline(exc)
    sock = None
    trusted = False
    try:
        # Bambu LAN uses self-signed certificates; ssl_ctx_for keeps its
        # existing compatibility policy for this printer type.
        sock = ssl_ctx_for(printer).wrap_socket(raw, server_hostname=host)
        _verify_bambu_socket(printer, "mqtt", sock)
        trusted = True
        # CONNECT: protocol "MQTT" v4, flags user+pass+clean-session, 60s keepalive.
        var = _mqtt_field(b"MQTT") + bytes([4, 0xC2]) + struct.pack("!H", 60)
        payload = _mqtt_field(b"orca-printers") + _mqtt_field(b"bblp") + _mqtt_field(code.encode("utf-8"))
        body = var + payload
        sock.sendall(b"\x10" + _mqtt_len(len(body)) + body)
        header, connack = _mqtt_read_packet(sock, deadline)
        if (header & 0xF0) != 0x20 or len(connack) < 2:
            return _bambu_offline(ValueError("invalid MQTT CONNACK"))
        if connack[1] != 0:
            return _bambu_offline("authentication rejected")

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
            if 2 + topic_len > len(packet):
                continue
            try:
                parts = packet[2:2 + topic_len].decode("utf-8").split("/")
            except UnicodeError:
                continue
            if (len(parts) != 3 or parts[0] != "device" or not parts[1]
                    or parts[2] != "report"):
                continue
            if serial and parts[1].casefold() != serial.casefold():
                continue
            try:
                msg = json.loads(packet[2 + topic_len:].decode("utf-8", "replace"))
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue
            info = msg.get("print")
            if isinstance(info, dict):
                if not serial:
                    serial = parts[1]
                    printer["serial"] = serial  # caller persists the discovery
                    send_pushall(serial)
                printer["_seen_verified_serial"] = serial
                seen = _normalize_model_code(info.get("printer_type"))
                if seen:
                    printer["_seen_model_code"] = seen  # caller persists it
            if not isinstance(info, dict) or ("nozzle_temper" not in info and "gcode_state" not in info):
                continue
            gstate = info.get("gcode_state", "")
            pct = info.get("mc_percent")
            job_state = _BAMBU_STATES.get(gstate, (gstate or "unknown").lower())
            return {
                "online": True,
                "state": job_state,
                "job_state": job_state,
                **_bambu_stats(info, job_state),
                "nozzle": info.get("nozzle_temper"), "target_nozzle": info.get("nozzle_target_temper"),
                "bed": info.get("bed_temper"), "target_bed": info.get("bed_target_temper"),
                "progress": pct if pct is not None else None,
                "filename": info.get("subtask_name") or info.get("gcode_file") or "",
                "thumbnail": "",
            }
        return {"online": True, "state": "connected", "nozzle": None, "target_nozzle": None,
                "bed": None, "target_bed": None, "progress": None, "filename": "", "thumbnail": ""}
    except Exception as exc:
        return _bambu_offline(exc)
    finally:
        if trusted and sock is not None:
            try:
                sock.sendall(b"\xe0\x00")  # DISCONNECT
            except Exception:
                pass
        try:
            (sock or raw).close()
        except Exception:
            pass


_STORE_LOCK = threading.RLock()


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
            if (entry.get("target_uuid") == printer.get("target_uuid")
                    and entry.get("owner_scope_uuid") == printer.get("owner_scope_uuid")
                    and entry.get("target_selector") == printer.get("target_selector")
                    and entry.get("binding_revision") == printer.get("binding_revision")
                    and _endpoint_key(entry.get("url")) == _endpoint_key(printer.get("url"))
                    and backend_for_type(entry.get("type")) == "bambu"
                    and not (entry.get("serial") or "").strip()):
                entry["serial"] = serial
                owner_scope = entry["owner_scope_uuid"]
                entry["server_uuid"] = str(uuid.uuid5(
                    uuid.UUID(owner_scope), "server:" + serial.casefold()
                ))
                entry["binding_revision"] = entry.get("binding_revision", 1) + 1
                entry["serial_source"] = "mqtt"
                entry["serial_value"] = serial
                entry["serial_endpoint"] = _endpoint_key(entry.get("url"))
                entry["serial_binding_revision"] = entry["binding_revision"]
                changed = True
        if changed:
            save_manual_printers(manual)


def persist_model_code(printer, code):
    """Store a model observed over this exact Bambu card's MQTT connection.
    A different code for the same serial removes the stored one instead of
    overwriting it (the sources disagree, so nothing is trusted)."""
    serial = str(printer.get("serial") or "").strip()
    code = _normalize_model_code(code)
    if not serial or not code:
        return False
    with _STORE_LOCK:
        manual = load_manual_printers()
        changed = False
        for entry in manual:
            if (entry.get("target_uuid") != printer.get("target_uuid")
                    or entry.get("owner_scope_uuid") != printer.get("owner_scope_uuid")
                    or entry.get("binding_revision") != printer.get("binding_revision")
                    or _endpoint_key(entry.get("url")) != _endpoint_key(printer.get("url"))
                    or str(entry.get("serial") or "").strip().casefold() != serial.casefold()
                    or backend_for_type(entry.get("type")) != "bambu"):
                continue
            current = _card_model_code(entry)
            if current == code:
                continue
            if current:
                entry.pop("model_code", None)
                entry.pop("model_code_serial", None)
                entry.pop("model_code_source", None)
                entry.pop("model_code_endpoint", None)
                entry.pop("model_code_binding_revision", None)
            else:
                entry["model_code"] = code
                entry["model_code_serial"] = serial
                entry["model_code_source"] = "mqtt"
                entry["model_code_endpoint"] = _endpoint_key(entry.get("url"))
                entry["model_code_binding_revision"] = entry.get("binding_revision")
            changed = True
        if changed:
            save_manual_printers(manual)
    return changed


def persist_verified_bambu_serial(printer):
    """Mark a configured serial observed through this card's MQTT session."""
    if not printer.get("serial") or _verified_bambu_serial(printer):
        return False
    with _STORE_LOCK:
        manual = load_manual_printers()
        changed = False
        for entry in manual:
            if (entry.get("target_uuid") == printer.get("target_uuid")
                    and entry.get("owner_scope_uuid") == printer.get("owner_scope_uuid")
                    and entry.get("binding_revision") == printer.get("binding_revision")
                    and _endpoint_key(entry.get("url")) == _endpoint_key(printer.get("url"))
                    and str(entry.get("serial") or "").casefold()
                    == str(printer.get("serial") or "").casefold()
                    and backend_for_type(entry.get("type")) == "bambu"):
                entry["serial_source"] = "mqtt"
                entry["serial_value"] = entry.get("serial")
                entry["serial_endpoint"] = _endpoint_key(entry.get("url"))
                entry["serial_binding_revision"] = entry.get("binding_revision")
                changed = True
        if changed:
            save_manual_printers(manual)
    return changed


# Bambu Lab (LAN mode): sending a print. Upload goes over implicit FTPS on 990
# (same bblp / access-code credentials as MQTT); the print is then started with
# an MQTT command. Plain .gcode uses the firmware's gcode_file command; sliced
# .3mf projects use project_file, which is what Bambu's own clients send.
# --------------------------------------------------------------------------- #
_IMPLICIT_FTPS_CLASS = None
_IMPLICIT_FTPS_LOCK = threading.Lock()


def _implicit_ftps_class():
    """The implicit-TLS FTP client class; ftplib is loaded on first upload only."""
    global _IMPLICIT_FTPS_CLASS
    with _IMPLICIT_FTPS_LOCK:
        if _IMPLICIT_FTPS_CLASS is None:
            import ftplib

            class ImplicitFTPS(ftplib.FTP_TLS):
                # Bambu's FTP server is implicit TLS (the socket is TLS from byte
                # one), which ftplib doesn't speak natively: wrap the control
                # socket before the welcome.
                def connect(self, host="", port=990, timeout=UPLOAD_TIMEOUT):
                    self.host = host or self.host
                    self.port = port or self.port
                    self.timeout = timeout
                    raw = socket.create_connection((self.host, self.port), self.timeout)
                    try:
                        self.sock = self.context.wrap_socket(raw, server_hostname=self.host)
                        _verify_bambu_socket(self._bambu_printer, "ftps", self.sock)
                    except Exception:
                        (self.sock if getattr(self, "sock", None) is not None else raw).close()
                        raise
                    self.af = self.sock.family
                    self.file = self.sock.makefile("r", encoding=self.encoding)
                    self.welcome = self.getresp()
                    return self.welcome

                def makepasv(self):
                    _address, port = super().makepasv()
                    # The control peer, not a PASV-advertised address, owns the
                    # pinned TLS identity for this transfer.
                    return self.sock.getpeername()[0], port

                def ntransfercmd(self, cmd, rest=None):
                    # Bambu's vsftpd requires the data connection to reuse the
                    # control channel's TLS session. FTP_TLS wraps a new session
                    # by default.
                    conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
                    if self._prot_p:
                        kwargs = {"server_hostname": self.host}
                        session = getattr(self.sock, "session", None)
                        if session is not None:
                            kwargs["session"] = session
                        try:
                            conn = self.context.wrap_socket(conn, **kwargs)
                            _verify_bambu_socket(self._bambu_printer, "ftps", conn)
                        except Exception:
                            conn.close()
                            raise
                    return conn, size

            _IMPLICIT_FTPS_CLASS = ImplicitFTPS
    return _IMPLICIT_FTPS_CLASS


def _bambu_publish(host, code, topic, payload, timeout=10, ssl_context=None,
                   printer=None):
    """Connect to the printer's MQTT broker, publish one message, disconnect.
    Returns "" on success or an error string."""
    if printer is None:
        return "tls_trust_stale"
    with _bambu_target_lock(printer):
        return _bambu_publish_locked(host, code, topic, payload, timeout, printer)


def _bambu_publish_locked(host, code, topic, payload, timeout, printer):
    try:
        printer = hydrate_credentials(printer)
    except _pf_credentials.Unavailable:
        return "credential_unavailable"
    if printer.get("credential_refs"):
        code = printer.get("access_code") or printer.get("apikey") or ""
    try:
        _bambu_pin(printer, "mqtt")
    except BambuTlsError as exc:
        return exc.code
    if urllib.parse.urlparse(normalize_url(printer.get("url"))).hostname != host:
        return "tls_trust_stale"
    deadline = time.monotonic() + timeout
    try:
        raw = socket.create_connection((host, BAMBU_MQTT_PORT), timeout=timeout)
    except Exception as exc:
        return str(exc)
    sock = None
    trusted = False
    try:
        sock = ssl_ctx_for(printer).wrap_socket(raw, server_hostname=host)
        _verify_bambu_socket(printer, "mqtt", sock)
        trusted = True
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
    except BambuTlsError as exc:
        return exc.code
    except Exception as exc:
        return str(exc)
    finally:
        if trusted and sock is not None:
            try:
                sock.sendall(b"\xe0\x00")  # DISCONNECT
            except Exception:
                pass
        try:
            (sock or raw).close()
        except Exception:
            pass


def bambu_start_print(host, code, serial, remote_path, use_ams, plate_entry=None,
                      ssl_context=None, printer=None):
    filename = remote_path.rsplit("/", 1)[-1]
    if remote_path.lower().endswith(".3mf"):
        if not isinstance(plate_entry, str) or not re.fullmatch(
                r"Metadata/plate_[1-9][0-9]*\.gcode", plate_entry):
            return "invalid selected plate"
        payload = {"print": {
            "sequence_id": "0", "command": "project_file",
            "param": plate_entry,
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
    return _bambu_publish(host, code, "device/%s/request" % serial, payload,
                          printer=printer)


# Auto-naming: nobody should stare at bare IPs. Bambu printers announce
# themselves over SSDP (UDP 2021) with a DevName header; Moonraker exposes the
# Klipper host name. Placeholder names get replaced once and persisted.
# --------------------------------------------------------------------------- #
def ssdp_bambu_names(duration=3.0):
    """Passively collect Bambu SSDP announcements: ip -> {name, serial, model_code}.
    Printers NOTIFY every few seconds, so a short listen window is enough."""
    names = {}
    duration = max(0.0, min(float(duration), 3.0))
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
        received = 0
        while time.monotonic() < end and received < 256:
            try:
                data, addr = sock.recvfrom(2049)
            except socket.timeout:
                continue
            except OSError:
                break
            received += 1
            if len(data) > 2048 or not isinstance(addr, tuple) or not addr:
                continue
            try:
                sender = ipaddress.IPv4Address(addr[0])
                payload = data.decode("utf-8")
            except (ValueError, UnicodeError, TypeError):
                continue
            if sender.is_multicast or sender.is_unspecified or int(sender) == 0xffffffff:
                continue
            info = {}
            for line in payload.splitlines()[:40]:
                key, _, value = line.partition(":")
                if _ and len(key) <= 64 and len(value) <= 160:
                    info[key.strip().lower()] = value.strip()
            location = info.get("location", "")
            if location:
                try:
                    location_host = (urllib.parse.urlparse(location).hostname
                                     if "://" in location else location)
                except ValueError:
                    continue
                if location_host != str(sender):
                    continue
            name = info.get("devname.bambu.com")
            serial = info.get("usn") or ""
            if (name and len(name) <= 100 and name.isprintable()
                    and len(serial) <= 64 and (not serial or re.fullmatch(r"[A-Za-z0-9-]+", serial))):
                names[str(sender)] = {"name": name, "serial": serial,
                             "model_code": _normalize_model_code(info.get("devmodel.bambu.com"))}
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
    need = [p for p in printers if _needs_name(p)
            and backend_for_type(p.get("type")) != "bambu"]
    if not need:
        return False
    updates = {}
    for p in need:
        kind = backend_for_type(p.get("type"))
        name = ""
        if kind == "moonraker":
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
# Settings (Plugins dialog > Settings tab). Each capability has its own stored
# config, read through that capability's instance; values are validated by
# plugin_settings and cached for a few seconds, so hot paths never touch disk.
# --------------------------------------------------------------------------- #
_SETTINGS_TTL = 4.0
_SETTINGS_LOCK = threading.Lock()
_SETTINGS_CACHE = {}       # kind -> (monotonic time, validated settings)
_SETTINGS_OWNERS = {}      # kind -> capability instance that can read its config
_SETTINGS_STATE_NAME = "settings_state.json"
_SETTINGS_STATE_LOCK = threading.Lock()
_HISTORY_TOKEN_SEEN = None  # last processed clear-history token (memory copy)


def plugin_settings(kind, fresh=False):
    """Validated settings for "outbox" or "dashboard"; defaults on any failure."""
    now = time.monotonic()
    with _SETTINGS_LOCK:
        cached = _SETTINGS_CACHE.get(kind)
        owner = _SETTINGS_OWNERS.get(kind)
    if cached is not None and not fresh and now - cached[0] < _SETTINGS_TTL:
        return dict(cached[1])
    raw = None
    getter = getattr(owner, "get_config", None)
    if callable(getter):
        try:
            raw = getter()
        except Exception:
            raw = None
    values = _pf_settings.normalize(kind, raw)
    with _SETTINGS_LOCK:
        _SETTINGS_CACHE[kind] = (now, values)
    return dict(values)


def _settings_state_path():
    return os.path.join(PLUGIN_STORAGE_DIR, _SETTINGS_STATE_NAME)


def _processed_history_token():
    with _SETTINGS_STATE_LOCK:
        if _HISTORY_TOKEN_SEEN is not None:
            return _HISTORY_TOKEN_SEEN
    try:
        with open(_settings_state_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        value = data.get("history_cleared_token") if isinstance(data, dict) else ""
        return value if isinstance(value, str) else ""
    except (OSError, ValueError):
        return ""


def _mark_history_token(token):
    global _HISTORY_TOKEN_SEEN
    if _STORAGE_DENIED or not PLUGIN_STORAGE_DIR:
        return False
    payload = json.dumps({"history_cleared_token": token}).encode("utf-8")
    try:
        write_bytes_atomic(_settings_state_path(), payload)
    except OSError:
        return False
    with _SETTINGS_STATE_LOCK:
        _HISTORY_TOKEN_SEEN = token
    return True


def apply_history_clear_request(settings=None):
    """Clear the print journal once per "Clear print history" click.

    The settings page can only store a one-time token. The token is marked as
    handled first (so a failed mark can never turn into repeated clearing), a
    damaged or unavailable store is never overwritten, and the same token is
    never acted on twice. Returns True when the journal was cleared."""
    if settings is None:
        settings = plugin_settings(_pf_settings.DASHBOARD)
    token = settings.get("clear_history_token") or ""
    if not token or token == _processed_history_token():
        return False
    with _HISTORY_LOCK:
        if token == _processed_history_token():
            return False
        records = load_print_history()
        if _STORAGE_DENIED or _HISTORY_STORE_DAMAGED:
            _mark_history_token(token)   # consumed: never clears later by surprise
            return False
        if not _mark_history_token(token):
            return False
        return bool(save_print_history([])) if records else True


def history_recording_enabled():
    return bool(plugin_settings(_pf_settings.DASHBOARD)["keep_history"])


def outbox_hint_visible():
    return outbox_hook_missing() and plugin_settings(_pf_settings.OUTBOX)["profile_hint"]


# --------------------------------------------------------------------------- #
# Outbox: every sliced G-code lands next to its printer on the dashboard.
# A SlicingPipeline capability (psGCodePostProcess) copies the produced file
# into the plugin's outbox and records which machine preset it was sliced for;
# the dashboard shows each file on the matching printer's tile with one-click
# send. The hook fires for process presets that select this capability through
# OrcaSlicer's own Slicing pipeline plugin field; the host may call it on every
# slice (Bambu presets) and for other presets more than once, so identical
# files (same SHA-256, same target) are stored once. A plugin cannot safely
# synthesize that manifest entry: cloud installs are keyed by a host-owned
# UUID, so an all-process trigger must come from the host.
# --------------------------------------------------------------------------- #
OUTBOX_DIR = os.path.join(PLUGIN_DIR, "outbox")
OUTBOX_INDEX = os.path.join(PLUGIN_DIR, "outbox_index.json")
OUTBOX_CAP = "Printers Outbox"
_OUTBOX_LOCK = threading.Lock()
_OUTBOX_STORE_DAMAGED = False


def load_outbox_index():
    global _OUTBOX_STORE_DAMAGED
    if _STORAGE_DENIED:
        _OUTBOX_STORE_DAMAGED = True
        return []
    try:
        present = _optional_regular_file(OUTBOX_INDEX)
    except OSError:
        _OUTBOX_STORE_DAMAGED = True
        return []
    if not present:
        if os.path.lexists(OUTBOX_INDEX):
            _OUTBOX_STORE_DAMAGED = True
            return []
        _OUTBOX_STORE_DAMAGED = False
        return []
    try:
        with open(OUTBOX_INDEX, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, list) or not all(
            isinstance(record, dict) and isinstance(record.get("file"), str)
            for record in data
        ):
            raise ValueError("invalid outbox index")
        _OUTBOX_STORE_DAMAGED = False
        return data
    except (OSError, ValueError):
        _OUTBOX_STORE_DAMAGED = True
        return []


def save_outbox_index(index):
    if _STORAGE_DENIED or _OUTBOX_STORE_DAMAGED:
        return False
    tmp = OUTBOX_INDEX + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(index, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, OUTBOX_INDEX)
        return True
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


_GCODE_HEAD_BYTES = 1024 * 1024
_GCODE_TAIL_BYTES = 200 * 1000


def _scan_printer_lines(text, found, stop_at_end):
    for line in text.splitlines():
        s = line.strip()
        if not s.startswith(";"):
            continue
        if stop_at_end and "CONFIG_BLOCK_END" in s:
            return True
        if "printer_settings_id =" in s:
            found["sid"] = s.split("=", 1)[1].strip().strip('"')
        elif "printer_model =" in s:
            found["model"] = s.split("=", 1)[1].strip().strip('"')
    return False


def parse_gcode_printer(path):
    """Read the machine identity Orca writes into the G-code config block:
    printer_settings_id and printer_model. OrcaSlicer 2.5 puts the block right
    after the header (start of file); older output has it at the tail. Only a
    bounded head and tail are read; a value found in either is used (tail wins)."""
    found = {"sid": "", "model": ""}
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            head = fh.read(min(size, _GCODE_HEAD_BYTES))
            _scan_printer_lines(head.decode("utf-8", errors="replace"), found, True)
            if size > len(head):
                start = max(len(head), size - _GCODE_TAIL_BYTES)
                fh.seek(start)
                _scan_printer_lines(
                    fh.read(size - start).decode("utf-8", errors="replace"), found, False)
    except OSError:
        pass
    return found["sid"], found["model"]


def _is_temp_output_name(name):
    """True for names a slicer invents for its working files (".48540.0.gcode")."""
    lowered = name.lower()
    if lowered.startswith(".") or lowered.endswith((".pp", ".tmp", ".part")):
        return True
    stem = lowered
    while True:
        stem, ext = os.path.splitext(stem)
        if not ext:
            break
    return not stem or all(ch.isdigit() or ch in "._-" for ch in stem)


def outbox_display_name(gcode_path, output_name=""):
    """Readable file name for a new outbox record.

    The host passes the real export name when it has one; for a plain slice it
    is empty or a temporary working name, which would be hidden and meaningless.
    The slicing context carries no project name, so the last resort is a
    timestamped name without a leading dot."""
    for source in (output_name, gcode_path):
        base = os.path.basename(str(source or "").replace("\\", "/")).strip()
        if base and not _is_temp_output_name(base):
            return base
    ext = ".bgcode" if str(gcode_path or "").lower().endswith(".bgcode") else ".gcode"
    stamp = time.strftime("%Y-%m-%d %H-%M-%S")
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", ui_text("outboxFallbackName", stamp=stamp)).strip(" .")
    return (name or "slice " + stamp) + ext


def _file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(192 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _slice_host_endpoint(host):
    """Endpoint key of a preset's print_host; "File"/empty is not an address."""
    host = str(host or "").strip()
    return "" if not host or host.casefold() == "file" else _endpoint_key(host)


def outbox_add(gcode_path, output_name="", host="", identity=None):
    """Copy a produced G-code into the outbox and index it. Returns the record.

    `identity` carries values the slicing context gave directly
    (printer_settings_id, printer_model, print_host, flashforge serial); the
    G-code config block is only the fallback. A file with the same SHA-256 and
    the same target is not stored twice: the existing record is refreshed."""
    if _STORAGE_DENIED:
        raise OSError("plugin storage unavailable")
    original = outbox_display_name(gcode_path, output_name)
    identity = identity if isinstance(identity, dict) else {}
    sid = str(identity.get("printer_settings_id") or "").strip()
    model = str(identity.get("printer_model") or "").strip()
    if not (sid or model):
        sid, model = parse_gcode_printer(gcode_path)
    with _OUTBOX_LOCK:
        index = load_outbox_index()
        if _OUTBOX_STORE_DAMAGED:
            raise OSError("outbox index damaged")
        normalized_host = str(host or "").strip()
        record = {"name": original, "printer_settings_id": sid,
                  "printer_model": model,
                  "size": os.path.getsize(gcode_path), "ts": int(time.time()),
                  "host": normalized_host,
                  "host_handled": bool(normalized_host and normalized_host.casefold() != "file")}
        record["repetier_slug"] = str(identity.get("printhost_port") or "").strip()
        record["host_type"] = str(identity.get("host_type") or "").strip()
        record["match_url"] = normalize_url(identity.get("print_host") or normalized_host)
        endpoint = _slice_host_endpoint(identity.get("print_host") or normalized_host)
        if endpoint:
            record["match_endpoint"] = endpoint
        serial = str(identity.get("serial") or "").strip()
        if serial:
            record["match_serial"] = serial
        matched = outbox_target_match(record, dashboard_printers())
        wanted_target = matched.get("target_uuid") if matched else None
        source_digest = _file_sha256(gcode_path)
        for existing in index:
            if (existing.get("sha256") == source_digest
                    and (existing.get("target_uuid") or None) == (wanted_target or None)
                    and (not (record.get("host_type") == "repetier" or record.get("repetier_slug"))
                         or all(existing.get(key) == record.get(key) for key in
                                ("host_type", "repetier_slug", "match_url")))):
                existing_path = _safe_outbox_path(existing.get("file"))
                if existing_path and os.path.isfile(existing_path):
                    existing["ts"] = int(time.time())
                    if not save_outbox_index(index):
                        raise OSError("could not save outbox index")
                    return existing
        folder = _outbox_folder(wanted_target)
        os.makedirs(folder, exist_ok=True)
        stored = os.path.join(os.path.basename(folder), "%d__%s" % (int(time.time() * 1000), original)).replace("\\", "/")
        target = _safe_outbox_path(stored)
        if target is None:
            raise OSError("invalid outbox filename")
        _copy_exclusive(gcode_path, target)
        record.update({"file": stored, "size": os.path.getsize(target)})
        record["sha256"] = _file_sha256(target)
        if matched is not None:
            record.update({
                "target_uuid": matched.get("target_uuid"),
                "owner_scope_uuid": matched.get("owner_scope_uuid"),
                "target_selector": matched.get("target_selector"),
                "binding_revision": matched.get("binding_revision"),
            })
        index.append(record)
        if not save_outbox_index(index):
            raise OSError("could not save outbox index")
    return record


def outbox_remove(stored_name):
    if _safe_outbox_path(stored_name) is None:
        return False
    with _OUTBOX_LOCK:
        current = load_outbox_index()
        if not any(r.get("file") == stored_name for r in current):
            return False
        index = [r for r in current if r.get("file") != stored_name]
        try:
            path = _safe_outbox_path(stored_name)
            if path and os.path.lexists(path):
                if not os.path.islink(path):
                    os.chmod(path, os.stat(path, follow_symlinks=False).st_mode | stat.S_IWRITE)
                os.remove(path)
        except OSError:
            return False
        # Keep the index entry visible until physical deletion succeeds. If the
        # index write then fails, the existing row remains available for retry.
        return save_outbox_index(index)


def _outbox_group_key(record):
    """Which printer group a stored copy belongs to (bound target, else the
    identity the slice carried). Copies are only ever compared inside a group."""
    if record.get("target_uuid"):
        return ("t", str(record.get("target_uuid")))
    return ("u",) + tuple(
        str(record.get(key) or "").strip().casefold()
        for key in ("match_serial", "match_endpoint", "printer_settings_id", "printer_model"))


def prune_outbox_keep_last(keep, group_of=None, busy=None):
    """Keep only the `keep` newest copies of one printer group; off when 0.

    Removes the oldest copies beyond `keep` from the plugin's own storage,
    through outbox_remove. A copy tied to a print-history entry by SHA-256 is
    in use and stays (it still counts towards `keep`); nothing is removed while
    an operation could be using a copy. Never raises. Returns the removed count."""
    try:
        keep = int(keep)
        if keep <= 0 or _OUTBOX_STORE_DAMAGED or not isinstance(group_of, dict):
            return 0
        if busy is None:
            dashboard = _SETTINGS_OWNERS.get(_pf_settings.DASHBOARD)
            blocker = getattr(dashboard, "_leftover_blocker", None)
            busy = bool(blocker()) if callable(blocker) else False
        if busy:
            return 0
        group = _outbox_group_key(group_of)
        members = [(i, r) for i, r in enumerate(load_outbox_index())
                   if isinstance(r, dict) and isinstance(r.get("file"), str)
                   and _outbox_group_key(r) == group]
        if _OUTBOX_STORE_DAMAGED or len(members) <= keep:
            return 0

        def age_key(item):
            ts = item[1].get("ts")
            ok = isinstance(ts, (int, float)) and not isinstance(ts, bool)
            return (ts if ok else 0, item[0])

        members.sort(key=age_key, reverse=True)
        used = {_history_digest(r.get("sha256")) for r in load_print_history()}
        used.discard(None)
        removed = 0
        for _index, record in members[keep:]:
            if _history_digest(record.get("sha256")) in used:
                continue
            if outbox_remove(record["file"]):
                removed += 1
        return removed
    except Exception:
        return 0


_OUTBOX_PRUNE_INTERVAL = 24 * 3600
_OUTBOX_PRUNE_DELAY = 30.0
_PRUNE_STOP = None


def prune_old_outbox(now=None, busy=None):
    """Delete stored copies older than the configured age; only if switched on.

    Works on the index and goes through outbox_remove (safe path, index kept
    consistent). Skipped entirely while an operation could be using a copy.
    Returns the number of removed copies."""
    settings = plugin_settings(_pf_settings.OUTBOX, fresh=True)
    if not settings["auto_delete"]:
        return 0
    if busy is None:
        dashboard = _SETTINGS_OWNERS.get(_pf_settings.DASHBOARD)
        blocker = getattr(dashboard, "_leftover_blocker", None)
        busy = bool(blocker()) if callable(blocker) else False
    if busy:
        return 0
    limit = (time.time() if now is None else now) - settings["auto_delete_days"] * 86400
    removed = 0
    for record in load_outbox_index():
        if _OUTBOX_STORE_DAMAGED:
            break
        ts = record.get("ts")
        stored = record.get("file")
        if (isinstance(ts, (int, float)) and not isinstance(ts, bool)
                and 0 < ts <= limit and isinstance(stored, str)
                and outbox_remove(stored)):
            removed += 1
    return removed


def _prune_tick(handle):
    if _PRUNE_STOP is not handle:
        return
    handle["timer"] = None
    try:
        prune_old_outbox()
    except Exception:
        pass
    _schedule_prune(handle, _OUTBOX_PRUNE_INTERVAL)


def _schedule_prune(handle, delay):
    timer = threading.Timer(delay, _prune_tick, args=(handle,))
    timer.daemon = True
    if not start_thread_if_allowed(timer):
        return False
    handle["timer"] = timer
    return True


def start_outbox_pruning():
    """First pass shortly after start, then once a day, off the slicer thread."""
    global _PRUNE_STOP
    stop_outbox_pruning()
    handle = {"timer": None}
    _PRUNE_STOP = handle
    _schedule_prune(handle, _OUTBOX_PRUNE_DELAY)


def resume_deferred_pruning():
    """Schedule the first prune that a loader thread had to leave undone."""
    with _PLUGIN_RUNTIME_LOCK:
        if (_PLUGIN_RUNTIME_ACTIVE and _PRUNE_STOP is not None
                and _PRUNE_STOP["timer"] is None):
            _schedule_prune(_PRUNE_STOP, _OUTBOX_PRUNE_DELAY)


def stop_outbox_pruning():
    global _PRUNE_STOP
    handle, _PRUNE_STOP = _PRUNE_STOP, None
    if handle is not None and handle["timer"] is not None:
        handle["timer"].cancel()


def _safe_outbox_path(stored_name):
    if not isinstance(stored_name, str) or not stored_name or "\\" in stored_name:
        return None
    root = os.path.realpath(OUTBOX_DIR)
    parts = stored_name.split("/")
    if any(part in {"", ".", ".."} for part in parts) or any(":" in part for part in parts):
        return None
    if len(parts) > 2 or (len(parts) == 2 and parts[0] not in {"unassigned"} and not _is_uuid(parts[0])):
        return None
    path = os.path.abspath(os.path.join(root, *parts))
    real_path = os.path.realpath(path)
    try:
        if os.path.commonpath([root, real_path]) != root:
            return None
    except ValueError:
        return None
    current = root
    for part in parts[:-1]:
        current = os.path.join(current, part)
        if os.path.islink(current):
            return None
    if os.path.islink(path):
        return None
    return real_path


def _is_uuid(value):
    try:
        return str(uuid.UUID(value)) == value.lower()
    except (ValueError, AttributeError, TypeError):
        return False


def _outbox_folder(target_uuid):
    if _is_uuid(target_uuid):
        return os.path.join(OUTBOX_DIR, target_uuid.lower())
    return os.path.join(OUTBOX_DIR, "unassigned")


def _copy_exclusive(source, target):
    """Copy bytes without ever replacing an existing outbox file."""
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(source, "rb") as src, open(target, "xb") as dst:
        shutil.copyfileobj(src, dst)
        dst.flush()
        os.fsync(dst.fileno())


def _migrate_flat_outbox(index, printers):
    """Copy legacy flat files to scoped folders; retain originals permanently."""
    legacy = [r for r in index if isinstance(r.get("file"), str) and "/" not in r["file"]]
    if not legacy:
        return index
    updates = []
    created = []
    migrated_sources = []
    try:
        for record in index:
            item = dict(record)
            old_name = item.get("file")
            if not isinstance(old_name, str) or "/" in old_name:
                updates.append(item)
                continue
            source = _safe_outbox_path(old_name)
            if source is None or not os.path.isfile(source) or os.path.islink(source):
                return index
            match = outbox_target_match(item, printers)
            target_uuid = match.get("target_uuid") if match else None
            digest = hashlib.sha256()
            with open(source, "rb") as fh:
                for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                    digest.update(chunk)
            folder_name = target_uuid.lower() if _is_uuid(target_uuid) else "unassigned"
            basename = os.path.basename(old_name)
            new_name = "%s/%s__%s" % (folder_name, digest.hexdigest()[:20], basename)
            destination = _safe_outbox_path(new_name)
            if destination is None:
                return index
            if os.path.exists(destination):
                existing = hashlib.sha256()
                with open(destination, "rb") as fh:
                    for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                        existing.update(chunk)
                if existing.digest() != digest.digest():
                    return index
            else:
                _copy_exclusive(source, destination)
                created.append(destination)
            item["file"] = new_name
            item["size"] = os.path.getsize(destination)
            if match:
                item.update({"target_uuid": match.get("target_uuid"),
                             "owner_scope_uuid": match.get("owner_scope_uuid"),
                             "target_selector": match.get("target_selector"),
                             "binding_revision": match.get("binding_revision")})
            updates.append(item)
            migrated_sources.append((source, digest.hexdigest(), old_name))
    except (OSError, ValueError):
        # Source remains authoritative; orphaned copies are harmless and recoverable.
        return index
    raw = pathlib.Path(OUTBOX_INDEX).read_bytes()
    backup = OUTBOX_INDEX + ".layout-backup"
    try:
        if os.path.exists(backup):
            if hashlib.sha256(pathlib.Path(backup).read_bytes()).digest() != hashlib.sha256(raw).digest():
                return index
        else:
            with open(backup, "xb") as fh:
                fh.write(raw)
                fh.flush()
                os.fsync(fh.fileno())
        if not save_outbox_index(updates):
            return index
    except OSError:
        return index
    # Keep the original bytes in a sibling recovery area. Only relocate source
    # files after the atomic index switch, so failed migrations stay readable.
    recovery = os.path.join(os.path.dirname(OUTBOX_DIR), "outbox-legacy-recovery")
    try:
        os.makedirs(recovery, exist_ok=True)
        for source, digest_hex, old_name in migrated_sources:
            if not os.path.isfile(source) or os.path.islink(source):
                continue
            recovery_name = "%s__%s" % (digest_hex[:20], os.path.basename(old_name))
            destination = os.path.join(recovery, recovery_name)
            if os.path.lexists(destination):
                continue
            os.replace(source, destination)
    except OSError:
        # Indexed copies remain authoritative; originals stay as recoverable
        # unindexed data if relocation is denied.
        pass
    return updates


def _name_is_matchable(printer):
    if backend_for_type(printer.get("type")) != "bambu":
        return True
    name = printer.get("name")
    if not name or printer.get("name_match_value") != name:
        return False
    if printer.get("name_match_binding_revision") != printer.get("binding_revision"):
        return False
    provenance = printer.get("name_provenance")
    if provenance == "user":
        return True
    if provenance == "orca" and printer.get("source") == "orca":
        return printer.get("discovery_ref") == _orca_discovery_ref(
            name, printer.get("url"), printer.get("type"))
    return False


def outbox_target_match(record, printers):
    """Return the sole printer a record belongs to, or None (never a guess):
    serial, address, exact preset name, then the Bambu model code."""
    if record.get("target_uuid"):
        matches = [p for p in printers
                   if p.get("target_uuid") == record.get("target_uuid")
                   and p.get("owner_scope_uuid") == record.get("owner_scope_uuid")
                   and p.get("target_selector") == record.get("target_selector")
                   and p.get("binding_revision") == record.get("binding_revision")]
        return matches[0] if len(matches) == 1 else None
    # Repetier routing requires both the full server URL and the exact slug.
    repetier = [p for p in printers if p.get("type") == "repetier"]
    route = record.get("match_url") or record.get("host")
    slug = record.get("repetier_slug")
    if record.get("host_type") == "repetier" or slug:
        matches = [p for p in repetier if _pf_repetier.valid_slug(slug)
                   and p.get("repetier_slug") == slug
                   and normalize_url(p.get("url")) == normalize_url(route)]
        return matches[0] if len(matches) == 1 else None
    # No fallback can assign a file to Repetier without a slug.
    printers = [p for p in printers if p.get("type") != "repetier"]
    # Most specific first: serial, then the preset's print_host address, then
    # the preset name. Several cards on one key is ambiguous, never a guess.
    serial = str(record.get("match_serial") or "").strip().casefold()
    if serial:
        matches = [p for p in printers
                   if str(p.get("serial") or "").strip().casefold() == serial
                   and (backend_for_type(p.get("type")) != "bambu"
                        or _verified_bambu_serial(p))]
        if matches:
            return matches[0] if len(matches) == 1 else None
    endpoint = record.get("match_endpoint")
    if isinstance(endpoint, str) and endpoint:
        matches = [p for p in printers if _endpoint_key(p.get("url")) == endpoint]
        if matches:
            return matches[0] if len(matches) == 1 else None
    sid = (record.get("printer_settings_id") or "").strip()
    model = (record.get("printer_model") or "").strip()
    identity = sid or model
    matches = [p for p in printers
               if identity and (p.get("name") or "").strip() == identity
               and _name_is_matchable(p)]
    if matches:
        return matches[0] if len(matches) == 1 else None
    # Last resort, only for a slice with no serial/address of its own: the
    # Bambu model code. Nozzle size and profile name play no part here.
    code = _slice_model_code(model)
    if code and not serial and not (isinstance(endpoint, str) and endpoint):
        matches = [p for p in printers
                   if backend_for_type(p.get("type")) == "bambu"
                   and _card_model_code(p) == code]
        if len(matches) == 1:
            return matches[0]
    return None


def attach_outbox(printers):
    """Group queued files onto their printers (by the machine preset name the
    file was sliced for). Returns the count that matched no printer."""
    index = load_outbox_index()
    if not _OUTBOX_STORE_DAMAGED:
        index = _migrate_flat_outbox(index, printers)
    for p in printers:
        p["queued"] = []
        p["outbox_count"] = 0
        p["outbox_bytes"] = 0
    unmatched = 0
    for record in index:
        printer = outbox_target_match(record, printers)
        if printer is not None:
            printer["queued"].append(record)
            printer["outbox_count"] += 1
            printer["outbox_bytes"] += max(0, int(record.get("size") or 0))
        else:
            unmatched += 1
    return unmatched


def outbox_physical_usage(printers=()):
    """Count physical outbox and recovery bytes without following symlinks."""
    groups = {}
    errors = []
    names = {p.get("target_uuid"): p.get("name") or p.get("url") or p.get("target_uuid")
             for p in printers if _is_uuid(p.get("target_uuid"))}

    def add(key, kind, label, relative_path, size):
        row = groups.setdefault(key, {"kind": kind, "label": label, "path": relative_path,
                                      "count": 0, "size": 0})
        row["count"] += 1
        row["size"] += max(0, size)

    def walk(root, rel_root, classify):
        try:
            root_info = os.stat(root, follow_symlinks=False)
            if not stat.S_ISDIR(root_info.st_mode):
                errors.append(rel_root)
                return
            entries = list(os.scandir(root))
        except FileNotFoundError:
            return
        except OSError:
            errors.append(rel_root)
            return
        for entry in entries:
            rel = (rel_root + "/" + entry.name).strip("/")
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                errors.append(rel)
                continue
            if stat.S_ISLNK(info.st_mode):
                errors.append(rel)
                key, kind, label, group_path = classify(rel)
                add(key, kind, label, group_path, info.st_size)
            elif stat.S_ISDIR(info.st_mode):
                walk(entry.path, rel, classify)
            elif stat.S_ISREG(info.st_mode):
                key, kind, label, group_path = classify(rel)
                add(key, kind, label, group_path, info.st_size)
            else:
                errors.append(rel)

    def classify_outbox(rel):
        outbox_relative = rel[len(rel_outbox):].lstrip("/")
        first = outbox_relative.split("/", 1)[0]
        if first == "unassigned":
            return "unassigned", "outboxStorageKindUnassigned", "", rel_outbox + "/unassigned"
        if _is_uuid(first):
            return "printer:" + first, "printer", names.get(first, first), rel_outbox + "/" + first
        return "loose", "outboxStorageKindLoose", "", rel_outbox

    storage_root = os.path.dirname(OUTBOX_DIR)
    try:
        rel_outbox = os.path.relpath(OUTBOX_DIR, PLUGIN_STORAGE_DIR).replace(os.sep, "/")
    except ValueError:
        rel_outbox = "outbox"
    walk(OUTBOX_DIR, rel_outbox, classify_outbox)
    incoming = os.path.join(PLUGIN_STORAGE_DIR, "incoming")
    walk(incoming, "incoming",
         lambda rel: ("incoming", "outboxStorageKindIncoming", "", "incoming"))
    recovery = os.path.join(storage_root, "outbox-legacy-recovery")
    recovery_rel = os.path.relpath(recovery, PLUGIN_STORAGE_DIR).replace(os.sep, "/")
    walk(recovery, recovery_rel,
         lambda rel: ("recovery", "outboxStorageKindRecovery", "", recovery_rel))
    storage_parent = os.path.dirname(PLUGIN_STORAGE_DIR)
    storage_name = os.path.basename(os.path.normpath(PLUGIN_STORAGE_DIR))
    try:
        candidates = list(os.scandir(storage_parent))
    except FileNotFoundError:
        candidates = []
    except OSError:
        errors.append(os.path.relpath(storage_parent, PLUGIN_STORAGE_DIR).replace(os.sep, "/"))
        candidates = []
    for entry in candidates:
        if not (entry.name.startswith(storage_name + ".legacy-backup")
                or entry.name.startswith(storage_name + ".import-stage-")):
            continue
        rel = os.path.relpath(entry.path, PLUGIN_STORAGE_DIR).replace(os.sep, "/")
        walk(entry.path, rel,
             lambda child, path=rel: ("plugin-recovery:" + path,
                                      "outboxStorageKindRecovery", "", path))
    for path, key, label in (
        (OUTBOX_INDEX, "index", "outboxStorageKindIndex"),
        (OUTBOX_INDEX + ".layout-backup", "index-backup", "outboxStorageKindIndexBackup"),
    ):
        try:
            info = os.stat(path, follow_symlinks=False)
            rel = os.path.relpath(path, PLUGIN_STORAGE_DIR).replace(os.sep, "/")
            if stat.S_ISREG(info.st_mode):
                add(key, "index" if key == "index" else "outboxStorageKindIndexBackup",
                    label, rel, info.st_size)
            else:
                errors.append(rel)
        except FileNotFoundError:
            continue
        except OSError:
            errors.append(os.path.basename(path))
    return {"ok": not errors, "groups": list(groups.values()),
            "errors": sorted(set(errors))}


_REPARSE_POINT = 0x400


def format_size(value):
    size = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return ("%d %s" % (size, unit)) if unit == "B" else ("%.1f %s" % (size, unit))
        size /= 1024


def _is_link_like(info):
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & _REPARSE_POINT)


def _inside(root, path):
    try:
        return os.path.commonpath([root, os.path.realpath(path)]) == root
    except ValueError:
        return False


def outbox_leftover_files(protected=()):
    """List files Print Farm itself left behind in its own storage.

    Covers loose files directly in the outbox (not in a printer or "unassigned"
    folder and not named by the index), prepared uploads in ``incoming``, the
    legacy-recovery copies and the index layout backup. Per-printer folders,
    "unassigned", the index itself and the sibling legacy-backup/import-stage
    folders (they back up printers and history) are never listed. Links and
    reparse points are never followed; they come back in ``skipped``.
    Returns ``(files, skipped)``: files are ``(kind, absolute_path, size)``.
    """
    files, skipped = [], []
    if not PLUGIN_STORAGE_DIR:
        return files, skipped
    storage = os.path.realpath(PLUGIN_STORAGE_DIR)
    protected = {os.path.realpath(str(item)) for item in protected}
    referenced = set()
    for record in load_outbox_index():
        name = record.get("file") if isinstance(record, dict) else None
        if isinstance(name, str):
            referenced.add(name)

    def rel(path):
        try:
            return os.path.relpath(path, storage).replace(os.sep, "/")
        except ValueError:
            return os.path.basename(path)

    def walk(kind, root):
        try:
            info = os.lstat(root)
        except FileNotFoundError:
            return
        except OSError:
            skipped.append(rel(root))
            return
        real_root = os.path.realpath(root)
        if (_is_link_like(info) or not stat.S_ISDIR(info.st_mode)
                or not _inside(storage, real_root)):
            skipped.append(rel(root))
            return
        stack = [root]
        while stack:
            directory = stack.pop()
            try:
                entries = list(os.scandir(directory))
            except OSError:
                skipped.append(rel(directory))
                continue
            for entry in entries:
                try:
                    entry_info = entry.stat(follow_symlinks=False)
                except OSError:
                    skipped.append(rel(entry.path))
                    continue
                is_dir = stat.S_ISDIR(entry_info.st_mode)
                if (kind == "loose" and directory == root and is_dir
                        and (entry.name == "unassigned" or _is_uuid(entry.name))):
                    continue
                if _is_link_like(entry_info):
                    skipped.append(rel(entry.path))
                elif is_dir:
                    stack.append(entry.path)
                elif stat.S_ISREG(entry_info.st_mode):
                    real = os.path.realpath(entry.path)
                    inside_outbox = rel(entry.path)
                    if kind == "loose":
                        inside_outbox = os.path.relpath(entry.path, root).replace(os.sep, "/")
                    if (not _inside(real_root, real) or real in protected
                            or (kind == "loose" and inside_outbox in referenced)):
                        continue
                    files.append((kind, entry.path, entry_info.st_size))
                else:
                    skipped.append(rel(entry.path))

    walk("loose", OUTBOX_DIR)
    walk("incoming", os.path.join(PLUGIN_STORAGE_DIR, "incoming"))
    walk("recovery", os.path.join(os.path.dirname(OUTBOX_DIR), "outbox-legacy-recovery"))
    backup = OUTBOX_INDEX + ".layout-backup"
    try:
        info = os.lstat(backup)
    except FileNotFoundError:
        info = None
    except OSError:
        skipped.append(rel(backup))
        info = None
    if info is not None:
        if (_is_link_like(info) or not stat.S_ISREG(info.st_mode)
                or not _inside(storage, backup)):
            skipped.append(rel(backup))
        else:
            files.append(("index-backup", backup, info.st_size))
    return files, sorted(set(skipped))


def outbox_remove_leftovers(files):
    """Delete the listed leftovers one by one; report deletions and failures apart."""
    deleted, size, failed = 0, 0, []
    storage = os.path.realpath(PLUGIN_STORAGE_DIR) if PLUGIN_STORAGE_DIR else ""
    for _kind, path, expected_size in files:
        label = os.path.relpath(path, storage).replace(os.sep, "/") if storage else path
        try:
            if not storage or not _inside(storage, path):
                raise OSError("outside plugin storage")
            info = os.lstat(path)
            if _is_link_like(info) or not stat.S_ISREG(info.st_mode):
                raise OSError("not a regular file")
            os.chmod(path, info.st_mode | stat.S_IWRITE)
            os.remove(path)
            deleted += 1
            size += max(0, expected_size)
        except FileNotFoundError:
            continue
        except OSError:
            failed.append(label)
    return {"deleted": deleted, "size": size, "failed": failed}


_SLICE_IDENTITY_KEYS = (
    ("printer_settings_id", "printer_settings_id"),
    ("printer_model", "printer_model"),
    ("print_host", "print_host"),
    ("printhost_port", "printhost_port"),
    ("host_type", "host_type"),
    ("serial", "flashforge_serial_number"),
)


def _slice_identity(ctx):
    """Printer identity from the slicing context's config, when the host has it.

    Only these non-secret keys are requested (never printhost_apikey); a host
    without config_value, or any error, yields {} and the G-code is parsed."""
    config_value = getattr(ctx, "config_value", None)
    if not callable(config_value):
        return {}
    found = {}
    for name, key in _SLICE_IDENTITY_KEYS:
        try:
            value = config_value(key)
        except Exception:
            continue
        if isinstance(value, str) and value.strip():
            found[name] = value.strip()
    return found


# --------------------------------------------------------------------------- #
# OrcaSlicer lifecycle events. They reach every enabled capability, without any
# profile selection, so they are the only signal for sends and prints started
# from OrcaSlicer itself. Finished events arrive on the UI thread: the handler
# only touches in-memory state and the local history file (no host, no network).
# --------------------------------------------------------------------------- #
ORCA_HINT_DELAY_SECONDS = 5.0
_ORCA_EVENT_LOCK = threading.Lock()
_ORCA_STATE = {
    "slice_at": None,   # last GCodeExportStarted/Finished (monotonic)
    "hook_at": None,    # last PrintersOutbox.execute call (monotonic)
    "no_hook": False,   # a slice finished and the hook did not run after it
    "names": {},        # normalized temp G-code path -> model name
    "starts": {},       # (is_send, device, name, job) -> time.time() of Started
    "recent": {},       # finished-event key -> monotonic time (duplicate guard)
}
_ORCA_RECENT_SECONDS = 30.0
_ORCA_MAX_ENTRIES = 32


def _orca_enum_name(value):
    name = getattr(value, "name", None)
    if not isinstance(name, str) or not name:
        name = str(value).rsplit(".", 1)[-1]
    return name


def _orca_event_name(event):
    """Name of a LifecycleEvent member, or "" when the host lacks the enum."""
    enum = getattr(orca, "LifecycleEvent", None)
    if enum is None:
        return ""
    name = _orca_enum_name(event)
    return name if getattr(enum, name, None) is not None else ""


def _orca_path_key(path):
    text = str(path or "").strip()
    return os.path.normcase(os.path.normpath(text)) if text else ""


def _orca_bound(mapping):
    while len(mapping) > _ORCA_MAX_ENTRIES:
        mapping.pop(next(iter(mapping)))


def _orca_note_hook_ran():
    with _ORCA_EVENT_LOCK:
        _ORCA_STATE["hook_at"] = time.monotonic()
        _ORCA_STATE["no_hook"] = False


def outbox_hook_missing():
    with _ORCA_EVENT_LOCK:
        return bool(_ORCA_STATE["no_hook"])


def _orca_check_hook():
    with _ORCA_EVENT_LOCK:
        slice_at, hook_at = _ORCA_STATE["slice_at"], _ORCA_STATE["hook_at"]
        _ORCA_STATE["no_hook"] = slice_at is not None and (
            hook_at is None or hook_at < slice_at)


def _orca_output_name_hint(path, output_name):
    """Prefer the model name from GCodeExportStarted over an invented name.

    Used only when neither the export name nor the file name is meaningful and
    the event's path equals this G-code path (the message text is no contract)."""
    for source in (output_name, path):
        base = os.path.basename(str(source or "").replace("\\", "/")).strip()
        if base and not _is_temp_output_name(base):
            return output_name
    with _ORCA_EVENT_LOCK:
        model = _ORCA_STATE["names"].get(_orca_path_key(path))
    model = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", str(model or "")).strip(" .")
    if not model:
        return output_name
    if not model.lower().endswith((".gcode", ".bgcode")):
        model += ".bgcode" if str(path or "").lower().endswith(".bgcode") else ".gcode"
    return model


def _orca_printer_for_device(device_id):
    """Card whose serial equals the event's device id; ambiguity means none."""
    wanted = str(device_id or "").strip().casefold()
    if not wanted:
        return None
    try:
        found = [p for p in load_manual_printers()
                 if str(p.get("serial") or "").strip().casefold() == wanted]
    except Exception:
        return None
    return found[0] if len(found) == 1 else None


def record_orca_job(printer, filename, state, now=None, duration=0):
    """Journal a send/print that OrcaSlicer itself reported (printer may be None)."""
    if not history_recording_enabled():
        return None
    now = int(time.time()) if now is None else int(now)
    duration = _history_int(duration, minimum=0,
                            maximum=PRINT_HISTORY_MAX_DURATION_SECONDS)
    with _HISTORY_LOCK:
        records = load_print_history()
        record = _new_history_record(printer or {}, filename, now, "orca", duration)
        record["state"] = state
        if state in _HISTORY_ACTIVE_STATES:
            for item in records:
                if (item.get("printer_key") == record["printer_key"]
                        and item.get("state") in _HISTORY_ACTIVE_STATES):
                    _update_history_record(item, {"filename": filename}, "ended", now)
        else:
            record["finished_at"] = now
        records.insert(0, record)
        save_print_history(records)
    return record


def handle_orca_lifecycle_event(event, ctx):
    name = _orca_event_name(event)
    if not name or ctx is None:
        return
    now = time.monotonic()
    if name in ("GCodeExportStarted", "GCodeExportFinished"):
        with _ORCA_EVENT_LOCK:
            _ORCA_STATE["slice_at"] = now
            key = _orca_path_key(getattr(ctx, "msg", ""))
            model = str(getattr(ctx, "name", "") or "").strip()
            if key and model:
                _ORCA_STATE["names"].pop(key, None)
                _ORCA_STATE["names"][key] = model
                _orca_bound(_ORCA_STATE["names"])
        return
    if name == "SlicingJobComplete":
        if _orca_enum_name(getattr(ctx, "code", None)) == "Ok":
            timer = threading.Timer(ORCA_HINT_DELAY_SECONDS, _orca_check_hook)
            timer.daemon = True
            start_thread_if_allowed(timer)
        return
    if name not in ("PrintJobStarted", "PrintJobFinished",
                    "SendJobStarted", "SendJobFinished"):
        return
    is_send = name.startswith("Send")
    source = str(getattr(ctx, "source", "") or "")
    if source not in ("print_job", "send_job", "task_manager"):
        return
    device = str(getattr(ctx, "device_id", "") or "").strip()
    title = str(getattr(ctx, "name", "") or "").strip()
    job = str(getattr(ctx, "job_id", "") or "")
    ident = (is_send, device.casefold(), title, job)
    if name.endswith("Started"):
        with _ORCA_EVENT_LOCK:
            _ORCA_STATE["starts"][ident] = time.time()
            _orca_bound(_ORCA_STATE["starts"])
        return
    code = _orca_enum_name(getattr(ctx, "code", None))
    with _ORCA_EVENT_LOCK:
        recent = _ORCA_STATE["recent"]
        for key in [k for k, t in recent.items() if now - t > _ORCA_RECENT_SECONDS]:
            recent.pop(key)
        marker = ident + (code,)
        if marker in recent:
            return
        recent[marker] = now
        _orca_bound(recent)
        started = _ORCA_STATE["starts"].pop(ident, None)
    duration = max(0, time.time() - started) if started is not None else 0
    printer = _orca_printer_for_device(device)
    if code == "Ok":
        state = "sent" if is_send or printer is None else "printing"
    elif code == "Warn":
        state = "cancelled"
    else:
        state = "failed"
    if plugin_settings(_pf_settings.OUTBOX)["journal_orca_events"]:
        record_orca_job(printer, title, state, duration=duration)


class PrintersOutbox(
    _PluginRuntimeLifecycleMixin,
    orca.slicing.SlicingPipelineCapabilityBase,
):
    def __init__(self):
        super().__init__()
        _SETTINGS_OWNERS[_pf_settings.OUTBOX] = self

    def get_name(self):
        return OUTBOX_CAP

    def has_config_ui(self):
        return True

    def get_config_ui(self):
        language = refresh_ui_language() or _CACHED_UI_LANGUAGE or "en"
        return _pf_settings.build_page(_pf_settings.OUTBOX, ui_text, language)

    def get_default_config(self):
        return _pf_settings.defaults(_pf_settings.OUTBOX)

    def on_lifecycle_event(self, event, ctx):
        # Only here, not in the shared mixin: every enabled capability gets
        # every event, and a second subscriber would record each one twice.
        try:
            handle_orca_lifecycle_event(event, ctx)
        except Exception:
            pass  # never let a bookkeeping error reach the slicer

    def execute(self, ctx):
        _orca_note_hook_ran()
        step = getattr(ctx, "step", None)
        step_enum = getattr(orca.slicing, "Step", None)
        post = getattr(step_enum, "psGCodePostProcess", None)
        if post is None:
            post = getattr(orca.slicing, "psGCodePostProcess", None)
        if step is not None and post is not None and step != post:
            return orca.ExecutionResult.skipped(ui_text("outboxWrongStep"))
        if not plugin_settings(_pf_settings.OUTBOX)["keep_copies"]:
            return orca.ExecutionResult.skipped(ui_text("outboxCopiesOff"))
        path = getattr(ctx, "gcode_path", "") or ""
        if not path or not os.path.exists(path):
            return orca.ExecutionResult.skipped(ui_text("outboxNotReady"))
        try:
            record = outbox_add(path, _orca_output_name_hint(
                                    path, getattr(ctx, "output_name", "") or ""),
                                getattr(ctx, "host", "") or "",
                                _slice_identity(ctx))
        except Exception as exc:
            # A storage failure must not abort the user's export or upload.
            return orca.ExecutionResult.skipped(
                "%s: %s" % (ui_text("outboxWriteFailed"), exc))
        try:
            keep = plugin_settings(_pf_settings.OUTBOX)["keep_last"]
            if keep:
                prune_outbox_keep_last(keep, record)
        except Exception:
            pass  # trimming is optional; the new copy is already stored
        return orca.ExecutionResult.success(ui_text(
            "outboxQueued",
            name=record["name"],
            target=record.get("printer_settings_id") or ui_text("other"),
        ))


_DUET_PROTOCOLS = frozenset({"rrf", "dsf"})
_ADAPTER_AUTH_CODES = frozenset({"authentication_failed", "authorization_denied"})


def _duet_pin(printer):
    """Protocol pinned for this Duet target ("rrf" or "dsf"), else None."""
    value = (printer or {}).get("protocol")
    value = value.lower() if isinstance(value, str) else ""
    return value if value in _DUET_PROTOCOLS else None


def parse_ws_port(value):
    """None for "use the default", an int 1-65535, or False when invalid."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        return False
    if isinstance(value, str):
        text = value.strip()
        if not text.isascii() or not text.isdigit() or len(text) > 5:
            return False
        value = int(text)
    if not isinstance(value, int) or not 1 <= value <= 65535:
        return False
    return value


def creality_ws_port(printer):
    value = (printer or {}).get("ws_port")
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        return CREALITY_DEFAULT_WS_PORT
    return value


def _adapter_auth_code(status):
    """Report a wrong password like every other backend's authorization_denied."""
    if status.get("error_code") in _ADAPTER_AUTH_CODES:
        status["error_code"] = "authorization_denied"
    return status


def _poll_creality(printer, base):
    try:
        ws = _pf_creality.ws_base(base, creality_ws_port(printer))
    except ValueError:
        ws = ""
    status = dict(_pf_creality.poll(ws, None, ""))
    # Read-only for now: the adapter keeps its write gate closed because the
    # upload target is not verified on hardware. That is a reason shown on the
    # card, not a failure of the status read.
    if status.get("error_code") == "writable_target_unverified":
        status.pop("error_code")
        status["upload_reason"] = "writable_target_unverified"
    status["upload_ready"] = False
    status["can_control"] = False
    return _adapter_auth_code(status)


def _poll_flashforge(printer, base, ctx):
    """Read-only status. The serial is a regular field, the check code is the
    stored secret; neither is returned. Writes stay closed in the driver until a
    model is verified on hardware, which is a card reason, not a poll failure."""
    status = dict(_pf_flashforge.poll(
        base, ctx, printer.get("apikey", ""),
        serial_number=str(printer.get("serial") or "")))
    status["upload_ready"] = status.get("upload_ready") is True
    status["can_control"] = False
    status["can_start"] = False
    return _adapter_auth_code(status)


def persist_duet_protocol(printer, protocol):
    """Pin the protocol seen on the first good poll (no revision bump: it is not
    part of the target identity, only a guard against silent route changes)."""
    if (protocol not in _DUET_PROTOCOLS or not printer.get("target_uuid")
            or _duet_pin(printer)):
        return False
    with _STORE_LOCK:
        manual = load_manual_printers()
        for entry in manual:
            if (entry.get("target_uuid") == printer.get("target_uuid")
                    and entry.get("owner_scope_uuid") == printer.get("owner_scope_uuid")
                    and entry.get("binding_revision") == printer.get("binding_revision")
                    and entry.get("type") == printer.get("type")
                    and normalize_url(entry.get("url")) == normalize_url(printer.get("url"))
                    and not _duet_pin(entry)):
                entry["protocol"] = protocol
                return save_manual_printers(manual)
    return False


def _poll_backend(kind, printer, base, ctx):
    if kind == "moonraker":
        return poll_moonraker(base, ctx, moonraker_headers(printer))
    if kind == "octoprint":
        return poll_octoprint(base, ctx, printer.get("apikey", ""))
    if kind == "repetier":
        return _adapter_auth_code(_pf_repetier.poll(base, ctx, printer.get("apikey", ""),
                                 slug=printer.get("repetier_slug", "")))
    if kind == "duet":
        return _adapter_auth_code(dict(_pf_duet.poll(
            base, ctx, printer.get("apikey", ""), backend=_duet_pin(printer))))
    if kind == "crealityprint":
        return _poll_creality(printer, base)
    if kind == "flashforge":
        return _poll_flashforge(printer, base, ctx)
    if kind in _ADAPTER_BACKENDS:
        return _ADAPTER_BACKENDS[kind].poll(base, ctx, printer.get("apikey", ""))
    raise ValueError("unsupported pinned backend")


STATUS_FRESHNESS_SECONDS = 30.0


def _status_device_health(state):
    value = str(state or "").strip().casefold()
    if not value or value == "unknown":
        return "unknown"
    if "disconnect" in value or value in {"offline", "not connected"}:
        return "disconnected"
    if "error" in value or "fail" in value or value in {"shutdown", "halted"}:
        return "error"
    return "healthy"


def _finalize_status(status, backend, *, authorization=None, transport_online=None):
    result = dict(status or {})
    online = result.get("online")
    auth_denied = result.get("error_code") == "authorization_denied"
    if auth_denied:
        online = True
    if transport_online is None:
        transport_online = True if auth_denied else online
    result["online"] = online
    result["transport_online"] = transport_online
    result["backend"] = backend
    result.setdefault("supported", backend in _ACTIVE_BACKENDS)
    result.setdefault("job_state", None)
    result["device_state"] = result.get("state")
    result["device_health"] = _status_device_health(result.get("state"))
    if authorization is None:
        authorization = result.get("authorization") or (
            "rejected" if auth_denied else "accepted" if online is True else "unknown"
        )
    result["authorization"] = authorization
    result["observed_at"] = time.time()
    result["freshness"] = "fresh" if online is True else "unknown"
    if result.get("error") or result.get("error_code"):
        result["error"] = ui_text("genericError")
        result.setdefault("error_code", "request_failed")
    else:
        result["error"] = ""
    return result


def _poll_failure_status(backend, exc):
    code, _safe_message = safe_transport_error(exc)
    auth_denied = code == "authorization_denied"
    return _finalize_status({
        "online": True if auth_denied else False,
        "state": "error" if auth_denied else "offline",
        "error": ui_text("genericError"),
        "error_code": code,
        "nozzle": None,
        "target_nozzle": None,
        "bed": None,
        "target_bed": None,
        "progress": None,
        "filename": "",
        "thumbnail": "",
    }, backend, authorization="rejected" if auth_denied else "unknown",
       transport_online=True if auth_denied else False)


def status_is_fresh(status, now=None):
    if not isinstance(status, dict):
        return False
    observed = status.get("observed_at")
    if isinstance(observed, bool) or not isinstance(observed, (int, float)):
        return False
    age = (time.time() if now is None else now) - observed
    return 0 <= age <= STATUS_FRESHNESS_SECONDS and status.get("freshness") == "fresh"


def poll_printer(printer):
    original_printer = printer
    try:
        printer = hydrate_credentials(printer)
    except _pf_credentials.Unavailable:
        return _finalize_status({"online": None, "state": "unavailable",
            "error": ui_text("credentialUnavailable"), "error_code": "credential_unavailable"},
            backend_for_type(printer.get("type")), authorization="unknown", transport_online=None)
    base = normalize_url(printer.get("url"))
    kind = (printer.get("type") or "moonraker").lower()
    backend = backend_for_type(kind)
    if backend == "unavailable":
        return _finalize_status({
            "online": None,
            "state": "not available yet",
            "supported": False,
            "error": ui_text("integrationUnavailable"),
            "error_code": "unsupported_backend",
            "nozzle": None,
            "target_nozzle": None,
            "bed": None,
            "target_bed": None,
            "progress": None,
            "filename": "",
            "thumbnail": "",
        }, backend, authorization="unknown", transport_online=None)
    try:
        if backend == "bambu":
            had_serial = bool((printer.get("serial") or "").strip())
            printer.pop("_seen_verified_serial", None)
            status = poll_bambu(printer)
            _copy_bambu_observations(printer, original_printer)
            verified = printer.pop("_seen_verified_serial", None)
            serial = (printer.get("serial") or "").strip()
            if verified and verified.casefold() == serial.casefold() and not had_serial:
                persist_discovered_serial(printer)
                # Serial discovery advances the card binding. Model evidence from
                # this same authenticated report belongs to that updated card.
                with _STORE_LOCK:
                    refreshed = next((card for card in load_manual_printers()
                        if card.get("target_uuid") == printer.get("target_uuid")
                        and card.get("owner_scope_uuid") == printer.get("owner_scope_uuid")
                        and card.get("serial") == serial), None)
                if refreshed:
                    for key in ("binding_revision", "server_uuid", "serial_source", "serial_value",
                                "serial_endpoint", "serial_binding_revision"):
                        if key in refreshed:
                            printer[key] = refreshed[key]
                            original_printer[key] = refreshed[key]
            elif verified and verified.casefold() == serial.casefold():
                persist_verified_bambu_serial(printer)
            seen = printer.pop("_seen_model_code", "")
            if seen and (printer.get("serial") or "").strip():
                persist_model_code(printer, seen)
        else:
            status = _poll_backend(backend, printer, base, ssl_ctx_for(printer))
            if backend == "duet" and status.get("online") is True:
                protocol = status.get("protocol")
                if not _duet_pin(printer) and protocol in _DUET_PROTOCOLS:
                    persist_duet_protocol(printer, protocol)
                    printer["protocol"] = protocol
        return _finalize_status(status, backend)
    except Exception as exc:
        return _poll_failure_status(backend, exc)


def status_is_busy(status):
    state = str((status or {}).get("state") or "").lower()
    return "paus" in state or "print" in state or state == "running"


def status_can_start(status, printer=None):
    if (not isinstance(status, dict) or status.get("online") is not True
            or status.get("transport_online") is not True
            or status.get("authorization") != "accepted"
            or status.get("device_health") != "healthy"
            or not status_is_fresh(status)):
        return False
    state = str(status.get("state") or "").casefold()
    backend = str(status.get("backend") or "").casefold()
    if not _status_target_matches(status, printer):
        return False
    if backend in _ADAPTER_BACKENDS and backend not in _START_CAPABLE_ADAPTERS:
        return False
    if backend in {"duet", "repetier"} and (status.get("can_start") is not True
                              or status.get("upload_ready") is not True):
        return False
    if printer is not None and backend != backend_for_type(printer.get("type")):
        return False
    if backend == "octoprint" and str(status.get("job_state") or "").casefold() != "operational":
        return False
    if backend == "moonraker" and str(status.get("job_state") or "").casefold() != "standby":
        return False
    return state in {
        "moonraker": {"standby"},
        "octoprint": {"operational"},
        # A finished Bambu job is a normal state for the next one. It does not
        # prove the bed is clear, so the bed confirmation stays mandatory.
        "bambu": {"idle", "finished"},
        "duet": {"idle"},
        "repetier": {"idle"},
    }.get(backend, set())


def start_compatibility(printer, artifact, plate_entry):
    """Decide whether this exact file may be started on this printer.

    Returns ``(verdict, reason_key)`` where verdict is ``"compatible"``,
    ``"incompatible"`` or ``"unknown"``; reason_key is a UI catalog key.

    * incompatible: a hard mismatch we can prove (unsliced/project 3MF, a plate
      that is not in the file, a file kind the connection cannot start, a
      connection that cannot start prints yet).
    * compatible: the file's printer profile (``printer_settings_id``) is exactly
      the OrcaSlicer preset this card was adopted from. The adopted record keeps
      the non-editable ``discovery_ref`` hash of (preset name, address, type), so
      renaming the card never changes the verdict.
    * unknown: everything else. The operator must confirm the file was sliced
      for this printer; nothing is guessed from names.
    """
    printer = printer if isinstance(printer, dict) else {}
    backend = backend_for_type(printer.get("type"))
    if backend in _ADAPTER_BACKENDS and backend not in _START_CAPABLE_ADAPTERS:
        return "incompatible", ("startReasonUnsupported" if backend == "crealityprint"
                                else "startReasonNoStart")
    if backend not in {"moonraker", "octoprint", "bambu"} | _START_CAPABLE_ADAPTERS:
        return "incompatible", "startReasonUnsupported"
    manifest = getattr(artifact, "manifest", None) or {}
    kind = manifest.get("kind")
    plates = tuple(getattr(artifact, "plate_entries", ()) or ())
    if kind == "sliced_3mf":
        if not plates:
            return "incompatible", "startReasonProject"
        if plate_entry not in plates:
            return "incompatible", "startReasonPlate"
    elif kind == "gcode":
        if plate_entry:
            return "incompatible", "startReasonPlate"
        if backend == "bambu":
            return "incompatible", "startReasonSlicedOnly"
    else:
        return "incompatible", "startReasonFileKind"
    metadata = getattr(artifact, "metadata", None) or {}
    if backend == "repetier":
        # A preset name can be reused after changing its server or printer.
        # Only identity captured from the exact slice can establish a match.
        if (metadata.get("host_type") != "repetier"
                or not _pf_repetier.valid_slug(metadata.get("printhost_port"))
                or metadata["printhost_port"] != printer.get("repetier_slug")
                or not isinstance(metadata.get("print_host"), str)
                or not metadata["print_host"]
                or normalize_url(metadata["print_host"]) != normalize_url(printer.get("url"))):
            return "unknown", "startReasonProfileUnknown"
    profile = metadata.get("printer_settings_id")
    reference = printer.get("discovery_ref")
    if (isinstance(profile, str) and profile and printer.get("source") == "orca"
            and isinstance(reference, str)
            and reference == _orca_discovery_ref(
                profile, printer.get("url"), normalize_host_type(printer.get("type")),
                printer.get("repetier_slug", ""))):
        return "compatible", "startReasonProfileMatch"
    return "unknown", "startReasonProfileUnknown"


def _unassigned_compatibility(printer, artifact):
    """Verdict for a file linked to no printer: never better than "unknown".

    The operator must always confirm the file was sliced for the chosen printer.
    """
    verdict, reason = start_compatibility(printer, artifact, None)
    if verdict == "compatible":
        return "unknown", "startReasonProfileUnknown"
    return verdict, reason


def outbox_target_allowed(record, printer, printers, explicit=False):
    """May ``record`` be sent to ``printer``?

    Normal files: only the printer the record is bound to (outbox_target_match).
    explicit=True is the one-off send of a file linked to no printer: the target
    is the user's choice (already resolved by its exact persisted binding), only
    for an unbound record stored under ``unassigned/``. Nothing is re-linked.
    """
    if not isinstance(record, dict) or printer is None:
        return False
    if explicit:
        stored = record.get("file")
        return (isinstance(stored, str) and stored.startswith("unassigned/")
                and not record.get("target_uuid")
                and any(item is printer for item in printers))
    return outbox_target_match(record, printers) is printer


def _status_target_matches(status, printer):
    if printer is None or status.get("backend") != "repetier":
        return True
    return (status.get("repetier_slug") == printer.get("repetier_slug")
            and normalize_url(status.get("repetier_url")) == normalize_url(printer.get("url"))
            and printer.get("target_selector") == repetier_target_selector(printer))


def _repetier_write_guard(printer):
    """Freeze runtime, page and persisted target through the adapter's reads."""
    root, epoch = PLUGIN_STORAGE_DIR, _PLUGIN_RUNTIME_EPOCH
    dashboard = getattr(_SCHEDULER_ACTION_CONTEXT, "dashboard", None)
    page_epoch = getattr(dashboard, "_credential_page_epoch", None)
    keys = ("owner_scope_uuid", "target_uuid", "target_selector", "binding_revision",
            "url", "type", "repetier_slug", "credential_refs")
    binding = {key: printer.get(key) for key in keys}
    def is_current():
        if (not _credential_work_is_active() or root != PLUGIN_STORAGE_DIR
                or epoch != _PLUGIN_RUNTIME_EPOCH
                or dashboard is not None and page_epoch != dashboard._credential_page_epoch):
            return False
        with _STORE_LOCK:
            matches = [card for card in load_manual_printers()
                       if card.get("target_uuid") == binding["target_uuid"]
                       and card.get("owner_scope_uuid") == binding["owner_scope_uuid"]]
            return len(matches) == 1 and all(matches[0].get(key) == binding[key] for key in keys)
    return is_current


def status_can_upload(status, printer=None):
    if (not isinstance(status, dict) or status.get("online") is not True
            or status.get("transport_online") is not True
            or status.get("authorization") != "accepted"
            or status.get("device_health") != "healthy"
            or not status_is_fresh(status)):
        return False
    backend = str(status.get("backend") or "").casefold()
    if not _status_target_matches(status, printer):
        return False
    if backend not in _ACTIVE_BACKENDS:
        return False
    if backend in _ADAPTER_BACKENDS and status.get("upload_ready") is not True:
        return False
    return printer is None or backend == backend_for_type(printer.get("type"))


def status_can_control(status, action, printer=None):
    if (not isinstance(status, dict) or status.get("online") is not True
            or status.get("transport_online") is not True
            or status.get("authorization") != "accepted"
            or status.get("device_health") != "healthy"
            or not status_is_fresh(status)):
        return False
    backend = str(status.get("backend") or "").casefold()
    if not _status_target_matches(status, printer):
        return False
    if ((backend in _ADAPTER_BACKENDS and backend != "repetier")
            or status.get("can_control") is not True and backend == "repetier"
            or status.get("can_control") is False):
        return False
    if printer is not None and backend != backend_for_type(printer.get("type")):
        return False
    state = str(status.get("job_state") or "").casefold()
    if action == "pause":
        return state in {"printing", "running"}
    if action == "resume":
        return state in {"paused", "pause"}
    if action == "cancel":
        return state in {"printing", "running", "paused", "pause"}
    return False


def _legacy_state_digest(root, names):
    digest = hashlib.sha256()
    for name in names:
        path = os.path.join(root, name)
        if not os.path.lexists(path):
            continue
        digest.update(name.encode("utf-8"))
        if os.path.islink(path):
            raise ValueError("symlink in legacy state")
        if os.path.isdir(path):
            for current, directories, files in os.walk(path, followlinks=False):
                if any(os.path.islink(os.path.join(current, item)) for item in directories + files):
                    raise ValueError("symlink in legacy state")
                for filename in sorted(files):
                    item_path = os.path.join(current, filename)
                    relative = os.path.relpath(item_path, root).replace(os.sep, "/")
                    digest.update(relative.encode("utf-8"))
                    with open(item_path, "rb") as stream:
                        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                            digest.update(chunk)
        else:
            with open(path, "rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
    return digest.hexdigest()


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
_HISTORY_STORE_DAMAGED = False
_HISTORY_ACTIVE_STATES = {"printing", "paused"}
_HISTORY_TERMINAL_STATES = {"completed", "failed", "cancelled", "ended"}
# "sent": OrcaSlicer reported that a file went to a printer. It is not a print.
_HISTORY_KNOWN_STATES = _HISTORY_ACTIVE_STATES | _HISTORY_TERMINAL_STATES | {"sent"}
_HISTORY_SOURCES = {"print_farm", "orca"}


def printer_history_key(printer):
    target_uuid = str((printer or {}).get("target_uuid") or "").strip()
    if target_uuid:
        owner_scope = str((printer or {}).get("owner_scope_uuid") or "").strip()
        return "target:%s:%s" % (owner_scope, target_uuid)
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
    record["target_uuid"] = str(record.get("target_uuid") or "")[:80]
    record["printer_name"] = str(record.get("printer_name") or "")[:160]
    record["backend"] = str(record.get("backend") or "")[:40].lower()
    record["filename"] = _history_filename(record.get("filename"))
    state = str(record.get("state") or "").lower()
    record["state"] = state if state in _HISTORY_KNOWN_STATES else "ended"
    record["source"] = (
        record["source"] if record.get("source") in _HISTORY_SOURCES else "observed"
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
    global _HISTORY_STORE_DAMAGED
    if _STORAGE_DENIED:
        _HISTORY_STORE_DAMAGED = True
        return []
    try:
        present = _optional_regular_file(PRINT_HISTORY_FILE)
    except OSError:
        _HISTORY_STORE_DAMAGED = True
        return []
    if not present:
        if os.path.lexists(PRINT_HISTORY_FILE):
            _HISTORY_STORE_DAMAGED = True
            return []
        _HISTORY_STORE_DAMAGED = False
        return []
    try:
        with open(PRINT_HISTORY_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        _HISTORY_STORE_DAMAGED = True
        return []
    if not isinstance(data, list) or not all(
        isinstance(item, dict) and item.get("id") for item in data
    ):
        _HISTORY_STORE_DAMAGED = True
        return []
    _HISTORY_STORE_DAMAGED = False
    records = [_normalized_history_record(item) for item in data]
    records.sort(key=lambda item: _history_int(item.get("started_at")), reverse=True)
    return records[:PRINT_HISTORY_MAX]


def save_print_history(records):
    if _STORAGE_DENIED or _HISTORY_STORE_DAMAGED:
        return False
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
        "target_uuid": str((printer or {}).get("target_uuid") or "")[:80],
        "owner_scope_uuid": str((printer or {}).get("owner_scope_uuid") or "")[:80],
        "binding_revision": _history_int(
            (printer or {}).get("binding_revision"), 0, minimum=0
        ),
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
    already_finished = (
        record.get("state") in _HISTORY_TERMINAL_STATES
        and record.get("finished_at") is not None
    )
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
    previous_duration = _history_int(record.get("duration_seconds"), minimum=0)
    duration = previous_duration if already_finished else min(
        PRINT_HISTORY_MAX_DURATION_SECONDS,
        max(previous_duration, elapsed or 0, observed_duration),
    )
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
    finished_at = (
        record.get("finished_at") if already_finished else
        now if state in _HISTORY_TERMINAL_STATES else None
    )
    if record.get("finished_at") != finished_at:
        record["finished_at"] = finished_at
        changed = True
    if changed:
        record["updated_at"] = now
    return changed


def _history_digest(value):
    """A SHA-256 hex digest, or None. It is the only link to a stored file."""
    if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value):
        return value
    return None


def record_print_started(printer, filename, now=None, sha256=None):
    """Persist a job immediately after a backend accepts a start request.

    ``sha256`` is the digest of the exact file Print Farm sent. It lets the
    journal offer that stored copy again; it is never guessed afterwards."""
    if not history_recording_enabled():
        return None
    now = int(time.time()) if now is None else int(now)
    key = printer_history_key(printer)
    with _HISTORY_LOCK:
        records = load_print_history()
        for item in records:
            if item.get("printer_key") == key and item.get("state") in _HISTORY_ACTIVE_STATES:
                _update_history_record(item, {"filename": filename}, "ended", now)
        record = _new_history_record(printer, filename, now, "print_farm")
        if _history_digest(sha256):
            record["sha256"] = sha256
        records.insert(0, record)
        save_print_history(records)
    return records[0]


def record_file_sent(printer, filename, now=None, sha256=None):
    """Journal a file Print Farm uploaded without starting it."""
    if not history_recording_enabled():
        return None
    now = int(time.time()) if now is None else int(now)
    with _HISTORY_LOCK:
        records = load_print_history()
        record = _new_history_record(printer, filename, now, "print_farm")
        record["state"] = "sent"
        record["finished_at"] = now
        if _history_digest(sha256):
            record["sha256"] = sha256
        records.insert(0, record)
        save_print_history(records)
    return record


def record_print_observation(printer, status, now=None):
    """Reconcile one live status into the bounded local history store."""
    state = _history_state(status)
    if state is None or not history_recording_enabled():
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
                state in {"idle", "completed"}
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


def _public_history_record(record, printer_name=None, stored_file=None):
    """Return only the fields required by the history UI.

    ``stored_file`` is the outbox name of the copy whose digest this record
    carries; the digest itself stays private."""
    source = record if isinstance(record, dict) else {}
    name = printer_name if printer_name is not None else source.get("printer_name")
    public = _public_history_fields(source, name)
    if isinstance(stored_file, str) and stored_file:
        public["file"] = stored_file
    return public


def _public_history_fields(source, name):
    return {
        "filename": _history_filename(source.get("filename")),
        "printer_name": str(name or "")[:160],
        "state": source.get("state") if source.get("state") in
            _HISTORY_KNOWN_STATES else "ended",
        "started_at": _history_int(source.get("started_at"), minimum=0),
        "updated_at": _history_int(source.get("updated_at"), minimum=0),
        "finished_at": (None if source.get("finished_at") is None else
                         _history_int(source.get("finished_at"), minimum=0)),
        "duration_seconds": _history_int(
            source.get("duration_seconds"), minimum=0,
            maximum=PRINT_HISTORY_MAX_DURATION_SECONDS,
        ),
        "progress": (None if source.get("progress") is None else
                     _history_int(source.get("progress"), None, minimum=0, maximum=100)),
        "source": source["source"] if source.get("source") in _HISTORY_SOURCES else "observed",
    }


def _post_json(url, ctx, headers=None, payload=None, timeout=HTTP_TIMEOUT):
    body = json.dumps(payload if payload is not None else {}).encode("utf-8")
    hdrs = {"Content-Type": "application/json"}
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=body, headers=hdrs, method="POST")
    with _urlopen_no_redirect(req, timeout=timeout, context=ctx) as resp:
        return resp.getcode()


# Bambu names the cancel command "stop"; the two HTTP backends call it cancel.
_BAMBU_CONTROL = {"pause": "pause", "resume": "resume", "cancel": "stop"}
# Moonraker holds the HTTP response until the PAUSE/CANCEL macro finishes, and
# Klipper macros (Happy Hare parks and may home first) easily outlive a normal
# request timeout — don't report a command failed while it is still running.
CONTROL_TIMEOUT = 60


def printer_command(printer, action, status=None):
    """Pause/resume/cancel the current print on whichever backend the printer
    speaks. Returns {ok} or {ok: False, error}."""
    try:
        printer = hydrate_credentials(printer)
    except _pf_credentials.Unavailable:
        return credential_operation_error()
    if action not in _BAMBU_CONTROL:
        return operation_error("errorUnknownAction")
    if not status_can_control(status, action, printer):
        return {"ok": False, "error_key": "genericError", "error_values": {},
                "error_code": "not_ready"}
    base = normalize_url(printer.get("url"))
    ctx = ssl_ctx_for(printer)
    kind = backend_for_type(printer.get("type") or "moonraker")
    try:
        if kind == "repetier":
            raw = _pf_repetier.control(base, ctx, printer.get("apikey", ""), action,
                                       slug=printer.get("repetier_slug", ""),
                                       is_current=_repetier_write_guard(printer))
            return raw if raw.get("ok") else _adapter_failure(raw, "errorPrintCommand")
        if kind == "moonraker":
            _post_json(base + "/printer/print/" + ("cancel" if action == "cancel" else action), ctx,
                       moonraker_headers(printer), timeout=CONTROL_TIMEOUT)
        elif kind == "octoprint":
            headers = {}
            if printer.get("apikey"):
                headers["X-Api-Key"] = printer["apikey"]
            payload = {"command": "cancel"} if action == "cancel" else {"command": "pause", "action": action}
            _post_json(base + "/api/job", ctx, headers, payload, timeout=CONTROL_TIMEOUT)
        elif kind == "bambu":
            host = urllib.parse.urlparse(base).hostname or ""
            code = printer.get("access_code") or printer.get("apikey") or ""
            serial = (printer.get("serial") or "").strip()
            if not host or not code or not serial:
                return operation_error("errorNeedsBambuCredentials")
            err = _bambu_publish(host, code, "device/%s/request" % serial,
                                 {"print": {"sequence_id": "0", "command": _BAMBU_CONTROL[action]}},
                                 printer=printer)
            if err:
                if err == "credential_unavailable":
                    return credential_operation_error()
                if err in _BAMBU_TLS_ERRORS:
                    return {"ok": False, "error_key": _BAMBU_TLS_ERRORS[err],
                            "error_values": {}, "error_code": err,
                            "journal_state": "rejected", "evidence_ref": "tls:pre_dispatch"}
                return transport_operation_error(RuntimeError("printer request failed"), "errorPrintCommand")
        else:
            return operation_error("errorUnsupportedBackend")
        return {"ok": True}
    except Exception as exc:
        return transport_operation_error(exc, "errorPrintCommand")


def send_gcode(printer, filename, file_bytes, start, use_ams=True, status=None):
    try:
        printer = hydrate_credentials(printer)
    except _pf_credentials.Unavailable:
        return credential_operation_error()
    if not status_can_upload(status, printer):
        return {"ok": False, "error_key": "genericError", "error_values": {},
                "error_code": "not_ready"}
    if start and not status_can_start(status, printer):
        return {"ok": False, "error_key": "genericError", "error_values": {},
                "error_code": "not_ready"}
    base = normalize_url(printer.get("url"))
    kind = backend_for_type(printer.get("type") or "moonraker")
    ctx = ssl_ctx_for(printer)
    try:
        if kind == "moonraker":
            fields = {"print": "true"} if start else {}
            _multipart_upload(base + "/server/files/upload", ctx, moonraker_headers(printer), filename, file_bytes, fields)
        elif kind == "octoprint":
            headers = {}
            if printer.get("apikey"):
                headers["X-Api-Key"] = printer["apikey"]
            fields = {"select": "true", "print": "true"} if start else {}
            _multipart_upload(base + "/api/files/local", ctx, headers, filename, file_bytes, fields)
        elif kind == "bambu":
            return operation_error("errorUnsupportedBackend")
        else:
            return operation_error("errorUnsupportedBackend")
        return {"ok": True}
    except Exception as exc:
        return transport_operation_error(exc, "errorUpload")


def _bambu_send_file(printer, filename, file_path, start, use_ams, plate_entry):
    with _bambu_target_lock(printer):
        return _bambu_send_file_locked(printer, filename, file_path, start,
                                       use_ams, plate_entry)


def _bambu_send_file_locked(printer, filename, file_path, start, use_ams, plate_entry):
    try:
        printer = hydrate_credentials(printer)
    except _pf_credentials.Unavailable:
        return credential_operation_error()
    base = normalize_url(printer.get("url"))
    host = urllib.parse.urlparse(base).hostname or ""
    code = printer.get("access_code") or printer.get("apikey") or ""
    serial = (printer.get("serial") or "").strip()
    if not host or not code or (start and not serial):
        return operation_error("errorNeedsBambuCredentials")
    try:
        _bambu_pin(printer, "ftps")
    except BambuTlsError as exc:
        return {"ok": False, "error_key": _BAMBU_TLS_ERRORS[exc.code],
                "error_values": {}, "error_code": exc.code,
                "journal_state": "rejected", "evidence_ref": "tls:pre_dispatch"}
    extension = ".3mf" if filename.lower().endswith(".3mf") else ".gcode"
    remote = "pf-" + uuid.uuid4().hex + extension
    ftp = _implicit_ftps_class()(context=ssl_ctx_for(printer))
    ftp._bambu_printer = printer
    store_attempted = False
    control_trusted = False
    try:
        ftp.connect(host, 990, timeout=UPLOAD_TIMEOUT)
        control_trusted = True
        ftp.login("bblp", code)
        ftp.prot_p()
        with open(file_path, "rb") as stream:
            store_attempted = True
            ftp.storbinary("STOR " + remote, stream, blocksize=64 * 1024)
    except BambuTlsError as exc:
        result = {"ok": False, "error_key": _BAMBU_TLS_ERRORS[exc.code],
                  "error_values": {}, "error_code": (
                      "unknown_outcome" if store_attempted else exc.code)}
        if not store_attempted:
            result.update(journal_state="rejected", evidence_ref="tls:pre_dispatch")
        return result
    except Exception:
        return {"ok": False, "error_key": "errorUpload", "error_values": {},
                "error_code": "unknown_outcome"}
    finally:
        try:
            if control_trusted:
                ftp.quit()
            else:
                ftp.close()
        except Exception:
            ftp.close()
    if start:
        result = bambu_start_print(host, code, serial, remote, use_ams, plate_entry,
                                   printer=printer)
        if result:
            if result in _BAMBU_TLS_ERRORS:
                return {"ok": False, "error_key": _BAMBU_TLS_ERRORS[result],
                        "error_values": {}, "error_code": "unknown_outcome"}
            return {"ok": False, "error_key": "errorPrintCommand", "error_values": {},
                    "error_code": "unknown_outcome"}
    return {"ok": True, "evidence_ref": "ftps:complete"}


# Fixed, English, secret-free reasons for adapter result codes (same style as
# safe_transport_error); adapter text is never shown to the user as-is.
_ADAPTER_ERROR_LABELS = {
    "authorization_denied": "authentication rejected",
    "device_not_idle": "printer is not idle",
    "device_busy": "printer is busy",
    "printer_busy": "printer is busy",
    "protocol_mismatch": "printer protocol changed since it was added; save the printer again",
    "status_unavailable": "printer status is unavailable",
    "upload_rejected": "printer rejected the upload",
    "start_rejected": "printer rejected the start command",
    "verification_failed": "uploaded file did not pass the size check",
    "file_not_found": "file is not on the printer",
    "file_unverified": "file could not be checked on the printer",
    "file_size_mismatch": "file on the printer has a different size",
    "start_name_invalid": "file name cannot be started (use ASCII letters, digits and . _ ( ) - only)",
    "upload_unverified": "upload could not be verified",
    "invalid_request": "request was not sent",
    "invalid_filename": "file name is not accepted by this printer",
    "timeout": "printer request timed out",
    "connection_failed": "printer could not be reached",
    "redirect_rejected": "printer redirected the request",
    "no_sessions_available": "printer has no free sessions",
    "dcs_incompatible": "printer software is not compatible",
    "unsupported_unverified": "not verified on hardware",
    "writable_target_unverified": "not verified on hardware",
    "unknown_outcome": "printer may or may not have received the request",
    "lan_mode_disabled": "LAN mode is off on the printer",
    "unsupported_model": "printer model is not supported",
    "identity_unverified": "printer model could not be verified",
    "credentials_required": "serial number and check code are required",
}
_ADAPTER_NOT_SENT = frozenset({"not_sent", "rejected"})


def _adapter_failure(raw, key, *, uploaded=False):
    """Plugin result for a failed adapter write, with journal outcome attached."""
    raw = raw if isinstance(raw, dict) else {}
    code = str(raw.get("error_code") or "request_failed")
    if code in _ADAPTER_AUTH_CODES:
        code = "authorization_denied"
    outcome = raw.get("outcome")
    result = {"error_code": code, "ok": False, "outcome": outcome}
    label = _ADAPTER_ERROR_LABELS.get(code, "printer request failed")
    if outcome in _ADAPTER_NOT_SENT:
        # Nothing was applied: a definitive, evidence-backed rejection.
        result["journal_state"] = "rejected"
        result["evidence_ref"] = "adapter:" + re.sub(r"[^A-Za-z0-9_]", "_", code)[:60]
    else:
        result["journal_state"] = "unknown_outcome"
        if outcome == "unknown" or code == "unknown_outcome":
            key, label = "errorOutcomeUnknown", ""
    if uploaded and key != "errorOutcomeUnknown":
        key = "errorUploadedNotStarted"
    result["error_key"] = key
    result["error_values"] = {"error": label} if label else {}
    result["uploaded"] = bool(uploaded)
    return result


def _adapter_not_sent(code, key):
    return _adapter_failure({"error_code": code, "outcome": "not_sent"}, key)


def _duet_send(printer, base, filename, file_path, start, status):
    """Upload (verified by size) and, when asked, start with the same pin.

    One request per step, never repeated. The start is only attempted after an
    upload that was verified on the printer; the second call re-checks idle.
    """
    password = printer.get("apikey", "")
    ctx = ssl_ctx_for(printer)
    pin = _duet_pin(printer)
    live = _duet_pin({"protocol": (status or {}).get("protocol")})
    if pin and live and pin != live:
        return _adapter_not_sent("protocol_mismatch", "errorUpload")
    pin = pin or live
    if start:
        try:
            _pf_duet._validate_start_name(filename)
        except ValueError:
            return _adapter_not_sent("start_name_invalid", "errorPrintCommand")
    up = _pf_duet.upload(base, ctx, password, file_path, filename, backend=pin)
    if not up.get("ok"):
        return _adapter_failure(up, "errorUpload")
    protocol = up.get("protocol") if up.get("protocol") in _DUET_PROTOCOLS else pin
    verified = up.get("verified") is True
    if not start:
        result = {"ok": True, "verified": verified, "protocol": protocol,
                  "evidence_ref": "duet:%s:%s" % (protocol or "unknown",
                                                  "size_match" if verified else "unverified")}
        if verified:
            result["journal_state"] = "verified"
        return result
    if not verified:
        failure = _adapter_not_sent("upload_unverified", "errorUploadUnverified")
        failure["uploaded"] = True
        return failure
    started = _pf_duet.start(base, ctx, password, filename, backend=protocol,
                             expected_size=up.get("size"))
    if not started.get("ok"):
        return _adapter_failure(started, "errorPrintCommand", uploaded=True)
    return {"ok": True, "uploaded": True, "protocol": protocol,
            "evidence_ref": started.get("evidence_ref") or "duet:start_accepted"}


def _creality_send(printer, base, filename, file_path, start):
    if start:
        return _adapter_not_sent("unsupported_unverified", "errorUnsupportedBackend")
    try:
        ws = _pf_creality.ws_base(base, creality_ws_port(printer))
    except ValueError:
        return _adapter_not_sent("invalid_request", "errorUpload")
    raw = _pf_creality.upload(base, ssl_ctx_for(printer), "", file_path, filename,
                              status_base=ws)
    if raw.get("ok"):
        return {"ok": True, "evidence_ref": raw.get("evidence_ref") or "creality:accepted"}
    raw = dict(raw)
    code = raw.get("error_code")
    raw["outcome"] = ("unknown" if code in {"unknown_outcome", "redirect_rejected"}
                      else "not_sent")
    return _adapter_failure(raw, "errorUpload")


def _flashforge_send(printer, base, filename, file_path, start):
    """Upload (never starts by itself) and, when asked, start the uploaded file.

    Both calls stay closed in the driver (unsupported_unverified, nothing sent)
    until a model is verified on hardware; the plugin also refuses earlier, at
    status_can_upload. One request per step, never repeated.
    """
    ctx = ssl_ctx_for(printer)
    secret = printer.get("apikey", "")
    serial = str(printer.get("serial") or "")
    up = _pf_flashforge.upload(base, ctx, secret, file_path, filename, serial_number=serial)
    if not up.get("ok"):
        return _adapter_failure(up, "errorUpload")
    if not start:
        return {"ok": True, "verified": False,
                "evidence_ref": up.get("evidence_ref") or "flashforge:accepted"}
    started = _pf_flashforge.start(base, ctx, secret, filename, serial_number=serial)
    if not started.get("ok"):
        return _adapter_failure(started, "errorPrintCommand", uploaded=True)
    return {"ok": True, "uploaded": True,
            "evidence_ref": started.get("evidence_ref") or "flashforge:start_accepted"}


def send_file_path(printer, filename, file_path, start, use_ams=True, status=None,
                   plate_entry=None):
    """Send an already verified private artifact without reading it into RAM."""
    try:
        printer = hydrate_credentials(printer)
    except _pf_credentials.Unavailable:
        return credential_operation_error()
    if not status_can_upload(status, printer) or (start and not status_can_start(status, printer)):
        return {"ok": False, "error_key": "genericError", "error_values": {},
                "error_code": "not_ready"}
    base = normalize_url(printer.get("url"))
    kind = backend_for_type(printer.get("type") or "moonraker")
    try:
        if kind == "moonraker":
            code = _pf_upload.post_multipart_file(
                base + "/server/files/upload", file_path,
                filename=filename, fields={"print": "true"} if start else {},
                headers=moonraker_headers(printer), timeout=UPLOAD_TIMEOUT,
                ssl_context=ssl_ctx_for(printer),
            )
        elif kind == "octoprint":
            headers = {"X-Api-Key": printer["apikey"]} if printer.get("apikey") else {}
            fields = {"select": "true", "print": "true"} if start else {}
            code = _pf_upload.post_multipart_file(
                base + "/api/files/local", file_path,
                filename=filename, fields=fields, headers=headers,
                timeout=UPLOAD_TIMEOUT, ssl_context=ssl_ctx_for(printer),
            )
        elif kind == "bambu":
            return _bambu_send_file(printer, filename, file_path, start, use_ams, plate_entry)
        elif kind == "repetier":
            raw = _pf_repetier.upload(base, ssl_ctx_for(printer), printer.get("apikey", ""),
                file_path, filename, slug=printer.get("repetier_slug", ""), start=start,
                is_current=_repetier_write_guard(printer))
            return raw if raw.get("ok") else _adapter_failure(raw, "errorUpload")
        elif kind == "duet":
            return _duet_send(printer, base, filename, file_path, start, status)
        elif kind == "crealityprint":
            return _creality_send(printer, base, filename, file_path, start)
        elif kind == "flashforge":
            return _flashforge_send(printer, base, filename, file_path, start)
        elif kind in _ADAPTER_BACKENDS:
            return _ADAPTER_BACKENDS[kind].upload(
                base, ssl_ctx_for(printer), printer.get("apikey", ""), file_path, filename)
        else:
            return operation_error("errorUnsupportedBackend")
        if not 200 <= code < 300:
            result = transport_operation_error(
                urllib.error.HTTPError(base, code, "upload rejected", None, None),
                "errorUpload",
            )
            result["evidence_ref"] = "http:%d" % code
            return result
        return {"ok": True, "evidence_ref": "http:%d" % code}
    except Exception as exc:
        return transport_operation_error(exc, "errorUpload")


# The page — native, host-themed. Tiles, thumbnail preview, per-tile select for
# batch print, and a "Send G-code to selected" action.
# --------------------------------------------------------------------------- #
PAGE = r"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="frame-src 'none'; object-src 'none'; base-uri 'none'">
<style>
  html, body { margin:0; height:100%; }
  body { display:flex; flex-direction:column; background:var(--orca-bg,#1e1e2e);
         color:var(--orca-fg,#e0e0e0); font-family:var(--orca-font,sans-serif); }
  #bar { flex:0 0 auto; display:flex; flex-direction:column; gap:7px; padding:8px 12px;
         border-bottom:1px solid var(--orca-border,#3c3c4c); }
  .bar-main, .manual-form { display:flex; align-items:center; gap:7px; min-width:0; }
  .bar-main { width:100%; }
  #bar input:not([type="checkbox"]), #bar select { font:inherit; font-size:12px; padding:4px 8px;
         background:var(--orca-bg,#1e1e2e); color:var(--orca-fg,#e0e0e0);
         border:1px solid var(--orca-border,#3c3c4c); border-radius:4px; }
  .select-wrap { position:relative; display:flex; align-items:center; min-width:0; }
  .select-wrap select { appearance:none; width:100%; padding-right:27px !important; }
  .select-wrap .disclosure-icon { position:absolute; right:9px; pointer-events:none; }
  #adopt-wrap { flex:0 1 220px; min-width:170px; max-width:260px; }
  #type-wrap { flex:0 1 190px; min-width:150px; }
  #status { flex:1 1 auto; min-width:40px; overflow:hidden; text-overflow:ellipsis;
            white-space:nowrap; }
  .manual-form { display:none; justify-content:flex-end; flex-wrap:wrap; padding-top:7px;
                 border-top:1px solid var(--orca-border,#3c3c4c); }
  .manual-form.open { display:flex; }
  .manual-form #f-url { flex:0 1 160px; min-width:140px; box-sizing:border-box; }
  .manual-form #f-name { flex:0 1 150px; min-width:90px; box-sizing:border-box; }
  .compact-option { display:flex; align-items:center; gap:5px; min-height:24px;
                    box-sizing:border-box; padding:3px 7px; border-radius:4px;
                    border:1px solid var(--orca-border,#3c3c4c); cursor:pointer;
                    color:var(--orca-muted,#a0a0a0); font-size:10px; white-space:nowrap; }
  .compact-option input { margin:0; }
  .disclosure-icon { width:11px; height:7px; flex:0 0 auto;
                     transition:transform 120ms ease; }
  #manual-toggle[aria-expanded="true"] .disclosure-icon { transform:rotate(180deg); }
  button { font:inherit; font-size:12px; cursor:pointer; padding:4px 12px; border-radius:4px;
           background:var(--orca-accent,#009688); color:var(--orca-accent-fg,#fff);
           border:1px solid var(--orca-accent,#009688); }
  button.ghost { background:transparent; color:var(--orca-fg,#e0e0e0); border-color:var(--orca-border,#3c3c4c); }
  button.danger { background:transparent; color:#c0504d; border-color:#c0504d; }
  button:disabled { opacity:.5; cursor:default; }
  #grid { flex:1 1 auto; overflow:auto; padding:12px;
          display:grid; grid-template-columns:repeat(auto-fill,minmax(310px,1fr));
          gap:12px; align-content:start; align-items:start; }
  #grid, .queue-scroll, .history-list { scrollbar-width:thin;
          scrollbar-color:var(--orca-border,#555565) transparent; }
  #grid::-webkit-scrollbar, .queue-scroll::-webkit-scrollbar,
  .history-list::-webkit-scrollbar { width:10px; height:10px; }
  #grid::-webkit-scrollbar-track, .queue-scroll::-webkit-scrollbar-track,
  .history-list::-webkit-scrollbar-track { background:transparent; }
  #grid::-webkit-scrollbar-thumb, .queue-scroll::-webkit-scrollbar-thumb,
  .history-list::-webkit-scrollbar-thumb {
    background:var(--orca-border,#555565); border:2px solid transparent;
    background-clip:padding-box; border-radius:10px; }
  #grid::-webkit-scrollbar-thumb:hover, .queue-scroll::-webkit-scrollbar-thumb:hover,
  .history-list::-webkit-scrollbar-thumb:hover { background:var(--orca-muted,#a0a0a0); }
  .tile { width:100%; max-width:400px; box-sizing:border-box;
          border:1px solid var(--orca-border,#3c3c4c); border-radius:8px; padding:12px;
          display:flex; flex-direction:column; gap:8px; }
  .tile .top { display:flex; align-items:center; gap:8px; }
  .dot { width:9px; height:9px; border-radius:50%; background:#888; flex:0 0 auto; }
  .dot.on { background:#3fbf6f; } .dot.off { background:#c0504d; }
  .name-wrap { flex:1 1 auto; min-width:0; display:flex; align-items:center; gap:4px; }
  .tile .name { min-width:0; overflow:hidden; text-overflow:ellipsis;
                white-space:nowrap; font-weight:600; }
  .tile .remove { flex:0 0 auto; width:26px; height:26px; padding:5px;
                  color:var(--orca-muted,#a0a0a0); border-color:transparent; }
  .tile .remove:hover, .tile .remove:focus-visible {
                  color:var(--orca-fg,#e0e0e0); border-color:var(--orca-border,#3c3c4c); }
  .tile .remove svg { display:block; width:14px; height:14px; pointer-events:none; }
  .tile .remove.armed { width:auto; min-width:58px; padding:4px 7px;
                        color:#c0504d; border-color:#c0504d; }
  .src { font-size:10px; padding:1px 5px; border-radius:3px; border:1px solid var(--orca-border,#3c3c4c);
         color:var(--orca-muted,#a0a0a0); }
  .tile .state { max-width:120px; font-size:11px; color:var(--orca-muted,#a0a0a0);
                 white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .body { display:flex; gap:10px; }
  .thumb { width:64px; height:64px; flex:0 0 auto; border:1px solid var(--orca-border,#3c3c4c);
           border-radius:6px; object-fit:contain; background:rgba(255,255,255,.04); display:none; }
  .thumb.show { display:block; }
  .meta { flex:1 1 auto; display:flex; flex-direction:column; gap:6px; min-width:0; }
  .temps { display:flex; flex-wrap:wrap; gap:4px 14px; font-size:12px; } .temps b { font-weight:600; }
  .toolmark { font-style:normal; font-size:8px; margin-left:2px; vertical-align:middle;
              color:var(--orca-accent,#009688); }
  .head-temps { display:flex; flex-wrap:wrap; gap:4px; min-width:0; }
  .head-temp { flex:0 0 auto; width:max-content; max-width:100%; padding:3px 6px;
               font-size:11px; white-space:normal; }
  .head-temp[aria-pressed="true"] { outline:1px solid var(--orca-accent,#009688); outline-offset:-1px; }
  .head-temp.actual-active { font-weight:600; }
  .head-detail { font-size:11px; color:var(--orca-muted,#a0a0a0); overflow-wrap:anywhere; }
  .ctl-note { font-size:11px; color:var(--orca-muted,#a0a0a0); }
  .fname { font-size:11px; color:var(--orca-muted,#a0a0a0); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .fname.tls-needed { white-space:normal; color:#d9a441; }
  .ams { display:flex; flex-wrap:wrap; gap:3px 10px; font-size:11px; }
  .ams span { display:inline-flex; align-items:center; gap:4px; }
  .ams i { width:10px; height:10px; border-radius:2px; border:1px solid var(--orca-border,#3c3c4c); background:transparent; }
  .bambu-err { color:#c0504d; }
  .bar { height:6px; border-radius:3px; background:var(--orca-border,#3c3c4c); overflow:hidden; }
  .bar > i { display:block; height:100%; background:var(--orca-accent,#009688); }
  /* Keep print controls and ordinary tile actions on separate rows. Button
     widths follow their localized labels; narrow cards wrap whole actions
     instead of clipping text or overflowing into the next grid column. */
  .tile-actions { display:flex; flex-direction:column; gap:6px; margin-top:auto; min-width:0; }
  .ctl-row { display:flex; flex-wrap:wrap; justify-content:space-between;
             align-items:center; gap:6px; min-height:28px; }
  .row { display:flex; flex-wrap:wrap; justify-content:flex-end;
         gap:6px; align-items:center; min-width:0; }
  .row label { display:flex; align-items:center; gap:4px; font-size:11px; margin-right:auto; }
  .row label.select-control.disabled { color:var(--orca-muted,#a0a0a0);
                                       opacity:.45; cursor:not-allowed; }
  .row label.select-control.disabled input { cursor:not-allowed; }
  .row button { flex:0 0 auto; width:max-content; max-width:100%; white-space:normal;
                padding-left:8px; padding-right:8px; }
  .row .print { min-width:72px; }
  .row .open { min-width:60px; }
  .ctl-slot { display:flex; flex:0 0 auto; flex-wrap:wrap; gap:6px;
              width:auto; min-width:126px; max-width:100%;
              justify-content:flex-end; margin-left:auto; }
  .ctl-slot button { flex:0 0 auto; width:max-content; min-width:60px;
                     max-width:100%; white-space:normal;
                     padding-left:8px; padding-right:8px; }
  .spacer { visibility:hidden; pointer-events:none; }
  .empty { color:var(--orca-muted,#a0a0a0); padding:24px; text-align:center; }
  #status { font-size:11px; color:var(--orca-muted,#a0a0a0); }
  #feedback-corner { flex:0 0 36px; min-height:36px; display:flex; align-items:center;
                     justify-content:flex-end; padding:0 8px; box-sizing:border-box; }
  #feedback { flex:0 0 34px; width:34px; height:34px; padding:8px; border:1px solid transparent;
              border-radius:6px; color:var(--orca-muted,#a0a0a0); background:transparent; }
  #feedback svg { width:16px; height:16px; display:block; }
  #feedback:hover, #feedback:focus-visible { color:var(--orca-fg,#e0e0e0);
    background:var(--orca-border,#3c3c4c); }
  #feedback:focus-visible { outline:2px solid var(--orca-accent,#009688); outline-offset:0; }
  .queue { display:flex; flex-direction:column; gap:4px; min-width:0; }
  .queue-scroll { display:flex; flex-direction:column; gap:4px; min-width:0;
                  max-height:92px; overflow-y:auto; scrollbar-gutter:stable; }
  .queue.expanded .queue-scroll { max-height:316px; }
  .queue-toggle { align-self:center; display:flex; align-items:center; gap:6px;
                  padding:2px 8px; font-size:11px; color:var(--orca-muted,#a0a0a0); }
  .queue-toggle[aria-expanded="true"] .disclosure-icon { transform:rotate(180deg); }
  .qrow { display:flex; align-items:center; gap:6px; font-size:11px;
          height:28px; flex:0 0 28px; box-sizing:border-box; min-width:0;
          border:1px dashed var(--orca-border,#3c3c4c); border-radius:5px; padding:3px 6px; }
  .qrow .qname { margin-right:auto; min-width:0; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .qrow .qmeta { flex:0 1 auto; min-width:0; color:var(--orca-muted,#a0a0a0); white-space:nowrap;
                 overflow:hidden; text-overflow:ellipsis; }
  .qrow .qstatus { color:var(--orca-muted,#a0a0a0); white-space:nowrap; }
  .qrow button { padding:1px 8px; font-size:11px; }
  .storage-warn { color:#c0504d; font-size:11px; }
  .linklike { background:none; border:none; padding:0; font-size:11px; cursor:pointer;
              color:var(--orca-muted,#a0a0a0); text-decoration:underline; }
  .journal-storage { flex-direction:column; align-items:flex-start; gap:6px; }
  .journal-actions { display:flex; flex-wrap:wrap; gap:6px; }
  .journal-actions button { padding:2px 10px; font-size:11px; }
  .journal-picker { display:flex; flex-direction:column; gap:4px; }
  .history-modal { display:none; position:fixed; inset:0; background:rgba(0,0,0,.55);
                   align-items:center; justify-content:center; z-index:25; padding:18px; }
  .history-card { display:flex; flex-direction:column; width:min(800px,100%); max-height:90%;
                  background:var(--orca-bg,#1e1e2e); border:1px solid var(--orca-border,#3c3c4c);
                  border-radius:10px; box-shadow:0 12px 38px rgba(0,0,0,.35); }
  .history-head { display:flex; align-items:center; gap:10px; padding:16px 18px;
                  border-bottom:1px solid var(--orca-border,#3c3c4c); }
  .history-head strong { margin-right:auto; min-width:0; overflow:hidden; text-overflow:ellipsis;
                         white-space:nowrap; font-size:17px; line-height:1.25; }
  .history-note { padding:12px 18px; font-size:11px; line-height:1.5;
                  color:var(--orca-muted,#a0a0a0); }
  .history-list { overflow:auto; padding:0 18px 18px; display:flex; flex-direction:column; gap:8px; }
  .history-item { border:1px solid var(--orca-border,#3c3c4c); border-left:2px solid var(--orca-border,#3c3c4c); border-radius:7px;
                  padding:12px 14px; display:flex; flex-direction:column; gap:7px; }
  .history-item.state-completed { border-left-color:#3fbf6f; }
  .history-item.state-failed { border-left-color:#c0504d; }
  .history-item.state-printing, .history-item.state-paused { border-left-color:var(--orca-accent,#009688); }
  .history-item-top { display:flex; align-items:center; gap:8px; }
  .history-file { margin-right:auto; min-width:0; overflow:hidden; text-overflow:ellipsis;
                  white-space:nowrap; font-size:13px; font-weight:600; }
  .history-state { font-size:11px; padding:3px 8px; border-radius:999px;
                   border:1px solid var(--orca-border,#3c3c4c); white-space:nowrap; }
  .history-item.state-completed .history-state { color:#3fbf6f; border-color:rgba(63,191,111,.45); }
  .history-item.state-failed .history-state { color:#d87370; border-color:rgba(192,80,77,.5); }
  .history-item.state-printing .history-state, .history-item.state-paused .history-state {
    color:var(--orca-accent,#009688); border-color:var(--orca-accent,#009688); }
  .history-meta { color:var(--orca-muted,#a0a0a0); font-size:11px; display:flex;
                   flex-wrap:wrap; gap:5px 14px; }
  .history-toolbar { display:flex; flex-direction:column; gap:10px; padding:0 18px 14px; }
  .history-summary { display:flex; flex-wrap:wrap; gap:8px; }
  .history-stat { display:flex; align-items:baseline; gap:6px; min-width:0; }
  .history-stat span { display:block; padding:7px 11px; border-radius:6px;
                       background:rgba(128,128,128,.09); color:var(--orca-fg,#e0e0e0);
                       font-size:12px; font-weight:600; }
  .history-search { width:100%; box-sizing:border-box; font:inherit; font-size:12px; padding:7px 9px;
                    color:var(--orca-fg,#e0e0e0); background:var(--orca-bg,#1e1e2e);
                    border:1px solid var(--orca-border,#3c3c4c); border-radius:6px; min-height:38px; }
  .history-search:focus-visible, button:focus-visible { outline:2px solid var(--orca-accent,#009688); outline-offset:2px; }
  .history-printer { color:var(--orca-muted,#a0a0a0); font-size:12px; }
  .history-results { color:var(--orca-muted,#a0a0a0); font-size:11px; }
  .row .cam { padding:3px 7px; min-width:0; }
  .row .cam svg { display:block; width:15px; height:15px; pointer-events:none; }
  .cam-card { width:min(720px,100%); }
  .cam-view { display:flex; justify-content:center; padding:0 18px; }
  #cam-img { display:block; max-width:100%; max-height:68vh; background:#000; border-radius:6px; }
  #cam-img:not([src]) { display:none; }
  .cam-actions { padding:0 18px 16px; display:flex; justify-content:flex-end; }
  @media (max-width:900px) {
    #type-wrap { flex-basis:160px; }
    .manual-form #f-apikey { flex:0 1 130px; width:130px; box-sizing:border-box; }
  .manual-form #f-wsport { flex:0 1 96px; width:96px; box-sizing:border-box; }
  }
  @media (max-width:760px) {
    .bar-main, .manual-form { flex-wrap:wrap; }
    #status { order:8; flex-basis:100%; }
    #adopt-wrap { flex:1 1 170px; max-width:none; }
    .history-modal { padding:8px; }
    .history-card { width:100%; max-height:94%; }
    .history-summary { gap:6px; }
    .history-stat { gap:4px; }
    .history-stat span { padding:6px 8px; font-size:11px; }
  }
  @media (max-width:340px) {
    #grid { grid-template-columns:1fr; }
  }
</style></head>
<body>
  <div id="credential-notice" class="history-note" style="display:none" role="status"></div>
  <div id="bar">
    <div class="bar-main">
      <label id="sel-all-l" title="Select or deselect every printer"><input id="sel-all" type="checkbox"> <span id="sel-all-text">all</span></label>
      <button id="send" class="ghost" disabled>Send G-code to selected…</button>
      <button id="journal-open" class="ghost" type="button"></button>
      <span id="storage-warn" class="storage-warn" style="display:none"></span>
      <button id="unassigned-link" class="linklike" type="button" style="display:none"></button>
      <span id="status"></span>
      <span id="adopt-wrap" class="select-wrap" style="display:none">
        <select id="f-adopt" title="Add a printer configured in OrcaSlicer"></select>
        <svg class="disclosure-icon" viewBox="0 0 12 8" aria-hidden="true" focusable="false">
          <path d="M1 1.5 6 6.5 11 1.5" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"></path>
        </svg>
      </span>
      <button id="manual-toggle" class="ghost" type="button" aria-expanded="false">
        <span id="manual-toggle-text">Add</span>
        <svg class="disclosure-icon" viewBox="0 0 12 8" aria-hidden="true" focusable="false">
          <path d="M1 1.5 6 6.5 11 1.5" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"></path>
        </svg>
      </button>
      <button id="refresh" class="ghost" type="button">
        <span aria-hidden="true">↻</span> <span id="refresh-text">Refresh</span>
      </button>
    </div>
    <div id="outbox-hint" class="history-note" style="display:none" role="status">
      <span id="outbox-hint-text"></span> <span id="outbox-hint-where"></span>
    </div>
    <div id="legacy-import" class="history-note" style="display:none">
      <span id="legacy-import-text"></span>
      <button id="legacy-import-button" class="ghost" type="button"></button>
    </div>
    <div id="manual-form" class="manual-form" aria-hidden="true">
      <input id="f-name" placeholder="Name" size="8">
      <input id="f-url" placeholder="192.168.0.42" size="14">
      <input id="f-serial" placeholder="Serial (auto)" size="10" style="display:none" title="Leave empty — the serial is discovered from the printer automatically. Fill only to pin a specific printer. MQTT port 8883 is used.">
      <input id="f-access" placeholder="Access code" size="10" style="display:none">
      <input id="f-apikey" type="password" autocomplete="off" placeholder="OctoPrint API key" size="18" style="display:none">
      <button id="repetier-load" style="display:none"></button>
      <select id="repetier-printer" style="display:none;width:auto;max-width:min(32ch,100%)"></select>
      <input id="f-wsport" type="text" inputmode="numeric" maxlength="5" autocomplete="off" placeholder="WS port (9999)" size="8" style="display:none">
      <label id="f-insecure-l" class="compact-option" title="Check only if the printer is served over HTTPS with a self-signed certificate — this skips TLS verification for it. Plain http:// LAN printers don't need this.">
        <input id="f-insecure" type="checkbox"> <span aria-hidden="true">TLS</span></label>
      <span id="type-wrap" class="select-wrap"><select id="f-type">
__TYPE_OPTIONS__
      </select><svg class="disclosure-icon" viewBox="0 0 12 8" aria-hidden="true" focusable="false">
        <path d="M1 1.5 6 6.5 11 1.5" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"></path>
      </svg></span>
      <button id="add">Add</button>
    </div>
    <input id="file" type="file" accept=".gcode,.gco,.3mf" style="display:none">
  </div>
  <div id="connection-options" class="history-modal" style="display:none;z-index:35">
    <section class="history-card" role="dialog" aria-modal="true" aria-labelledby="connection-options-title" style="width:min(520px,100%);padding:16px;box-sizing:border-box">
      <div id="connection-options-title" style="font-weight:600;margin-bottom:12px"></div>
      <label style="display:flex;flex-direction:column;gap:5px;margin-bottom:12px"><span id="connection-webui-label"></span>
        <input id="connection-webui" type="text" autocomplete="off" style="width:min(48ch,100%);box-sizing:border-box" placeholder="http://192.168.0.42"></label>
      <label style="display:flex;flex-direction:column;gap:5px;margin-bottom:12px"><span id="connection-camera-label"></span>
        <select id="connection-camera" style="width:max-content;max-width:100%"><option value="auto"></option><option value="snapmaker_u1"></option></select></label>
      <p id="connection-options-help" style="font-size:12px;margin:0 0 12px"></p>
      <div style="display:flex;gap:8px;justify-content:flex-end"><button id="connection-options-cancel" class="ghost"></button><button id="connection-options-save"></button></div>
    </section>
  </div>
  <div id="confirm" style="display:none;position:absolute;inset:0;background:rgba(0,0,0,.55);
       align-items:center;justify-content:center;z-index:20">
    <div style="background:var(--orca-bg,#1e1e2e);border:1px solid var(--orca-border,#3c3c4c);
         border-radius:10px;padding:18px;max-width:420px;width:90%">
      <div id="confirm-title" style="font-weight:600;margin-bottom:8px">Upload to selected printers?</div>
      <div id="confirm-body" style="font-size:12px;color:var(--orca-muted,#a0a0a0);margin-bottom:12px"></div>
      <label style="display:none;align-items:center;gap:6px;font-size:12px;margin-bottom:12px">
        <input id="confirm-start" type="checkbox"> <span id="confirm-start-text">Start printing immediately after upload</span></label>
      <label id="confirm-ams-l" style="display:none;align-items:center;gap:6px;font-size:12px;margin-bottom:12px">
        <input id="confirm-ams" type="checkbox" checked> <span id="confirm-ams-text">Use AMS (Bambu)</span></label>
      <label id="confirm-plate-l" style="display:none;align-items:center;gap:6px;font-size:12px;margin-bottom:12px">
        <span id="confirm-plate-text">Plate</span><select id="confirm-plate"></select></label>
      <div style="display:flex;gap:8px;justify-content:flex-end">
        <button id="confirm-cancel" class="ghost">Cancel</button>
        <button id="confirm-ok">Send</button>
      </div>
    </div>
  </div>
  <div id="ask-modal" class="history-modal" role="presentation" style="z-index:40">
    <section class="history-card" role="alertdialog" aria-modal="true" aria-describedby="ask-body" style="width:min(440px,100%)">
      <div id="ask-body" style="padding:18px;line-height:1.5;white-space:pre-line;overflow:auto"></div>
      <div style="display:flex;gap:8px;justify-content:flex-end;padding:0 18px 18px">
        <button id="ask-cancel" class="ghost">Cancel</button>
        <button id="ask-ok">OK</button>
      </div>
    </section>
  </div>
  <div id="tls-modal" class="history-modal" role="presentation" style="z-index:41">
    <section class="history-card" role="dialog" aria-modal="true" aria-labelledby="tls-title"
      style="width:min(500px,100%);max-width:calc(100vw - 24px);box-sizing:border-box">
      <div class="history-head"><strong id="tls-title"></strong>
        <button id="tls-close" class="ghost" type="button"></button></div>
      <div style="padding:0 18px 18px;display:flex;flex-direction:column;gap:10px;min-width:0">
        <div id="tls-intro" class="history-note"></div>
        <select id="tls-protocol" style="width:max-content;max-width:100%;box-sizing:border-box">
          <option value="mqtt"></option><option value="ftps"></option></select>
        <div id="tls-address" style="overflow-wrap:anywhere"></div>
        <div><span id="tls-current-label"></span><br><code id="tls-current"
          style="overflow-wrap:anywhere;word-break:break-all"></code></div>
        <div><span id="tls-observed-label"></span><br><code id="tls-observed"
          style="overflow-wrap:anywhere;word-break:break-all"></code></div>
        <div id="tls-note" class="history-note" role="status"></div>
        <div style="display:flex;flex-wrap:wrap;gap:8px;justify-content:flex-end">
          <button id="tls-probe" class="ghost" type="button"></button>
          <button id="tls-confirm" type="button" disabled></button>
        </div>
      </div>
    </section>
  </div>
  <div id="history-modal" class="history-modal" role="presentation">
    <section class="history-card" role="dialog" aria-modal="true" aria-labelledby="history-title">
      <div class="history-head">
        <strong id="history-title">Print history</strong>
        <button id="history-close" class="ghost">Close</button>
      </div>
      <div id="history-note" class="history-note"></div>
      <div id="journal-storage" class="history-note journal-storage" style="display:none"></div>
      <div id="journal-holds" class="history-list" style="display:none"></div>
      <div id="history-toolbar" class="history-toolbar" style="display:none">
        <div class="history-summary" aria-live="polite">
          <div class="history-stat"><span id="journal-all-label"></span></div>
          <div class="history-stat"><span id="journal-completed-label"></span></div>
          <div class="history-stat"><span id="journal-active-label"></span></div>
        </div>
        <label id="journal-search-label" for="journal-search"></label>
        <input id="journal-search" class="history-search" type="search" autocomplete="off">
        <div id="history-results" class="history-results" aria-live="polite"></div>
      </div>
      <div id="history-list" class="history-list"><div class="empty">Loading…</div></div>
    </section>
  </div>
  <div id="cam-modal" class="history-modal" role="presentation" style="z-index:30">
    <section class="history-card cam-card" role="dialog" aria-modal="true" aria-labelledby="cam-title">
      <div class="history-head">
        <strong id="cam-title"></strong>
        <button id="cam-close" class="ghost" type="button">&times;</button>
      </div>
      <div class="cam-view"><img id="cam-img" alt="" referrerpolicy="no-referrer"></div>
      <div id="cam-note" class="history-note" role="status" aria-live="polite"></div>
      <div class="cam-actions"><button id="cam-window" class="ghost" type="button" style="display:none"></button></div>
    </section>
  </div>
  <div id="grid"><div class="empty">Loading…</div></div>
  <footer id="feedback-corner">
    <!-- Lucide Bug icon, ISC License.
    Copyright (c) for portions of Lucide are held by Cole Bemis 2013-2023 as part
    of Feather (MIT). All other copyright (c) for Lucide are held by Lucide
    Contributors 2025.
    Permission to use, copy, modify, and/or distribute this software for any
    purpose with or without fee is hereby granted, provided that the above
    copyright notice and this permission notice appear in all copies.
    THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES
    WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
    MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR
    ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES
    WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN
    ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF
    OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.
    The MIT License (MIT), for portions derived from Feather:
    Copyright (c) 2013-2023 Cole Bemis.
    Permission is hereby granted, free of charge, to any person obtaining a copy
    of this software and associated documentation files (the "Software"), to deal
    in the Software without restriction, including without limitation the rights
    to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
    copies of the Software, and to permit persons to whom the Software is
    furnished to do so, subject to the following conditions:
    The above copyright notice and this permission notice shall be included in all
    copies or substantial portions of the Software.
    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
    IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
    FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
    AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
    LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
    OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
    SOFTWARE. -->
    <button id="feedback" class="ghost" type="button" aria-label="Feedback" title="Feedback">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
        stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">
        <path d="M12 20v-9"/><path d="M14 7a4 4 0 0 1 4 4v3a6 6 0 0 1-12 0v-3a4 4 0 0 1 4-4z"/>
        <path d="M14.12 3.88 16 2"/><path d="M21 21a4 4 0 0 0-3.81-4"/>
        <path d="M21 5a4 4 0 0 1-3.55 3.97"/><path d="M22 13h-4"/>
        <path d="M3 21a4 4 0 0 1 3.81-4"/><path d="M3 5a4 4 0 0 0 3.55 3.97"/>
        <path d="M6 13H2"/><path d="m8 2 1.88 1.88"/><path d="M9 7.13V6a3 3 0 1 1 6 0v1.13"/>
      </svg>
    </button>
  </footer>
<script>
'use strict';
var UI_COPY = __UI_COPY__;
var hostLanguage = '__HOST_UI_LANGUAGE__';
// Supplied by the plugin so the page never keeps its own list of connections.
var ACTIVE_TYPES = __ACTIVE_TYPES__;
var ADAPTER_TYPES = __ADAPTER_TYPES__;
var CREDENTIAL_TYPES = __CREDENTIAL_TYPES__;
var CAMERA_TYPES = __CAMERA_TYPES__;
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
  document.getElementById('f-wsport').placeholder = t('crealityWsPortPlaceholder');
  title('f-wsport', 'crealityWsPortTitle');
  document.getElementById('f-access').placeholder = t('accessCode');
  title('f-insecure-l', 'selfSignedTitle');
  document.getElementById('f-insecure-l').setAttribute('data-tls-warning',
    'When enabled, TLS certificate checks are disabled for this printer. The connection and printer credentials may be intercepted.');
  document.getElementById('f-insecure-l').title = t('selfSignedTitle') +
    ' When enabled, TLS certificate checks are disabled and printer credentials may be exposed.';
  document.getElementById('f-insecure').setAttribute('aria-label', t('selfSigned'));
  text('manual-toggle-text', 'add'); title('manual-toggle', 'addPrinter');
  text('add', 'add'); text('refresh-text', 'refresh'); title('refresh', 'refresh');
  text('confirm-title', 'confirmUploadTitle'); text('confirm-start-text', 'startImmediately');
  text('confirm-ams-text', 'useAms'); text('confirm-plate-text', 'plateLabel');
  text('confirm-cancel', 'cancel');
  text('confirm-ok', 'send');
  text('history-close', 'close'); text('history-note', 'historyCoverage');
  text('journal-open', 'journalButton'); title('journal-open', 'journalButton');
  text('journal-search-label', 'journalSearchLabel');
  document.getElementById('journal-search').placeholder = t('journalSearchPlaceholder');
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
window.lastUnassignedOutbox = [];
var selected = {};   // owner scope + target UUID + binding revision -> true
var expandedQueues = {};  // target binding -> true
var queueScrollPositions = {};  // target binding -> scroll offset

function esc(s){ return String(s == null ? '' : s).replace(/[&<>"]/g, function(c){
  return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]; }); }
function temp(v){ return v == null ? '—' : Math.round(v) + '°'; }
// <bambu-stats>
var count = function(v, max){ return typeof v === 'number' && isFinite(v) && v >= 0 && v <= max && Math.floor(v) === v; };
// Time-left / layer line shared by every backend; exists only when data was sent.
function jobStatsHtml(s){
  var parts = [];
  if (count(s.remaining_minutes, 100000))
    parts.push(esc(s.remaining_minutes >= 60
      ? t('bambuLeftHM', {hours:Math.floor(s.remaining_minutes / 60), minutes:s.remaining_minutes % 60})
      : t('bambuLeftM', {minutes:s.remaining_minutes})));
  if (count(s.layer, 100000) && count(s.total_layers, 100000) && s.total_layers > 0 && s.layer <= s.total_layers)
    parts.push(esc(t('bambuLayer', {current:s.layer, total:s.total_layers})));
  return parts.length ? '<div class="fname">' + parts.join(' · ') + '</div>' : '';
}
// Extra rows of a Bambu card; every row exists only when the printer sent it.
function bambuStatsHtml(s){
  var out = '';
  var trays = (Array.isArray(s.ams) ? s.ams : []).filter(function(tray){
    return tray && typeof tray.type === 'string' && /^[A-Za-z0-9][A-Za-z0-9 +\-]{0,15}$/.test(tray.type); }).slice(0, 16);
  if (trays.length)
    out += '<div class="ams">' + trays.map(function(tray){
      var color = /^#[0-9A-Fa-f]{6}$/.test(tray.color || '') ? ' style="background:' + tray.color + '"' : '';
      var remain = count(tray.remain, 100) ? ' ' + tray.remain + '%' : '';
      return '<span><i' + color + '></i>' + esc(tray.type + remain) + '</span>';
    }).join('') + '</div>';
  if (typeof s.print_error === 'string' && /^[0-9A-F]{8}$/.test(s.print_error) && s.print_error !== '00000000')
    out += '<div class="fname bambu-err" role="status">' + esc(t('bambuError', {code:s.print_error})) + '</div>';
  return out;
}
// </bambu-stats>
// Friendly badge for the connection type — the ecosystem we actually detect,
// not the transport. (OctoPrint can front any firmware, so we don't claim Marlin.)
function typeLabel(value, status){
  var key = (value || '').toLowerCase();
  if (key === 'duet' && status && /^(rrf|dsf)$/.test(status.protocol || ''))
    return 'Duet ' + status.protocol.toUpperCase();
  return { moonraker:'Klipper', klipper:'Klipper', octoprint:'OctoPrint',
    prusalink:'PrusaLink', prusaconnect:'PrusaConnect', duet:'Duet', repetier:'Repetier',
    mks:'MKS', flashair:'FlashAir', astrobox:'AstroBox', esp3d:'ESP3D',
    crealityprint:'Creality', flashforge:'FlashForge', simplyprint:'SimplyPrint',
    elegoolink:'Elegoo', '3dprinteros':'3DPrinterOS', obico:'Obico',
    bambu:'Bambu Lab', generic:t('other') }[(value || '').toLowerCase()] ||
    (value || t('other'));
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
  return ACTIVE_TYPES.indexOf(String(t || '').toLowerCase()) >= 0;
}

function targetKey(p){
  return [p.owner_scope_uuid || '', p.target_uuid || '', p.binding_revision || ''].join(':');
}
function targetIdentity(p){ return { targetUuid:p.target_uuid, ownerScope:p.owner_scope_uuid,
  targetSelector:p.target_selector, bindingRevision:p.binding_revision, url:p.url,
  connectionType:p.type, name:p.name || p.url }; }

var tlsDialogCard = null, tlsDialogToken = null, tlsDialogRequest = 0;
function clearTlsChallenge(){
  tlsDialogToken = null;
  tlsDialogRequest += 1;
  document.getElementById('tls-confirm').disabled = true;
  document.getElementById('tls-current').textContent = t('tlsTrustNoPin');
  document.getElementById('tls-observed').textContent = '';
  document.getElementById('tls-address').textContent = '';
  document.getElementById('tls-note').textContent = '';
  orca.postMessage({type:'tls-close'});
}
function closeTlsTrust(){
  clearTlsChallenge(); tlsDialogCard = null;
  document.getElementById('tls-modal').style.display = 'none';
}
function openTlsTrust(p){
  clearTlsChallenge(); tlsDialogCard = p;
  document.getElementById('tls-title').textContent = t('tlsTrustTitle');
  document.getElementById('tls-intro').textContent = t('tlsTrustIntro');
  document.getElementById('tls-current-label').textContent = t('tlsTrustCurrent');
  document.getElementById('tls-observed-label').textContent = t('tlsTrustObserved');
  document.getElementById('tls-close').textContent = t('close');
  document.getElementById('tls-probe').textContent = t('tlsTrustProbe');
  document.getElementById('tls-probe').disabled = false;
  document.getElementById('tls-confirm').textContent = t('tlsTrustConfirm');
  var protocol = document.getElementById('tls-protocol');
  protocol.options[0].textContent = t('tlsTrustMqtt');
  protocol.options[1].textContent = t('tlsTrustFtps');
  protocol.value = 'mqtt';
  document.getElementById('tls-modal').style.display = 'flex';
}
document.getElementById('tls-close').addEventListener('click', closeTlsTrust);
document.getElementById('tls-protocol').addEventListener('change', clearTlsChallenge);
document.getElementById('tls-probe').addEventListener('click', function(){
  if (!tlsDialogCard) return;
  clearTlsChallenge();
  var msg = targetIdentity(tlsDialogCard);
  msg.type = 'tls-probe'; msg.protocol = document.getElementById('tls-protocol').value;
  msg.requestId = tlsDialogRequest;
  document.getElementById('tls-note').textContent = t('tlsTrustWorking');
  orca.postMessage(msg);
});
document.getElementById('tls-confirm').addEventListener('click', function(){
  if (!tlsDialogCard || !tlsDialogToken) return;
  var token = tlsDialogToken; tlsDialogToken = null;
  this.disabled = true;
  document.getElementById('tls-note').textContent = t('tlsTrustWorking');
  orca.postMessage({type:'tls-confirm', token:token, consent:true,
    requestId:tlsDialogRequest});
});

var actionRejectSequence = 0;
var actionRejectCallbacks = Object.create(null);
function sendRejectableAction(button, request, reset){
  var id = ++actionRejectSequence;
  button.disabled = true;
  actionRejectCallbacks[id] = function(){
    if (document.contains(button)) {
      button.disabled = false;
      if (reset) reset();
    }
  };
  // Accepted actions never emit rejection; keep only a bounded recent set.
  var ids = Object.keys(actionRejectCallbacks);
  if (ids.length > 256) delete actionRejectCallbacks[ids[0]];
  request.requestId = id;
  orca.postMessage(request);
}

function statusFresh(s){
  var observed = Number(s && s.observed_at);
  return !!s && s.freshness === 'fresh' && isFinite(observed) &&
    observed <= Date.now() / 1000 && Date.now() / 1000 - observed <= 30;
}

function statusTargetMatches(p, s){
  return s.backend !== 'repetier' || (s.repetier_slug === p.repetier_slug &&
    String(s.repetier_url || '').replace(/\/+$/, '') === String(p.url || '').replace(/\/+$/, ''));
}

function printerSelectable(p){
  var s = p.status || {};
  var readyStates = {moonraker:['standby'], octoprint:['operational'], bambu:['idle', 'finished']};
  ADAPTER_TYPES.forEach(function(key){ readyStates[key] = ['idle']; });
  var backend = String(s.backend || '').toLowerCase();
  var declared = String(p.type || '').toLowerCase();
  var expected = ['moonraker','klipper','mainsail','fluidd'].indexOf(declared) >= 0
    ? 'moonraker' : declared;
  return backendReady(p.type) && s.supported !== false && s.online === true &&
    s.transport_online === true && s.authorization === 'accepted' &&
    s.device_health === 'healthy' && statusFresh(s) && statusTargetMatches(p, s) && backend === expected &&
    (ADAPTER_TYPES.indexOf(backend) < 0 || s.upload_ready === true) &&
    (readyStates[backend] || []).indexOf(String(s.state || '').toLowerCase()) >= 0 &&
    (readyStates[backend] || []).indexOf(String(s.job_state || '').toLowerCase()) >= 0;
}

function printerBusy(p){
  var s = p.status || {};
  if (!backendReady(p.type) || s.supported === false || s.online !== true) return false;
  var kind = stateKind(s);
  return kind === 'printing' || kind === 'paused' ||
    /^state_(printing|running|paused|pause)$/.test(String(s.upload_reason || ''));
}

function tlsTrustNeeded(p, s){
  return String(s.backend || p.type || '').toLowerCase() === 'bambu' &&
    /^tls_(trust_required|certificate_changed|trust_stale)$/.test(s.error_code || '');
}
function statusControlReady(p, action){
  var s = p.status || {};
  var state = String(s.job_state || '').toLowerCase();
  if (!backendReady(p.type) || s.can_control === false ||
      (ADAPTER_TYPES.indexOf(String(s.backend || '').toLowerCase()) >= 0 &&
       (s.backend !== 'repetier' || s.can_control !== true)) ||
      s.online !== true || s.transport_online !== true ||
      s.authorization !== 'accepted' || s.device_health !== 'healthy' || !statusFresh(s) || !statusTargetMatches(p, s))
    return false;
  if (action === 'pause') return state === 'printing' || state === 'running';
  if (action === 'resume') return state === 'paused' || state === 'pause';
  if (action === 'cancel') return state === 'printing' || state === 'running' ||
    state === 'paused' || state === 'pause';
  return false;
}

function updateSendBtn(){
  var eligible = current.filter(printerSelectable);
  var n = eligible.filter(function(p){ return selected[targetKey(p)]; }).length;
  sendBtn.disabled = n === 0;
  sendBtn.textContent = n ? t('sendSelectedCount', {count:n}) : t('sendSelected');
  var all = document.getElementById('sel-all');
  all.disabled = eligible.length === 0;
  all.checked = eligible.length > 0 && eligible.every(function(p){ return selected[targetKey(p)]; });
}

document.getElementById('sel-all').addEventListener('change', function(e){
  current.forEach(function(p){ selected[targetKey(p)] = printerSelectable(p) && e.target.checked; });
  render(current);
});

// <outbox-report>
// Stored slice copies live inside the print journal: one list, newest first.
// Print Farm writes the digest of the exact file it sent into its own journal
// record, and the server names the stored copy only when that digest matches;
// nothing here guesses a link between an Orca event and a file. The only other
// storage UI is the size warning and the removal of leftover files.
var STORAGE_LARGE = 500 * 1024 * 1024, JOURNAL_ROWS = 100;
var journalUi = {picker:'', shown:JOURNAL_ROWS};
function storageLarge(report){
  var groups = (report && report.groups) || [];
  return groups.some(function(group){ return (Number(group.size) || 0) >= STORAGE_LARGE; })
    || groups.reduce(function(n, group){ return n + (Number(group.size) || 0); }, 0) >= STORAGE_LARGE;
}
function journalCatalog(){
  var catalog = {};
  (window.lastUnassignedOutbox || []).forEach(function(r){
    catalog[r.file] = {file:r.file, name:r.name || r.file, size:Number(r.size) || 0, ts:Number(r.ts) || 0, printer:null};
  });
  (current || []).forEach(function(p){
    (p.queued || []).forEach(function(r){
      catalog[r.file] = {file:r.file, name:r.name || r.file, size:Number(r.size) || 0, ts:Number(r.ts) || 0, printer:p};
    });
  });
  return catalog;
}
// History records first, then stored copies no record points to; newest on top.
function journalRows(){
  var catalog = journalCatalog(), linked = {};
  var rows = historyItems.map(function(item, i){
    var stored = historyMode === 'all' && item.file && catalog[item.file] ? catalog[item.file] : null;
    if (stored) linked[stored.file] = true;
    return {item:item, stored:stored, ts:Number(item.started_at) || 0, i:i};
  });
  if (historyMode === 'all') Object.keys(catalog).forEach(function(key){
    if (!linked[key]) rows.push({item:null, stored:catalog[key], ts:catalog[key].ts, i:rows.length});
  });
  return rows.sort(function(a, b){ return (b.ts - a.ts) || (a.i - b.i); });
}
// One-off send of a file linked to no printer: pick a ready printer card, then
// confirm in the page. Nothing is linked; the choice is used for this file only.
function journalPicker(stored){
  var box = document.createElement('div'); box.className = 'journal-picker';
  var ready = (current || []).filter(printerSelectable);
  if (!ready.length) {
    var none = document.createElement('div'); none.textContent = t('unassignedNoPrinter'); box.appendChild(none);
    return box;
  }
  ready.forEach(function(p){
    var line = document.createElement('div'); line.className = 'qrow';
    var name = document.createElement('span'); name.className = 'qname';
    name.textContent = p.name || p.url; name.title = p.name || p.url; line.appendChild(name);
    [false, true].forEach(function(start){
      var b = document.createElement('button'); b.className = 'ghost'; b.type = 'button';
      b.textContent = t(start ? 'unassignedUploadStart' : 'unassignedUpload');
      b.addEventListener('click', function(){
        var request = targetIdentity(p); request.type = 'outbox-start-prepare';
        request.file = stored.file; request.unassigned = true; request.start = start;
        journalUi.picker = ''; renderHistoryItems();
        orca.postMessage(request);
      });
      line.appendChild(b);
    });
    box.appendChild(line);
  });
  return box;
}
// Buttons of a row that has a stored copy. Every send still goes through the
// same approval, bed and file-check confirmations on the Python side.
function journalFileActions(row){
  var stored = row.stored; if (!stored) return null;
  var linked = !!row.item, printer = stored.printer;
  var box = document.createElement('div'); box.className = 'journal-actions';
  var send = document.createElement('button'); send.className = 'ghost'; send.type = 'button';
  send.textContent = t(linked ? 'journalResend' : 'unassignedSend');
  if (!printer) {
    send.setAttribute('aria-expanded', journalUi.picker === stored.file ? 'true' : 'false');
    send.addEventListener('click', function(){
      journalUi.picker = journalUi.picker === stored.file ? '' : stored.file; renderHistoryItems();
    });
  } else if (!printerSelectable(printer)) {
    send.disabled = true;
    send.title = printerBusy(printer) ? t('printerBusy') : t('printerNotOnline');
  } else {
    send.addEventListener('click', function(){
      askConfirm(t('journalSendConfirm', {name:stored.name, printer:printer.name || printer.url}),
        t('uploadShort'), function(){
          var identity = targetIdentity(printer); identity.type = 'outbox-send';
          identity.file = stored.file; identity.start = false;
          sendRejectableAction(send, identity);
        });
    });
  }
  var drop = document.createElement('button'); drop.className = 'ghost'; drop.type = 'button';
  drop.textContent = t(linked ? 'journalFileDelete' : 'unassignedDelete');
  drop.addEventListener('click', function(){
    askConfirm(t('unassignedDeleteConfirm', {name:stored.name, size:formatBytes(stored.size)}), drop.textContent, function(){
      if (printer) {
        var identity = targetIdentity(printer); identity.type = 'outbox-drop';
        identity.file = stored.file; sendRejectableAction(drop, identity);
      } else sendRejectableAction(drop, {type:'outbox-unassigned-drop', file:stored.file});
    });
  });
  box.appendChild(send); box.appendChild(drop);
  return box;
}
// Size warning in the top bar, and the link to stored files with no printer.
function updateStorageNotices(){
  var warn = document.getElementById('storage-warn');
  var large = storageLarge(window.lastOutboxStorage || null);
  warn.textContent = large ? t('outboxStorageWarning') : '';
  warn.style.display = large ? '' : 'none';
  var count = (window.lastUnassignedOutbox || []).length;
  var link = document.getElementById('unassigned-link');
  link.textContent = count ? t('unmatchedQueued', {count:count}) : '';
  link.style.display = count ? '' : 'none';
}
// The one storage block left, inside the journal and only when it has something
// to say: scan failure, size warning, leftover files.
function renderJournalStorage(){
  var box = document.getElementById('journal-storage');
  box.innerHTML = '';
  var report = window.lastOutboxStorage || null;
  var cleanup = (report && report.cleanup) || {};
  var failed = !!report && report.ok === false;
  var large = storageLarge(report);
  if (historyMode !== 'all' || (!cleanup.count && !failed && !large)) { box.style.display = 'none'; return; }
  box.style.display = 'flex';
  function line(text, cls){
    var el = document.createElement('div'); el.textContent = text; if (cls) el.className = cls;
    box.appendChild(el); return el;
  }
  if (failed) line(t('outboxStorageScanFailed', {paths:(report.errors || []).join(', ')}), 'storage-warn');
  if (large) line(t('outboxStorageWarning'), 'storage-warn');
  if (cleanup.count) {
    line(t('outboxStorageManualCleanup'));
    var cleanButton = document.createElement('button'); cleanButton.className = 'ghost'; cleanButton.type = 'button';
    cleanButton.textContent = t('outboxStorageCleanButton', {count:cleanup.count, size:formatBytes(cleanup.size)});
    cleanButton.disabled = !cleanup.available;
    cleanButton.addEventListener('click', function(){
      askConfirm(t('outboxStorageCleanConfirm', {count:cleanup.count, size:formatBytes(cleanup.size)}),
        cleanButton.textContent, function(){
          sendRejectableAction(cleanButton, {type:'outbox-cleanup'});
        });
    });
    box.appendChild(cleanButton);
    if (!cleanup.available) line(t('outboxStorageCleanBlocked'));
  }
}
// </outbox-report>

// In-page confirmation. Native script dialogs are not guaranteed by
// the embedded webview, so every confirmation goes through this modal.
var askState = null;
function closeAsk(restoreFocus){
  var state = askState; askState = null;
  document.getElementById('ask-modal').style.display = 'none';
  if (restoreFocus && state && state.opener && document.contains(state.opener)) state.opener.focus();
}
function askConfirm(message, okLabel, onOk){
  // A replacement keeps the focus target of the question it replaces: by now
  // the active element is one of the dialog's own (hidden) buttons.
  var opener = askState ? askState.opener : document.activeElement;
  if (askState) closeAsk(false);
  document.getElementById('ask-body').textContent = message;
  document.getElementById('ask-ok').textContent = okLabel;
  document.getElementById('ask-cancel').textContent = t('cancel');
  askState = {onOk:onOk, opener:opener};
  document.getElementById('ask-modal').style.display = 'flex';
  document.getElementById('ask-cancel').focus();
}
document.getElementById('ask-ok').addEventListener('click', function(){
  var state = askState; closeAsk(true);
  if (state) state.onOk();
});
document.getElementById('ask-cancel').addEventListener('click', function(){ closeAsk(true); });
document.getElementById('ask-modal').addEventListener('click', function(event){
  if (event.target === this) closeAsk(true);
});
document.addEventListener('keydown', function(event){
  if (!askState) return;
  if (event.key === 'Escape') { event.preventDefault(); event.stopImmediatePropagation(); closeAsk(true); return; }
  if (event.key === 'Tab') {
    var first = document.getElementById('ask-cancel'), last = document.getElementById('ask-ok');
    event.stopImmediatePropagation();
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    else if (document.activeElement !== first && document.activeElement !== last) { event.preventDefault(); first.focus(); }
  }
}, true);

function queuedNewestFirst(files){
  return files.map(function(f, i){ return {f:f, i:i}; }).sort(function(a, b){
    return ((Number(b.f.ts) || 0) - (Number(a.f.ts) || 0)) || (b.i - a.i);
  }).map(function(x){ return x.f; });
}
// The retained slice copies under a card: the newest three show, "N more"
// reveals the rest. Nothing is drawn when the printer has no copies.
function buildQueue(p, selectable, printDisabledReason){
  var q = document.createElement('div');
  q.className = 'queue';
  var qList = document.createElement('div');
  qList.className = 'queue-scroll';
  q.appendChild(qList);
  if (expandedQueues[targetKey(p)]) q.classList.add('expanded');
  // The plugin can't know whether Orca (or anything else) already sent a
  // file to a printer, so it doesn't pretend to. The queue is just sliced files
  // you can push to this printer: one Upload action, gated on its current state.
  queuedNewestFirst(p.queued).forEach(function(r){
    var row = document.createElement('div');
    row.className = 'qrow';
    var name = document.createElement('span'); name.className = 'qname';
    name.title = r.name; name.textContent = r.name;
    var meta = null, metaText = queueMeta(r);
    if (metaText) { meta = document.createElement('span'); meta.className = 'qmeta'; meta.textContent = metaText; meta.title = metaText; }
    var pb = document.createElement('button'); pb.className = 'qprint'; pb.textContent = t('uploadShort');
    if (selectable) pb.title = t('uploadToPrinterTitle');
    else { pb.disabled = true; pb.title = printDisabledReason; }
    var drop = document.createElement('button'); drop.className = 'qdrop ghost';
    drop.title = t('removeRetainedTitle'); drop.textContent = '\u00d7';
    row.appendChild(name); if (meta) row.appendChild(meta); row.appendChild(pb); row.appendChild(drop);
    if (selectable) {
      pb.addEventListener('click', function(){
        if (!pb._armed) {
          pb._armed = true; pb.textContent = t('sure');
          setTimeout(function(){ pb._armed = false; pb.textContent = t('uploadShort'); }, 3000);
          return;
        }
        var identity = targetIdentity(p); identity.type = 'outbox-send';
        identity.file = r.file; identity.start = false;
        sendRejectableAction(pb, identity, function(){
          pb._armed = false; pb.textContent = t('uploadShort');
        });
      });
    }
    drop.addEventListener('click', function(){
      var identity = targetIdentity(p); identity.type = 'outbox-drop';
      identity.file = r.file; orca.postMessage(identity);
    });
    qList.appendChild(row);
  });
  qList.addEventListener('scroll', function(){ queueScrollPositions[targetKey(p)] = qList.scrollTop; });
  if (p.queued.length > 3) {
    var qToggle = document.createElement('button');
    qToggle.className = 'queue-toggle ghost';
    qToggle.type = 'button';
    var updateQueueToggle = function(){
      var open = q.classList.contains('expanded');
      var label = open ? t('queueLess') : t('queueMore', {count:p.queued.length - 3});
      qToggle.setAttribute('aria-expanded', open ? 'true' : 'false');
      qToggle.setAttribute('aria-label', label);
      qToggle.title = label;
      qToggle.innerHTML = '<span>' + esc(label) + '</span>' +
        '<svg class="disclosure-icon" viewBox="0 0 12 8" aria-hidden="true" focusable="false">' +
        '<path d="M1 1.5 6 6.5 11 1.5" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"></path></svg>';
    };
    updateQueueToggle();
    qToggle.addEventListener('click', function(){
      var open = q.classList.toggle('expanded');
      expandedQueues[targetKey(p)] = open;
      if (!open) { qList.scrollTop = 0; queueScrollPositions[targetKey(p)] = 0; }
      updateQueueToggle();
    });
    q.appendChild(qToggle);
  }
  return q;
}
// Card actions. With Pause/Stop on the card, History shares their row and the
// other actions get a row of their own (only if there are any). Without them
// everything is one wrapping row in DOM and Tab order: Select on the left, then
// History, Print..., camera, Open. Empty parts and empty rows are never drawn.
function cardActionsHtml(parts){
  var inner;
  if (parts.ctl) {
    var rest = (parts.select || '') + (parts.print || '') +
      (parts.cam || '') + (parts.open || '');
    inner = '<div class="ctl-row">' + parts.history + (parts.tls || '') + parts.ctl + '</div>' +
      (rest ? '<div class="row">' + rest + '</div>' : '');
  } else {
    inner = '<div class="row">' + (parts.select || '') + parts.history + (parts.print || '') +
      (parts.tls || '') +
      (parts.cam || '') + (parts.open || '') + '</div>';
  }
  return '<div class="tile-actions">' + inner + '</div>';
}

function renderCredentialNotice(printers){
  var box = document.getElementById('credential-notice');
  var preview = window.credentialMigration || {};
  box.innerHTML = '';
  if (preview.count) {
    var text = document.createElement('span');
    text.textContent = t('credentialLegacyWarning', {count:preview.count});
    box.appendChild(text);
    var migrate = document.createElement('button'); migrate.className = 'ghost';
    migrate.textContent = t('credentialMigrate');
    migrate.onclick = function(){ askConfirm(t('credentialMigrationConfirm'), t('credentialMigrate'), function(){
      orca.postMessage({type:'credential-migrate', confirmation:preview.confirmation});
    }); };
    box.appendChild(migrate);
  }
  if ((printers || []).some(function(p){ return (p.status || {}).error_code === 'credential_unavailable'; })) {
    var unavailable = document.createElement('span'); unavailable.textContent = t('credentialUnavailable');
    box.appendChild(unavailable);
    var retry = document.createElement('button'); retry.className = 'ghost';
    retry.textContent = t('credentialRetry');
    retry.onclick = function(){ orca.postMessage({type:'credential-retry'}); };
    box.appendChild(retry);
  }
  box.style.display = box.childNodes.length ? 'flex' : 'none';
  box.style.flexWrap = 'wrap'; box.style.alignItems = 'center'; box.style.gap = '8px';
}
// <tool-details>
var toolDetailSelections = Object.create(null);
function toolDetailKey(p){ return String(p.target_uuid || '') + ':' + String(p.binding_revision || ''); }
function pruneToolDetails(printers){
  var valid = Object.create(null);
  printers.forEach(function(p){ valid[toolDetailKey(p)] = true; });
  Object.keys(toolDetailSelections).forEach(function(key){ if (!valid[key]) delete toolDetailSelections[key]; });
}
function toolDetailsText(tool, tools){
  var label = 'T' + tool.index;
  var name = String(tool.name || '').trim();
  if (name && name.toUpperCase() !== label) label += ' (' + name + ')';
  var knownActive = tools.some(function(item){ return item.active === true; });
  return t('toolDetails', {tool:label, current:temp(tool.current), target:temp(tool.target),
    state:t(tool.active === true ? 'toolActive' : knownActive ? 'toolInactive' : 'stateUnknown')});
}
function toolDetailsHtml(p, tools){
  var key = toolDetailKey(p);
  var chosen = tools.filter(function(tool){ return tool.index === toolDetailSelections[key]; })[0];
  if (!chosen) chosen = tools.filter(function(tool){ return tool.active === true; })[0] || tools[0];
  if (!chosen) return '';
  toolDetailSelections[key] = chosen.index;
  var buttons = tools.map(function(tool){
    var active = tool.active === true;
    var label = 'T' + tool.index + (active ? ' — ' + t('toolActive') : '');
    return '<button type="button" class="head-temp ghost' + (active ? ' actual-active' : '') +
      '" data-tool="' + esc(tool.index) + '" aria-pressed="' + (chosen.index === tool.index ? 'true' : 'false') +
      '" aria-label="' + esc(label + '. ' + t('toolViewOnly')) + '" title="' + esc(t('toolViewOnly')) +
      '">T' + esc(tool.index) + (active ? '<i class="toolmark" aria-hidden="true">●</i>' : '') +
      ' <b>' + temp(tool.current) + '</b> / ' + temp(tool.target) + '</button>';
  }).join('');
  return '<div class="head-temps">' + buttons + '</div><div class="head-detail" role="status">' +
    esc(toolDetailsText(chosen, tools)) + '</div>';
}
function bindToolDetails(tile, p, tools){
  Array.prototype.forEach.call(tile.querySelectorAll('.head-temp'), function(button){
    button.addEventListener('click', function(){
      var index = Number(button.getAttribute('data-tool'));
      var chosen = tools.filter(function(tool){ return tool.index === index; })[0];
      if (!chosen) return;
      toolDetailSelections[toolDetailKey(p)] = index;
      Array.prototype.forEach.call(tile.querySelectorAll('.head-temp'), function(item){
        item.setAttribute('aria-pressed', item === button ? 'true' : 'false');
      });
      var detail = tile.querySelector('.head-detail');
      if (detail) detail.textContent = toolDetailsText(chosen, tools);
    });
  });
}
// </tool-details>

function render(printers){
  pruneToolDetails(printers);
  renderCredentialNotice(printers);
  Array.prototype.forEach.call(grid.querySelectorAll('.tile'), function(tile){
    var list = tile.querySelector('.queue-scroll');
    if (list) queueScrollPositions[tile.dataset.targetUuid] = list.scrollTop;
  });
  current = printers;
  grid.innerHTML = '';
  updateStorageNotices();
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
      complete:'stateComplete', completed:'stateComplete', finished:'stateComplete', error:'stateError',
      failed:'stateFailed', aborted:'stateAborted', halted:'stateError', unknown:'stateUnknown',
      busy:'stateBusy'
    };
    var stateKey = stateLabels[String(rawState).toLowerCase()];
    var stateText = stateKey ? t(stateKey) : rawState;
    var tile = document.createElement('div'); tile.className = 'tile';
    tile.dataset.printerUrl = p.url;
    tile.dataset.targetUuid = targetKey(p);
    var multiTool = Array.isArray(s.tools) && s.tools.length > 1;
    var toolRow = '';
    if (multiTool) {
      toolRow = s.backend === 'moonraker' ? toolDetailsHtml(p, s.tools) : '<div class="temps">' + s.tools.map(function(tool){
        var label = (tool.name || ('T' + tool.index)) + (tool.active ? ' \u2014 ' + t('toolActive') : '');
        return '<span title="' + esc(label) + '">T' + esc(tool.index) +
          (tool.active ? '<i class="toolmark">\u25cf</i>' : '') + ' <b>' + temp(tool.current) + '</b>' +
          (tool.target ? '/' + temp(tool.target) : '') + '</span>';
      }).join('') + '</div>';
    }
    var nozzleCell = multiTool ? '' : '<span>' + esc(t('nozzle')) + ' <b>' + temp(s.nozzle) + '</b>' + (s.target_nozzle ? '/' + temp(s.target_nozzle) : '') + '</span>';
    var prog = (s.progress == null) ? '' : '<div class="bar"><i style="width:' + s.progress + '%"></i></div>';
    var thumb = s.thumbnail ? '<img class="thumb show" src="' + s.thumbnail + '">' : '<div class="thumb"></div>';
    var fname = s.filename ? '<div class="fname" title="' + esc(s.filename) + '">' + esc(s.filename) + '</div>' : '';
    if (supported && String(s.backend || '').toLowerCase() === 'flashforge') {
      if (s.model && s.identity_verified === true)
        fname = '<div class="fname" title="' + esc([s.model_family, s.firmware].filter(Boolean).join(' / ')) +
          '">' + esc(s.model) + '</div>' + fname;
      var hintKey = {authorization_denied:'flashforgeErrAuth', lan_mode_disabled:'flashforgeErrLanMode',
        unsupported_model:'flashforgeErrUnsupportedModel', identity_unverified:'flashforgeErrIdentity',
        credentials_required:'flashforgeErrCredentials'}[s.error_code];
      if (hintKey) fname += '<div class="fname" role="status">' + esc(t(hintKey)) + '</div>';
    }
    var tlsNeeded = supported && tlsTrustNeeded(p, s);
    if (tlsNeeded) fname += '<div class="fname tls-needed" role="status">' + esc(s.error || t('tlsTrustRequired')) +
      ' — «' + esc(t('tlsTrustButton')) + '»</div>';
    if (supported && s.backend === 'repetier' && (s.upload_reason || s.control_reason))
      fname += '<div class="ctl-note">' + esc(t(s.upload_reason || s.control_reason)) + '</div>';
    var jobKind = stateKind(s);
    if (supported && (jobKind === 'printing' || jobKind === 'paused')) fname += jobStatsHtml(s);
    if (supported && String(s.backend || '').toLowerCase() === 'bambu') fname += bambuStatsHtml(s);
    tile.innerHTML =
      '<div class="top"><span class="dot ' + dotClass + '"></span>' +
      '<span class="name-wrap"><span class="name" title="' + esc(p.name || p.url) + '">' + esc(p.name || p.url) + '</span>' +
      '<button class="remove ghost" title="' + esc(t('remove')) + '" aria-label="' + esc(t('remove')) + '">' +
      '<svg viewBox="0 0 16 16" aria-hidden="true" focusable="false"><path d="M3 4h10M6 4V2.5h4V4m-6 0 .6 9h6.8l.6-9M6.5 6.5v4M9.5 6.5v4" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/></svg></button></span>' +
      '<span class="src">' + esc(typeLabel(p.type, s)) + '</span>' +
      '<span class="state" title="' + esc(s.error || stateText) + '">' + esc(stateText) + '</span></div>' +
      '<div class="body">' + thumb + '<div class="meta">' +
      '<div class="temps">' + nozzleCell +
      '<span>' + esc(t('bed')) + ' <b>' + temp(s.bed) + '</b>' + (s.target_bed ? '/' + temp(s.target_bed) : '') + '</span></div>' +
      toolRow + fname + prog + '</div></div>';
    var kind = stateKind(s);
    var busy = supported && s.online === true && (kind === 'printing' || kind === 'paused');
    var selectable = printerSelectable(p);
    if (!selectable) selected[targetKey(p)] = false;
    var ctl = '';
    if (s.online && kind === 'printing' && statusControlReady(p, 'pause'))
      ctl += '<button class="ctl ghost" data-a="pause">' + esc(t('pause')) + '</button><button class="ctl danger" data-a="cancel">' + esc(t('stop')) + '</button>';
    else if (s.online && kind === 'paused' && statusControlReady(p, 'resume'))
      ctl += '<button class="ctl ghost" data-a="resume">' + esc(t('resume')) + '</button><button class="ctl danger" data-a="cancel">' + esc(t('stop')) + '</button>';
    else if (supported && s.online === true && (kind === 'printing' || kind === 'paused') &&
             ADAPTER_TYPES.indexOf(String(s.backend || '').toLowerCase()) >= 0)
      ctl += '<button class="ctl ghost" disabled title="' + esc(t('controlsUnverified')) + '">' +
        esc(t(kind === 'paused' ? 'resume' : 'pause')) + '</button><button class="ctl danger" disabled title="' +
        esc(t('controlsUnverified')) + '">' + esc(t('stop')) + '</button>';
    // No controls, no container: an empty slot would leave a hole in the card.
    if (ctl) ctl = '<span class="ctl-slot">' + ctl + '</span>';
    var printDisabledReason = !supported
      ? t('integrationUnavailable')
      : (s.upload_reason === 'writable_target_unverified' || s.upload_reason === 'hardware_unverified' ? t('uploadUnverifiedHardware')
        : (busy || (s.online === true && /^state_/.test(s.upload_reason || '')) ? t('printerBusy')
          : t('printerNotOnline')));
    var printAttrs = selectable ? '' : ' disabled title="' + esc(printDisabledReason) + '"';
    // While a job runs or is paused, selecting and Print... cannot apply, so they
    // are left out rather than shown disabled. Other reasons keep their explanation.
    var hideSend = printerBusy(p);
    var camHtml = CAMERA_TYPES.indexOf(String(p.type || '').toLowerCase()) >= 0 && !noCamera[p.target_uuid]
      ? '<button class="cam ghost" type="button" data-uuid="' + esc(p.target_uuid) + '" title="' +
        esc(t('cameraButton')) + '" aria-label="' + esc(t('cameraButton')) + '">' +
        '<svg viewBox="0 0 16 16" aria-hidden="true" focusable="false"><path d="M2 5h2.2l1-1.5h5.6L11.8 5H14v7.5H2z" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/><circle cx="8" cy="8.6" r="2" fill="none" stroke="currentColor" stroke-width="1.3"/></svg></button>'
      : '';
    // Bambu has no LAN web page to open; its full UI lives in OrcaSlicer's Device tab.
    var openHtml = p.type === 'bambu' ? ''
      : '<button class="open ghost">' + esc(t('open')) + '</button>';
    var selectHtml = hideSend ? '' :
      '<label class="select-control' + (selectable ? '' : ' disabled') + '"' +
      (selectable ? '' : ' aria-disabled="true" title="' + esc(printDisabledReason) + '"') +
      '><input type="checkbox" class="sel"' +
      (selected[targetKey(p)] ? ' checked' : '') + (selectable ? '' : ' disabled') + '> ' + esc(t('select')) + '</label>';
    var printHtml = hideSend ? '' :
      '<button class="print ghost"' + printAttrs + '>' + esc(t('print')) + '</button>';
    var historyHtml = '<button class="history ghost">' + esc(t('history')) + '</button>' +
      '<button class="connection-options ghost" type="button">' + esc(t('connectionOptions')) + '</button>';
    var tlsHtml = String(p.type || '').toLowerCase() === 'bambu'
      ? '<button class="tls-trust ' + (supported && tlsTrustNeeded(p, s) ? 'primary' : 'ghost') + '" type="button">' + esc(t('tlsTrustButton')) + '</button>' : '';
    tile.innerHTML += cardActionsHtml({select:selectHtml, history:historyHtml, print:printHtml,
      tls:tlsHtml, cam:camHtml, open:openHtml, ctl:ctl});
    if (multiTool && s.backend === 'moonraker') bindToolDetails(tile, p, s.tools);
    var selBox = tile.querySelector('.sel');
    if (selBox) selBox.addEventListener('change', function(e){
      selected[targetKey(p)] = e.target.checked; updateSendBtn(); });
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
        var identity = targetIdentity(p); identity.type = 'printer-cmd'; identity.action = a;
        sendRejectableAction(btn, identity, function(){
          if (a === 'cancel') { btn._armed = false; btn.textContent = t('stop'); }
        });
      });
    });
    var printBtn0 = tile.querySelector('.print');
    if (printBtn0) printBtn0.addEventListener('click', function(){ pickAndSend([targetKey(p)]); });
    tile.querySelector('.history').addEventListener('click', function(){ openPrintHistory(p); });
    tile.querySelector('.connection-options').addEventListener('click', function(){ openConnectionOptions(p); });
    var tlsBtn = tile.querySelector('.tls-trust');
    if (tlsBtn) tlsBtn.addEventListener('click', function(){ openTlsTrust(p); });
    // Outbox records may be local exports waiting for an action, or copies of a
    // file Orca has already handed to a print host. Never offer a duplicate send
    // for the latter. Print arms on first click; × drops the retained copy.
    var qList = null;
    if (p.queued && p.queued.length) {
      var q = buildQueue(p, selectable, printDisabledReason);
      qList = q.children[0];
      tile.appendChild(q);
    }
    var camBtn = tile.querySelector('.cam');
    if (camBtn) camBtn.addEventListener('click', function(){ openCamera(p, camBtn); });
    var openBtn = tile.querySelector('.open');
    if (openBtn && !openBtn.disabled) openBtn.addEventListener('click', function(){
      var identity = targetIdentity(p); identity.type = 'open'; identity.name = p.name || p.url;
      orca.postMessage(identity); });
    var removeBtn = tile.querySelector('.remove');
    var removeIdleMarkup = removeBtn.innerHTML;
    function resetRemove(){
      if (removeBtn._resetTimer) clearTimeout(removeBtn._resetTimer);
      removeBtn._resetTimer = null;
      removeBtn._armed = false;
      removeBtn.classList.remove('armed');
      removeBtn.innerHTML = removeIdleMarkup;
      removeBtn.title = t('remove');
      removeBtn.setAttribute('aria-label', t('remove'));
    }
    removeBtn.addEventListener('click', function(){
      if (!removeBtn._armed) {
        removeBtn._armed = true;
        removeBtn.classList.add('armed');
        removeBtn.textContent = t('sure');
        removeBtn.title = t('sure');
        removeBtn.setAttribute('aria-label', t('sure'));
        removeBtn._resetTimer = setTimeout(resetRemove, 3000);
        return;
      }
      clearTimeout(removeBtn._resetTimer);
      var identity = targetIdentity(p); identity.type = 'remove';
      sendRejectableAction(removeBtn, identity, resetRemove);
    });
    removeBtn.addEventListener('blur', function(){
      if (removeBtn._armed) resetRemove();
    });
    grid.appendChild(tile);
    if (qList) qList.scrollTop = queueScrollPositions[targetKey(p)] || 0;
  });
  updateSendBtn();
}

function formatBytes(value){
  var n = Math.max(0, Number(value) || 0);
  if (n < 1024 * 1024) return Math.round(n / 1024) + ' KB';
  return (n / (1024 * 1024)).toFixed(1) + ' MB';
}
// "1.2 MB · 22.10, 11:01": size and the time the copy was added, so slices of
// one project that differ only in content can be told apart. Empty when unknown.
function queueMeta(r){
  var parts = [];
  if (Number(r.size) > 0) parts.push(formatBytes(r.size));
  if (Number(r.ts) > 0) {
    try {
      parts.push(new Date(r.ts * 1000).toLocaleString(undefined,
        {day:'numeric', month:'numeric', hour:'2-digit', minute:'2-digit'}));
    } catch (e) {}
  }
  return parts.join(' · ');
}


var historyExpected = null;
var historyRequestSequence = 0;
var historyItems = [];
var historyMode = 'printer';
var historyOpener = null;
var historyStoreUnavailable = false;
var historyHoldsUnavailable = false;
var historyCounts = {};
function historyStateLabel(state){
  return t({ printing:'historyStatePrinting', paused:'historyStatePaused',
    completed:'historyStateCompleted', failed:'historyStateFailed',
    cancelled:'historyStateCancelled', ended:'historyStateEnded',
    sent:'historyStateSent' }[state] || 'historyStateEnded');
}
function historyDuration(seconds){
  var value = Math.max(0, Number(seconds) || 0);
  var hours = Math.floor(value / 3600);
  var minutes = Math.floor((value % 3600) / 60);
  return hours
    ? t('historyDurationHoursMinutes', {hours:hours, minutes:minutes})
    : t('historyDurationMinutes', {minutes:minutes});
}
function beginHistoryView(mode, opener){
  historyMode = mode;
  historyOpener = opener || document.activeElement;
  historyRequestSequence += 1;
  var requestId = historyRequestSequence;
  historyExpected = {mode:mode, requestId:requestId};
  historyItems = [];
  historyStoreUnavailable = false;
  historyHoldsUnavailable = false;
  historyCounts = {};
  journalUi.picker = ''; journalUi.shown = JOURNAL_ROWS;
  document.getElementById('history-note').textContent = t('historyCoverage');
  document.getElementById('history-toolbar').style.display = 'flex';
  document.getElementById('journal-all-label').textContent = t('journalAllCount', {count:0});
  document.getElementById('journal-completed-label').textContent = t('journalCompletedCount', {count:0});
  document.getElementById('journal-active-label').textContent = t('journalActiveCount', {count:0});
  document.getElementById('history-list').innerHTML = '<div class="empty">' + esc(t('historyLoading')) + '</div>';
  document.getElementById('history-modal').style.display = 'flex';
  document.getElementById('journal-search').value = '';
  document.getElementById('journal-search').focus();
  return requestId;
}
function openPrintHistory(printer){
  var requestId = beginHistoryView('printer');
  var identity = targetIdentity(printer);
  historyExpected.targetUuid = identity.targetUuid;
  historyExpected.ownerScope = identity.ownerScope;
  historyExpected.url = identity.url;
  historyExpected.targetSelector = identity.targetSelector;
  historyExpected.bindingRevision = identity.bindingRevision;
  historyExpected.connectionType = identity.connectionType;
  document.getElementById('history-title').textContent = t('historyTitle', {
    printer:printer.name || printer.url
  });
  document.getElementById('journal-all-label').parentNode.style.display = 'none';
  document.getElementById('journal-completed-label').parentNode.style.display = 'none';
  document.getElementById('journal-active-label').parentNode.style.display = 'none';
  identity.type = 'print-history'; identity.requestId = requestId; orca.postMessage(identity);
}
function openAllPrintHistory(){
  var requestId = beginHistoryView('all', this);
  document.getElementById('history-title').textContent = t('journalTitle');
  document.getElementById('journal-all-label').parentNode.style.display = '';
  document.getElementById('journal-completed-label').parentNode.style.display = '';
  document.getElementById('journal-active-label').parentNode.style.display = '';
  document.getElementById('history-note').textContent = t('historyCoverage');
  orca.postMessage({type:'print-history-all', requestId:requestId});
}
function renderJournalHolds(holds){
  var box = document.getElementById('journal-holds');
  box.innerHTML = '';
  if (historyExpected && historyExpected.mode === 'all' && historyHoldsUnavailable) {
    box.style.display = 'block'; box.textContent = t('journalHoldUnavailable'); return;
  }
  if (!Array.isArray(holds) || !holds.length) { box.style.display = 'none'; return; }
  box.style.display = 'block';
  var heading = document.createElement('div'); heading.className = 'history-note';
  heading.textContent = t('journalHoldsHeading'); box.appendChild(heading);
  holds.forEach(function(hold){
    var row = document.createElement('div'); row.className = 'history-item';
    var title = document.createElement('strong');
    title.textContent = (hold.target_name || hold.target || t('journalPrinter')) + ' · ' +
      (hold.artifact_name || hold.action || t('historyUnknownFile'));
    var state = document.createElement('div'); state.className = 'history-meta';
    state.textContent = t('journalHoldState', {state:hold.state});
    row.appendChild(title); row.appendChild(state);
    if (/^(submitted|acknowledged|unknown_outcome)$/.test(hold.state)) {
      var button = document.createElement('button'); button.className = 'ghost';
      button.textContent = t('journalReleaseHold');
      button.addEventListener('click', function(){
        var message = t('journalReleaseConfirm', {target:title.textContent});
        askConfirm(message, t('journalReleaseHold'), function(){
          sendRejectableAction(button, {type:'journal-resolve-hold', operationId:hold.operation_id,
            historyRequestId:historyExpected ? historyExpected.requestId : 0});
        });
      });
      row.appendChild(button);
    } else {
      var locked = document.createElement('div'); locked.className = 'history-meta';
      locked.textContent = t('journalHoldLocked'); row.appendChild(locked);
    }
    box.appendChild(row);
  });
}
function closePrintHistory(){
  historyExpected = null;
  document.getElementById('history-modal').style.display = 'none';
  if (historyOpener && document.contains(historyOpener)) historyOpener.focus();
  historyOpener = null;
}
function renderPrintHistory(payload){
  if (!historyExpected || !payload) return;
  if (payload.requestId !== undefined && payload.requestId !== historyExpected.requestId) return;
  if (historyExpected.mode === 'all') {
    if (payload.mode !== 'all' || payload.requestId !== historyExpected.requestId) return;
    historyCounts = payload.counts || {};
    historyStoreUnavailable = payload.available === false;
    historyHoldsUnavailable = payload.holds_available === false;
    renderJournalHolds(payload.holds || []);
    document.getElementById('history-note').textContent = historyStoreUnavailable
      ? t('journalStoreUnavailable') : t('historyCoverage');
    historyItems = Array.isArray(payload.items) ? payload.items : [];
  } else {
    if (payload.mode === 'all' ||
        historyExpected.targetUuid !== payload.targetUuid ||
        historyExpected.ownerScope !== payload.ownerScope ||
        historyExpected.targetSelector !== payload.targetSelector ||
        historyExpected.bindingRevision !== payload.bindingRevision ||
        historyExpected.url !== payload.printerUrl ||
        historyExpected.connectionType !== payload.connectionType) return;
    if (payload.available !== undefined) {
      historyStoreUnavailable = payload.available === false;
      document.getElementById('history-note').textContent = historyStoreUnavailable
        ? t('journalStoreUnavailable') : t('historyCoverage');
    }
    document.getElementById('history-title').textContent = t('historyTitle', {
      printer:payload.printerName || historyExpected.url
    });
    historyItems = Array.isArray(payload.items) ? payload.items : [];
  }
  renderHistoryItems();
}
function historyRowHaystack(row){
  if (!row.item) return [row.stored.name, row.stored.printer && (row.stored.printer.name || row.stored.printer.url),
    t('journalFileUnsent')].join(' ').toLocaleLowerCase();
  var item = row.item;
  var started = item.started_at ? new Date(item.started_at * 1000).toLocaleString() : '';
  return [item.filename, item.printer_name, item.state, historyStateLabel(item.state), started]
    .join(' ').toLocaleLowerCase();
}
function renderHistoryItems(){
  if (!historyExpected) return;
  var list = document.getElementById('history-list');
  var keepScroll = list.scrollTop || 0;
  renderJournalStorage();
  if (historyStoreUnavailable) {
    document.getElementById('history-results').textContent = '';
    list.innerHTML = '<div class="empty">' + esc(t('journalStoreUnavailable')) + '</div>';
    return;
  }
  var query = String(document.getElementById('journal-search').value || '').trim().toLocaleLowerCase();
  var all = journalRows();
  if (historyMode === 'all') {
    var unsent = all.filter(function(row){ return !row.item; }).length;
    document.getElementById('journal-all-label').textContent = t('journalAllCount', {count:(historyCounts.all || 0) + unsent});
    document.getElementById('journal-completed-label').textContent = t('journalCompletedCount', {count:historyCounts.completed || 0});
    document.getElementById('journal-active-label').textContent = t('journalActiveCount', {count:historyCounts.active || 0});
  }
  var rows = all.filter(function(row){ return !query || historyRowHaystack(row).indexOf(query) >= 0; });
  if (!all.length) {
    document.getElementById('history-results').textContent = t('journalResultsCount', {count:0});
    list.innerHTML = '<div class="empty">' + esc(historyMode === 'all' ? t('journalEmptyAll') : t('historyEmpty')) + '</div>';
    return;
  }
  if (!rows.length) {
    document.getElementById('history-results').textContent = t('journalResultsCount', {count:0});
    list.innerHTML = '<div class="empty">' + esc(t('journalNoResults')) + '</div>';
    return;
  }
  document.getElementById('history-results').textContent = t('journalResultsCount', {count:rows.length});
  list.innerHTML = '';
  rows.slice(0, journalUi.shown).forEach(function(entry){
    var item = entry.item, stored = entry.stored;
    var row = document.createElement('div');
    if (!item) {
      var added = stored.ts ? new Date(stored.ts * 1000).toLocaleString() : '\u2014';
      row.className = 'history-item state-unsent';
      row.innerHTML = '<div class="history-item-top"><span class="history-file" title="' + esc(stored.name) + '">' +
        esc(stored.name) + '</span><span class="history-state" data-state="unsent">' + esc(t('journalFileUnsent')) +
        '</span></div>' +
        (stored.printer ? '<div class="history-printer">' + esc(stored.printer.name || stored.printer.url) + '</div>' : '') +
        '<div class="history-meta"><span>' + esc(added) + '</span><span>' + esc(formatBytes(stored.size)) + '</span></div>';
    } else {
      row.className = 'history-item state-' +
        (/^(printing|paused|completed|failed|cancelled|ended|sent)$/.test(item.state) ? item.state : 'ended');
      var source = item.source === 'print_farm' ? t('historyStartedHere') :
        (item.source === 'orca' ? t('historyFromOrca') : t('historyObserved'));
      var started = item.started_at ? new Date(item.started_at * 1000).toLocaleString() : '\u2014';
      var progress = item.progress == null ? '' : t('historyProgress', {progress:item.progress});
      row.innerHTML = '<div class="history-item-top"><span class="history-file" title="' +
        esc(item.filename || t('historyUnknownFile')) + '">' + esc(item.filename || t('historyUnknownFile')) +
        '</span><span class="history-state" data-state="' + esc(item.state) + '">' + esc(historyStateLabel(item.state)) + '</span></div>' +
        (historyMode === 'all' ? '<div class="history-printer">' + esc(item.printer_name || t('journalPrinter')) + '</div>' : '') +
        '<div class="history-meta"><span>' + esc(started) + '</span><span>' +
        esc(historyDuration(item.duration_seconds)) + '</span><span>' + esc(source) + '</span>' +
        (progress ? '<span>' + esc(progress) + '</span>' : '') +
        (stored ? '<span>' + esc(formatBytes(stored.size)) + '</span>' : '') + '</div>';
    }
    var actions = journalFileActions(entry);
    if (actions) row.appendChild(actions);
    if (stored && !stored.printer && journalUi.picker === stored.file) row.appendChild(journalPicker(stored));
    list.appendChild(row);
  });
  if (rows.length > journalUi.shown) {
    var more = document.createElement('button'); more.className = 'ghost'; more.type = 'button';
    more.textContent = t('storageMore', {count:rows.length - journalUi.shown});
    more.addEventListener('click', function(){ journalUi.shown += JOURNAL_ROWS; renderHistoryItems(); });
    list.appendChild(more);
  }
  list.scrollTop = keepScroll;
}
document.getElementById('history-close').addEventListener('click', closePrintHistory);
document.getElementById('journal-open').addEventListener('click', openAllPrintHistory);
document.getElementById('unassigned-link').addEventListener('click', openAllPrintHistory);
document.getElementById('journal-search').addEventListener('input', renderHistoryItems);
document.getElementById('history-modal').addEventListener('click', function(event){
  if (event.target === this) closePrintHistory();
});
document.addEventListener('keydown', function(event){
  if (document.getElementById('history-modal').style.display !== 'flex') return;
  if (event.key === 'Escape') { event.preventDefault(); closePrintHistory(); }
  if (event.key === 'Tab') {
    var first = document.getElementById('history-close');
    var last = document.getElementById('journal-search');
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  }
});

// <connection-options>
var connectionOptionsTarget = null, connectionOptionsSequence = 0, connectionOptionsOpener = null;
function closeConnectionOptions(){
  connectionOptionsTarget = null;
  document.getElementById('connection-options').style.display = 'none';
  if (connectionOptionsOpener && document.contains(connectionOptionsOpener)) connectionOptionsOpener.focus();
  connectionOptionsOpener = null;
}
function openConnectionOptions(p){
  connectionOptionsOpener = document.activeElement;
  connectionOptionsTarget = targetIdentity(p);
  connectionOptionsTarget.requestId = ++connectionOptionsSequence;
  document.getElementById('connection-options-title').textContent = t('connectionOptions');
  document.getElementById('connection-webui-label').textContent = t('webuiAddress');
  document.getElementById('connection-webui').value = p.webui_url || '';
  document.getElementById('connection-camera-label').textContent = t('cameraMode');
  var select = document.getElementById('connection-camera');
  select.options[0].textContent = t('cameraModeAuto');
  select.options[1].textContent = t('cameraModeU1');
  select.options[1].disabled = ['moonraker','klipper','mainsail','fluidd'].indexOf(String(p.type || '').toLowerCase()) < 0;
  select.value = p.camera_mode || 'auto';
  document.getElementById('connection-options-help').textContent = t('connectionOptionsHelp');
  document.getElementById('connection-options-cancel').textContent = t('cancel');
  var save = document.getElementById('connection-options-save');
  save.textContent = t('connectionSave'); save.disabled = false;
  document.getElementById('connection-options').style.display = 'flex';
  document.getElementById('connection-webui').focus();
}
document.getElementById('connection-options-cancel').addEventListener('click', closeConnectionOptions);
document.getElementById('connection-options-save').addEventListener('click', function(){
  if (!connectionOptionsTarget) return;
  this.disabled = true;
  var request = Object.assign({}, connectionOptionsTarget, {type:'connection-options-save',
    webui_url:document.getElementById('connection-webui').value.trim(),
    camera_mode:document.getElementById('connection-camera').value});
  orca.postMessage(request);
});
document.getElementById('connection-options').addEventListener('click', function(event){
  if (event.target === this) closeConnectionOptions();
});
document.addEventListener('keydown', function(event){
  if (!connectionOptionsTarget) return;
  if (event.key === 'Escape') { event.preventDefault(); closeConnectionOptions(); }
  if (event.key === 'Tab') {
    var first = document.getElementById('connection-webui');
    var last = document.getElementById('connection-options-save');
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  }
});
document.getElementById('feedback').title = t('feedbackButton');
document.getElementById('feedback').setAttribute('aria-label', t('feedbackButton'));
document.getElementById('feedback').addEventListener('click', function(){ orca.postMessage({type:'feedback'}); });
// </connection-options>

// <camera-overlay>
// Live camera overlay. The only <img> that ever loads a camera address lives
// here, and closing the overlay always empties its src, so a closed overlay
// and every card cost no camera traffic. The address (checked by Python to be
// on the printer's own host) reaches the page only as img.src.
var camState = null, camSeq = 0;
var noCamera = {};  // target uuid -> true once a lookup found no camera; page memory only
var CAM_LOOKUP_MS = 12000, CAM_FRAME_MS = 6000, CAM_REFRESH_MS = 2000;
function camEl(id){ return document.getElementById(id); }
function camSafeUrl(value){
  return typeof value === 'string' && /^https?:\/\/[^\s"'<>]+$/i.test(value) ? value : '';
}
function camHaltImage(){
  var img = camEl('cam-img');
  img.onload = null; img.onerror = null;
  img.src = '';
  img.removeAttribute('src');
}
function camClearTimers(){
  if (!camState) return;
  clearTimeout(camState.lookupTimer); clearTimeout(camState.frameTimer); clearTimeout(camState.refreshTimer);
  clearTimeout(camState.leaseTimer); clearTimeout(camState.expiryTimer);
}
function camNote(text, withWindow){
  camEl('cam-note').textContent = text;
  camEl('cam-window').style.display = withWindow ? '' : 'none';
}
function camFail(key, withWindow){
  if (camState && camState.mode === 'native') {
    camState.failed = true;
    camState.sessionId = null;
    var close = Object.assign({}, camState.identity, {type:'camera-native-close', requestId:camState.requestId});
    orca.postMessage(close);
  }
  camClearTimers(); camHaltImage();
  camNote(t(key), withWindow);
}
function closeCamera(restoreFocus){
  var state = camState;
  if (state && state.mode === 'native')
    orca.postMessage(Object.assign({}, state.identity, {type:'camera-native-close', requestId:state.requestId}));
  camClearTimers(); camHaltImage();
  camState = null;
  camEl('cam-modal').style.display = 'none';
  if (!restoreFocus || !state) return;
  var target = state.opener && document.contains(state.opener) ? state.opener : null;
  if (!target && typeof grid !== 'undefined' && grid.querySelectorAll)
    Array.prototype.forEach.call(grid.querySelectorAll('.cam'), function(button){
      if (button.dataset && button.dataset.uuid === state.uuid) target = button; });
  if (target) target.focus();
}
function camGiveUp(state, mode){
  if (camState !== state) return;
  if (mode === 'stream' && state.info.snapshot) camStart('snapshot');
  else camFail('cameraLoadFailed', true);
}
function camStart(mode){
  var state = camState, img = camEl('cam-img');
  camClearTimers(); camHaltImage();
  state.mode = mode; state.frames = 0; state.errors = 0;
  var address = mode === 'stream' ? state.info.stream : state.info.snapshot;
  function shown(){
    if (state.frames++) return;
    clearTimeout(state.frameTimer);
    camNote('', false);
  }
  function load(){
    if (camState !== state || state.mode !== mode) return;
    img.onload = function(){
      if (camState !== state) return;
      state.errors = 0; shown();
      if (mode === 'snapshot') state.refreshTimer = setTimeout(load, CAM_REFRESH_MS);
    };
    img.onerror = function(){
      if (camState !== state) return;
      if (mode === 'snapshot' && state.frames && ++state.errors < 3) {
        state.refreshTimer = setTimeout(load, CAM_REFRESH_MS); return;
      }
      camGiveUp(state, mode);
    };
    img.src = mode === 'stream' ? address
      : address + (address.indexOf('?') < 0 ? '?' : '&') + '_=' + Date.now();
  }
  state.frameTimer = setTimeout(function(){
    if (camState !== state || state.frames) return;
    if (img.naturalWidth > 0) { shown(); return; }  // a stream may not fire load
    camGiveUp(state, mode);
  }, CAM_FRAME_MS);
  camNote(t('cameraLoading'), false);
  load();
}
function openCamera(printer, opener){
  if (camState) closeCamera(false);  // one stream at a time
  var identity = targetIdentity(printer);
  var state = camState = {requestId:++camSeq, opener:opener, uuid:printer.target_uuid,
    identity:targetIdentity(printer), info:null, mode:'', frames:0, errors:0};
  camEl('cam-title').textContent = t('cameraTitle', {printer:printer.name || printer.url});
  camEl('cam-close').setAttribute('aria-label', t('cameraClose'));
  camEl('cam-close').title = t('cameraClose');
  camEl('cam-window').textContent = t('cameraOpenWindow');
  camNote(t('cameraLoading'), false);
  camEl('cam-modal').style.display = 'flex';
  camEl('cam-close').focus();
  state.lookupTimer = setTimeout(function(){
    if (camState === state && !state.info) camFail('cameraUnavailable', false);
  }, CAM_LOOKUP_MS);
  state.mode = printer.camera_mode === 'snapmaker_u1' ? 'native' : '';
  identity.type = state.mode === 'native' ? 'camera-native-open' : 'camera-info'; identity.requestId = state.requestId;
  orca.postMessage(identity);
}
function onCameraNativeFrame(data){
  var state = camState;
  if (!state || state.failed || state.mode !== 'native' || data.requestId !== state.requestId || data.targetUuid !== state.uuid) return;
  if (data.ok !== true) {
    camFail(data.code === 'thread_denied' ? 'cameraPermissionDenied' :
      data.code === 'credential_unavailable' ? 'credentialUnavailable' : 'cameraUnavailable', false); return;
  }
  if (data.started) {
    if (state.sessionId || typeof data.sessionId !== 'string') return;
    state.sessionId = data.sessionId; state.sequence = 0; state.info = {native:true};
    clearTimeout(state.lookupTimer);
    function renew(){
      if (camState !== state || state.failed) return;
      orca.postMessage(Object.assign({}, state.identity, {type:'camera-native-lease',
        requestId:state.requestId, sessionId:state.sessionId}));
      state.leaseTimer = setTimeout(renew, 5000);
    }
    renew();
    state.expiryTimer = setTimeout(function(){ if (camState === state) camFail('cameraUnavailable', false); }, 10000);
    return;
  }
  if (data.sessionId !== state.sessionId || !Number.isSafeInteger(data.sequence) || data.sequence <= state.sequence ||
      typeof data.image !== 'string' || data.image.length > 2800000 ||
      !/^data:image\/jpeg;base64,[A-Za-z0-9+/]+=*$/.test(data.image)) return;
  var received = Number(data.received_at), captured = Number(data.captured_at);
  if (!isFinite(received) || received <= 0 || Date.now()/1000 - received > 10) return;
  state.sequence = data.sequence;
  clearTimeout(state.expiryTimer);
  camEl('cam-img').src = data.image;
  var hasCaptured = data.captured_at != null && isFinite(captured) && captured > 0;
  camNote(t('cameraNativeSession') + ' · ' + t(hasCaptured ? 'cameraCapturedAt' : 'cameraReceivedAt',
    {time:new Date((hasCaptured ? captured : received)*1000).toLocaleTimeString()}), false);
  state.expiryTimer = setTimeout(function(){ if (camState === state) camFail('cameraUnavailable', false); }, 10000);
}
function camMarkNone(state){
  // This printer has no camera: drop its icon until the panel is reloaded.
  noCamera[state.uuid] = true;
  if (typeof grid === 'undefined' || !grid.querySelectorAll) return;
  Array.prototype.forEach.call(grid.querySelectorAll('.cam'), function(button){
    if (button.dataset && button.dataset.uuid === state.uuid && button.remove) button.remove(); });
}
function onCameraInfo(data){
  var state = camState;
  if (!state || state.info || data.requestId !== state.requestId || data.targetUuid !== state.uuid) return;
  clearTimeout(state.lookupTimer);
  if (data.ok !== true) {
    if (data.code === 'no_camera') camMarkNone(state);
    camFail(data.code === 'no_camera' ? 'cameraNone' : 'cameraUnavailable', false); return; }
  state.info = {stream:camSafeUrl(data.stream), snapshot:camSafeUrl(data.snapshot)};
  if (!state.info.stream && !state.info.snapshot) { camMarkNone(state); camFail('cameraNone', false); return; }
  camStart(state.info.stream ? 'stream' : 'snapshot');
}
camEl('cam-close').addEventListener('click', function(){ closeCamera(true); });
camEl('cam-window').addEventListener('click', function(){
  var state = camState;
  if (!state || !state.info) return;
  var request = state.identity; request.type = 'camera-open';
  closeCamera(true);
  orca.postMessage(request);
});
camEl('cam-modal').addEventListener('click', function(event){
  if (event.target === camEl('cam-modal')) closeCamera(true);
});
document.addEventListener('keydown', function(event){
  if (!camState) return;
  if (event.key === 'Escape') { event.preventDefault(); event.stopImmediatePropagation(); closeCamera(true); return; }
  if (event.key === 'Tab') {
    var first = camEl('cam-close');
    var last = camEl('cam-window').style.display === 'none' ? first : camEl('cam-window');
    event.stopImmediatePropagation();
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    else if (document.activeElement !== first && document.activeElement !== last) { event.preventDefault(); first.focus(); }
  }
}, true);
document.addEventListener('visibilitychange', function(){
  if (document.hidden && camState) closeCamera(false);
});
// </camera-overlay>

// Bambu (LAN) needs a serial + access code. Authenticated OctoPrint instances
// need an API key when they are added manually; Orca-discovered keys stay in
// Python and are never reflected into this page.
var typeSel = document.getElementById('f-type');
var networkPrefillName = null;
var manualForm = document.getElementById('manual-form');
var manualToggle = document.getElementById('manual-toggle');
function setManualFormOpen(open){
  manualForm.classList.toggle('open', !!open);
  manualForm.setAttribute('aria-hidden', open ? 'false' : 'true');
  manualToggle.setAttribute('aria-expanded', open ? 'true' : 'false');
}
manualToggle.addEventListener('click', function(){
  setManualFormOpen(!manualForm.classList.contains('open'));
});
var repetierRequest = 0;
var repetierSelection = document.getElementById("repetier-printer");
function resetRepetier(){
  ++repetierRequest; repetierSelection.textContent = "";
  var option = document.createElement("option"); option.value = "";
  option.textContent = t("repetierSelectPrinter"); repetierSelection.appendChild(option);
  document.getElementById("repetier-load").disabled = false;
}
document.getElementById("f-url").addEventListener("input", resetRepetier);
document.getElementById("f-apikey").addEventListener("input", resetRepetier);
document.getElementById("f-insecure").addEventListener("change", resetRepetier);
document.getElementById("repetier-load").addEventListener("click", function(){
  resetRepetier(); this.disabled = true;
  orca.postMessage({type:"repetier-list", requestId:repetierRequest,
    url:document.getElementById("f-url").value.trim(),
    apikey:document.getElementById("f-apikey").value,
    insecure:document.getElementById("f-insecure").checked});
});
function syncTypeFields(){
  resetRepetier();
  document.getElementById("repetier-load").style.display = typeSel.value === "repetier" ? "" : "none";
  document.getElementById("repetier-load").textContent = t("repetierLoadPrinters");
  repetierSelection.style.display = typeSel.value === "repetier" ? "" : "none";
  var bambu = typeSel.value === 'bambu';
  var needsCredential = CREDENTIAL_TYPES.indexOf(typeSel.value) >= 0;
  // The serial is never typed: the MQTT wildcard learns it on first poll and
  // Network announcements do not establish identity; MQTT learns the serial.
  document.getElementById('f-access').style.display = bambu ? '' : 'none';
  var flashforge = typeSel.value === 'flashforge';
  var serialField = document.getElementById('f-serial');
  serialField.style.display = flashforge ? '' : 'none';
  serialField.placeholder = flashforge ? t('flashforgeSerialPlaceholder') : t('serialAuto');
  serialField.title = flashforge ? t('flashforgeSerialTitle') : t('serialTitle');
  var secretField = document.getElementById('f-apikey');
  secretField.style.display = needsCredential ? '' : 'none';
  secretField.placeholder = typeSel.value === 'duet' ? t('duetPasswordPlaceholder') :
    flashforge ? t('flashforgeCheckCodePlaceholder') :
    typeSel.value === 'prusalink' ? t('prusalinkApiKeyPlaceholder') :
    typeSel.value === 'repetier' ? t('repetierApiKeyPlaceholder') : t('octoprintApiKeyPlaceholder');
  secretField.title = flashforge ? t('flashforgeCheckCodeTitle') : '';
  document.getElementById('f-insecure-l').style.display = '';
  document.getElementById('f-url').placeholder = bambu ? t('printerIpPlaceholder') : '192.168.0.42';
  document.getElementById('f-wsport').style.display = typeSel.value === 'crealityprint' ? '' : 'none';
}
typeSel.addEventListener('change', syncTypeFields);
syncTypeFields();

document.getElementById('add').addEventListener('click', function(){
  var name = document.getElementById('f-name').value.trim();
  var url = document.getElementById('f-url').value.trim();
  if (!url) return;
  var access = document.getElementById('f-access').value;
  var apiKey = document.getElementById('f-apikey').value;
  if (typeSel.value === 'bambu' && !access) {
    document.getElementById('f-access').focus();
    return;
  }
  if (typeSel.value === 'flashforge') {
    if (!document.getElementById('f-serial').value.trim()) { document.getElementById('f-serial').focus(); return; }
    if (!apiKey) { document.getElementById('f-apikey').focus(); return; }
  }
  if (typeSel.value === 'repetier' && !repetierSelection.value) { repetierSelection.focus(); return; }
  pendingAdoptUrl = url;
  pendingFormAdd = true;
  document.getElementById('add').disabled = true;
  document.getElementById('status').textContent = t('loading');
  orca.postMessage({ type:'add', name:name, url:url,
                     nameUserChosen:!!name && name !== networkPrefillName,
                     kind:typeSel.value, repetier_slug:typeSel.value === 'repetier' ? repetierSelection.value : '',
                     serial:(typeSel.value === 'bambu' || typeSel.value === 'flashforge') ? document.getElementById('f-serial').value.trim() : '',
                     access_code:access,
                      apikey:CREDENTIAL_TYPES.indexOf(typeSel.value) >= 0 ? apiKey : '',
                     ws_port:typeSel.value === 'crealityprint' ? document.getElementById('f-wsport').value.trim() : '',
                     insecure:document.getElementById('f-insecure').checked });
});
document.getElementById('refresh').addEventListener('click', function(){ orca.postMessage({ type:'refresh' }); });
document.getElementById('legacy-import-button').addEventListener('click', function(){
  askConfirm(t('legacyImportConfirm'), t('legacyImportButton'), function(){
    orca.postMessage({type:'legacy-import-confirm', confirmation:window.legacyImportConfirmation || ''});
  });
});

// Print flow freezes each selected connection binding before file selection.
var pending = null;
var pendingTargets = null;
var transfer = null;
var TRANSFER_CHUNK_BYTES = 32 * 1024;
function transferFailed(){
  transfer = null;
  pending = null;
  document.getElementById('status').textContent = t('fileReadFailed');
}
function sendNextChunk(){
  var active = transfer;
  if (!active || !active.token || active.reading) return;
  if (active.offset === active.file.size) {
    orca.postMessage({type:'artifact-finish', token:active.token});
    return;
  }
  active.reading = true;
  var slice = active.file.slice(active.offset, active.offset + TRANSFER_CHUNK_BYTES);
  slice.arrayBuffer().then(function(buffer){
    if (transfer !== active) return;
    var bytes = new Uint8Array(buffer);
    if (!bytes.length || bytes.length > TRANSFER_CHUNK_BYTES) throw new Error('invalid chunk');
    var raw = '';
    for (var i = 0; i < bytes.length; i++) raw += String.fromCharCode(bytes[i]);
    var postChunk = function(digest){
      active.inflight = bytes.length;
      active.reading = false;
      orca.postMessage({type:'artifact-chunk', token:active.token,
        sequence:active.sequence, dataB64:btoa(raw), sha256:digest});
    };
    if (window.crypto && crypto.subtle) {
      crypto.subtle.digest('SHA-256', buffer).then(function(hash){
        postChunk(Array.from(new Uint8Array(hash)).map(function(n){
          return n.toString(16).padStart(2, '0');
        }).join(''));
      }).catch(transferFailed);
    } else postChunk(null);
  }).catch(transferFailed);
}
function pickAndSend(targetKeys){
  if (!targetKeys || !targetKeys.length) return;
  pendingTargets = current.filter(function(p){ return targetKeys.indexOf(targetKey(p)) >= 0; }).map(targetIdentity);
  if (!pendingTargets.length) return;
  fileInput.click();
}
sendBtn.addEventListener('click', function(){ pickAndSend(Object.keys(selected).filter(function(k){ return selected[k]; })); });
fileInput.addEventListener('change', function(){
  var file = fileInput.files && fileInput.files[0]; fileInput.value = '';
  var targets = pendingTargets; pendingTargets = null;
  if (!file || !targets || !targets.length) return;
  pending = {file:file, targets:targets};
  transfer = {file:file, offset:0, sequence:0, reading:false, inflight:0, token:null};
  document.getElementById('status').textContent = t('loading');
  orca.postMessage({type:'artifact-begin', filename:file.name, size:file.size});
});
function resetStartReview(){
  document.getElementById('confirm-title').textContent = t('confirmUploadTitle');
  document.getElementById('confirm-ok').textContent = t('send');
  document.getElementById('confirm-ok').disabled = false;
  ['confirm-start', 'confirm-ams', 'confirm-plate'].forEach(function(id){
    document.getElementById(id).disabled = false;
  });
}
function startReviewReady(row){
  return !!row.bed && row.bed.checked && (!row.check || row.check.checked);
}
function updateStartReview(){
  if (!pending || !pending.review) return;
  var count = pending.review.rows.filter(startReviewReady).length;
  pending.review.summary.textContent = t('startWillStart', {count:count});
  document.getElementById('confirm-ok').disabled = count === 0;
}
function addReviewBox(row, key){
  var label = document.createElement('label');
  label.style.cssText = 'display:flex;align-items:flex-start;gap:6px;margin-top:4px;color:var(--orca-fg,#e0e0e0)';
  var box = document.createElement('input'); box.type = 'checkbox';
  box.addEventListener('change', updateStartReview);
  var text = document.createElement('span'); text.textContent = t(key);
  label.appendChild(box); label.appendChild(text); row.appendChild(label);
  return box;
}
// One row per selected printer: verdict, exact reason, and the confirmations
// that this print needs. Only printers whose boxes are all ticked will start.
function renderStartReview(prepared, plate){
  var body = document.getElementById('confirm-body');
  body.textContent = '';
  var intro = document.createElement('div');
  intro.textContent = pending.file.name + (plate ? ' / ' + plate : '') +
    (prepared.profile ? ' \u00b7 ' + prepared.profile : '');
  body.appendChild(intro);
  var labels = {compatible:'startVerdictCompatible', unknown:'startVerdictUnknown',
    incompatible:'startVerdictIncompatible'};
  var colors = {compatible:'#4caf50', unknown:'#d6a43a', incompatible:'#c0504d'};
  var rows = [];
  (prepared.targets || []).forEach(function(grant){
    var verdict = labels[grant.verdict] ? grant.verdict : 'unknown';
    var row = document.createElement('div');
    row.style.cssText = 'margin-top:8px;padding-top:6px;border-top:1px solid var(--orca-border,#3c3c4c)';
    var head = document.createElement('div');
    var name = document.createElement('b'); name.textContent = grant.printer;
    var tag = document.createElement('span'); tag.style.color = colors[verdict];
    tag.textContent = ' \u2014 ' + t(labels[verdict]);
    head.appendChild(name); head.appendChild(tag); row.appendChild(head);
    var why = document.createElement('div'); why.textContent = t(grant.reason); row.appendChild(why);
    var entry = {grant:grant, bed:null, check:null};
    if (verdict !== 'incompatible' && grant.nonce) {
      if (verdict === 'unknown') entry.check = addReviewBox(row, 'startFileCheck');
      entry.bed = addReviewBox(row, 'startBedClear');
    }
    body.appendChild(row); rows.push(entry);
  });
  var summary = document.createElement('div'); summary.style.marginTop = '8px';
  body.appendChild(summary);
  pending.review = {rows:rows, summary:summary, plate:plate,
    useAms:document.getElementById('confirm-ams').checked};
  document.getElementById('confirm-title').textContent = t('startReviewTitle');
  document.getElementById('confirm-ok').textContent = t('startPrint');
  ['confirm-start', 'confirm-ams', 'confirm-plate'].forEach(function(id){
    document.getElementById(id).disabled = true;
  });
  updateStartReview();
}
function submitStartReview(){
  var review = pending.review;
  var ready = review.rows.filter(startReviewReady);
  var targets = ready.map(function(row){
    return pending.targets.find(function(identity){ return identity.targetUuid === row.grant.targetUuid; });
  });
  if (!ready.length || targets.some(function(identity){ return !identity; })) return;
  var file = pending.file, token = pending.artifactToken;
  pending = null; document.getElementById('confirm').style.display = 'none';
  resetStartReview();
  document.getElementById('status').textContent = t('sendingFile',
    {name:file.name, count:targets.length});
  orca.postMessage({type:'mass-print', artifactToken:token, plate:review.plate,
    targets:targets, start:true, use_ams:review.useAms,
    approvals:ready.map(function(row){
      return {targetUuid:row.grant.targetUuid, nonce:row.grant.nonce, bedClear:true,
        operatorChecked:!!(row.check && row.check.checked)};
    })});
}
document.getElementById('confirm-cancel').addEventListener('click', function(){
  if (pending && pending.artifactToken)
    orca.postMessage({type:'artifact-discard', token:pending.artifactToken});
  pending = null; document.getElementById('confirm').style.display = 'none';
  resetStartReview(); });
document.getElementById('confirm-ok').addEventListener('click', function(){
  if (!pending || !pending.artifactToken) return;
  if (pending.review) { submitStartReview(); return; }
  var start = document.getElementById('confirm-start').checked;
  var file = pending.file, targets = pending.targets, artifactToken = pending.artifactToken;
  var plate = document.getElementById('confirm-plate').value;
  var useAms = document.getElementById('confirm-ams').checked;
  if (start) {
    orca.postMessage({type:'mass-start-prepare', artifactToken:artifactToken,
      plate:plate, targets:targets, use_ams:useAms});
    return;
  }
  pending = null; document.getElementById('confirm').style.display = 'none';
  document.getElementById('status').textContent = t('sendingFile', {name:file.name, count:targets.length});
  orca.postMessage({type:'mass-print', artifactToken:artifactToken, plate:plate,
    targets:targets, start:false, use_ams:useAms});
});

// "Add from OrcaSlicer": populate a picker with printers Orca knows about that
// are not on the dashboard yet; choosing one adopts it (user decides, no auto-add).
var lastAvailable = [];
var lastDiscovered = [];
var pendingAdoptUrl = '';
var pendingAdoptRef = '';
var pendingFormAdd = false;
function normalizedPrinterUrl(value){
  var url = String(value || '').trim().replace(/\/+$/, '').toLowerCase();
  if (url && url.indexOf('http://') !== 0 && url.indexOf('https://') !== 0)
    url = 'http://' + url;
  return url;
}
function renderAvailable(){
  var sel = document.getElementById('f-adopt');
  var wrap = document.getElementById('adopt-wrap');
  var activeUrls = {};
  var activeSerials = {};
  var activeRefs = {};
  current.forEach(function(p){
    activeUrls[normalizedPrinterUrl(p.url)] = true;
    if (p.serial) activeSerials[String(p.serial).trim()] = true;
    if (p.source === 'orca' && p.discovery_ref) activeRefs[p.discovery_ref] = true;
  });
  var items = lastAvailable.concat(lastDiscovered).filter(function(p){
    if (p.source === 'orca') return !activeRefs[p.discovery_ref];
    return !activeUrls[normalizedPrinterUrl(p.url)] &&
      !(p.serial && activeSerials[String(p.serial).trim()]);
  });
  if (!items.length){
    wrap.style.display = 'none'; sel.innerHTML = ''; sel.disabled = false;
    return;
  }
  var opts = '<option value="">' + esc(t('addPrinter')) + '</option>';
  items.forEach(function(p, i){
    var label = (p.source === 'network') ? (p.name + ' — ' + t('foundOnNetwork')) : (p.name || p.url);
    var ready = backendReady(p.type);
    opts += '<option value="' + i + '"' + (ready ? '' : ' disabled') + '>' +
      esc(label + (ready ? '' : ' — ' + t('unavailableSuffix'))) + '</option>';
  });
  sel.innerHTML = opts;
  wrap.style.display = '';
  sel._items = items;
  if (pendingAdoptUrl) {
    var pendingIndex = items.findIndex(function(p){
      return pendingAdoptRef ? p.discovery_ref === pendingAdoptRef : p.url === pendingAdoptUrl;
    });
    if (pendingIndex >= 0) sel.value = String(pendingIndex);
  }
  sel.disabled = !!pendingAdoptUrl;
}
document.getElementById('f-adopt').addEventListener('change', function(e){
  var i = e.target.value;
  if (i === '') return;
  var p = (e.target._items || [])[i];
  if (!p) return;
  if (p.source === 'network') {
    // SSDP finds the printer but not its access code — prefill the form and
    // let the user finish the add with the code from the printer's screen.
    document.getElementById('f-name').value = p.name || '';
    networkPrefillName = p.name || '';
    document.getElementById('f-url').value = p.url || '';
    document.getElementById('f-type').value = 'bambu';
    setManualFormOpen(true);
    syncTypeFields();
    document.getElementById('f-serial').value = '';
    document.getElementById('f-access').focus();
    e.target.value = '';
    return;
  }
  pendingAdoptUrl = p.url || '';
  pendingAdoptRef = p.discovery_ref || '';
  e.target.disabled = true;
  document.getElementById('status').textContent = t('loading');
  orca.postMessage({ type:'adopt', discoveryRef:p.discovery_ref });
});

orca.onMessage(function(data){
  if (!data) return;
  if (data.tlsTrustResult && tlsDialogCard &&
      data.tlsTrustResult.requestId === tlsDialogRequest) {
    var trust = data.tlsTrustResult;
    var note = document.getElementById('tls-note');
    if (trust.error) {
      tlsDialogToken = null;
      document.getElementById('tls-confirm').disabled = true;
      note.textContent = t(trust.error);
    } else if (trust.saved) {
      tlsDialogToken = null;
      document.getElementById('tls-confirm').disabled = true;
      document.getElementById('tls-probe').disabled = true;
      note.textContent = t('tlsTrustSaved');
    } else if (trust.token && trust.protocol === document.getElementById('tls-protocol').value) {
      tlsDialogToken = trust.token;
      document.getElementById('tls-address').textContent =
        String(trust.address || '') + ' / ' + trust.protocol.toUpperCase() + ':' + trust.port;
      document.getElementById('tls-current').textContent = trust.old || t('tlsTrustNoPin');
      document.getElementById('tls-observed').textContent = trust.observed || '';
      note.textContent = trust.old && trust.old !== trust.observed
        ? t('tlsTrustReplacement') : t('tlsTrustIntro');
      document.getElementById('tls-confirm').disabled = false;
    }
  }
  if (data.actionRejected && Number.isSafeInteger(data.actionRejected.requestId)) {
    var rejectedId = data.actionRejected.requestId;
    var rejectedCallback = actionRejectCallbacks[rejectedId];
    delete actionRejectCallbacks[rejectedId];
    if (rejectedCallback) rejectedCallback();
  }
  if (data.artifactBegin && transfer && !transfer.token) {
    transfer.token = data.artifactBegin.token;
    sendNextChunk();
  }
  if (data.artifactChunk && transfer && data.artifactChunk.token === transfer.token) {
    if (data.artifactChunk.sequence !== transfer.sequence || !transfer.inflight) {
      transferFailed();
    } else {
      transfer.offset += transfer.inflight;
      transfer.inflight = 0;
      transfer.sequence += 1;
      sendNextChunk();
    }
  }
  if (data.artifactReady && transfer && data.artifactReady.token === transfer.token && pending) {
    pending.artifactToken = transfer.token;
    transfer = null;
    resetStartReview();
    var names = pending.targets.map(function(identity){
      var printer = current.find(function(p){ return p.target_uuid === identity.targetUuid; });
      return esc(printer ? (printer.name || printer.url) : identity.url);
    });
    var hasBambu = pending.targets.some(function(p){ return (p.connectionType || '').toLowerCase() === 'bambu'; });
    document.getElementById('confirm-ams-l').style.display = hasBambu ? 'flex' : 'none';
    var plates = data.artifactReady.plates || [];
    var plateSelect = document.getElementById('confirm-plate');
    plateSelect.textContent = '';
    plates.forEach(function(entry){
      var option = document.createElement('option');
      option.value = entry;
      option.textContent = entry;
      plateSelect.appendChild(option);
    });
    document.getElementById('confirm-plate-l').style.display = plates.length ? 'flex' : 'none';
    document.getElementById('confirm-body').innerHTML = t('uploadToPrinters', {
      name:'<b>' + esc(pending.file.name) + '</b>', count:pending.targets.length}) + '<br>' + names.join(', ');
    document.getElementById('confirm').style.display = 'flex';
    document.getElementById('status').textContent = '';
  }
  if (data.artifactError) transferFailed();
  if (data.massStartApprovals && pending &&
      data.massStartApprovals.artifactToken === pending.artifactToken) {
    var prepared = data.massStartApprovals;
    var selectedPlate = document.getElementById('confirm-plate').value || null;
    if (selectedPlate !== prepared.plate ||
        !document.getElementById('confirm-start').checked) return;
    var grants = prepared.targets || [];
    if (grants.length !== pending.targets.length) return;
    renderStartReview(prepared, selectedPlate);
  }
  if (data.outboxStartApproval) {
    var item = data.outboxStartApproval;
    if (item.verdict === 'incompatible' || !item.nonce) {
      document.getElementById('status').textContent = t('startBlocked',
        {printer:item.printer, reason:t(item.reason)});
    } else {
      var summary = item.filename + '\n' + (item.profile || '') + '\n' + item.printer + '\n' + t(item.reason);
      var sendStart = function(){
        var request = item.target;
        request.type = 'outbox-send'; request.file = item.file;
        request.start = item.unassigned ? item.start === true : true;
        request.approvalNonce = item.nonce;
        request.bedClear = request.start; request.operatorChecked = item.verdict === 'unknown';
        request.use_ams = request.start;
        if (item.unassigned) request.unassigned = true;
        orca.postMessage(request);
      };
      if (item.unassigned && item.start !== true) {
        askConfirm(t('startFileCheck') + '\n\n' + summary, t('uploadShort'), sendStart);
        return;
      }
      var askBedClear = function(){
        askConfirm(t('startBedClear') + '\n\n' + summary, t('startPrint'), sendStart);
      };
      if (item.verdict !== 'unknown') askBedClear();
      else askConfirm(t('startFileCheck') + '\n\n' + summary, t('startPrint'), askBedClear);
    }
  }
  if (data.legacyImport) window.legacyImportConfirmation = data.legacyImport.confirmation || '';
  if (data.unassignedOutbox) window.lastUnassignedOutbox = data.unassignedOutbox;
  if (data.outboxPhysicalUsage) window.lastOutboxStorage = data.outboxPhysicalUsage;
  if (data.credentialMigration) window.credentialMigration = data.credentialMigration;
  if (data.printers) {
    if (tlsDialogCard) {
      var previousTlsCard = tlsDialogCard;
      tlsDialogCard = data.printers.find(function(p){
        return p.target_uuid === previousTlsCard.target_uuid &&
          p.owner_scope_uuid === previousTlsCard.owner_scope_uuid &&
          p.target_selector === previousTlsCard.target_selector && p.type === 'bambu';
      }) || null;
      if (!tlsDialogCard) closeTlsTrust();
      else document.getElementById('tls-probe').disabled = false;
    }
    render(data.printers); renderAvailable();
    // An open journal follows the store: a send or delete changes both lists.
    if (historyExpected && historyExpected.mode === 'all') {
      historyRequestSequence += 1; historyExpected.requestId = historyRequestSequence;
      orca.postMessage({type:'print-history-all', requestId:historyRequestSequence});
    }
  }
  if (data.printerStatus) {
    current.forEach(function(p){
      if (p.target_uuid === data.printerStatus.targetUuid &&
          p.owner_scope_uuid === data.printerStatus.ownerScope &&
          p.target_selector === data.printerStatus.targetSelector &&
          p.binding_revision === data.printerStatus.bindingRevision &&
          p.url === data.printerStatus.url &&
          p.type === data.printerStatus.connectionType)
        p.status = data.printerStatus.status;
    });
    render(current);
  }
  if (data.available) { lastAvailable = data.available; renderAvailable(); }
  if (data.discovered) { lastDiscovered = data.discovered; renderAvailable(); }
  if (data.repetierPrinters && data.repetierPrinters.requestId === repetierRequest && typeSel.value === "repetier") {
    var reply = data.repetierPrinters;
    document.getElementById("repetier-load").disabled = false;
    if (!reply.ok) document.getElementById("status").textContent = t("repetierLoadFailed");
    else (reply.printers || []).forEach(function(p){
      var option = document.createElement("option"); option.value = p.slug;
      option.textContent = p.name + " — " + p.slug; repetierSelection.appendChild(option);
    });
  }
  if (data.adoptResult) {
    var completedFormAdd = pendingFormAdd;
    pendingAdoptUrl = '';
    pendingAdoptRef = '';
    pendingFormAdd = false;
    document.getElementById('f-adopt').disabled = false;
    document.getElementById('add').disabled = false;
    if (!data.adoptResult.ok) {
      document.getElementById('status').textContent = t('credentialSaveFailed');
    } else {
      document.getElementById('status').textContent = '';
      if (completedFormAdd) {
        document.getElementById('f-name').value = '';
        document.getElementById('f-url').value = '';
        document.getElementById('f-serial').value = '';
        document.getElementById('f-access').value = '';
        document.getElementById('f-apikey').value = '';
        document.getElementById('f-wsport').value = '';
        document.getElementById('f-insecure').checked = false;
        setManualFormOpen(false);
      }
    }
    renderAvailable();
  }
  if (data.settings) applyPollSettings(data.settings);
  if (data.printHistory) renderPrintHistory(data.printHistory);
  if (typeof data.outboxHint === 'boolean') {
    document.getElementById('outbox-hint-text').textContent = data.outboxHint ? t('outboxHintText') : '';
    document.getElementById('outbox-hint-where').textContent = data.outboxHint ? t('outboxHintWhere') : '';
    document.getElementById('outbox-hint').style.display = data.outboxHint ? '' : 'none';
  }
  if (data.cameraInfo) onCameraInfo(data.cameraInfo);
  if (data.cameraNativeFrame) onCameraNativeFrame(data.cameraNativeFrame);
  if (data.connectionOptionsResult && connectionOptionsTarget &&
      data.connectionOptionsResult.requestId === connectionOptionsTarget.requestId &&
      data.connectionOptionsResult.targetUuid === connectionOptionsTarget.targetUuid) {
    document.getElementById('connection-options-save').disabled = false;
    if (data.connectionOptionsResult.ok) { closeCamera(false); closeConnectionOptions(); noCamera = {}; }
    else document.getElementById('connection-options-help').textContent = t('connectionOptionsFailed');
  }
  if (data.legacyImport) {
    var preview = data.legacyImport;
    var panel = document.getElementById('legacy-import');
    panel.style.display = preview.available ? 'flex' : 'none';
    if (preview.available) {
      document.getElementById('legacy-import-text').textContent = t('legacyImportDetail', {
        printers:preview.printers, history:preview.history,
        outbox:preview.outbox, files:preview.outbox_files
      });
      document.getElementById('legacy-import-button').textContent = t('legacyImportButton');
    }
  }
  if (data.status !== undefined) document.getElementById('status').textContent = data.status;
});
orca.postMessage({ type:'ready' });
// Live dashboard: refresh statuses while the tab is open. Status-only tick —
// the store and picker are rebuilt on initial load, explicit Refresh and actions.
var pollSettings = { seconds:15, pauseHidden:true };
var pollTimer = null;
function pollTick(){
  if (pollSettings.pauseHidden && document.hidden) return;
  try { orca.postMessage({ type:'poll' }); } catch (e) { /* bridge not ready */ }
}
function applyPollSettings(next){
  var seconds = [5, 15, 30, 60].indexOf(next && next.pollSeconds) >= 0 ? next.pollSeconds : 15;
  pollSettings.pauseHidden = !(next && next.pauseHidden === false);
  if (pollTimer !== null && seconds === pollSettings.seconds) return;
  pollSettings.seconds = seconds;
  if (pollTimer !== null) clearInterval(pollTimer);
  pollTimer = setInterval(pollTick, seconds * 1000);
}
applyPollSettings(null);
document.addEventListener('visibilitychange', function(){
  if (!document.hidden && pollSettings.pauseHidden) pollTick();
});
</script>
</body>
</html>
"""


def _active_host_types():
    known = (_MOONRAKER_COMPATIBLE | _OCTOPRINT_COMPATIBLE | {"bambu"}
             | set(_ADAPTER_BACKENDS))
    return sorted(host_type for host_type in known if backend_is_active(host_type))


def _connection_options_html():
    rows = []
    for value, label in _CONNECTION_TYPES:
        if backend_is_active(value):
            rows.append('        <option value="%s">%s</option>' % (value, label))
        else:
            rows.append('        <option value="%s" disabled>%s \u2014 coming later</option>'
                        % (value, label))
    return "\n".join(rows)


def render_page():
    language = refresh_ui_language()
    return PAGE.replace(
        "__UI_COPY__",
        json.dumps(UI_COPY, ensure_ascii=False).replace("</", "<\\/"),
    ).replace("__HOST_UI_LANGUAGE__", language).replace(
        "__TYPE_OPTIONS__", _connection_options_html(),
    ).replace(
        "__ACTIVE_TYPES__", json.dumps(_active_host_types()),
    ).replace(
        "__ADAPTER_TYPES__", json.dumps(sorted(_ADAPTER_BACKENDS)),
    ).replace(
        "__CREDENTIAL_TYPES__", json.dumps(sorted(_CREDENTIAL_TYPES)),
    ).replace(
        "__CAMERA_TYPES__", json.dumps(sorted(_CAMERA_TYPES)),
    )


# A printer UI stays inside a separate host window. The parent document permits
# only the selected printer origin, and the iframe cannot open popups or replace
# Orca's top-level page.
VIEWER_PAGE = """<!DOCTYPE html><html><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; frame-src __ORIGIN__">
<style>html,body{margin:0;height:100%;font:12px sans-serif}body{display:flex;flex-direction:column}
header{padding:8px;display:flex;flex-wrap:wrap;gap:6px}input{width:min(65ch,100%);box-sizing:border-box}
p{margin:0;width:100%}iframe{border:0;width:100%;flex:1;min-height:0;display:block}</style>
</head><body><header><input readonly aria-label="URL" value="__URL__"><p>__HELP__</p></header>
<iframe src="__URL__" sandbox="allow-same-origin allow-scripts allow-forms allow-downloads allow-modals allow-pointer-lock" allow="fullscreen" referrerpolicy="no-referrer"></iframe></body></html>"""


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
    ).replace(
        "__HELP__", html.escape(ui_text("webuiFallbackHelp")),
    )


def validated_webui_url(value, api_url):
    if not value:
        return ""
    if not isinstance(value, str) or any(ord(c) < 32 or ord(c) == 127 for c in value):
        return ""
    safe = validated_printer_web_url(value)
    api = validated_printer_web_url(api_url)
    if not safe or not api:
        return ""
    return safe if urllib.parse.urlsplit(safe).hostname == urllib.parse.urlsplit(api).hostname else ""


def printer_webui_url(printer):
    return (validated_webui_url(printer.get("webui_url"), printer.get("url"))
            or validated_printer_web_url(printer.get("url")))


def render_feedback_page():
    labels = json.dumps({"copied": ui_text("feedbackCopied")}, ensure_ascii=True).replace("<", "\\u003c")
    return ('<!DOCTYPE html><html><head><meta charset="utf-8">'
        '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; script-src \'unsafe-inline\'">'
        '<style>body{font:14px sans-serif;padding:16px}.row{display:flex;gap:8px;align-items:center}'
        'input{width:min(40ch,100%);box-sizing:border-box}#note{font-size:12px;min-height:1.2em;margin-top:8px}</style>'
        '</head><body><p>' + html.escape(ui_text("feedbackHelp")) + '</p>'
        '<div class="row"><input id="url" readonly aria-label="URL" value="https://filamenthub.ru">'
        '<button id="copy" type="button">' + html.escape(ui_text("feedbackCopy")) + '</button></div>'
        '<div id="note" role="status"></div>'
        '<script>var L=' + labels + ';var u=document.getElementById("url");'
        'document.getElementById("copy").addEventListener("click",function(){'
        'u.focus();u.select();var ok=false;'
        'try{ok=document.execCommand("copy");}catch(e){}'
        'if(!ok&&navigator.clipboard){navigator.clipboard.writeText(u.value).then(function(){'
        'document.getElementById("note").textContent=L.copied;});return;}'
        'if(ok)document.getElementById("note").textContent=L.copied;});</script>'
        '</body></html>')


# --------------------------------------------------------------------------- #
# Live camera (Moonraker / OctoPrint, read-only). The camera address is looked
# up lazily when the user opens the overlay, never during ordinary polling, and
# is accepted only when it points at the printer's own host.
# --------------------------------------------------------------------------- #
CAMERA_TIMEOUT = 5
_CAMERA_TYPES = _MOONRAKER_COMPATIBLE | _OCTOPRINT_COMPATIBLE
_CAMERA_CODES = frozenset({"no_camera", "unavailable"})
_CAMERA_URL_MAX = 2048
# Moonraker services an <img> can show; WebRTC/HLS/iframe streams cannot.
_MOONRAKER_MJPEG_SERVICES = frozenset({
    "", "mjpegstreamer", "mjpegstreamer-adaptive", "uv4l-mjpeg", "ipstream"})


def validated_camera_url(value, printer_url, webui_url=None):
    """Absolute http(s) camera URL on the printer's own host, or ""."""
    if not isinstance(value, str):
        return ""
    value = value.strip()
    base = validated_printer_web_url(printer_url)
    if not value or len(value) > _CAMERA_URL_MAX or not base:
        return ""
    try:
        origin = validated_webui_url(webui_url, printer_url) or base
        joined = urllib.parse.urljoin(origin + "/", value)
    except ValueError:
        return ""
    if any(char.isspace() for char in joined):
        return ""
    safe = validated_printer_web_url(joined)
    if not safe:
        return ""
    if joined.endswith("/") and not safe.endswith("/"):
        safe += "/"  # validation trims one trailing slash; a stream path may need it
    try:
        same_host = (urllib.parse.urlsplit(safe).hostname or "").lower() == \
            (urllib.parse.urlsplit(base).hostname or "").lower()
    except ValueError:
        return ""
    return safe if same_host else ""


def _camera_result(stream="", snapshot="", code=None):
    if code:
        return {"ok": False, "code": code, "stream": "", "snapshot": ""}
    if not (stream or snapshot):
        return {"ok": False, "code": "no_camera", "stream": "", "snapshot": ""}
    return {"ok": True, "code": "", "stream": stream, "snapshot": snapshot}


def _moonraker_camera(printer, base):
    try:
        data = _get_json(base + "/server/webcams/list", ssl_ctx_for(printer),
                         dict(moonraker_headers(printer), Accept="application/json"),
                         timeout=CAMERA_TIMEOUT)
    except urllib.error.HTTPError as exc:
        return _camera_result(code="no_camera" if exc.code == 404 else "unavailable")
    result = data.get("result") if isinstance(data, dict) else None
    cams = result.get("webcams") if isinstance(result, dict) else None
    for cam in cams if isinstance(cams, list) else []:
        if not isinstance(cam, dict) or cam.get("enabled") is False:
            continue
        service = cam.get("service")
        service = service.strip().lower() if isinstance(service, str) else ""
        stream = (validated_camera_url(cam.get("stream_url"), base, printer.get("webui_url"))
                  if service in _MOONRAKER_MJPEG_SERVICES else "")
        snapshot = validated_camera_url(cam.get("snapshot_url"), base, printer.get("webui_url"))
        if stream or snapshot:
            return _camera_result(stream, snapshot)
    return _camera_result()


def _octoprint_camera(printer, base):
    headers = {"Accept": "application/json"}
    if printer.get("apikey"):
        headers["X-Api-Key"] = printer["apikey"]
    try:
        data = _get_json(base + "/api/settings", ssl_ctx_for(printer), headers,
                         timeout=CAMERA_TIMEOUT)
        cam = data.get("webcam") if isinstance(data, dict) else None
        if isinstance(cam, dict):
            # Settings are readable: trust them. No enabled, configured camera
            # means no camera; never guess an address here.
            if cam.get("webcamEnabled") is False:
                return _camera_result()
            raw = [cam.get("streamUrl"), cam.get("snapshotUrl")]
            if any(isinstance(item, str) and item.strip() for item in raw):
                return _camera_result(validated_camera_url(raw[0], base, printer.get("webui_url")),
                                      validated_camera_url(raw[1], base, printer.get("webui_url")))
            return _camera_result()
    except urllib.error.HTTPError:
        pass
    # Only when the settings are unreadable (key without access) or not
    # recognised do we fall back to OctoPrint's standard webcam paths.
    stream = validated_camera_url("/webcam/?action=stream", base, printer.get("webui_url"))
    snapshot = validated_camera_url("/webcam/?action=snapshot", base, printer.get("webui_url"))
    return _camera_result(stream, snapshot)


def camera_lookup(printer):
    """Read-only camera address lookup; never raises, never returns credentials."""
    try:
        printer = hydrate_credentials(printer)
        base = normalize_url((printer or {}).get("url"))
        backend = backend_for_type((printer or {}).get("type"))
        if not base or not validated_printer_web_url(base):
            return _camera_result(code="unavailable")
        if backend == "moonraker":
            return _moonraker_camera(printer, base)
        if backend == "octoprint":
            return _octoprint_camera(printer, base)
        return _camera_result(code="no_camera")
    except Exception:
        return _camera_result(code="unavailable")


def public_camera_info(result, request_id, printer):
    """Whitelisted DTO for the page; the stored credentials never leave Python."""
    result = result if isinstance(result, dict) else {}
    ok = result.get("ok") is True
    code = result.get("code")
    return {
        "requestId": request_id,
        "targetUuid": (printer or {}).get("target_uuid"),
        "ownerScope": (printer or {}).get("owner_scope_uuid"),
        "ok": ok,
        "code": "" if ok else (code if code in _CAMERA_CODES else "unavailable"),
        "stream": result.get("stream") if ok and isinstance(result.get("stream"), str) else "",
        "snapshot": result.get("snapshot") if ok and isinstance(result.get("snapshot"), str) else "",
    }


_PAGES = getattr(orca, "pages", None)
_PAGE_CAPABILITY_BASE =getattr(_PAGES, "PagesPluginCapabilityBase", None)
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
        self._polling = False
        self._poll_token = None
        self._discovered_printers = {}
        self._viewers = []
        self._camera_urls = {}  # target uuid -> last validated camera address
        self._u1_camera_lock = threading.RLock()
        self._u1_camera = None
        self._transfer_owner = str(uuid.uuid4())
        self._transfer_manager = None
        self._active_transfer_token = None
        self._received_artifacts = {}
        self._artifact_use_lock = threading.Lock()
        self._artifact_tokens_in_use = set()
        self._approval_store = _pf_approval.OperationApprovalStore()
        self._action_lock = threading.Lock()
        self._action_generation = 0
        self._action_scheduler = None
        self._accept_actions = True
        self._credential_page_epoch = getattr(self, "_credential_page_epoch", 0) + 1
        self._tls_lock = threading.Lock()
        self._tls_epoch = 0
        self._tls_challenges = {}
        self._tls_pending_id = None
        self.win = _PageWindowProxy(self) if _PAGE_CAPABILITY_BASE is not None else None
        _SETTINGS_OWNERS[_pf_settings.DASHBOARD] = self

    def get_name(self):
        return "Print Farm"

    def has_config_ui(self):
        return True

    def get_config_ui(self):
        language = refresh_ui_language() or _CACHED_UI_LANGUAGE or "en"
        clear = {"label": ui_text("cfgClearHistory"), "confirm": ui_text("cfgClearConfirm"),
                 "cancel": ui_text("cancel"), "done": ui_text("cfgClearDone")}
        return _pf_settings.build_page(_pf_settings.DASHBOARD, ui_text, language, clear)

    def get_default_config(self):
        return _pf_settings.defaults(_pf_settings.DASHBOARD)

    def _page_settings(self):
        """Settings pushed to the page; also applies a pending history clear."""
        settings = plugin_settings(_pf_settings.DASHBOARD)
        apply_history_clear_request(settings)
        return {"pollSeconds": settings["poll_seconds"],
                "pauseHidden": settings["pause_hidden"]}

    def get_icon(self):
        return ensure_icon()

    def get_ui(self):
        resume_deferred_pruning()
        return render_page()

    def _open(self):
        if _PAGE_CAPABILITY_BASE is not None:
            return False
        if self.win is not None and self.win.is_open():
            return False
        self._accept_actions = True
        self._credential_page_epoch = getattr(self, "_credential_page_epoch", 0) + 1
        resume_deferred_pruning()
        self.win = orca.host.ui.create_window(
            title="Print Farm", html=render_page(), width=1000, height=700,
            on_message=self.on_message, on_close=self.on_close,
        )
        return True

    def execute(self):
        self._open()
        return orca.ExecutionResult.success(ui_text("dashboardOpened"))

    def on_close(self):
        self._cancel_u1_camera()
        self._invalidate_tls_challenges(reset_pending=True)
        self._reset_dashboard_poll()
        self._cancel_artifact_transfer()
        self._accept_actions = False
        self._credential_page_epoch += 1
        self._stop_action_scheduler()
        self._discard_idle_artifacts()
        self.win = None

    def on_load(self):
        self._accept_actions = True
        self._credential_page_epoch = getattr(self, "_credential_page_epoch", 0) + 1
        super().on_load()

    def _close_owned_windows(self):
        self._invalidate_tls_challenges()
        self._reset_dashboard_poll()
        self._cancel_artifact_transfer()
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
        self._cancel_u1_camera()
        self._close_owned_windows()
        self._accept_actions = False
        self._credential_page_epoch += 1
        self._stop_action_scheduler()
        self._discard_idle_artifacts()
        stop_plugin_runtime()

    def on_unload(self):
        self._cancel_u1_camera()
        self._close_owned_windows()
        self._accept_actions = False
        self._credential_page_epoch += 1
        self._stop_action_scheduler()
        self._discard_idle_artifacts()
        stop_plugin_runtime()

    def _add_manual(self, entry):
        self._invalidate_tls_challenges()
        with _bambu_target_lock(entry):
            return self._add_manual_locked(entry)

    def _add_manual_locked(self, entry):
        if entry.get("type") == "repetier" and not _pf_repetier.valid_slug(entry.get("repetier_slug")):
            return None
        if any(private_credential(entry.get(key, "")) is None
               for key in ("apikey", "access_code")):
            return None
        with _STORE_LOCK:
            manual = load_manual_printers()
            snapshot = json.dumps(manual, sort_keys=True)
            root, epoch = PLUGIN_STORAGE_DIR, _PLUGIN_RUNTIME_EPOCH
        normalized = normalize_url(entry.get("url"))
        requested_uuid = entry.get("target_uuid")
        if requested_uuid:
            matches = [i for i, existing in enumerate(manual)
                       if existing.get("target_uuid") == requested_uuid
                       and entry.get("owner_scope_uuid") == existing.get("owner_scope_uuid")
                       and entry.get("target_selector") == existing.get("target_selector")
                       and entry.get("binding_revision") == existing.get("binding_revision")
                       and entry.get("type") == existing.get("type")]
            if len(matches) != 1:
                return None
        else:
            # A route is not target identity. Adding the same endpoint can
            # intentionally represent another target selector. A captured
            # Orca preset reference identifies a repeated picker choice.
            discovery_ref = entry.get("discovery_ref")
            matches = [
                i for i, existing in enumerate(manual)
                if isinstance(discovery_ref, str) and discovery_ref
                and entry.get("source") == "orca"
                and existing.get("source") == "orca"
                and existing.get("discovery_ref") == discovery_ref
            ]
            if len(matches) > 1:
                return None
        if len(matches) == 1:
            index = matches[0]
            existing = _with_orca_key(manual[index])
            stable_id = existing.get("id")
            replacement = dict(existing)
            if (normalize_url(existing.get("url")) != normalized
                    or existing.get("type") != entry.get("type")
                    or (entry.get("type") == "repetier" and existing.get("repetier_slug") != entry.get("repetier_slug"))):
                # Credentials must be entered again for another endpoint.
                # This also protects legacy plaintext cards before migration.
                required = set(existing.get("credential_refs") or {}) | {
                    field for field in _pf_credentials.FIELDS if existing.get(field)}
                if any(not entry.get(field) for field in required):
                    return None
            prior_serial = str(existing.get("serial") or "").strip()
            replacement.update({key: value for key, value in entry.items()
                                if key not in _pf_credentials.FIELDS or value
                                if key not in {"target_uuid", "owner_scope_uuid",
                                               "target_selector", "server_uuid",
                                               "binding_revision"}})
            replacement["url"] = normalized
            replacement["id"] = stable_id
            # Saving the card again is the user's way to re-detect a Duet.
            replacement.pop("protocol", None)
            if str(replacement.get("serial") or "").strip() != prior_serial:
                replacement["server_uuid"] = _server_uuid_for(
                    replacement, existing["owner_scope_uuid"]
                )
            elif normalize_url(existing.get("url")) != normalized:
                replacement["server_uuid"] = _server_uuid_for(
                    replacement, existing["owner_scope_uuid"]
                )
            _ensure_printer_identity(replacement, existing.get("owner_scope_uuid"))
            if any(existing.get(key) != replacement.get(key)
                   for key in ("url", "type", "serial", "server_uuid", "apikey",
                               "access_code", "insecure", "ws_port", "repetier_slug", "target_selector")):
                replacement["binding_revision"] = existing.get("binding_revision", 1) + 1
            if (normalize_url(existing.get("url")) != normalized
                    or existing.get("type") != replacement.get("type")
                    or str(existing.get("serial") or "").strip()
                       != str(replacement.get("serial") or "").strip()):
                replacement.pop("tls_pins", None)
                replacement["tls_trust_revision"] = (
                    int(existing.get("tls_trust_revision") or 0) + 1)
            if entry.get("name_provenance") in ("user", "orca"):
                replacement["name_match_value"] = replacement.get("name")
                replacement["name_match_binding_revision"] = replacement.get("binding_revision")
            if entry.get("serial_source") == "user" and replacement.get("serial"):
                replacement["serial_value"] = replacement.get("serial")
                replacement["serial_endpoint"] = _endpoint_key(replacement.get("url"))
                replacement["serial_binding_revision"] = replacement.get("binding_revision")
            try:
                replacement = secure_card_credentials(replacement, entry, root=root, epoch=epoch)
            except _pf_credentials.Unavailable:
                return None
            with _STORE_LOCK:
                if (not _credential_work_is_active() or root != PLUGIN_STORAGE_DIR or epoch != _PLUGIN_RUNTIME_EPOCH
                        or snapshot != json.dumps(load_manual_printers(), sort_keys=True)):
                    return None
                manual[index] = replacement
                return replacement if save_manual_printers(manual) else None
        new_id = max(
            [
                p.get("id", 0)
                for p in manual
                if isinstance(p.get("id"), int)
                and not isinstance(p.get("id"), bool)
            ],
            default=0,
        ) + 1
        stored = dict(entry)
        stored["url"] = normalized
        stored["id"] = new_id
        owner_scope = next((p.get("owner_scope_uuid") for p in manual
                            if p.get("owner_scope_uuid")), None)
        _ensure_printer_identity(stored, owner_scope)
        if stored.get("name_provenance") in ("user", "orca"):
            stored["name_match_value"] = stored.get("name")
            stored["name_match_binding_revision"] = stored.get("binding_revision")
        if stored.get("serial_source") == "user" and stored.get("serial"):
            stored["serial_value"] = stored.get("serial")
            stored["serial_endpoint"] = _endpoint_key(stored.get("url"))
            stored["serial_binding_revision"] = stored.get("binding_revision")
        try:
            stored = secure_card_credentials(stored, root=root, epoch=epoch)
        except _pf_credentials.Unavailable:
            return None
        with _STORE_LOCK:
            if (not _credential_work_is_active() or root != PLUGIN_STORAGE_DIR or epoch != _PLUGIN_RUNTIME_EPOCH
                    or snapshot != json.dumps(load_manual_printers(), sort_keys=True)):
                return None
            manual.append(stored)
            return stored if save_manual_printers(manual) else None

    def _remove_manual(self, identity):
        self._invalidate_tls_challenges()
        with _bambu_target_lock({"target_uuid": (identity or {}).get("targetUuid")}):
            return self._remove_manual_locked(identity)

    def _remove_manual_locked(self, identity):
        with _STORE_LOCK:
            manual = load_manual_printers()
            target = resolve_printer_target(manual, identity if isinstance(identity, dict) else {})
            if target is None:
                return False
            manual = [p for p in manual if p.get("target_uuid") != target.get("target_uuid")]
            save_manual_printers(manual)
            return True

    def _post(self, payload):
        return post_window(self.win, payload)

    def _invalidate_tls_challenges(self, reset_pending=False):
        with self._tls_lock:
            self._tls_epoch += 1
            self._tls_challenges.clear()
            if reset_pending:
                self._tls_pending_id = None

    def _tls_result(self, protocol, error=None, expected_epoch=None, **values):
        with self._tls_lock:
            if (not self._accept_actions or
                    expected_epoch is not None and expected_epoch != self._tls_epoch):
                return
            self._post({"tlsTrustResult": {"protocol": protocol, "error": error, **values}})

    def _submit_tls_work(self, function, msg):
        with self._tls_lock:
            if self._tls_pending_id is not None:
                accepted = False
            else:
                accepted = True
                pending_id = uuid.uuid4().hex
                self._tls_pending_id = pending_id
                page_epoch = self._tls_epoch
        if not accepted:
            self._tls_result(msg.get("protocol"), "tlsTrustBusy",
                             requestId=msg.get("requestId"))
            return

        def release():
            with self._tls_lock:
                if self._tls_pending_id == pending_id:
                    self._tls_pending_id = None

        def run():
            try:
                with self._tls_lock:
                    current = page_epoch == self._tls_epoch and self._tls_pending_id == pending_id
                if current:
                    function(msg, page_epoch=page_epoch)
            finally:
                release()

        self._submit_ui_work(run, on_rejected=lambda: (
            release(), self._tls_result(msg.get("protocol"), "tlsTrustBusy",
                                        expected_epoch=page_epoch,
                                        requestId=msg.get("requestId"))))

    def _probe_tls_trust(self, msg, page_epoch=None):
        protocol = msg.get("protocol")
        request_id = msg.get("requestId")
        with self._tls_lock:
            if page_epoch is not None and page_epoch != self._tls_epoch:
                return
            page_epoch = self._tls_epoch
            self._tls_challenges.clear()
        result = lambda *args, **kwargs: self._tls_result(
            *args, expected_epoch=page_epoch, **kwargs)
        if protocol not in _BAMBU_TLS_PORTS:
            result(protocol, "tlsProbeFailed", requestId=request_id)
            return
        runtime_epoch = _PLUGIN_RUNTIME_EPOCH
        storage_root = os.path.normcase(os.path.realpath(PLUGIN_STORAGE_DIR))
        printer = resolve_printer_target(load_manual_printers(), msg)
        if printer is None or backend_for_type(printer.get("type")) != "bambu":
            result(protocol, "tlsTrustStale", requestId=request_id)
            return
        lock = _bambu_target_lock(printer)
        if not lock.acquire(blocking=False):
            result(protocol, "tlsTrustBusy", requestId=request_id)
            return
        try:
            current = _stored_bambu_record(printer)
            if current is None:
                raise BambuTlsError("tls_trust_stale")
            pins = current.get("tls_pins") or {}
            old = pins.get(protocol) if isinstance(pins, dict) else None
            observed = _probe_bambu_certificate(printer, protocol)
            if (not self._accept_actions or runtime_epoch != _PLUGIN_RUNTIME_EPOCH
                    or storage_root != os.path.normcase(os.path.realpath(PLUGIN_STORAGE_DIR))
                    or not ACTION_WORKER.current_job_is_active()):
                raise BambuTlsError("tls_trust_stale")
            with self._tls_lock:
                if page_epoch != self._tls_epoch:
                    return
                token = uuid.uuid4().hex
                self._tls_challenges[token] = {
                    "expires": time.monotonic() + 90,
                    "page_epoch": page_epoch, "runtime_epoch": runtime_epoch,
                    "storage_root": storage_root,
                    "identity": _bambu_tls_identity(current),
                    "trust_revision": int(current.get("tls_trust_revision") or 0),
                    "protocol": protocol, "old": old, "observed": observed,
                }
            result(protocol, token=token, old=old, observed=observed,
                             address=_endpoint_key(current.get("url")),
                             port=_BAMBU_TLS_PORTS[protocol], requestId=request_id)
        except BambuTlsError as exc:
            result(protocol, _BAMBU_TLS_ERRORS[exc.code], requestId=request_id)
        finally:
            lock.release()

    def _confirm_tls_trust(self, msg, page_epoch=None):
        token = msg.get("token")
        request_id = msg.get("requestId")
        with self._tls_lock:
            if page_epoch is not None and page_epoch != self._tls_epoch:
                return
            page_epoch = self._tls_epoch
            challenge = self._tls_challenges.pop(token, None) if isinstance(token, str) else None
        if challenge is None or msg.get("consent") is not True:
            self._tls_result(None, "tlsTrustStale", expected_epoch=page_epoch, requestId=request_id)
            return
        protocol = challenge["protocol"]
        result = lambda *args, **kwargs: self._tls_result(
            *args, expected_epoch=challenge["page_epoch"], **kwargs)
        if (time.monotonic() > challenge["expires"]
                or challenge["page_epoch"] != self._tls_epoch
                or challenge["runtime_epoch"] != _PLUGIN_RUNTIME_EPOCH
                or challenge["storage_root"] != os.path.normcase(os.path.realpath(PLUGIN_STORAGE_DIR))
                or not self._accept_actions):
            result(protocol, "tlsTrustStale", requestId=request_id)
            return
        identity = challenge["identity"]
        lock = _bambu_target_lock({"target_uuid": identity[1]})
        if not lock.acquire(blocking=False):
            result(protocol, "tlsTrustBusy", requestId=request_id)
            return
        try:
            with _STORE_LOCK:
                manual = load_manual_printers()
                matches = [entry for entry in manual if _bambu_tls_identity(entry) == identity]
                if len(matches) != 1:
                    raise BambuTlsError("tls_trust_stale")
                record = matches[0]
                pins = record.get("tls_pins") or {}
                old = pins.get(protocol) if isinstance(pins, dict) else None
                if (old != challenge["old"] or
                        int(record.get("tls_trust_revision") or 0) != challenge["trust_revision"]):
                    raise BambuTlsError("tls_trust_stale")
            # Re-observe the certificate after explicit consent. Never trust a
            # fingerprint supplied by JavaScript or a probe made before an edit.
            if _probe_bambu_certificate(record, protocol) != challenge["observed"]:
                raise BambuTlsError("tls_trust_stale")
            with _STORE_LOCK, self._tls_lock:
                manual = load_manual_printers()
                matches = [entry for entry in manual if _bambu_tls_identity(entry) == identity]
                if len(matches) != 1:
                    raise BambuTlsError("tls_trust_stale")
                record = matches[0]
                pins = record.get("tls_pins") or {}
                if (not isinstance(pins, dict) or pins.get(protocol) != challenge["old"]
                        or int(record.get("tls_trust_revision") or 0) != challenge["trust_revision"]
                        or challenge["page_epoch"] != self._tls_epoch
                        or challenge["runtime_epoch"] != _PLUGIN_RUNTIME_EPOCH
                        or challenge["storage_root"] != os.path.normcase(os.path.realpath(PLUGIN_STORAGE_DIR))
                        or not self._accept_actions
                        or not ACTION_WORKER.current_job_is_active()):
                    raise BambuTlsError("tls_trust_stale")
                updated = dict(pins)
                updated[protocol] = challenge["observed"]
                record["tls_pins"] = updated
                record["tls_trust_revision"] = challenge["trust_revision"] + 1
                keep_name = _name_is_matchable(record)
                record["binding_revision"] += 1
                if keep_name:
                    record["name_match_binding_revision"] = record["binding_revision"]
                if not save_manual_printers(manual):
                    raise BambuTlsError("tls_trust_save_failed")
            result(protocol, saved=True, requestId=request_id)
            with self._tls_lock:
                self._tls_challenges.clear()
                if challenge["page_epoch"] == self._tls_epoch and self._accept_actions:
                    # Keep the final list update in the same page lifecycle;
                    # close/ready must not slip between its guard and post.
                    self._refresh_worker(None, False)
        except BambuTlsError as exc:
            result(protocol, _BAMBU_TLS_ERRORS[exc.code], requestId=request_id)
        finally:
            lock.release()

    def _submit_ui_work(self, function, *args, on_rejected=None, **kwargs):
        page_epoch = self._credential_page_epoch
        guard = lambda: self._accept_actions and page_epoch == self._credential_page_epoch
        def guarded(*work_args, **work_kwargs):
            if guard():
                return _run_credential_work(guard, function, *work_args, **work_kwargs)
        guarded.__wrapped__ = function
        try:
            return ACTION_WORKER.submit(guarded, *args, **kwargs)
        except (PermissionError, RuntimeError):
            if on_rejected is not None:
                on_rejected()
            self._post_status(ui_text("genericError"))
            return None

    def _post_action_rejected(self, msg):
        request_id = msg.get("requestId") if isinstance(msg, dict) else None
        if (isinstance(request_id, int) and not isinstance(request_id, bool)
                and 0 < request_id <= 2**53 - 1):
            self._post({"actionRejected": {"requestId": request_id}})

    def _post_rejected_history(self, msg, mode):
        request_id = _history_int(msg.get("requestId"), minimum=0)
        if mode == "all":
            payload = {"mode": "all", "requestId": request_id, "available": False,
                       "counts": {}, "items": [], "holds": [], "holds_available": False}
        else:
            payload = {"mode": "printer", "requestId": request_id,
                       "targetUuid": msg.get("targetUuid"), "ownerScope": msg.get("ownerScope"),
                       "targetSelector": msg.get("targetSelector"),
                       "bindingRevision": msg.get("bindingRevision"),
                       "printerUrl": msg.get("url"), "connectionType": msg.get("connectionType"),
                       "printerName": msg.get("url"), "available": False, "items": []}
        self._post({"printHistory": payload})

    def _ensure_action_scheduler(self):
        with self._action_lock:
            if not self._accept_actions:
                return None, self._action_generation
            if self._action_scheduler is None:
                try:
                    scheduler = _pf_scheduler.ActionScheduler(
                        max_pending=64, max_workers=4, name="print-farm-action",
                    )
                except (PermissionError, RuntimeError):
                    self._post_status(ui_text("genericError"))
                    return None, self._action_generation
                self._action_generation += 1
                self._action_scheduler = scheduler
            return self._action_scheduler, self._action_generation

    def _stop_action_scheduler(self):
        with self._action_lock:
            scheduler = self._action_scheduler
            self._action_scheduler = None
            self._action_generation += 1
        if scheduler is not None:
            # Queued callbacks observe the changed generation and do no work,
            # but still run their finalizers to release staged batch artifacts.
            scheduler.shutdown(wait=False, cancel_pending=False)

    def _action_context_is_active(self):
        return (getattr(_SCHEDULER_ACTION_CONTEXT, "dashboard", None) is self
                and getattr(_SCHEDULER_ACTION_CONTEXT, "generation", None) == self._action_generation
                and self._action_scheduler is not None)

    def _work_is_active(self):
        if getattr(_SCHEDULER_ACTION_CONTEXT, "dashboard", None) is self:
            return self._action_context_is_active()
        return ACTION_WORKER.current_job_is_active()

    def _submit_target_action(self, action, msg, kind):
        """Resolve the persisted target before deriving the scheduler key."""
        printer = resolve_printer_target(dashboard_printers(), msg)
        if printer is None:
            self._post_action_rejected(msg)
            self._post_status(ui_text("genericError"))
            return False
        target_id = str(printer.get("owner_scope_uuid") or "") + ":" + str(
            printer.get("target_uuid") or "")
        if not all((printer.get("owner_scope_uuid"), printer.get("target_uuid"))):
            self._post_action_rejected(msg)
            self._post_status(ui_text("genericError"))
            return False
        scheduler, generation = self._ensure_action_scheduler()
        if scheduler is None:
            self._post_action_rejected(msg)
            return False

        def run():
            _SCHEDULER_ACTION_CONTEXT.dashboard = self
            _SCHEDULER_ACTION_CONTEXT.generation = generation
            try:
                if self._action_context_is_active():
                    action(dict(msg))
            finally:
                for key in ("dashboard", "generation"):
                    try:
                        delattr(_SCHEDULER_ACTION_CONTEXT, key)
                    except AttributeError:
                        pass

        result = scheduler.submit(target_id, str(uuid.uuid4()), run, kind=kind)
        if result.get("status") != scheduler.ACCEPTED:
            self._post_action_rejected(msg)
            self._post_status(ui_text("genericError"))
            return False
        return True

    @staticmethod
    def _start_approval_plan(printer, artifact, plate_entry, use_ams, unassigned=None):
        binding = {key: printer.get(key) for key in (
            "owner_scope_uuid", "target_uuid", "target_selector",
            "binding_revision", "url", "type", "serial", "repetier_slug",
        )}
        if unassigned:
            verdict, reason = _unassigned_compatibility(printer, artifact)
        else:
            verdict, reason = start_compatibility(printer, artifact, plate_entry)
        metadata = {
            "artifact_parameters_sha256": artifact.manifest.get("parameters_sha256"),
            "artifact_metadata": dict(artifact.metadata),
            "plate_entry": plate_entry,
            # The operator's file check is part of the approved plan: an
            # "unknown" verdict is only approvable together with that check.
            "start_check": {"verdict": verdict, "reason": reason,
                            "operator_checked": verdict == "unknown"},
        }
        if unassigned:
            # One-off send of a file linked to no printer: the chosen target
            # (already in target_identity), the stored file and the mode are
            # all bound into the digest.
            metadata["unassigned_send"] = {"file": unassigned.get("file"),
                                           "mode": unassigned.get("mode")}
        metadata_digest = hashlib.sha256(json.dumps(
            metadata, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        return {
            "target_identity": json.dumps(binding, sort_keys=True,
                                          ensure_ascii=False, separators=(",", ":")),
            "binding_revision": printer.get("binding_revision"),
            "artifact_sha256": artifact.manifest.get("sha256"),
            "plate_entry": plate_entry or None,
            "action": "start",
            "use_ams": use_ams is True,
            "metadata_digest": metadata_digest,
        }

    def _prepare_mass_start(self, msg):
        scheduler, generation = self._ensure_action_scheduler()
        if scheduler is None:
            return
        token = msg.get("artifactToken")
        artifact = self._received_artifacts.get(token) if isinstance(token, str) else None
        identities = msg.get("targets")
        plate = msg.get("plate") or None
        if (artifact is None or not _pf_artifact.verify_staged_artifact(artifact)
                or not isinstance(identities, list) or not identities):
            self._post_status(ui_text("startApprovalExpired"))
            return
        try:
            if artifact.manifest.get("kind") == "sliced_3mf":
                _pf_artifact.select_plate_entry(artifact, plate)
            elif plate is not None:
                raise ValueError("unexpected plate")
            printers = dashboard_printers()
            prepared = []
            for identity in identities:
                printer = resolve_printer_target(printers, identity)
                if (printer is None or any(item["targetUuid"] == printer.get("target_uuid")
                                           for item in prepared)):
                    raise ValueError("invalid target")
                verdict, reason = start_compatibility(printer, artifact, plate)
                item = {
                    "targetUuid": printer["target_uuid"],
                    "printer": printer.get("name") or printer.get("url") or "",
                    "verdict": verdict, "reason": reason,
                    "nonce": None, "digest": None,
                }
                if verdict != "incompatible":
                    plan = self._start_approval_plan(
                        printer, artifact, plate, msg.get("use_ams") is True,
                    )
                    item["nonce"], item["digest"] = self._approval_store.create(
                        plan, ttl=300, epoch=generation,
                    )
                prepared.append(item)
            self._post({"massStartApprovals": {
                "artifactToken": token, "plate": plate,
                "profile": artifact.metadata.get("printer_settings_id")
                           or artifact.metadata.get("printer_model") or "",
                "targets": prepared,
            }})
        except (ValueError, TypeError, RuntimeError, _pf_artifact.ArtifactError):
            self._post_status(ui_text("startApprovalExpired"))

    def _submit_mass_print(self, msg):
        identities = msg.get("targets")
        if not isinstance(identities, list) or not identities:
            return
        printers = dashboard_printers()
        selected = []
        for identity in identities:
            printer = resolve_printer_target(printers, identity)
            if (printer is None or not printer.get("owner_scope_uuid")
                    or not printer.get("target_uuid")
                    or any(p.get("target_uuid") == printer.get("target_uuid") for p in selected)):
                self._post_status(ui_text("genericError"))
                return
            selected.append(printer)
        approved = False
        start_checks = {}
        if msg.get("start") is True:
            token = msg.get("artifactToken")
            artifact = self._received_artifacts.get(token) if isinstance(token, str) else None
            grants = msg.get("approvals")
            if (artifact is None or not _pf_artifact.verify_staged_artifact(artifact)
                    or not isinstance(grants, list) or len(grants) != len(selected)):
                self._post_status(ui_text("startApprovalExpired"))
                return
            plate = msg.get("plate") or None
            try:
                if artifact.manifest.get("kind") == "sliced_3mf":
                    _pf_artifact.select_plate_entry(artifact, plate)
                elif plate is not None:
                    raise ValueError("unexpected plate")
                verdicts = []
                # Check every target first so a refused batch consumes no nonce.
                for printer, grant in zip(selected, grants):
                    if (not isinstance(grant, dict)
                            or grant.get("targetUuid") != printer.get("target_uuid")):
                        raise ValueError("approval target changed")
                    verdict, reason = start_compatibility(printer, artifact, plate)
                    if verdict == "incompatible":
                        self._post_status(ui_text(
                            "startBlocked",
                            printer=printer.get("name") or printer.get("url") or "",
                            reason=ui_text(reason),
                        ))
                        return
                    # A new physical print always needs a fresh bed-clear
                    # confirmation; an unknown profile also needs the operator's
                    # explicit "sliced for this printer" check.
                    if (grant.get("bedClear") is not True
                            or (verdict == "unknown" and grant.get("operatorChecked") is not True)):
                        self._post_status(ui_text("startConfirmMissing"))
                        return
                    verdicts.append((printer, grant, verdict, reason))
                for printer, grant, verdict, reason in verdicts:
                    plan = self._start_approval_plan(
                        printer, artifact, plate, msg.get("use_ams") is True,
                    )
                    if not self._approval_store.consume(
                            grant.get("nonce"), plan, self._action_generation):
                        raise ValueError("approval expired")
                    start_checks[printer.get("target_uuid")] = {
                        "verdict": verdict, "reason": reason,
                        "operator_checked": verdict == "unknown", "bed_clear": True,
                    }
            except (ValueError, TypeError, _pf_artifact.ArtifactError):
                self._post_status(ui_text("startApprovalExpired"))
                return
            approved = True
        scheduler, generation = self._ensure_action_scheduler()
        if scheduler is None:
            return
        gate = threading.Event()
        state = {"remaining": 0, "lock": threading.Lock(),
                 "token": msg.get("artifactToken")}
        if not isinstance(state["token"], str):
            self._post_status(ui_text("fileReadFailed"))
            return
        with self._artifact_use_lock:
            if (state["token"] in self._artifact_tokens_in_use
                    or state["token"] not in self._received_artifacts):
                self._post_status(ui_text("fileReadFailed"))
                return
            self._artifact_tokens_in_use.add(state["token"])
        accepted = []
        for printer in selected:
            identity = {
                "targetUuid": printer.get("target_uuid"),
                "ownerScope": printer.get("owner_scope_uuid"),
                "targetSelector": printer.get("target_selector"),
                "bindingRevision": printer.get("binding_revision"),
                "url": printer.get("url"),
                "connectionType": printer.get("type"),
            }
            target_id = str(printer["owner_scope_uuid"]) + ":" + str(printer["target_uuid"])

            def run(identity=identity):
                gate.wait()
                _SCHEDULER_ACTION_CONTEXT.dashboard = self
                _SCHEDULER_ACTION_CONTEXT.generation = generation
                try:
                    if self._action_context_is_active():
                        task = dict(msg)
                        task["targets"] = [identity]
                        task["_retainArtifact"] = True
                        task["_approved"] = approved
                        task["_startCheck"] = start_checks.get(identity["targetUuid"])
                        self._do_mass_print(task)
                finally:
                    for key in ("dashboard", "generation"):
                        try:
                            delattr(_SCHEDULER_ACTION_CONTEXT, key)
                        except AttributeError:
                            pass
                    with state["lock"]:
                        state["remaining"] -= 1
                        done = state["remaining"] == 0
                    if done and isinstance(state["token"], str):
                        artifact = self._received_artifacts.pop(state["token"], None)
                        if artifact is not None:
                            try:
                                _pf_artifact.discard_staged_artifact(
                                    artifact, self._artifact_receiver().storage_dir,
                                )
                            except (OSError, ValueError):
                                self._post_status(ui_text("preparedUploadDiscardFailed"))
                        with self._artifact_use_lock:
                            self._artifact_tokens_in_use.discard(state["token"])

            result = scheduler.submit(target_id, str(uuid.uuid4()), run,
                                      kind="start" if msg.get("start") else "upload")
            if result.get("status") == scheduler.ACCEPTED:
                accepted.append(result)
        with state["lock"]:
            state["remaining"] = len(accepted)
        gate.set()
        if len(accepted) != len(selected):
            self._post_status(ui_text("genericError"))
        if not accepted and isinstance(state["token"], str):
            artifact = self._received_artifacts.pop(state["token"], None)
            if artifact is not None:
                try:
                    _pf_artifact.discard_staged_artifact(
                        artifact, self._artifact_receiver().storage_dir,
                    )
                except (OSError, ValueError):
                    self._post_status(ui_text("preparedUploadDiscardFailed"))
            with self._artifact_use_lock:
                self._artifact_tokens_in_use.discard(state["token"])

    # on_message runs on the UI thread — host reads happen here, network/disk on workers.
    def on_message(self, msg):
        msg = msg or {}
        kind = msg.get("type")
        if kind == "ready":
            self._cancel_u1_camera()
            self._credential_page_epoch += 1
            self._reset_dashboard_poll()
            self._invalidate_tls_challenges(reset_pending=True)
            # Populate both Orca-configured and LAN-discovered choices on open.
            # Periodic status ticks below remain discovery-free.
            self._refresh_async(discover_network=True)
        elif kind == "refresh":
            self._refresh_async(discover_network=True)
        elif kind == "tls-probe":
            self._submit_tls_work(self._probe_tls_trust, msg)
        elif kind == "tls-confirm":
            self._submit_tls_work(self._confirm_tls_trust, msg)
        elif kind == "tls-close":
            self._invalidate_tls_challenges()
        elif kind == "poll":
            # Periodic status tick from the page: statuses only, no store/picker
            # rebuild, and never more than one poll in flight.
            if not getattr(self, "_polling", False):
                self._polling = True
                token = self._poll_token = uuid.uuid4().hex
                self._submit_ui_work(lambda: self._poll_dashboard(token),
                                     on_rejected=lambda: self._finish_dashboard_poll(token))
        elif kind == "printer-cmd":
            self._submit_target_action(self._do_printer_cmd, msg,
                                      str(msg.get("action") or "command"))
        elif kind == "print-history":
            self._submit_ui_work(self._post_print_history, msg,
                                 on_rejected=lambda: self._post_rejected_history(msg, "printer"))
        elif kind == "print-history-all":
            self._submit_ui_work(self._post_print_history_all, msg,
                                 on_rejected=lambda: self._post_rejected_history(msg, "all"))
        elif kind == "journal-resolve-hold":
            self._submit_ui_work(self._resolve_journal_hold, msg,
                                 on_rejected=lambda: self._post_action_rejected(msg))
        elif kind == "legacy-import-confirm":
            confirmation = msg.get("confirmation")
            if isinstance(confirmation, str) and confirmation:
                self._submit_ui_work(self._import_legacy_and_refresh, confirmation)
        elif kind == "outbox-send":
            self._submit_target_action(self._do_outbox_send, msg, "upload")
        elif kind == "outbox-start-prepare":
            self._submit_ui_work(self._prepare_outbox_start, msg)
        elif kind == "outbox-drop":
            stored = msg.get("file") or ""
            if stored:
                self._submit_ui_work(
                    self._drop_target_and_refresh,
                    msg,
                    self._capture_discovered_printers(),
                    on_rejected=lambda: self._post_action_rejected(msg),
                )
        elif kind == "outbox-cleanup":
            self._submit_ui_work(
                self._cleanup_leftovers_and_refresh, self._capture_discovered_printers(),
                on_rejected=lambda: self._post_action_rejected(msg),
            )
        elif kind == "outbox-unassigned-drop-all":
            self._submit_ui_work(
                self._drop_unassigned_and_refresh, self._capture_discovered_printers(),
                on_rejected=lambda: self._post_action_rejected(msg),
            )
        elif kind == "outbox-unassigned-drop":
            stored = msg.get("file") or ""
            if stored.startswith("unassigned/"):
                self._submit_ui_work(
                    self._drop_and_refresh, stored, self._capture_discovered_printers(),
                    on_rejected=lambda: self._post_action_rejected(msg),
                )
        elif kind in ("credential-migrate", "credential-retry"):
            self._submit_ui_work(self._credential_action, kind, msg.get("confirmation"),
                                 self._capture_discovered_printers())
        elif kind == "repetier-list":
            page_epoch = self._credential_page_epoch
            self._submit_ui_work(self._load_repetier_printers, dict(msg),
                on_rejected=lambda: self._repetier_list_failure(msg, page_epoch))
        elif kind == "add":
            printer_type = msg.get("kind") or "moonraker"
            apikey = private_credential(msg.get("apikey"))
            access_code = private_credential(msg.get("access_code"))
            if apikey is None or access_code is None:
                self._post_status(ui_text("genericError"))
                return
            entry = {
                "name": msg.get("name") or msg.get("url"),
                "url": normalize_url(msg.get("url")),
                "type": printer_type,
                "apikey": apikey if printer_type in _CREDENTIAL_TYPES else "",
                "serial": msg.get("serial") or "",
                "access_code": access_code if printer_type == "bambu" else "",
                "insecure": bool(msg.get("insecure")),
                "source": "manual",
                "repetier_slug": msg.get("repetier_slug", "") if printer_type == "repetier" else "",
            }
            if printer_type == "bambu" and msg.get("nameUserChosen") is True:
                entry["name_provenance"] = "user"
            if (printer_type == "bambu" and msg.get("serialUserConfirmed") is True
                    and isinstance(entry["serial"], str) and entry["serial"].strip()):
                entry["serial_source"] = "user"
            if printer_type == "flashforge":
                serial = msg.get("serial")
                if (not isinstance(serial, str) or not serial.strip() or not apikey
                        or len(serial.strip()) > 64):
                    self._post_status(ui_text("genericError"))
                    return
                entry["serial"] = serial.strip()
            if printer_type == "crealityprint":
                ws_port = parse_ws_port(msg.get("ws_port"))
                if ws_port is False:
                    self._post_status(ui_text("genericError"))
                    return
                entry["ws_port"] = ws_port
            if msg.get("targetUuid"):
                entry.update({
                    "target_uuid": msg.get("targetUuid"),
                    "owner_scope_uuid": msg.get("ownerScope"),
                    "target_selector": msg.get("targetSelector"),
                    "binding_revision": msg.get("bindingRevision"),
                })
            if not backend_is_active(entry["type"]):
                self._post_status(
                    ui_text("supportPlanned", type=entry["type"] or ui_text("other"))
                )
                return
            self._submit_ui_work(
                self._add_and_refresh,
                entry,
                self._capture_discovered_printers(),
                on_rejected=lambda: self._post({"adoptResult": {
                    "url": entry.get("url") or "", "ok": False}}),
            )
        elif kind == "adopt":
            # User chose an OrcaSlicer-configured printer to put on the dashboard.
            discovery_ref = msg.get("discoveryRef")
            p = (
                self._discovered_printers.get(discovery_ref) or {}
                if isinstance(discovery_ref, str) and len(discovery_ref) <= 100
                else {}
            )
            if p.get("url"):
                apikey = private_credential(p.get("apikey"))
                access_code = private_credential(p.get("access_code"))
                if apikey is None or access_code is None:
                    self._post_status(ui_text("genericError"))
                    return
                entry = {
                    "name": p.get("name") or p.get("url"),
                    "url": normalize_url(p.get("url")),
                    "type": p.get("type") or "moonraker",
                    "apikey": apikey,
                    "serial": p.get("serial") or "",
                    "access_code": access_code,
                    "insecure": bool(p.get("insecure")),
                    "source": "orca",
                    "discovery_ref": discovery_ref,
                    "repetier_slug": p.get("repetier_slug", ""),
                }
                if (entry["type"] == "bambu"
                        and discovery_ref == _orca_discovery_ref(
                            entry["name"], entry["url"], entry["type"])):
                    entry["name_provenance"] = "orca"
                if not backend_is_active(entry["type"]):
                    self._post_status(
                        ui_text("supportPlanned", type=entry["type"] or ui_text("other"))
                    )
                    return
                self._submit_ui_work(
                    self._add_and_refresh,
                    entry,
                    self._capture_discovered_printers(),
                    on_rejected=lambda: self._post({"adoptResult": {
                        "url": entry.get("url") or "", "ok": False}}),
                )
        elif kind == "remove":
            self._submit_ui_work(
                self._remove_and_refresh,
                msg,
                self._capture_discovered_printers(),
                on_rejected=lambda: self._post_action_rejected(msg),
            )
        elif kind == "open":
            printer = resolve_printer_target(dashboard_printers(), msg)
            url = printer_webui_url(printer) if printer else ""
            if url:
                self._open_viewer(url, msg.get("name") or printer.get("name") or url)
        elif kind == "feedback":
            try:
                win = orca.host.ui.create_window(title=ui_text("feedbackButton"), html=render_feedback_page(),
                    width=560, height=220, on_message=lambda m: None, on_close=lambda: None)
                self._viewers = [v for v in self._viewers if v is not None and v.is_open()]
                self._viewers.append(win)
            except Exception:
                self._post_status(ui_text("feedbackHelp") + " https://filamenthub.ru")
        elif kind == "connection-options-save":
            self._submit_ui_work(self._save_connection_options, dict(msg),
                on_rejected=lambda: self._connection_options_result(msg, False))
        elif kind == "camera-native-open":
            self._begin_u1_camera(dict(msg))
        elif kind == "camera-native-lease":
            self._renew_u1_camera(msg)
        elif kind == "camera-native-close":
            self._cancel_u1_camera(msg)
        elif kind == "camera-info":
            self._submit_ui_work(self._post_camera_info, msg,
                                 on_rejected=lambda: self._post({"cameraInfo":
                                     public_camera_info(_camera_result(code="unavailable"),
                                         _history_int(msg.get("requestId"), minimum=0),
                                         {"target_uuid": msg.get("targetUuid"),
                                          "owner_scope_uuid": msg.get("ownerScope")})}))
        elif kind == "camera-open":
            printer = resolve_printer_target(dashboard_printers(), msg)
            cached = self._camera_urls.get(printer.get("target_uuid")) if printer else None
            url = validated_camera_url(cached, printer.get("url")) if cached else ""
            if url:
                self._open_viewer(url, "%s — %s" % (
                    printer.get("name") or printer.get("url"), ui_text("cameraButton")))
        elif kind == "mass-print":
            self._submit_mass_print(msg)
        elif kind == "mass-start-prepare":
            self._submit_ui_work(self._prepare_mass_start, msg)
        elif kind == "artifact-discard":
            self._submit_ui_work(self._do_artifact_discard, msg)
        elif kind == "artifact-begin":
            self._submit_ui_work(self._do_artifact_begin, msg,
                                 on_rejected=lambda: self._post({"artifactError": True}))
        elif kind == "artifact-chunk":
            data = msg.get("dataB64")
            if isinstance(data, str) and len(data) <= 44000:
                self._submit_ui_work(self._do_artifact_chunk, msg,
                                     on_rejected=self._reject_artifact_chunk)
            else:
                self._submit_ui_work(self._reject_artifact_chunk,
                                     on_rejected=self._reject_artifact_chunk)
        elif kind == "artifact-finish":
            self._submit_ui_work(self._do_artifact_finish, msg,
                                 on_rejected=self._reject_artifact_chunk)

    def _post_camera_info(self, msg):
        request_id = _history_int(msg.get("requestId"), minimum=0, maximum=2**31)
        printer = resolve_printer_target(dashboard_printers(), msg)
        if printer is not None and backend_for_type(printer.get("type")) in ("moonraker", "octoprint"):
            result = camera_lookup(printer)
        else:
            result = _camera_result(code="no_camera" if printer else "unavailable")
        if (not self._work_is_active() or not _credential_work_is_active()
                or printer is not None and resolve_printer_target(dashboard_printers(), msg) is None):
            return
        if printer is not None and result.get("ok"):
            # The window fallback reuses exactly the address the page was given.
            if len(self._camera_urls) >= 32:
                self._camera_urls.pop(next(iter(self._camera_urls)))
            self._camera_urls[printer.get("target_uuid")] = result["stream"] or result["snapshot"]
        self._post({"cameraInfo": public_camera_info(result, request_id, printer)})

    def _connection_options_result(self, msg, ok):
        if self._accept_actions and _credential_work_is_active():
            self._post({"connectionOptionsResult": {"ok": ok, "requestId": msg.get("requestId"),
                "targetUuid": msg.get("targetUuid")}})

    def _save_connection_options(self, msg):
        with _STORE_LOCK:
            cards = load_manual_printers()
            card = resolve_printer_target(cards, msg)
            if card is None or not _credential_work_is_active():
                self._connection_options_result(msg, False)
                return
            raw = msg.get("webui_url")
            mode = msg.get("camera_mode")
            if (not isinstance(raw, str) or len(raw) > 2048
                    or mode not in ("auto", "snapmaker_u1")
                    or mode == "snapmaker_u1" and backend_for_type(card.get("type")) != "moonraker"):
                self._connection_options_result(msg, False)
                return
            webui = validated_webui_url(raw, card.get("url")) if raw else ""
            if raw and not webui:
                self._connection_options_result(msg, False)
                return
            if webui != card.get("webui_url", "") or mode != card.get("camera_mode", "auto"):
                card["webui_url"], card["camera_mode"] = webui, mode
                card["binding_revision"] += 1
                if not save_manual_printers(cards):
                    self._connection_options_result(msg, False)
                    return
                self._cancel_u1_camera()
                self._camera_urls.pop(card.get("target_uuid"), None)
        self._connection_options_result(msg, True)
        self._refresh_worker(None)

    def _cancel_u1_camera(self, msg=None):
        global _U1_CAMERA_OWNER
        with self._u1_camera_lock:
            entry = self._u1_camera
            if entry is None:
                return
            if msg is not None and (msg.get("requestId") != entry["request_id"]
                    or msg.get("targetUuid") != entry["identity"].get("targetUuid")):
                return
            self._u1_camera = None
            entry["lease"] = 0
            client = entry.get("client")
            if not entry.get("running"):
                self._release_u1_slot(entry)
        with _U1_CAMERA_CLIENTS_LOCK:
            if _U1_CAMERA_OWNER and _U1_CAMERA_OWNER[1] is entry:
                _U1_CAMERA_OWNER = None
        if client:
            client.cancel()

    def _release_u1_slot(self, entry):
        with self._u1_camera_lock:
            if entry.get("slot_owned"):
                entry["slot_owned"] = False
                _U1_CAMERA_SLOT.release()

    def _renew_u1_camera(self, msg):
        with self._u1_camera_lock:
            entry = self._u1_camera
            if (entry and entry.get("client") and msg.get("sessionId") == entry["client"].id
                    and msg.get("requestId") == entry["request_id"]
                    and msg.get("targetUuid") == entry["identity"].get("targetUuid")
                    and time.monotonic() < entry["lease"]):
                entry["lease"] = time.monotonic() + 15

    def _begin_u1_camera(self, msg):
        global _U1_CAMERA_OWNER
        printer = resolve_printer_target(dashboard_printers(), msg)
        request_id = msg.get("requestId")
        if (printer is None or printer.get("camera_mode") != "snapmaker_u1"
                or backend_for_type(printer.get("type")) != "moonraker"
                or type(request_id) is not int or not 0 < request_id < 2**31):
            self._post({"cameraNativeFrame": {"ok": False, "requestId": request_id,
                "targetUuid": msg.get("targetUuid"), "code": "unavailable"}})
            return
        with self._u1_camera_lock:
            if self._u1_camera is not None:
                existing = self._u1_camera
                if existing["identity"] == msg and existing["request_id"] == request_id:
                    return
                self._cancel_u1_camera()
            if not _U1_CAMERA_SLOT.acquire(blocking=False):
                self._post({"cameraNativeFrame": {"ok": False, "requestId": request_id,
                    "targetUuid": printer["target_uuid"], "code": "unavailable"}})
                return
            entry = {"identity": dict(msg), "request_id": request_id, "lease": time.monotonic() + 15,
                     "page_epoch": self._credential_page_epoch, "runtime_epoch": _PLUGIN_RUNTIME_EPOCH,
                     "root": PLUGIN_STORAGE_DIR, "client": None, "slot_owned": True, "running": False}
            self._u1_camera = entry
        with _U1_CAMERA_CLIENTS_LOCK:
            _U1_CAMERA_OWNER = (self, entry)
        def guard():
            with self._u1_camera_lock:
                current = (self._u1_camera is entry and self._accept_actions
                    and entry["page_epoch"] == self._credential_page_epoch
                    and entry["runtime_epoch"] == _PLUGIN_RUNTIME_EPOCH and entry["root"] == PLUGIN_STORAGE_DIR
                    and time.monotonic() < entry["lease"] and CAMERA_WORKER.current_job_is_active())
            return current and resolve_printer_target(dashboard_printers(), entry["identity"]) is not None
        def publish(payload):
            if guard():
                self._post({"cameraNativeFrame": {"ok": True, "requestId": request_id,
                    "targetUuid": printer["target_uuid"], **payload}})
        def run():
            global _U1_CAMERA_OWNER
            try:
                with self._u1_camera_lock:
                    entry["running"] = True
                if not guard():
                    return
                current = hydrate_credentials(printer)
                client = _pf_u1_camera.Session(normalize_url(current.get("url")), current.get("apikey", ""),
                    ssl_ctx_for(current), _pf_creality, guard, publish)
                with _U1_CAMERA_CLIENTS_LOCK:
                    _U1_CAMERA_CLIENTS.add(client)
                with self._u1_camera_lock:
                    entry["client"] = client
                publish({"sessionId": client.id, "started": True})
                client.run()
            except Exception as exc:
                if guard():
                    self._post({"cameraNativeFrame": {"ok": False, "requestId": request_id,
                        "targetUuid": printer["target_uuid"], "code":
                            "credential_unavailable" if isinstance(exc, _pf_credentials.Unavailable) else "unavailable"}})
            finally:
                if entry.get("client"):
                    entry["client"].cancel()
                    with _U1_CAMERA_CLIENTS_LOCK:
                        _U1_CAMERA_CLIENTS.discard(entry["client"])
                with self._u1_camera_lock:
                    if self._u1_camera is entry:
                        self._u1_camera = None
                self._release_u1_slot(entry)
                with _U1_CAMERA_CLIENTS_LOCK:
                    if _U1_CAMERA_OWNER and _U1_CAMERA_OWNER[1] is entry:
                        _U1_CAMERA_OWNER = None
        try:
            CAMERA_WORKER.submit(lambda: _run_credential_work(guard, run))
        except (PermissionError, RuntimeError):
            self._cancel_u1_camera()
            self._release_u1_slot(entry)
            self._post({"cameraNativeFrame": {"ok": False, "requestId": request_id,
                "targetUuid": printer["target_uuid"], "code": "thread_denied"}})

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
        # `ready` is also the recovery seam for a page that survived a host
        # capability cancellation/reload.  start_plugin_runtime() is
        # idempotent and reactivates stopped worker generations when needed.
        start_plugin_runtime()
        discovered = self._capture_discovered_printers()
        # Do not make Orca-owned preset choices depend on disk, LAN discovery,
        # or polling.  The worker follows with the authoritative dashboard and
        # de-duplicated picker snapshot; credentials remain Python-private.
        self._post({
            "available": [public_printer_record(item) for item in discovered]
        })
        return self._submit_ui_work(
            self._refresh_worker,
            discovered,
            discover_network,
        )

    def _capture_discovered_printers(self):
        discovered = discover_orca_printers()
        remember_orca_keys(discovered)
        self._discovered_printers = {
            item["discovery_ref"]: dict(item)
            for item in discovered
            if isinstance(item.get("discovery_ref"), str)
        }
        return discovered

    def _refresh_worker(self, discovered, discover_network=False):
        if not ACTION_WORKER.current_job_is_active():
            return
        migrate_orca_keys()
        printers = dashboard_printers()
        unmatched = attach_outbox(printers)
        outbox_usage = outbox_physical_usage(printers)
        outbox_usage["cleanup"] = self._leftover_cleanup_state()
        usage_by_path = {group["path"]: group for group in outbox_usage["groups"]}
        for printer in printers:
            usage = usage_by_path.get("outbox/" + str(printer.get("target_uuid") or ""))
            if usage:
                printer["outbox_count"] = usage["count"]
                printer["outbox_bytes"] = usage["size"]
        active_bindings = {printer_binding_key(printer) for printer in printers}
        with self._status_lock:
            self._list_generation += 1
            generation = self._list_generation
            for stale in set(self._status_cache) - active_bindings:
                self._status_cache.pop(stale, None)
            for printer in printers:
                cached = self._status_cache.get(printer_binding_key(printer))
                printer["status"] = (
                    dict(cached)
                    if cached is not None
                    else {"online": None, "state": "checking"}
                )
        payload = {
            "printers": [public_printer_record(item) for item in printers],
            "unmatched": unmatched,
            "unassignedOutbox": [
                {"file": r.get("file"), "name": r.get("name") or r.get("file"),
                 "size": max(0, int(r.get("size") or 0)),
                 "ts": max(0, int(r.get("ts") or 0)) if isinstance(r.get("ts"), (int, float)) else 0}
                for r in load_outbox_index()
                if isinstance(r.get("file"), str) and r["file"].startswith("unassigned/")
            ],
            "outboxPhysicalUsage": outbox_usage,
            "legacyImport": legacy_import_preview(),
            "credentialMigration": credential_migration_preview(),
            "outboxHint": outbox_hint_visible(),
            "settings": self._page_settings(),
        }
        if discovered is not None:
            payload["available"] = available_orca_printers(discovered)
        self._post(payload)
        self._poll_and_push(printers, discover_network, generation=generation)

    def _import_legacy_and_refresh(self, confirmation):
        imported = import_legacy_storage(confirmation)
        self._post_status(ui_text("legacyImportDone" if imported else "legacyImportFailed"))
        self._refresh_worker(self._capture_discovered_printers())

    def _credential_action(self, kind, confirmation, discovered):
        if kind == "credential-retry":
            _CREDENTIAL_VAULT.clear()
        else:
            ok = migrate_credentials(confirmation)
            self._post_status(ui_text("credentialMigrationDone" if ok else "credentialUnavailable"))
        self._refresh_worker(discovered)

    def _repetier_list_failure(self, msg, page_epoch=None):
        if (self._accept_actions and
                (page_epoch is None or page_epoch == self._credential_page_epoch)
                and _credential_work_is_active()):
            self._post({"repetierPrinters": {"ok": False, "requestId": msg.get("requestId")}})

    def _load_repetier_printers(self, msg):
        result = {"ok": False}
        try:
            key = private_credential(msg.get("apikey"))
            if key is not None:
                result = _pf_repetier.list_printers(normalize_url(msg.get("url")),
                            ssl_ctx_for(msg), key)
        except Exception:
            result = {"ok": False}
        if _credential_work_is_active():
            self._post({"repetierPrinters": {**result, "requestId": msg.get("requestId")}})

    def _add_and_refresh(self, entry, discovered):
        if not ACTION_WORKER.current_job_is_active():
            return
        if entry.get("type") == "repetier":
            # Recheck server state; the browser selector is never authoritative.
            listing = _pf_repetier.list_printers(entry.get("url"), ssl_ctx_for(entry), entry.get("apikey", ""))
            if (not listing.get("ok") or len([p for p in listing.get("printers", [])
                    if p.get("slug") == entry.get("repetier_slug")]) != 1):
                self._post_status(ui_text("repetierLoadFailed"))
                self._post({"adoptResult": {"url": entry.get("url"), "ok": False}})
                return
        stored = self._add_manual(entry)
        if stored is None:
            self._post_status(ui_text("credentialSaveFailed"))
        if not ACTION_WORKER.current_job_is_active():
            return
        self._refresh_worker(discovered)
        self._post({
            "adoptResult": {
                "url": entry.get("url") or "",
                "ok": stored is not None,
            },
        })

    def _remove_and_refresh(self, identity, discovered):
        if not ACTION_WORKER.current_job_is_active():
            return
        self._remove_manual(identity)
        # A LAN printer that was removed should immediately become available
        # for adoption again, without making the user press Refresh.
        self._refresh_worker(discovered, discover_network=True)

    def _leftover_blocker(self):
        """Why leftovers must not be deleted right now ('' when they may be)."""
        if _STORAGE_DENIED or not PLUGIN_STORAGE_DIR:
            return "storage"
        if (self._received_artifacts or self._artifact_tokens_in_use
                or self._active_transfer_token):
            return "busy"
        manager = self._transfer_manager
        if manager is not None and manager.active_owners():
            return "busy"
        scheduler = self._action_scheduler
        if scheduler is not None:
            snapshot = scheduler.snapshot()
            if snapshot.get("pending") or snapshot.get("running"):
                return "busy"
        records = load_outbox_index()
        if _OUTBOX_STORE_DAMAGED or any(
                "/" not in record["file"] or _safe_outbox_path(record["file"]) is None
                for record in records):
            return "index"
        storage_root = os.path.realpath(PLUGIN_STORAGE_DIR)
        database = os.path.join(storage_root, "operations.sqlite3")
        if os.path.exists(database):
            try:
                journal = _pf_journal.OperationJournal(database)
                try:
                    account_id = hashlib.sha256(storage_root.encode("utf-8")).hexdigest()
                    if any(item.get("state") in ("planned", "dispatching")
                           for item in journal.list_unresolved(account_id, 500)):
                        return "busy"
                finally:
                    journal.close()
            except Exception:
                return "busy"
        return ""

    def _protected_paths(self):
        return [artifact.path for artifact in list(self._received_artifacts.values())
                if getattr(artifact, "path", None)]

    def _leftover_cleanup_state(self):
        try:
            files, _skipped = outbox_leftover_files(self._protected_paths())
        except OSError:
            return {"available": False, "count": 0, "size": 0}
        if not files:
            return {"available": False, "count": 0, "size": 0}
        return {"available": not self._leftover_blocker(), "count": len(files),
                "size": sum(item[2] for item in files)}

    def _cleanup_leftovers_and_refresh(self, discovered):
        if not ACTION_WORKER.current_job_is_active():
            return
        with _OUTBOX_LOCK:
            blocker = self._leftover_blocker()
            if blocker:
                result = None
            else:
                files, skipped = outbox_leftover_files(self._protected_paths())
                result = outbox_remove_leftovers(files)
                result["failed"] = sorted(set(result["failed"]) | set(skipped))
        if result is None:
            self._post_status(ui_text("outboxStorageCleanBlocked"))
        elif result["failed"]:
            self._post_status(ui_text(
                "outboxStorageCleanFailed", count=len(result["failed"]),
                paths=", ".join(result["failed"][:3]),
            ) + " " + ui_text("outboxStorageCleanDone", count=result["deleted"],
                              size=format_size(result["size"])))
        else:
            self._post_status(ui_text(
                "outboxStorageCleanDone", count=result["deleted"],
                size=format_size(result["size"]),
            ))
        self._refresh_worker(discovered)

    def _drop_and_refresh(self, stored, discovered):
        if not ACTION_WORKER.current_job_is_active():
            return
        removed = outbox_remove(stored)
        if not removed:
            self._post_status(ui_text("outboxRemoveFailed"))
        self._refresh_worker(discovered)

    def _drop_unassigned_and_refresh(self, discovered):
        if not ACTION_WORKER.current_job_is_active():
            return
        failed = 0
        for record in load_outbox_index():
            stored = record.get("file") if isinstance(record, dict) else None
            if isinstance(stored, str) and stored.startswith("unassigned/") and not outbox_remove(stored):
                failed += 1
        if failed:
            self._post_status(ui_text("outboxRemoveFailed"))
        self._refresh_worker(discovered)

    def _drop_target_and_refresh(self, identity, discovered):
        if not ACTION_WORKER.current_job_is_active():
            return
        printers = dashboard_printers()
        printer = resolve_printer_target(printers, identity)
        record = next((r for r in load_outbox_index()
                       if r.get("file") == identity.get("file")), None)
        if printer is None or record is None or outbox_target_match(record, printers) is not printer:
            return
        if not outbox_remove(identity.get("file")):
            self._post_status(ui_text("outboxRemoveFailed"))
        self._refresh_worker(discovered)

    def _poll_dashboard(self, token=None):
        if token is not None and token != self._poll_token:
            return
        if not ACTION_WORKER.current_job_is_active():
            self._finish_dashboard_poll(token)
            return
        with self._status_lock:
            generation = self._list_generation
        self._post({"outboxHint": outbox_hint_visible(),
                    "settings": self._page_settings()})
        self._poll_and_push(
            dashboard_printers(),
            on_complete=lambda: self._finish_dashboard_poll(token),
            generation=generation,
        )

    def _reset_dashboard_poll(self):
        with self._status_lock:
            self._poll_token = None
            self._polling = False
            self._list_generation += 1

    def _finish_dashboard_poll(self, token=None):
        with self._status_lock:
            if token is None or token == self._poll_token:
                self._polling = False
                self._poll_token = None

    def _poll_into(self, printer, batch=None, generation=None):
        page_epoch = self._credential_page_epoch
        guard = lambda: (self._accept_actions and page_epoch == self._credential_page_epoch
                         and (generation is None or generation == self._list_generation))
        return _run_credential_work(guard, self._poll_into_worker, printer, batch, generation)

    def _poll_into_worker(self, printer, batch=None, generation=None):
        try:
            status = poll_printer(printer)
            if not POLL_WORKERS.current_job_is_active():
                return
            printer["status"] = status
            with self._status_lock:
                if generation is not None and generation != self._list_generation:
                    return
                self._status_cache[printer_binding_key(printer)] = dict(status)
            history_changed = record_print_observation(printer, status)
            payload = {"printerStatus": {
                "url": printer.get("url"), "targetUuid": printer.get("target_uuid"),
                "ownerScope": printer.get("owner_scope_uuid"),
                "targetSelector": printer.get("target_selector"),
                "bindingRevision": printer.get("binding_revision"),
                "connectionType": printer.get("type"),
                "status": public_status(status),
            }}
            if history_changed:
                payload["printHistory"] = {
                    "mode": "printer",
                    "printerKey": printer_history_key(printer),
                    "printerId": printer.get("id"),
                    "targetUuid": printer.get("target_uuid"),
                    "ownerScope": printer.get("owner_scope_uuid"),
                    "targetSelector": printer.get("target_selector"),
                    "bindingRevision": printer.get("binding_revision"),
                    "printerUrl": printer.get("url") or "",
                    "connectionType": printer.get("type"),
                    "printerName": printer.get("name") or printer.get("url") or "",
                    "items": [
                        _public_history_record(item, printer.get("name") or printer.get("url") or "")
                        for item in print_history_for_printer(printer)
                    ],
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
            self._post({
                "printers": [public_printer_record(item) for item in printers]
            })
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
            heard = ssdp_bambu_names(2.0)
            for ip, info in heard.items():
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
            if is_current:
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
            or not POLL_WORKERS.accepting_work()
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
                try:
                    POLL_WORKERS.submit(self._poll_into, printer, batch, generation)
                except (PermissionError, RuntimeError):
                    printer["status"] = _poll_failure_status(
                        backend_for_type(printer.get("type")),
                        PermissionError("background work denied"),
                    )
                    batch.finish_one()
        if not discover:
            return
        try:
            POLL_WORKERS.submit(self._discover_and_push, list(printers), generation)
        except (PermissionError, RuntimeError):
            pass

    def _prepare_outbox_start(self, msg):
        scheduler, generation = self._ensure_action_scheduler()
        if scheduler is None:
            return
        printers = dashboard_printers()
        printer = resolve_printer_target(printers, msg)
        stored = msg.get("file")
        record = next((r for r in load_outbox_index() if r.get("file") == stored), None)
        explicit = msg.get("unassigned") is True
        start = msg.get("start") is True
        if (printer is None or record is None
                or not outbox_target_allowed(record, printer, printers, explicit)):
            self._post_status(ui_text("startApprovalExpired"))
            return
        try:
            path = _safe_outbox_path(stored)
            if path is None:
                raise ValueError("invalid outbox path")
            artifact = _pf_artifact.inspect_staged_path(
                path, record.get("name") or stored, OUTBOX_DIR,
                provenance="orca_export", expected_sha256=record.get("sha256"),
            )
            if explicit:
                verdict, reason = _unassigned_compatibility(printer, artifact)
            else:
                verdict, reason = start_compatibility(printer, artifact, None)
            nonce = digest = None
            if verdict != "incompatible":
                plan = self._start_approval_plan(
                    printer, artifact, None, start if explicit else True,
                    {"file": stored, "mode": "start" if start else "upload"}
                    if explicit else None)
                nonce, digest = self._approval_store.create(
                    plan, ttl=120, epoch=generation,
                )
        except (OSError, ValueError, RuntimeError):
            self._post_status(ui_text("startApprovalExpired"))
            return
        self._post({"outboxStartApproval": {
            "verdict": verdict, "reason": reason,
            "file": stored, "filename": artifact.filename,
            "unassigned": explicit, "start": start,
            "profile": artifact.metadata.get("printer_settings_id")
                       or artifact.metadata.get("printer_model") or "",
            "printer": printer.get("name") or printer.get("url") or "",
            "nonce": nonce, "digest": digest,
            "target": {
                "targetUuid": printer.get("target_uuid"),
                "ownerScope": printer.get("owner_scope_uuid"),
                "targetSelector": printer.get("target_selector"),
                "bindingRevision": printer.get("binding_revision"),
                "url": printer.get("url"),
                "connectionType": printer.get("type"),
            },
        }})

    def _do_outbox_send(self, msg):
        if not self._work_is_active():
            return
        printers = dashboard_printers()
        stored = msg.get("file") or ""
        record = next((r for r in load_outbox_index() if r.get("file") == stored), None)
        printer = resolve_printer_target(printers, msg)
        explicit = msg.get("unassigned") is True
        if record is None or printer is None or not outbox_target_allowed(
                record, printer, printers, explicit):
            return
        target = normalize_url(printer.get("url") or "")
        try:
            path = _safe_outbox_path(stored)
            if path is None:
                return
            digest = hashlib.sha256()
            with open(path, "rb") as fh:
                for block in iter(lambda: fh.read(192 * 1024), b""):
                    digest.update(block)
            if record.get("sha256") and record["sha256"] != digest.hexdigest():
                raise ValueError("queued artifact changed")
            artifact = _pf_artifact.inspect_staged_path(
                path, record.get("name") or stored, OUTBOX_DIR,
                provenance="orca_export", expected_sha256=digest.hexdigest(),
            )
        except (OSError, ValueError):
            self._post_status(ui_text("fileReadFailed"))
            return
        start = msg.get("start") is True
        start_check = None
        label = printer.get("name") or target
        if not outbox_target_allowed(record, printer, printers, explicit):
            self._post_status(ui_text("fileTargetMismatch", printer=label))
            return
        if start or explicit:
            if explicit:
                verdict, reason = _unassigned_compatibility(printer, artifact)
            else:
                verdict, reason = start_compatibility(printer, artifact, None)
            if verdict == "incompatible":
                self._post_status(ui_text(
                    "startBlocked", printer=label, reason=ui_text(reason)))
                return
            if ((start and msg.get("bedClear") is not True)
                    or (verdict == "unknown" and msg.get("operatorChecked") is not True)):
                self._post_status(ui_text("startConfirmMissing"))
                return
            plan = self._start_approval_plan(
                printer, artifact, None, msg.get("use_ams") is True,
                {"file": stored, "mode": "start" if start else "upload"}
                if explicit else None,
            )
            if not self._approval_store.consume(
                    msg.get("approvalNonce"), plan, self._action_generation):
                self._post_status(ui_text("startApprovalExpired"))
                return
            start_check = {"verdict": verdict, "reason": reason,
                           "operator_checked": verdict == "unknown", "bed_clear": start}
        live = poll_printer(printer) or {}
        if not self._work_is_active():
            return
        current = resolve_printer_target(dashboard_printers(), msg)
        if current is None:
            return
        if not status_can_upload(live, current):
            self._post_status(ui_text(
                "sendFailed", printer=label, error=ui_text("genericError")
            ))
            self._refresh_worker(None)
            return
        if explicit and not start and status_is_busy(live):
            self._post_status(ui_text(
                "busyNotStarted", printer=label,
                state=ui_state(live.get("state") or "unknown"),
            ))
            self._refresh_worker(None)
            return
        if start and not status_can_start(live, current):
            self._post_status(ui_text(
                "busyNotStarted", printer=label,
                state=ui_state(live.get("state") or "unknown"),
            ))
            self._refresh_worker(None)
            return
        printer = current
        if not _pf_artifact.verify_staged_artifact(artifact):
            self._post_status(ui_text("fileReadFailed"))
            return
        dispatch = self._journal_begin(
            printer, "start" if start else "upload",
            artifact_sha256=artifact.manifest.get("sha256"),
            artifact_name=record.get("name") or stored,
            start_check=start_check,
        )
        if dispatch is None:
            self._post_status(ui_text("sendFailed", printer=label,
                                      error=self._journal_failure_text()))
            return
        result = send_file_path(printer, record.get("name") or stored, artifact.path,
                            start=start, use_ams=msg.get("use_ams") is True,
                            status=live)
        self._journal_finish(dispatch, result)
        if not self._work_is_active():
            return
        if result.get("ok"):
            # Written by Print Farm itself, with the digest of the file it sent:
            # the journal can offer this stored copy again without guessing.
            recorder = (record_print_started if start and printer.get("type") != "repetier"
                        else record_file_sent)
            recorder(printer, record.get("name") or stored,
                     sha256=artifact.manifest.get("sha256"))
            done = ui_text("repetierRequestAccepted" if start and printer.get("type") == "repetier"
                           else "sentToPrint" if start else "uploadedNotStarted")
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
        printers = dashboard_printers()
        printer = resolve_printer_target(printers, msg)
        if printer is None:
            return
        items = print_history_for_printer(printer)
        self._post({
            "printHistory": {
                "mode": "printer",
                "requestId": _history_int(msg.get("requestId"), minimum=0),
                "printerKey": printer_history_key(printer),
                "printerId": printer.get("id"),
                "targetUuid": printer.get("target_uuid"),
                "ownerScope": printer.get("owner_scope_uuid"),
                "targetSelector": printer.get("target_selector"),
                "bindingRevision": printer.get("binding_revision"),
                "printerUrl": printer.get("url") or "",
                "connectionType": printer.get("type"),
                "printerName": printer.get("name") or printer.get("url") or "",
                "available": not _STORAGE_DENIED and not _HISTORY_STORE_DAMAGED,
                "items": [
                    _public_history_record(item, printer.get("name") or printer.get("url") or "")
                    for item in items
                ],
            }
        })

    def _post_print_history_all(self, msg):
        apply_history_clear_request()
        records = load_print_history()
        available = not _STORAGE_DENIED and not _HISTORY_STORE_DAMAGED
        stored = {}
        for outbox in load_outbox_index():
            digest = _history_digest(outbox.get("sha256"))
            if digest and isinstance(outbox.get("file"), str):
                newest = stored.get(digest)
                if newest is None or (outbox.get("ts") or 0) >= (newest.get("ts") or 0):
                    stored[digest] = outbox
        items = [
            _public_history_record(
                record, stored_file=(stored.get(_history_digest(record.get("sha256"))) or {}).get("file"))
            for record in records
        ]
        counts = {
            "all": len(items),
            "completed": sum(1 for item in items if item.get("state") == "completed"),
            "active": sum(1 for item in items if item.get("state") in _HISTORY_ACTIVE_STATES),
        }
        holds = []
        journal_error = False
        account_id = self._journal_account_id()
        if account_id:
            journal = None
            try:
                journal = _pf_journal.OperationJournal(
                    os.path.join(os.path.realpath(PLUGIN_STORAGE_DIR), "operations.sqlite3"))
                for operation in journal.list_unresolved(account_id, limit=100):
                    binding = operation.get("target_binding") or {}
                    holds.append({
                        "operation_id": operation["operation_id"],
                        "action": operation["action"], "state": operation["state"],
                        "artifact_name": operation.get("artifact_name"),
                        "target_name": binding.get("endpoint") or operation.get("target_id"),
                    })
            except Exception:
                journal_error = True
            finally:
                if journal is not None:
                    journal.close()
        elif not _STORAGE_DENIED:
            journal_error = True
        self._post({"printHistory": {
            "mode": "all",
            "requestId": _history_int(msg.get("requestId"), minimum=0),
            "available": available,
            "counts": counts,
            "items": items,
            "holds": holds,
            "holds_available": not journal_error,
        }})

    @staticmethod
    def _journal_account_id():
        if _STORAGE_DENIED or not PLUGIN_STORAGE_DIR:
            return None
        storage_root = os.path.realpath(PLUGIN_STORAGE_DIR)
        return hashlib.sha256(storage_root.encode("utf-8")).hexdigest()

    def _resolve_journal_hold(self, msg):
        if _STORAGE_DENIED or not PLUGIN_STORAGE_DIR:
            self._post_status(ui_text("journalHoldUnavailable"))
            return
        operation_id = msg.get("operationId")
        if not isinstance(operation_id, str) or not operation_id or len(operation_id) > 128:
            self._post_status(ui_text("journalHoldUnavailable"))
            return
        journal = None
        try:
            account_id = self._journal_account_id()
            journal = _pf_journal.OperationJournal(
                os.path.join(os.path.realpath(PLUGIN_STORAGE_DIR), "operations.sqlite3"))
            # list_unresolved applies the private-storage-derived account scope.
            # Never accept account or target bindings from the WebView.
            operation = next((item for item in journal.list_unresolved(account_id, limit=500)
                              if item.get("operation_id") == operation_id), None)
            if operation is None or operation.get("state") not in (
                    "submitted", "acknowledged", "unknown_outcome"):
                self._post_status(ui_text("journalHoldUnavailable"))
                return
            journal.resolve_unknown_manually(
                operation_id,
                reason="Operator checked the physical target, job, and bed before releasing the hold",
                actor="operator",
            )
        except Exception:
            self._post_status(ui_text("journalHoldUnavailable"))
            return
        finally:
            if journal is not None:
                journal.close()
        self._post_status(ui_text("journalHoldReleased"))
        self._post_print_history_all({"requestId": _history_int(
            msg.get("historyRequestId", msg.get("requestId")), minimum=0)})

    def _journal_begin(self, printer, action, *, artifact_sha256=None,
                       plate_entry=None, artifact_name=None, start_check=None):
        """Durably claim a target mutation before its first network request."""
        journal = None
        try:
            if _STORAGE_DENIED or not PLUGIN_STORAGE_DIR:
                raise OSError("private plugin storage unavailable")
            target_uuid = str(printer.get("target_uuid") or "")
            owner_scope = str(printer.get("owner_scope_uuid") or "")
            if not target_uuid or not owner_scope:
                raise ValueError("printer target has no persisted identity")
            parsed = urllib.parse.urlsplit(normalize_url(printer.get("url") or ""))
            host = (parsed.hostname or "").casefold()
            port = parsed.port
            endpoint = urllib.parse.urlunsplit((parsed.scheme.casefold(),
                host + ((":" + str(port)) if port else ""), parsed.path, "", ""))
            binding = {
                "target_uuid": target_uuid,
                "owner_scope_uuid": owner_scope,
                "binding_revision": int(printer.get("binding_revision") or 0),
                "endpoint": endpoint,
                "connection_type": str(printer.get("type") or ""),
                "serial": str(printer.get("serial") or ""),
                "repetier_slug": str(printer.get("repetier_slug") or ""),
            }
            if isinstance(start_check, dict):
                # The compatibility verdict and the operator's confirmations are
                # part of the immutable operation intent.
                binding["start_check"] = {
                    "verdict": str(start_check.get("verdict") or ""),
                    "reason": str(start_check.get("reason") or ""),
                    "operator_checked": start_check.get("operator_checked") is True,
                    "bed_clear": start_check.get("bed_clear") is True,
                }
            storage_root = os.path.realpath(PLUGIN_STORAGE_DIR)
            account_id = hashlib.sha256(storage_root.encode("utf-8")).hexdigest()
            target_id = owner_scope + ":" + target_uuid
            journal = _pf_journal.OperationJournal(
                os.path.join(storage_root, "operations.sqlite3"))
            operation = journal.prepare(
                account_id=account_id, target_id=target_id,
                target_binding=binding, action=action,
                artifact_sha256=artifact_sha256, plate_entry=plate_entry,
                artifact_name=artifact_name,
            )
            try:
                claimed = journal.begin_dispatch(operation["operation_id"])
            except Exception:
                _journal_cancel_plan(journal, operation["operation_id"])
                raise
            if not claimed:
                _journal_cancel_plan(journal, operation["operation_id"])
                raise _pf_journal.ConflictError("operation was already dispatched")
            _JOURNAL_CLAIM.blocked = False
            return journal, operation["operation_id"]
        except Exception as exc:
            _JOURNAL_CLAIM.blocked = isinstance(exc, _pf_journal.ConflictError)
            if journal is not None:
                try:
                    journal.close()
                except Exception:
                    pass
            return None

    @staticmethod
    def _journal_failure_text():
        """Why a new operation could not be claimed (hold vs. any other cause)."""
        if getattr(_JOURNAL_CLAIM, "blocked", False):
            return ui_text("journalBlockedByHold")
        return ui_text("genericError")

    @staticmethod
    def _journal_finish(dispatch, result):
        journal, operation_id = dispatch
        try:
            declared = result.get("journal_state")
            if result.get("ok"):
                if declared == "verified" and result.get("evidence_ref"):
                    # Only an adapter that read the file back may claim this.
                    journal.report(operation_id, "verified",
                                   evidence_ref=result["evidence_ref"], actor="transport")
                else:
                    # Transport submission is not evidence that physical printing began.
                    journal.report(operation_id, "submitted", actor="transport")
            elif declared == "rejected" and result.get("evidence_ref"):
                journal.report(operation_id, "rejected", evidence_ref=result["evidence_ref"])
            elif (result.get("evidence_ref") and result.get("error_code") in
                  {"authorization_denied", "endpoint_not_found"}):
                journal.report(operation_id, "rejected", evidence_ref=result["evidence_ref"])
            else:
                journal.report(operation_id, "unknown_outcome", actor="transport")
        except Exception:
            # Keep the durable dispatching state as a conservative unresolved hold.
            pass
        finally:
            journal.close()

    def _do_printer_cmd(self, msg):
        if not self._work_is_active():
            return
        printers = dashboard_printers()
        action = msg.get("action") or ""
        printer = resolve_printer_target(printers, msg)
        if printer is None:
            return
        target = normalize_url(printer.get("url") or "")
        live = poll_printer(printer) or {}
        if not self._work_is_active():
            return
        printer = resolve_printer_target(dashboard_printers(), msg)
        if printer is None:
            return
        if not status_can_control(live, action, printer):
            self._post_status(ui_text(
                "commandFailed",
                printer=printer.get("name") or target,
                action=ui_text({"pause": "pause", "resume": "resume", "cancel": "stop"}.get(
                    action, "genericError"
                )),
                error=ui_text("genericError"),
            ))
            self._refresh_worker(printers)
            return
        journal_action = {"pause": "pause", "resume": "resume", "cancel": "cancel"}.get(action)
        if journal_action is None:
            self._post_status(ui_text("genericError"))
            return
        dispatch = self._journal_begin(printer, journal_action)
        if dispatch is None:
            self._post_status(ui_text(
                "commandFailed", printer=printer.get("name") or target,
                action=ui_text({"pause": "pause", "resume": "resume", "cancel": "stop"}.get(
                    action, "genericError")), error=ui_text("genericError"),
            ))
            return
        result = printer_command(printer, action, status=live)
        self._journal_finish(dispatch, result)
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

    def _artifact_receiver(self):
        if _STORAGE_DENIED or not PLUGIN_STORAGE_DIR:
            raise OSError("plugin storage unavailable")
        if self._transfer_manager is None:
            self._transfer_manager = _pf_transfer.ArtifactTransferManager(
                os.path.join(PLUGIN_STORAGE_DIR, "incoming"),
            )
        return self._transfer_manager

    def _cancel_artifact_transfer(self):
        token = self._active_transfer_token
        self._active_transfer_token = None
        if token and self._transfer_manager is not None:
            try:
                self._transfer_manager.cancel(self._transfer_owner, token)
            except ValueError:
                pass

    def _reject_artifact_chunk(self):
        self._cancel_artifact_transfer()
        self._post({"artifactError": True})

    def _do_artifact_begin(self, msg):
        try:
            if len(self._received_artifacts) >= 128:
                raise ValueError("too many staged artifacts")
            token = self._artifact_receiver().begin(
                self._transfer_owner, msg.get("filename"), msg.get("size"),
            )
            self._active_transfer_token = token
            self._post({"artifactBegin": {"token": token}})
        except (OSError, ValueError):
            self._post({"artifactError": True})

    def _do_artifact_chunk(self, msg):
        try:
            encoded = msg.get("dataB64")
            if not isinstance(encoded, str) or len(encoded) > 44000:
                raise ValueError("invalid chunk")
            chunk = base64.b64decode(encoded, validate=True)
            token = msg.get("token")
            sequence = msg.get("sequence")
            self._artifact_receiver().append(
                self._transfer_owner, token, sequence, chunk,
                msg.get("sha256"),
            )
            self._post({"artifactChunk": {"token": token, "sequence": sequence}})
        except (OSError, ValueError, TypeError):
            self._cancel_artifact_transfer()
            self._post({"artifactError": True})

    def _do_artifact_finish(self, msg):
        received = None
        try:
            token = msg.get("token")
            receiver = self._artifact_receiver()
            received = receiver.finish(self._transfer_owner, token)
            self._active_transfer_token = None
            artifact = _pf_artifact.inspect_staged_path(
                received.path, received.filename, receiver.storage_dir,
                provenance="manual_import", expected_sha256=received.sha256,
            )
            with self._artifact_use_lock:
                if not self._accept_actions:
                    _pf_artifact.discard_staged_artifact(
                        artifact, receiver.storage_dir,
                    )
                    return
                self._received_artifacts[token] = artifact
            self._post({"artifactReady": {
                "token": token, "plates": list(artifact.plate_entries),
                "kind": artifact.manifest["kind"],
            }})
        except (OSError, ValueError):
            if received is not None:
                try:
                    pathlib.Path(received.path).unlink()
                except OSError:
                    pass
            self._post({"artifactError": True})

    def _do_artifact_discard(self, msg):
        token = msg.get("token")
        if not isinstance(token, str):
            return
        with self._artifact_use_lock:
            if token in self._artifact_tokens_in_use:
                return
            artifact = self._received_artifacts.pop(token, None)
        if artifact is None:
            return
        try:
            _pf_artifact.discard_staged_artifact(
                artifact, self._artifact_receiver().storage_dir,
            )
        except (OSError, ValueError):
            self._post_status(ui_text("preparedUploadDiscardFailed"))

    def _discard_idle_artifacts(self):
        with self._artifact_use_lock:
            idle = [self._received_artifacts.pop(token)
                    for token in list(self._received_artifacts)
                    if token not in self._artifact_tokens_in_use]
        for artifact in idle:
            try:
                _pf_artifact.discard_staged_artifact(
                    artifact, self._artifact_receiver().storage_dir,
                )
            except (OSError, ValueError):
                self._post_status(ui_text("preparedUploadDiscardFailed"))

    def _discard_temporary_plate(self, plate):
        if plate is None:
            return
        try:
            _pf_artifact.discard_staged_plate(
                plate, self._artifact_receiver().storage_dir,
            )
        except (OSError, ValueError):
            self._post_status(ui_text("preparedUploadDiscardFailed"))

    def _do_mass_print(self, msg):
        if not self._work_is_active():
            return
        token = msg.get("artifactToken")
        artifact = self._received_artifacts.get(token) if isinstance(token, str) else None
        if artifact is None or not _pf_artifact.verify_staged_artifact(artifact):
            self._post_status(ui_text("fileReadFailed"))
            return
        filename = artifact.filename
        kind = artifact.manifest.get("kind")
        plate_entry = msg.get("plate")
        if kind == "sliced_3mf":
            try:
                _pf_artifact.select_plate_entry(artifact, plate_entry)
            except ValueError:
                self._post_status(ui_text("fileReadFailed"))
                return
        elif plate_entry:
            self._post_status(ui_text("fileReadFailed"))
            return
        start = msg.get("start") is True
        if start and msg.get("_approved") is not True:
            self._post_status(ui_text("startApprovalExpired"))
            return
        use_ams = msg.get("use_ams") is True
        identities = msg.get("targets")
        if not isinstance(identities, list):
            return
        selected = []
        for identity in identities:
            printer = resolve_printer_target(dashboard_printers(), identity)
            if printer is None or any(p.get("target_uuid") == printer.get("target_uuid") for p in selected):
                self._post_status(ui_text("genericError"))
                return
            selected.append(printer)
        if not msg.get("_retainArtifact"):
            self._received_artifacts.pop(token, None)
        ok, fail, errors = 0, 0, []
        for printer in selected:
            extracted_plate = None
            if not self._work_is_active():
                return
            identity = {"targetUuid": printer.get("target_uuid"), "ownerScope": printer.get("owner_scope_uuid"),
                "targetSelector": printer.get("target_selector"), "bindingRevision": printer.get("binding_revision"),
                "url": printer.get("url"), "connectionType": printer.get("type")}
            current = resolve_printer_target(dashboard_printers(), identity)
            if current is None:
                fail += 1; errors.append(ui_text("genericError")); continue
            live = poll_printer(current) or {}
            if not self._work_is_active():
                return
            current = resolve_printer_target(dashboard_printers(), identity)
            if current is None:
                fail += 1; errors.append(ui_text("genericError")); continue
            if not status_can_upload(live, current):
                fail += 1; errors.append(ui_text("notReachable", printer=current.get("name") or "printer")); continue
            if start:
                verdict, reason = start_compatibility(current, artifact, plate_entry or None)
                if verdict == "incompatible":
                    fail += 1
                    errors.append("%s: %s" % (current.get("name") or "printer", ui_text(reason)))
                    continue
            if start and not status_can_start(live, current):
                fail += 1
                errors.append(ui_text("busy", printer=current.get("name") or "printer",
                                      state=ui_state(live.get("state") or "unknown")))
                continue
            send_path = artifact.path
            send_name = filename
            if kind == "sliced_3mf" and backend_for_type(current.get("type")) != "bambu":
                try:
                    extracted_plate = _pf_artifact.stage_selected_plate(
                        artifact, plate_entry, self._artifact_receiver().storage_dir,
                    )
                    if not _pf_artifact.verify_staged_plate(extracted_plate):
                        raise ValueError("staged plate changed")
                    send_path = extracted_plate.path
                    send_name = pathlib.Path(plate_entry).name
                except (OSError, ValueError):
                    self._discard_temporary_plate(extracted_plate)
                    fail += 1; errors.append(ui_text("fileReadFailed")); continue
            elif not _pf_artifact.verify_staged_artifact(artifact):
                fail += 1; errors.append(ui_text("fileReadFailed")); continue
            dispatch = self._journal_begin(
                current, "start" if start else "upload",
                artifact_sha256=artifact.manifest.get("sha256"),
                plate_entry=plate_entry,
                artifact_name=filename,
                start_check=msg.get("_startCheck") if start else None,
            )
            if dispatch is None:
                self._discard_temporary_plate(extracted_plate)
                fail += 1
                errors.append("%s: %s" % (current.get("name") or current.get("url"),
                                           self._journal_failure_text()))
                continue
            try:
                result = send_file_path(current, send_name, send_path, start, use_ams,
                                        status=live, plate_entry=plate_entry)
            finally:
                self._discard_temporary_plate(extracted_plate)
            self._journal_finish(dispatch, result)
            if not self._work_is_active():
                return
            if result.get("ok"):
                ok += 1
            else:
                fail += 1
                errors.append("%s: %s" % (current.get("name") or current.get("url"), operation_error_text(result)))
        failed_text = ui_text("massFailed", count=fail) if fail else ""
        text = ui_text("massResult", count=ok, failed=failed_text)
        if errors: text += " " + "; ".join(errors[:3])
        self._post_status(text)
        self._refresh_worker(None)
        return fail == 0 and ok == len(selected) and ok > 0

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
        request = getattr(orca, "request_permissions", None)
        if callable(request):
            try:
                request(threading=True)
            except TypeError:
                # Older hosts do not accept the threading permission argument.
                pass
        if not host_capability_lifecycle_available():
            start_plugin_runtime()
        orca.register_capability(PrintersDashboard)
        orca.register_capability(PrintersOutbox)
