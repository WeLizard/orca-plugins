"""Repetier-Server HTTP bridge, with explicit printer selection and no retries.

Contract: prgdoc.repetier-server.com/v1/docs/en/web-api/{direct,bridge}.md,
websocket/{jobs,events}.md and types/PrinterState.md (2026-10-28).
Only the classic server-managed jobs of 0.90--1.4.x permit writes.
"""
from __future__ import annotations

import http.client
import json
import math
import os
import re
import ssl
import urllib.parse
import uuid

_TIMEOUT = 6
_MAX_RESPONSE = 2 * 1024 * 1024
_CHUNK = 64 * 1024


class AdapterError(Exception):
    def __init__(self, code, outcome="not_sent"):
        self.code, self.outcome = code, outcome
        super().__init__(code)


def valid_slug(value):
    return (isinstance(value, str) and 0 < len(value) <= 128
            and value not in (".", "..")
            and re.fullmatch(r"[A-Za-z0-9_-]+", value) is not None)


def _parts(base):
    if (not isinstance(base, str) or not base
            or any(ord(c) < 33 or ord(c) == 127 for c in base)):
        raise AdapterError("invalid_address")
    try:
        p = urllib.parse.urlsplit(base)
        port = p.port
    except ValueError as exc:
        raise AdapterError("invalid_address") from exc
    if (p.scheme not in ("http", "https") or not p.hostname or p.username is not None
            or p.password is not None or p.query or p.fragment
            or "\\" in urllib.parse.unquote(base)
            or any(ord(c) < 32 or ord(c) == 127 for c in urllib.parse.unquote(p.path))
            or any(v in (".", "..") for v in
                                    urllib.parse.unquote(p.path).split("/"))):
        raise AdapterError("invalid_address")
    return p, port


def _headers(key):
    if not isinstance(key, str) or len(key) > 512 or any(ord(c) < 32 or ord(c) == 127 for c in key):
        raise AdapterError("invalid_api_key")
    return {"X-Api-Key": key} if key else {}


def _request(base, ctx, key, path, *, mutation=False, file_path=None, fields=None, filename=None,
             is_current=lambda: True):
    p, port = _parts(base)
    headers = _headers(key)
    connection = (http.client.HTTPSConnection(p.hostname, port, timeout=_TIMEOUT,
                  context=ctx or ssl.create_default_context()) if p.scheme == "https" else
                  http.client.HTTPConnection(p.hostname, port, timeout=_TIMEOUT))
    source = None
    dispatched = False
    try:
        prefix = suffix = b""
        size = 0
        if file_path is not None:
            if (not isinstance(filename, str) or not filename or filename in (".", "..")
                    or any(c in filename for c in '/\\"')
                    or any(ord(c) < 32 or ord(c) == 127 for c in filename)):
                raise AdapterError("invalid_filename")
            source = open(file_path, "rb")
            size = os.fstat(source.fileno()).st_size
            boundary = "PrintFarm" + uuid.uuid4().hex
            prefix = b"".join(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n" %
                               (boundary, k, v)).encode("utf-8") for k, v in fields.items())
            quoted = urllib.parse.quote(filename, safe="")
            prefix += ("--%s\r\nContent-Disposition: form-data; name=\"filename\"; "
                       "filename=\"%s\"; filename*=UTF-8''%s\r\nContent-Type: application/octet-stream\r\n\r\n" %
                       (boundary, filename.encode("ascii", "replace").decode(), quoted)).encode("ascii")
            suffix = ("\r\n--%s--\r\n" % boundary).encode("ascii")
            headers.update({"Content-Type": "multipart/form-data; boundary=" + boundary,
                            "Content-Length": str(len(prefix) + size + len(suffix))})
        connection.putrequest("POST" if mutation else "GET", p.path.rstrip("/") + path)
        for k, v in {"Accept": "application/json", "Connection": "close", **headers}.items():
            connection.putheader(k, v)
        if mutation and not is_current():
            raise AdapterError("target_changed", "not_sent")
        dispatched = mutation
        connection.endheaders()
        if source:
            connection.send(prefix)
            remaining = size
            while remaining:
                chunk = source.read(min(_CHUNK, remaining))
                if not chunk:
                    raise AdapterError("unknown_outcome", "unknown")
                connection.send(chunk)
                remaining -= len(chunk)
            connection.send(suffix)
        response = connection.getresponse()
        payload = response.read(_MAX_RESPONSE + 1)
        if response.status in (401, 403):
            raise AdapterError("authorization_denied", "rejected")
        if 300 <= response.status < 400:
            raise AdapterError("redirect_rejected", "unknown" if mutation else "not_sent")
        if not 200 <= response.status < 300:
            raise AdapterError("http_%d" % response.status, "unknown" if mutation else "not_sent")
        if len(payload) > _MAX_RESPONSE:
            raise AdapterError("malformed_response", "unknown" if mutation else "not_sent")
        try:
            value = json.loads(payload.decode("utf-8")) if payload.strip() else ({} if mutation else None)
        except (ValueError, UnicodeError) as exc:
            raise AdapterError("unknown_outcome" if mutation else "malformed_response",
                               "unknown" if mutation else "not_sent") from exc
        if not isinstance(value, dict) and not (not mutation and isinstance(value, list)):
            raise AdapterError("unknown_outcome" if mutation else "malformed_response",
                               "unknown" if mutation else "not_sent")
        if isinstance(value, dict) and (value.get("error") or value.get("ok") is False):
            code = "authorization_denied" if "authoriz" in str(value.get("error", "")).lower() else "request_rejected"
            raise AdapterError(code, "rejected")
        return value
    except AdapterError:
        raise
    except (OSError, ValueError, http.client.HTTPException) as exc:
        raise AdapterError("unknown_outcome" if dispatched else "connection_failed",
                           "unknown" if dispatched else "not_sent") from exc
    finally:
        if source:
            source.close()
        connection.close()


