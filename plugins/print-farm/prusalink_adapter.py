"""PrusaLink upload-only adapter using the pinned public OpenAPI contract.

This adapter deliberately does not implement Digest authentication or print
control. API-key authentication is sent only when explicitly advertised in the
server's /api/version capabilities.
"""

from __future__ import annotations

import http.client
import json
import math
import os
import re
import ssl
import urllib.parse
from pathlib import Path

_TIMEOUT = 6.0
_MAX_RESPONSE = 2 * 1024 * 1024
_CHUNK = 64 * 1024
_API_KEY_CAPABILITIES = ("x-api-key", "api-key")


class _AdapterError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _base_parts(base: str):
    if not isinstance(base, str) or not base.strip():
        raise _AdapterError("invalid_address")
    parsed = urllib.parse.urlsplit(base.strip())
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
        raise _AdapterError("invalid_address")
    if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
        raise _AdapterError("invalid_address")
    try:
        port = parsed.port
    except ValueError as exc:
        raise _AdapterError("invalid_address") from exc
    prefix = parsed.path.rstrip("/")
    return parsed.scheme.lower(), parsed.hostname, port, prefix


def _request(base, method, path, *, ssl_context=None, headers=None, body=None):
    scheme, host, port, prefix = _base_parts(base)
    connection_cls = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
    options = {"timeout": _TIMEOUT}
    if scheme == "https":
        options["context"] = ssl_context or ssl.create_default_context()
    conn = connection_cls(host, port=port, **options)
    target = prefix + path
    try:
        conn.putrequest(method, target, skip_accept_encoding=True)
        conn.putheader("Accept", "application/json")
        conn.putheader("Connection", "close")
        for name, value in (headers or {}).items():
            conn.putheader(name, value)
        if body is not None:
            conn.putheader("Content-Length", str(body[0]))
            conn.putheader("Content-Type", "application/octet-stream")
        conn.endheaders()
        if body is not None:
            size, file_obj = body
            remaining = size
            while remaining:
                chunk = file_obj.read(min(_CHUNK, remaining))
                if not chunk:
                    raise _AdapterError("source_changed")
                conn.send(chunk)
                remaining -= len(chunk)
        response = conn.getresponse()
        data = response.read(_MAX_RESPONSE + 1)
        if len(data) > _MAX_RESPONSE:
            raise _AdapterError("response_too_large")
        return response.status, dict(response.getheaders()), data
    except _AdapterError:
        raise
    except (TimeoutError, OSError, http.client.HTTPException, ssl.SSLError) as exc:
        if isinstance(exc, (TimeoutError,)) or "timed out" in str(exc).lower():
            raise _AdapterError("timeout") from exc
        raise _AdapterError("connection_failed") from exc
    finally:
        conn.close()


def _json_request(base, path, *, ssl_context=None, headers=None, allow_no_content=False):
    status, response_headers, payload = _request(
        base, "GET", path, ssl_context=ssl_context, headers=headers
    )
    if status == 401:
        raise _AdapterError("unauthorized")
    if status == 204 and allow_no_content:
        return None
    if status != 200:
        raise _AdapterError("http_%d" % status)
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _AdapterError("malformed_response") from exc
    if not isinstance(value, dict):
        raise _AdapterError("malformed_response")
    return value


def _api_headers(apikey, version):
    if not apikey:
        return {}
    capabilities = version.get("capabilities")
    if not isinstance(capabilities, dict) or not any(
        capabilities.get(key) is True for key in _API_KEY_CAPABILITIES
    ):
        raise _AdapterError("api_key_unsupported")
    if not isinstance(apikey, str) or any(ch in apikey for ch in "\r\n"):
        raise _AdapterError("invalid_api_key")
    return {"X-Api-Key": apikey}


def _version(base, ssl_context):
    value = _json_request(base, "/api/version", ssl_context=ssl_context)
    if not isinstance(value.get("api"), str) or not value["api"]:
        raise _AdapterError("malformed_response")
    return value


def _empty_status(state="offline", error=None):
    result = {
        "online": False,
        "state": state,
        "job_state": "",
        "nozzle": None,
        "target_nozzle": None,
        "bed": None,
        "target_bed": None,
        "progress": None,
        "filename": "",
        "thumbnail": "",
        "upload_ready": False,
        "can_control": False,
    }
    if error:
        result["error_code"] = error
    return result


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return value


