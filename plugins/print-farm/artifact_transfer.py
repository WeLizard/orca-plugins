"""Bounded, stateful receiver for artifact chunks sent by the plugin WebView.

The caller supplies a Python-owned storage directory and a stable owner key
(for example, the identity of the open dashboard window). Browser messages
must never supply filesystem paths. The module intentionally uses only the
standard library so its reviewed definitions can be embedded by the plugin
packager.
"""

import hashlib
import hmac
import os
import re
import secrets
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path


MAX_CHUNK_BYTES = 32 * 1024
DEFAULT_MAX_ARTIFACT_BYTES = 512 * 1024 * 1024
DEFAULT_STORAGE_QUOTA_BYTES = 2 * 1024 * 1024 * 1024
DEFAULT_IDLE_TIMEOUT_SECONDS = 15 * 60
_SHA256_RE = re.compile(r"[0-9a-fA-F]{64}\Z")
_ALLOWED_SUFFIXES = (".gcode", ".gco", ".3mf")


class ArtifactTransferError(ValueError):
    """A transfer is invalid, incomplete, expired, or over its configured bound."""


@dataclass(frozen=True)
class ReceivedArtifact:
    """A complete artifact atomically installed in the approved storage directory."""

    path: Path
    filename: str
    size_bytes: int
    sha256: str


@dataclass
class _Transfer:
    owner: str
    token: str
    filename: str
    expected_size: int
    expected_sha256: str
    temp_path: Path
    stream: object
    digest: object
    received_size: int
    next_sequence: int
    touched_at: float


def _valid_digest(value):
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


def _safe_filename(value):
    if not isinstance(value, str) or not value or len(value) > 255:
        raise ArtifactTransferError("invalid artifact filename")
    # The WebView may send a display name only, never a path.
    if "/" in value or "\\" in value:
        raise ArtifactTransferError("invalid artifact filename")
    leaf = value
    if leaf in ("", ".", "..") or any(ord(ch) < 32 or ch in ':\0<>|"?*' for ch in leaf):
        raise ArtifactTransferError("invalid artifact filename")
    if not leaf.lower().endswith(_ALLOWED_SUFFIXES):
        raise ArtifactTransferError("unsupported artifact type")
    return leaf