def _info(base, ctx, key):
    value = _request(base, ctx, key, "/printer/info")
    if not isinstance(value, dict):
        raise AdapterError("malformed_response")
    if "software" in value and value["software"] != "Repetier-Server":
        raise AdapterError("unsupported_server")
    if (not isinstance(value.get("version"), str) or not value["version"]
            or len(value["version"]) > 64):
        raise AdapterError("malformed_response")
    # Older/rebranded servers have no software field and a user-defined name.
    # Validate their published info schema; this is protocol compatibility,
    # never proof of the remote server's identity.
    if "software" not in value:
        _validate_printers(value.get("printers"))
    return value


def _classic(info):
    version = info.get("version")
    match = re.fullmatch(r"(\d+)\.(\d+)(?:\.\d+)?", version) if isinstance(version, str) else None
    return bool(match and (0, 90) <= tuple(map(int, match.groups())) < (1, 5))


def _printer_list(base, ctx, key, slug=None):
    value = _request(base, ctx, key, _bridge("listPrinter", slug, {}) if slug else "/printer/list")
    items = value if slug else value.get("data") if isinstance(value, dict) else None
    return _validate_printers(items)


def _validate_printers(items):
    if not isinstance(items, list) or len(items) > 1000:
        raise AdapterError("malformed_response")
    seen = set()
    for item in items:
        if (not isinstance(item, dict) or not valid_slug(item.get("slug"))
                or item["slug"] in seen or not isinstance(item.get("name"), str)
                or not 0 < len(item["name"]) <= 256
                or not isinstance(item.get("active"), bool)
                or type(item.get("online")) not in (int, bool) or item["online"] not in (0, 1)):
            raise AdapterError("malformed_response")
        seen.add(item["slug"])
    return items


def list_printers(base, ctx, apikey=""):
    try:
        _info(base, ctx, apikey)
        return {"ok": True, "printers": [{k: p[k] for k in ("slug", "name", "active", "online")}
                                          for p in _printer_list(base, ctx, apikey)]}
    except AdapterError as exc:
        return {"ok": False, "error_code": exc.code}


def _bridge(command, slug, data):
    return "/printer/api/" + urllib.parse.quote(slug, safe="") + "?" + urllib.parse.urlencode(
        {"a": command, "data": json.dumps(data, separators=(",", ":"))})


def _number(value):
    return value if type(value) in (int, float) and math.isfinite(value) else None