def poll(base, ssl_context, apikey=""):
    """Read PrusaLink telemetry; no mutating requests are sent."""
    try:
        version = _version(base, ssl_context)
        headers = _api_headers(apikey, version)
        telemetry = _json_request(
            base, "/api/v1/status", ssl_context=ssl_context, headers=headers
        )
        printer = telemetry.get("printer")
        job = telemetry.get("job")
        transfer = telemetry.get("transfer")
        if not isinstance(printer, dict) or not isinstance(printer.get("state"), str):
            raise _AdapterError("malformed_response")
        if job is not None and not isinstance(job, dict):
            raise _AdapterError("malformed_response")
        if transfer is not None and not isinstance(transfer, dict):
            raise _AdapterError("malformed_response")
        state = printer["state"].upper()
        job_state = (job or {}).get("state", "")
        if not isinstance(job_state, str):
            raise _AdapterError("malformed_response")
        filename = ""
        try:
            detail = _json_request(
                base, "/api/v1/job", ssl_context=ssl_context, headers=headers,
                allow_no_content=True,
            )
            if isinstance(detail, dict):
                file_info = detail.get("file")
                if isinstance(file_info, dict):
                    candidate = file_info.get("display_name") or file_info.get("name")
                    if isinstance(candidate, str):
                        filename = candidate
                if not job_state and isinstance(detail.get("state"), str):
                    job_state = detail["state"]
        except _AdapterError as exc:
            # A 204 means no current job; optional job details may be absent.
            if exc.code not in ("http_204",):
                raise
        busy = state in {"BUSY", "PRINTING", "PAUSED", "ATTENTION"} or bool(
            transfer and transfer.get("type") not in (None, "NO_TRANSFER")
        )
        capability = version.get("capabilities")
        upload_ready = (
            isinstance(capability, dict)
            and capability.get("upload-by-put") is True
            and not busy
        )
        progress = _number((job or {}).get("progress"))
        if progress is not None:
            progress = max(0, min(100, progress))
        return {
            "online": True,
            "state": state.lower(),
            "job_state": job_state.lower(),
            "nozzle": _number(printer.get("temp_nozzle")),
            "target_nozzle": _number(printer.get("target_nozzle")),
            "bed": _number(printer.get("temp_bed")),
            "target_bed": _number(printer.get("target_bed")),
            "progress": progress,
            "filename": filename,
            "thumbnail": "",
            "upload_ready": upload_ready,
            "can_control": False,
        }
    except _AdapterError as exc:
        if exc.code == "unauthorized":
            return _empty_status(error="auth_required")
        if exc.code == "timeout":
            return _empty_status(error="timeout")
        return _empty_status(error=exc.code)


def _storage_list(base, ssl_context, headers):
    value = _json_request(
        base, "/api/v1/storage", ssl_context=ssl_context, headers=headers
    )
    items = value.get("storage_list")
    if not isinstance(items, list):
        raise _AdapterError("malformed_response")
    for item in items:
        if not isinstance(item, dict):
            raise _AdapterError("malformed_response")
        if not isinstance(item.get("path"), str) or not item["path"].startswith("/"):
            raise _AdapterError("malformed_response")
        if not isinstance(item.get("available"), bool):
            raise _AdapterError("malformed_response")
    return items


def _safe_filename(filename):
    if not isinstance(filename, str) or not filename or filename in (".", ".."):
        return False
    return (
        "/" not in filename and "\\" not in filename
        and not any(ord(char) < 32 or ord(char) == 127 for char in filename)
    )


def upload(base, ssl_context, apikey, file_path, filename):
    """Upload a file to a discovered writable storage without starting a print.

    Ambiguous network failures return ``outcome_unknown`` and are never retried.
    """
    try:
        if not _safe_filename(filename):
            raise _AdapterError("invalid_filename")
        path = Path(file_path)
        if not path.is_file():
            raise _AdapterError("source_unavailable")
        size = path.stat().st_size
        version = _version(base, ssl_context)
        headers = _api_headers(apikey, version)
        capabilities = version.get("capabilities")
        if not isinstance(capabilities, dict) or capabilities.get("upload-by-put") is not True:
            raise _AdapterError("upload_capability_missing")
        telemetry = _json_request(
            base, "/api/v1/status", ssl_context=ssl_context, headers=headers
        )
        printer = telemetry.get("printer")
        transfer = telemetry.get("transfer")
        if not isinstance(printer, dict) or not isinstance(printer.get("state"), str):
            raise _AdapterError("malformed_response")
        if transfer is not None and not isinstance(transfer, dict):
            raise _AdapterError("malformed_response")
        if printer["state"].upper() in {"BUSY", "PRINTING", "PAUSED", "ATTENTION"}:
            raise _AdapterError("printer_busy")
        if transfer and transfer.get("type") not in (None, "NO_TRANSFER"):
            raise _AdapterError("transfer_busy")
        storages = _storage_list(base, ssl_context, headers)
        writable = [
            item for item in storages
            if item["available"] is True and item.get("read_only") is False
        ]
        if not writable:
            raise _AdapterError("no_writable_storage")
        storage = next((item for item in writable if item.get("type") == "LOCAL"), writable[0])
        storage_path = storage["path"].strip("/")
        if not storage_path or any(part in (".", "..") for part in storage_path.split("/")):
            raise _AdapterError("malformed_storage_path")
        encoded = "/".join(urllib.parse.quote(part, safe="") for part in storage_path.split("/"))
        encoded_name = urllib.parse.quote(filename, safe="")
        target = "/api/v1/files/%s/%s" % (encoded, encoded_name)
        with path.open("rb") as source:
            try:
                status, _, _ = _request(
                    base, "PUT", target, ssl_context=ssl_context,
                    headers={
                        **headers,
                        "Print-After-Upload": "?0",
                        "Overwrite": "?0",
                    },
                    body=(size, source),
                )
            except _AdapterError as exc:
                if exc.code == "timeout":
                    raise _AdapterError("outcome_unknown") from exc
                raise
        if status == 201:
            return {"ok": True, "evidence_ref": "PUT " + target}
        if status == 401:
            raise _AdapterError("unauthorized")
        if status == 409:
            raise _AdapterError("file_exists_or_conflict")
        if 300 <= status < 400:
            raise _AdapterError("redirect_rejected")
        raise _AdapterError("http_%d" % status)
    except _AdapterError as exc:
        return {"ok": False, "error_code": exc.code}
    except OSError:
        return {"ok": False, "error_code": "source_unavailable"}