class ArtifactTransferManager:
    """Receive bounded, ordered chunks into private files in one storage root.

    One transfer can be active for each owner key. The manager itself is safe
    to call from multiple threads. The owner key must be an application-owned
    string, never a value used directly as a path.
    """

    def __init__(self, storage_dir, max_artifact_bytes=DEFAULT_MAX_ARTIFACT_BYTES,
                 storage_quota_bytes=DEFAULT_STORAGE_QUOTA_BYTES,
                 idle_timeout_seconds=DEFAULT_IDLE_TIMEOUT_SECONDS,
                 clock=None):
        if type(max_artifact_bytes) is not int or max_artifact_bytes < 1:
            raise ValueError("max_artifact_bytes must be a positive integer")
        if type(storage_quota_bytes) is not int or storage_quota_bytes < 1:
            raise ValueError("storage_quota_bytes must be a positive integer")
        if (type(idle_timeout_seconds) not in (int, float) or
                idle_timeout_seconds <= 0):
            raise ValueError("idle_timeout_seconds must be positive")
        self.storage_dir = Path(storage_dir).expanduser().resolve()
        self.max_artifact_bytes = max_artifact_bytes
        self.storage_quota_bytes = storage_quota_bytes
        self.idle_timeout_seconds = float(idle_timeout_seconds)
        self._clock = clock or time.monotonic
        self._lock = threading.RLock()
        self._by_owner = {}

    def begin(self, owner, filename, total_bytes, sha256=None):
        """Reserve one transfer and return its unguessable, server-made token."""
        self._validate_owner(owner)
        safe_name = _safe_filename(filename)
        if type(total_bytes) is not int or not 1 <= total_bytes <= self.max_artifact_bytes:
            raise ArtifactTransferError("artifact size is outside the allowed range")
        if sha256 is not None and not _valid_digest(sha256):
            raise ArtifactTransferError("invalid artifact SHA-256")
        expected_digest = sha256.lower() if sha256 is not None else None

        with self._lock:
            self._expire_locked()
            if owner in self._by_owner:
                raise ArtifactTransferError("this owner already has an active transfer")
            self._ensure_storage()
            reserved = sum(item.expected_size for item in self._by_owner.values())
            current = self._storage_bytes()
            if current + reserved + total_bytes > self.storage_quota_bytes:
                raise ArtifactTransferError("artifact storage quota exceeded")

            token = secrets.token_urlsafe(32)
            temp_path = self.storage_dir / (".pf-transfer-" + secrets.token_hex(16) + ".part")
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_BINARY"):
                flags |= os.O_BINARY
            fd = os.open(str(temp_path), flags, 0o600)
            try:
                stream = os.fdopen(fd, "wb")
            except BaseException:
                os.close(fd)
                self._unlink(temp_path)
                raise
            self._by_owner[owner] = _Transfer(
                owner=owner,
                token=token,
                filename=safe_name,
                expected_size=total_bytes,
                expected_sha256=expected_digest,
                temp_path=temp_path,
                stream=stream,
                digest=hashlib.sha256(),
                received_size=0,
                next_sequence=0,
                touched_at=self._clock(),
            )
            return token

    def append(self, owner, token, sequence, chunk, chunk_sha256=None):
        """Persist one raw chunk after validating ownership, order, size and hash."""
        with self._lock:
            transfer = self._get_transfer(owner, token)
            if type(sequence) is not int or sequence != transfer.next_sequence:
                raise ArtifactTransferError("unexpected artifact chunk sequence")
            if not isinstance(chunk, bytes) or not 1 <= len(chunk) <= MAX_CHUNK_BYTES:
                raise ArtifactTransferError("artifact chunk must be 1..32 KiB of raw bytes")
            if chunk_sha256 is not None and not _valid_digest(chunk_sha256):
                raise ArtifactTransferError("invalid chunk SHA-256")
            if chunk_sha256 is not None:
                actual_chunk_digest = hashlib.sha256(chunk).hexdigest()
                if not hmac.compare_digest(actual_chunk_digest, chunk_sha256.lower()):
                    raise ArtifactTransferError("artifact chunk SHA-256 mismatch")
            if transfer.received_size + len(chunk) > transfer.expected_size:
                raise ArtifactTransferError("artifact exceeds declared size")
            try:
                transfer.stream.write(chunk)
            except OSError as exc:
                self._discard_locked(transfer)
                raise ArtifactTransferError("could not write artifact chunk") from exc
            transfer.digest.update(chunk)
            transfer.received_size += len(chunk)
            transfer.next_sequence += 1
            transfer.touched_at = self._clock()
            return transfer.received_size

    def finish(self, owner, token):
        """Verify the whole artifact, fsync it, and publish a generated filename."""
        with self._lock:
            transfer = self._get_transfer(owner, token)
            if transfer.received_size != transfer.expected_size:
                raise ArtifactTransferError("artifact transfer is incomplete")
            actual_sha256 = transfer.digest.hexdigest()
            if (transfer.expected_sha256 is not None and
                    not hmac.compare_digest(actual_sha256, transfer.expected_sha256)):
                self._discard_locked(transfer)
                raise ArtifactTransferError("artifact SHA-256 mismatch")

            final_path = self.storage_dir / (
                "pf-artifact-" + secrets.token_hex(16) + self._extension(transfer.filename)
            )
            try:
                transfer.stream.flush()
                os.fsync(transfer.stream.fileno())
                transfer.stream.close()
                # Reserve the generated destination exclusively before replacing
                # the placeholder with the complete same-directory file.
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                if hasattr(os, "O_BINARY"):
                    flags |= os.O_BINARY
                fd = os.open(str(final_path), flags, 0o600)
                os.close(fd)
                os.replace(str(transfer.temp_path), str(final_path))
                self._fsync_directory()
            except OSError as exc:
                self._unlink(final_path)
                self._discard_locked(transfer)
                raise ArtifactTransferError("could not finalize artifact") from exc

            del self._by_owner[owner]
            return ReceivedArtifact(
                path=final_path,
                filename=transfer.filename,
                size_bytes=transfer.received_size,
                sha256=actual_sha256,
            )

    def cancel(self, owner, token):
        """Cancel the matching owner's transfer and remove its incomplete file."""
        with self._lock:
            transfer = self._get_transfer(owner, token)
            self._discard_locked(transfer)

    def expire(self):
        """Remove and count all incomplete transfers past the idle timeout."""
        with self._lock:
            return self._expire_locked()

    def active_owners(self):
        """Return a snapshot of active owner keys for diagnostics and scheduling."""
        with self._lock:
            self._expire_locked()
            return tuple(self._by_owner)

    @staticmethod
    def _validate_owner(owner):
        if not isinstance(owner, str) or not owner or len(owner) > 256:
            raise ArtifactTransferError("invalid transfer owner")

    def _get_transfer(self, owner, token):
        self._validate_owner(owner)
        if not isinstance(token, str):
            raise ArtifactTransferError("invalid artifact transfer token")
        self._expire_locked()
        transfer = self._by_owner.get(owner)
        if transfer is None or not hmac.compare_digest(transfer.token, token):
            raise ArtifactTransferError("artifact transfer is unavailable")
        return transfer

    def _ensure_storage(self):
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        info = self.storage_dir.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise ArtifactTransferError("artifact storage directory is unsafe")

    def _storage_bytes(self):
        total = 0
        active = {item.temp_path.name for item in self._by_owner.values()}
        try:
            with os.scandir(self.storage_dir) as entries:
                for entry in entries:
                    if entry.name in active:
                        continue
                    try:
                        if entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                            if total > self.storage_quota_bytes:
                                return total
                    except OSError as exc:
                        raise ArtifactTransferError("could not inspect artifact storage quota") from exc
        except OSError as exc:
            raise ArtifactTransferError("could not inspect artifact storage quota") from exc
        return total

    def _expire_locked(self):
        now = self._clock()
        expired = [item for item in self._by_owner.values()
                   if now - item.touched_at >= self.idle_timeout_seconds]
        for transfer in expired:
            self._discard_locked(transfer)
        return len(expired)

    def _discard_locked(self, transfer):
        try:
            if not transfer.stream.closed:
                transfer.stream.close()
        finally:
            self._unlink(transfer.temp_path)
            if self._by_owner.get(transfer.owner) is transfer:
                del self._by_owner[transfer.owner]

    @staticmethod
    def _unlink(path):
        try:
            Path(path).unlink()
        except FileNotFoundError:
            pass
        except OSError:
            # Cleanup is best effort; the generated dot-part name is never
            # exposed to the WebView and a later quota check still accounts for it.
            pass

    @staticmethod
    def _extension(filename):
        lower = filename.lower()
        if lower.endswith(".gcode"):
            return ".gcode"
        if lower.endswith(".gco"):
            return ".gco"
        return ".3mf"

    def _fsync_directory(self):
        if os.name == "nt":
            return
        flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        try:
            fd = os.open(str(self.storage_dir), flags)
        except OSError:
            return
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