def poll(base, ctx, apikey="", *, slug=""):
    status = {"online": False, "state": "unknown", "job_state": "unknown", "nozzle": None,
              "target_nozzle": None, "bed": None, "target_bed": None, "progress": None,
              "filename": "", "thumbnail": "", "upload_ready": False, "can_start": False,
              "can_control": False, "repetier_slug": slug, "repetier_url": base}
    try:
        if not valid_slug(slug):
            raise AdapterError("printer_selection_required")
        info = _info(base, ctx, apikey)
        items = _printer_list(base, ctx, apikey, slug)
        matches = [p for p in items if p["slug"] == slug]
        if len(matches) != 1:
            raise AdapterError("selected_printer_missing")
        item = matches[0]
        status["online"] = item["active"] and bool(item["online"])
        if not status["online"]:
            status["state"] = "offline"
            return status
        states = _request(base, ctx, apikey, _bridge("stateList", slug, {"includeHistory": False}))
        state = states.get(slug) if isinstance(states, dict) else None
        if not isinstance(state, dict):
            raise AdapterError("malformed_response")
        extruders = state.get("extruder")
        active = state.get("activeExtruder")
        if isinstance(extruders, list) and type(active) is int and 0 <= active < len(extruders):
            heater = extruders[active]
            if isinstance(heater, dict):
                status.update(nozzle=_number(heater.get("tempRead")), target_nozzle=_number(heater.get("tempSet")))
        beds = state.get("heatedBeds")
        if isinstance(beds, list) and beds and isinstance(beds[0], dict):
            status.update(bed=_number(beds[0].get("tempRead")), target_bed=_number(beds[0].get("tempSet")))
        elif isinstance(state.get("heatedBed"), dict):
            status.update(bed=_number(state["heatedBed"].get("tempRead")),
                          target_bed=_number(state["heatedBed"].get("tempSet")))
        job, paused, pause_state = item.get("job"), item.get("paused"), item.get("pauseState")
        known = isinstance(job, str) and isinstance(paused, bool) and type(pause_state) is int
        idle = (known and job == "none" and paused is False and pause_state == 0
                and ("jobid" not in item or type(item["jobid"]) is int and item["jobid"] == 0))
        running = known and bool(job) and job != "none" and type(item.get("jobid")) is int and item["jobid"] > 0
        if idle:
            status.update(state="idle", job_state="idle")
        elif running and ((paused and pause_state > 0) or (not paused and pause_state == 0)):
            status.update(state="paused" if paused else "printing", job_state="paused" if paused else "printing",
                          filename=job[:512], progress=_number(item.get("done")))
        ready = type(state.get("condition")) is int and state["condition"] == 1
        writable = _classic(info) and ready
        status.update(upload_ready=writable and idle, can_start=writable and idle,
                      can_control=writable and status["job_state"] in ("printing", "paused"))
        if not _classic(info):
            status["upload_reason"] = status["control_reason"] = "repetierModeUnsupported"
        elif not ready:
            status["upload_reason"] = status["control_reason"] = "repetierNotReady"
        return status
    except AdapterError as exc:
        status.update(online=False, upload_ready=False, can_start=False, can_control=False,
                      error_code=exc.code)
        return status


def upload(base, ctx, apikey, file_path, filename, *, slug="", start=False, is_current=lambda: True):
    try:
        live = poll(base, ctx, apikey, slug=slug)
        if not live.get("upload_ready") or (start and not live.get("can_start")):
            raise AdapterError(live.get("error_code") or "not_ready")
        fields = {"a": "upload"}
        if start:
            fields.update(name=filename, autostart="true")
        path = "/printer/%s/%s" % ("job" if start else "model", urllib.parse.quote(slug, safe=""))
        _request(base, ctx, apikey, path, mutation=True, file_path=file_path,
                 filename=filename, fields=fields, is_current=is_current)
        return {"ok": True, "verified": False, "evidence_ref": "repetier:http_accepted"}
    except AdapterError as exc:
        return {"ok": False, "error_code": exc.code, "outcome": exc.outcome}


def control(base, ctx, apikey, action, *, slug="", is_current=lambda: True):
    commands = {"pause": ("send", {"cmd": "@pause User requested pause."}),
                "resume": ("continueJob", {}), "cancel": ("stopJob", {})}
    try:
        if action not in commands:
            raise AdapterError("unknown_action")
        live = poll(base, ctx, apikey, slug=slug)
        if (not live["can_control"] or live["job_state"] not in
                ({"printing"} if action == "pause" else {"paused"} if action == "resume" else {"printing", "paused"})):
            raise AdapterError(live.get("error_code") or "not_ready")
        command, data = commands[action]
        _request(base, ctx, apikey, _bridge(command, slug, data), mutation=True, is_current=is_current)
        return {"ok": True, "verified": False, "evidence_ref": "repetier:http_accepted"}
    except AdapterError as exc:
        return {"ok": False, "error_code": exc.code, "outcome": exc.outcome}
