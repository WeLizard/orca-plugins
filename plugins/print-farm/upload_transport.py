"""Bounded, non-redirecting multipart file uploads using the standard library."""

import http.client
import os
import re
import ssl
import urllib.parse
from collections.abc import Mapping
from pathlib import Path


DEFAULT_CHUNK_SIZE = 64 * 1024
MAX_CHUNK_SIZE = 1024 * 1024
_BOUNDARY = "----PrintFarmUpload7MA4YWxkTrZu0gW"
_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")


def post_multipart_file(
    url: str,
    file_path: str | os.PathLike[str],
    *,
    file_field: str = "file",
    filename: str | None = None,
    fields: Mapping[str, object] | None = None,
    headers: Mapping[str, str] | None = None,
    timeout: float = 30.0,
    ssl_context: ssl.SSLContext | None = None,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> int:
    """POST a file as multipart/form-data and return the HTTP status.

    The request body is sent in bounded pieces with an exact Content-Length.
    Redirects are never followed. Only ``http`` and ``https`` URLs without
    embedded credentials are accepted.
    """
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme.lower() not in ("http", "https"):
        raise ValueError("upload URL must use http or https")
    if not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise ValueError("upload URL must have a host and no embedded credentials")
    if parsed.fragment:
        raise ValueError("upload URL must not contain a fragment")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("upload URL has an invalid port") from exc

    if not isinstance(chunk_size, int) or isinstance(chunk_size, bool):
        raise ValueError("chunk_size must be an integer")
    if chunk_size < 1 or chunk_size > MAX_CHUNK_SIZE:
        raise ValueError("chunk_size must be between 1 and %d" % MAX_CHUNK_SIZE)
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        raise ValueError("timeout must be a positive number")

    body_prefix, body_suffix, content_type = _multipart_envelope(
        file_field=file_field,
        filename=filename if filename is not None else Path(file_path).name,
        fields=fields,
    )

    connection_type = (
        http.client.HTTPSConnection
        if parsed.scheme.lower() == "https"
        else http.client.HTTPConnection
    )
    connection_options = {"timeout": timeout}
    if parsed.scheme.lower() == "https":
        connection_options["context"] = ssl_context or ssl.create_default_context()
    connection = connection_type(parsed.hostname, port=port, **connection_options)
    request_target = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))

    request_headers = _validated_headers(headers)
    request_headers["Content-Type"] = content_type
    request_headers.setdefault("Connection", "close")

    try:
        with open(file_path, "rb") as source:
            file_size = os.fstat(source.fileno()).st_size
            content_length = len(body_prefix) + file_size + len(body_suffix)
            request_headers["Content-Length"] = str(content_length)

            connection.putrequest("POST", request_target)
            for name, value in request_headers.items():
                connection.putheader(name, value)
            connection.endheaders()

            connection.send(body_prefix)
            _send_file(connection, source, file_size, chunk_size)
            connection.send(body_suffix)

            response = connection.getresponse()
            return response.status
    finally:
        connection.close()


def _send_file(connection, source, file_size: int, chunk_size: int) -> None:
    remaining = file_size
    while remaining:
        chunk = source.read(min(chunk_size, remaining))
        if not chunk:
            raise OSError("upload file changed or ended before its declared size")
        connection.send(chunk)
        remaining -= len(chunk)


def _multipart_envelope(*, file_field, filename, fields):
    safe_file_field = _quoted_parameter(file_field, "file field")
    safe_filename = _quoted_filename(filename)
    prefix = bytearray()
    for key, value in (fields or {}).items():
        safe_key = _quoted_parameter(key, "field name")
        prefix.extend(("--%s\r\n" % _BOUNDARY).encode("ascii"))
        prefix.extend(
            ("Content-Disposition: form-data; name=\"%s\"\r\n\r\n" % safe_key).encode(
                "ascii"
            )
        )
        prefix.extend(str(value).encode("utf-8"))
        prefix.extend(b"\r\n")

    prefix.extend(("--%s\r\n" % _BOUNDARY).encode("ascii"))
    prefix.extend(
        (
            "Content-Disposition: form-data; name=\"%s\"; %s\r\n"
            "Content-Type: application/octet-stream\r\n\r\n"
            % (safe_file_field, safe_filename)
        ).encode("ascii")
    )
    suffix = ("\r\n--%s--\r\n" % _BOUNDARY).encode("ascii")
    return bytes(prefix), suffix, "multipart/form-data; boundary=" + _BOUNDARY


def _quoted_parameter(value, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("%s must be a non-empty string" % label)
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("%s must not contain control characters" % label)
    if any(ord(char) > 127 for char in value):
        raise ValueError("%s must contain only ASCII characters" % label)
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _quoted_filename(value) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("filename must be a non-empty string")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("filename must not contain control characters")

    ascii_fallback = "".join(
        char if 32 <= ord(char) < 127 else "_" for char in value
    )
    ascii_fallback = ascii_fallback.replace("\\", "\\\\").replace('"', '\\"')
    encoded = urllib.parse.quote(value.encode("utf-8"), safe="")
    return 'filename="%s"; filename*=UTF-8\'\'%s' % (ascii_fallback, encoded)


def _validated_headers(headers):
    result = {}
    seen = set()
    for name, value in (headers or {}).items():
        if not isinstance(name, str) or not _HEADER_NAME_RE.fullmatch(name):
            raise ValueError("invalid HTTP header name")
        lower_name = name.lower()
        if lower_name in seen:
            raise ValueError("duplicate HTTP header name")
        if lower_name in {"content-length", "transfer-encoding", "content-type", "host"}:
            raise ValueError("header %s is managed by the upload transport" % name)
        if not isinstance(value, str) or "\r" in value or "\n" in value:
            raise ValueError("invalid HTTP header value")
        result[name] = value
        seen.add(lower_name)
    return result
