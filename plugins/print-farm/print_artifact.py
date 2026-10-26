"""Bounded inspection and immutable staging for manually supplied print artifacts.

This module has no Orca/Qt dependencies. Callers provide a Python-owned source
path and an explicit staging directory on the intended filesystem; browser
messages must never supply either path.
"""
import hashlib
import json
import math
import os
import re
import stat
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping


# Open flags that exist on one platform family only (O_BINARY on Windows,
# O_NOFOLLOW on POSIX); 0 where the platform has no such flag.
try:
    _O_BINARY = os.O_BINARY
except AttributeError:
    _O_BINARY = 0
try:
    _O_NOFOLLOW = os.O_NOFOLLOW
except AttributeError:
    _O_NOFOLLOW = 0

DEFAULT_MAX_BYTES = 512 * 1024 * 1024
DEFAULT_CHUNK_BYTES = 192 * 1024
MAX_ZIP_ENTRIES = 20_000
MAX_ZIP_DIRECTORY_BYTES = 16 * 1024 * 1024
MAX_ZIP_UNPACKED_BYTES = 2 * 1024 * 1024 * 1024
MAX_ZIP_MEMBER_BYTES = 512 * 1024 * 1024
MAX_METADATA_MEMBER_BYTES = 8 * 1024 * 1024
MAX_JSON_NESTING = 32
MAX_ZIP_COMPRESSION_RATIO = 200
GCODE_METADATA_SCAN_BYTES = 1024 * 1024
_PROVENANCE = {"synthetic", "orca_export", "manual_import", "native_capture"}
_PLATE_ENTRY = re.compile(r"Metadata/plate_([1-9][0-9]*)\.gcode\Z")
_ARTIFACT_STAGE_NAME = re.compile(
    r"(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.artifact|"
    r"pf-artifact-[0-9a-f]{32}\.(?:gcode|gco|3mf))\Z"
)
_PLATE_STAGE_NAME = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.gcode\Z"
)
_GCODE_EXTENSIONS = {".gcode", ".gco"}


class ArtifactError(ValueError):
    """Input is invalid, unsafe, unsupported, or exceeds a configured bound."""


@dataclass(frozen=True)
class StagedArtifact:
    """Immutable handle for a verified file held in a private staging directory."""

    path: Path
    filename: str
    manifest: Mapping[str, object]
    metadata: Mapping[str, object]
    plate_entries: tuple[str, ...]
    manifest_complete: bool


@dataclass(frozen=True)
class StagedPlate:
    """Verified extracted G-code for one exact 3MF plate member."""

    path: Path
    plate_entry: str
    size_bytes: int
    sha256: str
    source_artifact_id: str


def _validate_limits(max_bytes: int, chunk_bytes: int) -> None:
    if type(max_bytes) is not int or max_bytes < 1:
        raise ValueError("max_bytes must be a positive integer")
    if type(chunk_bytes) is not int or not 1 <= chunk_bytes <= 4 * 1024 * 1024:
        raise ValueError("chunk_bytes must be between 1 and 4 MiB")


def _safe_filename(value: str | os.PathLike[str]) -> str:
    name = Path(os.fspath(value)).name
    if not name or name in {".", ".."} or any(ord(ch) < 32 or ch in '\\/:\0' for ch in name):
        raise ArtifactError("invalid artifact filename")
    lower = name.lower()
    if lower.endswith(".gcode.3mf") or lower.endswith(".3mf"):
        return name
    if Path(name).suffix.lower() in _GCODE_EXTENSIONS:
        return name
    raise ArtifactError("unsupported artifact type")


def _artifact_kind(filename: str) -> str:
    lower = filename.lower()
    if lower.endswith(".gcode.3mf") or lower.endswith(".3mf"):
        return "3mf"
    return "gcode"


def _hash_file(path: Path, chunk_bytes: int, max_bytes: int) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while True:
            block = stream.read(chunk_bytes)
            if not block:
                break
            size += len(block)
            if size > max_bytes:
                raise ArtifactError("artifact exceeds size limit")
            digest.update(block)
    return size, digest.hexdigest()


def _parse_gcode_metadata(path: Path, size: int) -> dict[str, str]:
    metadata: dict[str, str] = {}
    try:
        with path.open("rb") as stream:
            stream.seek(max(0, size - GCODE_METADATA_SCAN_BYTES))
            tail = stream.read(GCODE_METADATA_SCAN_BYTES)
    except OSError as exc:
        raise ArtifactError("could not read staged G-code metadata") from exc
    for raw_line in tail.splitlines():
        if not raw_line.startswith(b";") or b"=" not in raw_line:
            continue
        key, value = raw_line[1:].decode("utf-8", "replace").split("=", 1)
        key = key.strip()
        if key in {"printer_settings_id", "printer_model", "filament_type", "nozzle_diameter", "layer_height"}:
            value = value.strip().strip('"')
            if value and len(value) <= 512:
                metadata[key] = value
    return metadata


def _gcode_parameters_digest(metadata: Mapping[str, str]) -> str | None:
    if not metadata:
        return None
    canonical = json.dumps(dict(metadata), ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def manifest_is_complete(manifest: Mapping[str, object]) -> bool:
    """Check that the normalized v2 manifest has proven required fields."""
    required = {
        "schema_version", "provenance", "artifact_id", "sha256", "size_bytes",
        "kind", "slicer_build", "selected_plate_entry", "compatible_target_ids",
        "compatibility_evidence_refs", "parameters_sha256",
    }
    if not isinstance(manifest, Mapping) or not required.issubset(manifest):
        return False
    digest = manifest.get("sha256")
    parameter_digest = manifest.get("parameters_sha256")
    size = manifest.get("size_bytes")
    if (manifest.get("schema_version") != 2
            or manifest.get("provenance") not in _PROVENANCE
            or not isinstance(manifest.get("artifact_id"), str)
            or not manifest.get("artifact_id")
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or type(size) is not int or size < 1
            or not isinstance(parameter_digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", parameter_digest) is None
            or not isinstance(manifest.get("compatible_target_ids"), (list, tuple))
            or not isinstance(manifest.get("compatibility_evidence_refs"), (list, tuple))):
        return False
    kind = manifest.get("kind")
    if kind not in {"gcode", "bgcode", "sliced_3mf"}:
        return False
    if kind == "sliced_3mf":
        return isinstance(manifest.get("selected_plate_entry"), str) and bool(
            manifest.get("selected_plate_entry")
        )
    return manifest.get("selected_plate_entry") is None


def _safe_zip_member(name: str) -> bool:
    if (not name or len(name.encode("utf-8", "surrogatepass")) > 1024
            or "\\" in name or "\0" in name or name.startswith("/")):
        return False
    if re.match(r"^[A-Za-z]:", name):
        return False
    parts = name.rstrip("/").split("/")
    return all(part not in {"", ".", ".."} for part in parts)


def _read_zip_member(archive: zipfile.ZipFile, info: zipfile.ZipInfo, limit: int,
                     chunk_bytes: int) -> bytes:
    if info.file_size > limit:
        raise ArtifactError("archive member exceeds size limit")
    pieces = []
    observed = 0
    with archive.open(info, "r") as member:
        while True:
            block = member.read(min(chunk_bytes, limit - observed + 1))
            if not block:
                break
            observed += len(block)
            if observed > limit:
                raise ArtifactError("archive member exceeds size limit")
            pieces.append(block)
    if observed != info.file_size:
        raise ArtifactError("archive member size mismatch")
    return b"".join(pieces)


def _bounded_json_loads(raw: bytes, *, object_pairs_hook=None):
    """Reject deeply nested JSON before decoding and normalize parser failures."""
    depth = 0
    in_string = False
    escaped = False
    for byte in raw:
        if in_string:
            if escaped:
                escaped = False
            elif byte == 0x5C:
                escaped = True
            elif byte == 0x22:
                in_string = False
            continue
        if byte == 0x22:
            in_string = True
        elif byte in (0x7B, 0x5B):
            depth += 1
            if depth > MAX_JSON_NESTING:
                raise ArtifactError("JSON nesting exceeds supported limit")
        elif byte in (0x7D, 0x5D):
            depth = max(0, depth - 1)
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=object_pairs_hook)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise ArtifactError("invalid bounded JSON metadata") from exc


def _parameter_fingerprint(archive: zipfile.ZipFile, by_name: Mapping[str, zipfile.ZipInfo],
                           chunk_bytes: int) -> str | None:
    """Fingerprint bounded parameter configs without implying semantic equivalence.

    JSON is canonicalized after duplicate-key validation. XML is fingerprinted
    byte-for-byte with filename and format tags; it is never parsed or entity-expanded.
    """
    exact_names = {
        "Metadata/project_settings.config",
        "Metadata/print_profile.config",
        "Metadata/model_settings.config",
    }
    prefixes = ("Metadata/machine_settings_", "Metadata/process_settings_",
                "Metadata/filament_settings_", "Metadata/print_setting_")
    names = sorted(name for name in by_name if name in exact_names or (
        name.startswith(prefixes) and name.endswith(".config")
    ))
    if not names:
        return None
    digest = hashlib.sha256()
    for name in names:
        raw = _read_zip_member(archive, by_name[name], MAX_METADATA_MEMBER_BYTES, chunk_bytes)
        try:
            parsed = _bounded_json_loads(raw, object_pairs_hook=_unique_pairs)
        except ArtifactError as exc:
            if "nesting" in str(exc):
                raise ArtifactError("parameter metadata JSON nesting exceeds limit") from exc
            if raw.lstrip().startswith(b"<"):
                format_tag = b"xml-raw-v1"
                parameter_bytes = raw
            else:
                raise ArtifactError("invalid parameter metadata") from exc
        else:
            format_tag = b"json-canonical-v1"
            parameter_bytes = json.dumps(
                parsed, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"), allow_nan=False,
            ).encode("utf-8")
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(format_tag)
        digest.update(b"\0")
        digest.update(parameter_bytes)
        digest.update(b"\0")
    return digest.hexdigest()


def _sanitize_project_metadata(value: object, *, allow_list: bool = False) -> str | list[str] | None:
    """Keep only small, printable scalar values from known project fields."""
    def clean_scalar(item: object) -> str | None:
        if isinstance(item, str):
            result = item.strip()
        elif isinstance(item, (int, float)) and not isinstance(item, bool):
            if isinstance(item, float) and not math.isfinite(item):
                return None
            result = str(item)
        else:
            return None
        if not result or len(result) > 256 or any(ord(char) < 32 or ord(char) == 127 for char in result):
            return None
        return result

    if allow_list and isinstance(value, list):
        if not 1 <= len(value) <= 16:
            return None
        items = [clean_scalar(item) for item in value]
        if any(item is None for item in items):
            return None
        return [item for item in items if item is not None]
    return clean_scalar(value)


def _project_metadata(archive: zipfile.ZipFile, by_name: Mapping[str, zipfile.ZipInfo],
                      chunk_bytes: int) -> dict[str, str | list[str]]:
    """Extract bounded allowlisted project fields from Orca's config JSON."""
    name = "Metadata/project_settings.config"
    info = by_name.get(name)
    if info is None:
        return {}
    raw = _read_zip_member(archive, info, MAX_METADATA_MEMBER_BYTES, chunk_bytes)
    try:
        parsed = _bounded_json_loads(raw, object_pairs_hook=_unique_pairs)
    except ArtifactError as exc:
        if "nesting" in str(exc):
            raise ArtifactError("project metadata JSON nesting exceeds limit") from exc
        if raw.lstrip().startswith(b"<"):
            # This member is outside the supported project-settings JSON contract.
            return {}
        raise ArtifactError("invalid project metadata") from exc
    if not isinstance(parsed, dict):
        return {}
    result: dict[str, str | list[str]] = {}
    for key in ("printer_settings_id", "printer_model", "nozzle_diameter"):
        if key not in parsed:
            continue
        value = _sanitize_project_metadata(parsed[key], allow_list=(key == "nozzle_diameter"))
        if value is not None:
            result[key] = value
    return result


def _slicer_build_metadata(raw: bytes) -> str | None:
    """Read the build label from JSON or bounded Orca/Bambu XML header data."""
    try:
        parsed = _bounded_json_loads(raw, object_pairs_hook=_unique_pairs)
    except ArtifactError:
        # Orca/Bambu exports use XML for slice_info.config. Avoid a general XML
        # parser here; extracting one bounded, quoted header attribute cannot
        # resolve entities or expand document-defined content.
        text = raw.decode("utf-8", "replace")
        match = re.search(
            r'<header_item\b(?=[^>]*\bkey=["\']OrcaSlicer-Version["\'])'
            r'(?=[^>]*\bvalue=["\']([^"\']{1,256})["\'])[^>]*/?>',
            text,
        )
        return match.group(1) if match else None
    if isinstance(parsed, dict):
        candidate = parsed.get("OrcaSlicer-Version") or parsed.get("slicer_version")
        if isinstance(candidate, str) and len(candidate) <= 256:
            return candidate
    return None


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _validate_zip_directory(path: Path) -> None:
    """Reject oversized/multidisk/ZIP64 directories before ZipFile allocates entries."""
    try:
        size = path.stat().st_size
        tail_size = min(size, 22 + 65_535)
        with path.open("rb") as stream:
            stream.seek(size - tail_size)
            tail = stream.read(tail_size)
    except OSError as exc:
        raise ArtifactError("could not read archive directory") from exc
    signature = b"PK\x05\x06"
    offset = tail.rfind(signature)
    if offset < 0 or offset + 22 > len(tail):
        raise ArtifactError("invalid archive directory")
    disk_number = int.from_bytes(tail[offset + 4:offset + 6], "little")
    directory_disk = int.from_bytes(tail[offset + 6:offset + 8], "little")
    entries_on_disk = int.from_bytes(tail[offset + 8:offset + 10], "little")
    entries_total = int.from_bytes(tail[offset + 10:offset + 12], "little")
    directory_size = int.from_bytes(tail[offset + 12:offset + 16], "little")
    directory_offset = int.from_bytes(tail[offset + 16:offset + 20], "little")
    comment_length = int.from_bytes(tail[offset + 20:offset + 22], "little")
    absolute_eocd = size - tail_size + offset
    if (offset + 22 + comment_length != len(tail)
            or disk_number != 0 or directory_disk != 0
            or entries_on_disk != entries_total
            or entries_total in {0xFFFF}
            or directory_size == 0xFFFFFFFF
            or directory_offset == 0xFFFFFFFF
            or entries_total > MAX_ZIP_ENTRIES
            or directory_size > MAX_ZIP_DIRECTORY_BYTES
            or directory_offset + directory_size != absolute_eocd):
        raise ArtifactError("archive directory exceeds supported bounds")


def _inspect_3mf(path: Path, chunk_bytes: int) -> tuple[tuple[str, ...], dict[str, object], str | None]:
    try:
        _validate_zip_directory(path)
        with zipfile.ZipFile(path, "r") as archive:
            infos = archive.infolist()
            if not infos or len(infos) > MAX_ZIP_ENTRIES:
                raise ArtifactError("invalid archive entry count")
            by_name: dict[str, zipfile.ZipInfo] = {}
            folded: set[str] = set()
            total_unpacked = 0
            observed_unpacked = 0
            for info in infos:
                name = info.filename
                if not _safe_zip_member(name):
                    raise ArtifactError("unsafe archive path")
                key = name.casefold()
                if key in folded:
                    raise ArtifactError("duplicate archive member")
                folded.add(key)
                if info.flag_bits & 0x1:
                    raise ArtifactError("encrypted archive member")
                mode = info.external_attr >> 16
                if stat.S_ISLNK(mode):
                    raise ArtifactError("symbolic link in archive")
                if info.is_dir():
                    if info.file_size != 0:
                        raise ArtifactError("invalid archive directory")
                    continue
                if info.file_size > MAX_ZIP_MEMBER_BYTES:
                    raise ArtifactError("archive member exceeds size limit")
                total_unpacked += info.file_size
                if total_unpacked > MAX_ZIP_UNPACKED_BYTES:
                    raise ArtifactError("archive exceeds unpacked size limit")
                if (info.file_size > 1024 and
                        info.file_size > max(1, info.compress_size) * MAX_ZIP_COMPRESSION_RATIO):
                    raise ArtifactError("archive compression ratio exceeds limit")
                # Stream every member so CRCs are verified without extracting
                # entries or retaining decompressed payloads in memory.
                observed_member = 0
                with archive.open(info, "r") as member:
                    while True:
                        block = member.read(chunk_bytes)
                        if not block:
                            break
                        observed_member += len(block)
                        observed_unpacked += len(block)
                        if (observed_member > MAX_ZIP_MEMBER_BYTES
                                or observed_unpacked > MAX_ZIP_UNPACKED_BYTES):
                            raise ArtifactError("archive exceeds unpacked size limit")
                if observed_member != info.file_size:
                    raise ArtifactError("archive member size mismatch")
                by_name[name] = info
            if "3D/3dmodel.model" not in by_name:
                raise ArtifactError("3MF model entry is missing")
            plates = []
            for name, info in by_name.items():
                match = _PLATE_ENTRY.fullmatch(name)
                if match:
                    if info.file_size == 0:
                        raise ArtifactError("empty sliced plate")
                    plates.append((int(match.group(1)), name))
            if not plates:
                raise ArtifactError("unsliced 3MF project")
            plates.sort()
            if len({plate_id for plate_id, _ in plates}) != len(plates):
                raise ArtifactError("duplicate plate id")
            build = None
            if "Metadata/slice_info.config" in by_name:
                raw = _read_zip_member(archive, by_name["Metadata/slice_info.config"],
                                       MAX_METADATA_MEMBER_BYTES, chunk_bytes)
                build = _slicer_build_metadata(raw)
            parameter_digest = _parameter_fingerprint(archive, by_name, chunk_bytes)
            metadata: dict[str, object] = {"slicer_build": build}
            metadata.update(_project_metadata(archive, by_name, chunk_bytes))
            if parameter_digest:
                metadata["parameters_sha256"] = parameter_digest
            return tuple(name for _, name in plates), metadata, parameter_digest
    except ArtifactError:
        raise
    except (OSError, zipfile.BadZipFile, RuntimeError, EOFError, NotImplementedError) as exc:
        raise ArtifactError("invalid 3MF archive") from exc


def _stage_root(path: str | os.PathLike[str]) -> Path:
    root = Path(path).expanduser().absolute()
    if root.exists() and (root.is_symlink() or not root.is_dir()):
        raise ArtifactError("staging root must be a real directory")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.is_symlink():
        raise ArtifactError("staging root must not be a symbolic link")
    root = root.resolve(strict=True)
    try:
        os.chmod(root, 0o700)
    except OSError:
        # Windows chmod does not establish owner-only ACLs; digest checks and
        # exclusive creation remain the integrity controls there.
        pass
    return root


def stage_artifact(source_path: str | os.PathLike[str], staging_dir: str | os.PathLike[str], *,
                   provenance: str = "manual_import", max_bytes: int = DEFAULT_MAX_BYTES,
                   chunk_bytes: int = DEFAULT_CHUNK_BYTES, artifact_id: str | None = None) -> StagedArtifact:
    """Copy, inspect, and digest an artifact using bounded memory.

    `staging_dir` is a Python-supplied account storage path. The returned path
    is opaque and generated in Python; never accept it from JavaScript.
    """
    _validate_limits(max_bytes, chunk_bytes)
    if provenance not in _PROVENANCE:
        raise ArtifactError("invalid artifact provenance")
    filename = _safe_filename(source_path)
    source = Path(source_path)
    try:
        source_info = source.lstat()
    except OSError as exc:
        raise ArtifactError("source file is unavailable") from exc
    if not stat.S_ISREG(source_info.st_mode) or stat.S_ISLNK(source_info.st_mode):
        raise ArtifactError("source must be a regular file")
    if source_info.st_size < 1 or source_info.st_size > max_bytes:
        raise ArtifactError("artifact size is outside configured bounds")
    kind = _artifact_kind(filename)
    identifier = artifact_id or str(uuid.uuid4())
    try:
        identifier = str(uuid.UUID(identifier))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ArtifactError("invalid artifact id") from exc
    root = _stage_root(staging_dir)
    destination = root / (identifier + ".artifact")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= _O_BINARY | _O_NOFOLLOW
    created = False
    digest = hashlib.sha256()
    copied = 0
    try:
        source_flags = os.O_RDONLY | _O_BINARY | _O_NOFOLLOW
        source_fd = os.open(source, source_flags)
        try:
            target_fd = os.open(destination, flags, 0o600)
        except FileExistsError as exc:
            os.close(source_fd)
            raise ArtifactError("artifact id is already staged") from exc
        except BaseException:
            os.close(source_fd)
            raise
        created = True
        with os.fdopen(source_fd, "rb") as source_stream, os.fdopen(target_fd, "wb") as target_stream:
            opened_info = os.fstat(source_stream.fileno())
            if not stat.S_ISREG(opened_info.st_mode) or (opened_info.st_dev, opened_info.st_ino) != (
                source_info.st_dev, source_info.st_ino
            ):
                raise ArtifactError("source changed during staging")
            while True:
                block = source_stream.read(chunk_bytes)
                if not block:
                    break
                copied += len(block)
                if copied > max_bytes:
                    raise ArtifactError("artifact exceeds size limit")
                target_stream.write(block)
                digest.update(block)
            target_stream.flush()
            os.fsync(target_stream.fileno())
            after_info = os.fstat(source_stream.fileno())
            if copied != source_info.st_size or (after_info.st_size, after_info.st_mtime_ns) != (
                opened_info.st_size, opened_info.st_mtime_ns
            ):
                raise ArtifactError("source changed during staging")
            source_stream.seek(0)
            source_size, source_digest = _hash_stream(source_stream, chunk_bytes, max_bytes)
            if source_size != copied or source_digest != digest.hexdigest():
                raise ArtifactError("source changed during staging")
        os.chmod(destination, stat.S_IREAD)
        return inspect_staged_path(
            destination, filename, root, provenance=provenance,
            expected_sha256=digest.hexdigest(), max_bytes=max_bytes,
            chunk_bytes=chunk_bytes, artifact_id=identifier,
        )
    except BaseException:
        if created:
            try:
                os.chmod(destination, stat.S_IWRITE | stat.S_IREAD)
                destination.unlink()
            except OSError:
                pass
        raise


def inspect_staged_path(path: str | os.PathLike[str], filename: str,
                        staging_dir: str | os.PathLike[str], *,
                        provenance: str = "manual_import", expected_sha256: str,
                        max_bytes: int = DEFAULT_MAX_BYTES,
                        chunk_bytes: int = DEFAULT_CHUNK_BYTES,
                        artifact_id: str | None = None) -> StagedArtifact:
    """Inspect a receiver-created file in Python-owned staging without copying it.

    The caller must pass a private account-storage root and an opaque path that
    the Python receiver created. The path is never obtained from WebView input.
    Its complete digest must match the receiver's claimed digest.
    """
    _validate_limits(max_bytes, chunk_bytes)
    if provenance not in _PROVENANCE:
        raise ArtifactError("invalid artifact provenance")
    if not isinstance(expected_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None:
        raise ArtifactError("invalid expected artifact digest")
    safe_name = _safe_filename(filename)
    identifier = artifact_id or str(uuid.uuid4())
    try:
        identifier = str(uuid.UUID(identifier))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ArtifactError("invalid artifact id") from exc
    root = _stage_root(staging_dir)
    candidate = Path(path).absolute()
    try:
        if candidate.is_symlink():
            raise ArtifactError("staged path must not be a symbolic link")
        resolved = candidate.resolve(strict=True)
        if os.path.commonpath((str(root), str(resolved))) != str(root):
            raise ArtifactError("staged path is outside private storage")
        file_info = resolved.stat()
    except ArtifactError:
        raise
    except (OSError, ValueError) as exc:
        raise ArtifactError("staged file is unavailable") from exc
    if not stat.S_ISREG(file_info.st_mode) or file_info.st_size < 1 or file_info.st_size > max_bytes:
        raise ArtifactError("staged file size is outside configured bounds")
    file_size, digest = _hash_file(resolved, chunk_bytes, max_bytes)
    if file_size != file_info.st_size or digest != expected_sha256:
        raise ArtifactError("receiver digest does not match staged file")
    kind = _artifact_kind(safe_name)
    if kind == "3mf":
        plates, metadata, parameters_digest = _inspect_3mf(resolved, chunk_bytes)
    else:
        plates = ()
        metadata = _parse_gcode_metadata(resolved, file_size)
        parameters_digest = _gcode_parameters_digest(metadata)
    manifest: dict[str, object] = {
        "schema_version": 2,
        "provenance": provenance,
        "artifact_id": identifier,
        "sha256": digest,
        "size_bytes": file_size,
        "kind": "sliced_3mf" if kind == "3mf" else "gcode",
        "slicer_build": metadata.get("slicer_build"),
        "selected_plate_entry": None,
        "compatible_target_ids": (),
        "compatibility_evidence_refs": (),
    }
    if parameters_digest is not None:
        manifest["parameters_sha256"] = parameters_digest
    try:
        os.chmod(resolved, stat.S_IREAD)
    except OSError:
        pass
    return StagedArtifact(
        path=resolved,
        filename=safe_name,
        manifest=MappingProxyType(manifest),
        metadata=MappingProxyType(dict(metadata)),
        plate_entries=plates,
        manifest_complete=manifest_is_complete(manifest),
    )


def verify_staged_artifact(artifact: StagedArtifact, *,
                          chunk_bytes: int = DEFAULT_CHUNK_BYTES,
                          max_bytes: int = DEFAULT_MAX_BYTES) -> bool:
    """Re-check the immutable staged copy before a later consumer uses it."""
    _validate_limits(max_bytes, chunk_bytes)
    if not isinstance(artifact, StagedArtifact):
        return False
    try:
        if artifact.path.is_symlink() or not stat.S_ISREG(artifact.path.stat().st_mode):
            return False
        size, digest = _hash_file(artifact.path, chunk_bytes, max_bytes)
        return size == artifact.manifest.get("size_bytes") and digest == artifact.manifest.get("sha256")
    except (OSError, ArtifactError, ValueError, TypeError):
        return False


def select_plate_entry(artifact: StagedArtifact, plate_entry: str) -> Mapping[str, object]:
    """Return a manifest copy bound to an exact plate discovered by inspection."""
    if not isinstance(artifact, StagedArtifact) or artifact.manifest.get("kind") != "sliced_3mf":
        raise ArtifactError("artifact is not a sliced 3MF")
    if not isinstance(plate_entry, str) or plate_entry not in artifact.plate_entries:
        raise ArtifactError("selected plate was not discovered in this artifact")
    manifest = dict(artifact.manifest)
    manifest["selected_plate_entry"] = plate_entry
    return MappingProxyType(manifest)


def stage_selected_plate(artifact: StagedArtifact, plate_entry: str,
                         staging_dir: str | os.PathLike[str], *,
                         max_bytes: int = MAX_ZIP_MEMBER_BYTES,
                         chunk_bytes: int = DEFAULT_CHUNK_BYTES) -> StagedPlate:
    """Stream one inspected plate into a unique staged G-code file.

    Used by non-Bambu upload transports. Bambu should receive the original 3MF
    plus `plate_entry` returned by `select_plate_entry`.
    """
    _validate_limits(max_bytes, chunk_bytes)
    select_plate_entry(artifact, plate_entry)
    artifact_size = artifact.manifest.get("size_bytes")
    if type(artifact_size) is not int or artifact_size < 1:
        raise ArtifactError("source artifact has invalid size metadata")
    source_max_bytes = max(DEFAULT_MAX_BYTES, artifact_size)
    if not verify_staged_artifact(artifact, chunk_bytes=chunk_bytes,
                                  max_bytes=source_max_bytes):
        raise ArtifactError("source artifact failed digest verification")
    root = _stage_root(staging_dir)
    destination = root / (str(uuid.uuid4()) + ".gcode")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= _O_BINARY | _O_NOFOLLOW
    created = False
    try:
        source_fd = os.open(artifact.path, os.O_RDONLY | _O_BINARY | _O_NOFOLLOW)
        with os.fdopen(source_fd, "rb") as source:
            before_size, before_digest = _hash_stream(source, chunk_bytes, source_max_bytes)
            if (before_size != artifact.manifest.get("size_bytes")
                    or before_digest != artifact.manifest.get("sha256")):
                raise ArtifactError("source artifact failed digest verification")
            source.seek(0)
            with zipfile.ZipFile(source, "r") as archive:
                try:
                    info = archive.getinfo(plate_entry)
                except KeyError as exc:
                    raise ArtifactError("selected plate is missing") from exc
                match = _PLATE_ENTRY.fullmatch(info.filename)
                if (match is None or info.filename != plate_entry or info.is_dir()
                        or info.flag_bits & 0x1 or stat.S_ISLNK(info.external_attr >> 16)):
                    raise ArtifactError("selected plate entry is unsafe")
                if info.file_size < 1 or info.file_size > max_bytes:
                    raise ArtifactError("selected plate exceeds size limit")
                target_fd = os.open(destination, flags, 0o600)
                created = True
                digest = hashlib.sha256()
                copied = 0
                with os.fdopen(target_fd, "wb") as target, archive.open(info, "r") as member:
                    while True:
                        block = member.read(chunk_bytes)
                        if not block:
                            break
                        copied += len(block)
                        if copied > max_bytes:
                            raise ArtifactError("selected plate exceeds size limit")
                        target.write(block)
                        digest.update(block)
                    target.flush()
                    os.fsync(target.fileno())
                if copied != info.file_size:
                    raise ArtifactError("selected plate size mismatch")
            source.seek(0)
            after_size, after_digest = _hash_stream(source, chunk_bytes, source_max_bytes)
            if (after_size, after_digest) != (before_size, before_digest):
                raise ArtifactError("source artifact changed during plate extraction")
        os.chmod(destination, stat.S_IREAD)
        result = StagedPlate(destination, plate_entry, copied, digest.hexdigest(),
                             str(artifact.manifest.get("artifact_id", "")))
        if not verify_staged_plate(result, chunk_bytes=chunk_bytes, max_bytes=max_bytes):
            raise ArtifactError("extracted plate verification failed")
        return result
    except BaseException:
        if created:
            try:
                os.chmod(destination, stat.S_IWRITE | stat.S_IREAD)
                destination.unlink()
            except OSError:
                pass
        raise


def _is_reparse_point(info: os.stat_result) -> bool:
    """Recognize Windows reparse points as well as POSIX symbolic links."""
    attributes = getattr(info, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag)


def _discardable_stage_path(path: Path, staging_dir: str | os.PathLike[str],
                            expected_name: re.Pattern[str]) -> Path | None:
    """Validate a direct, plugin-named child of a real staging directory."""
    requested_root = Path(staging_dir).expanduser().absolute()
    try:
        root_info = requested_root.lstat()
        if (not stat.S_ISDIR(root_info.st_mode) or stat.S_ISLNK(root_info.st_mode)
                or _is_reparse_point(root_info)):
            raise ArtifactError("staging root must be a real directory")
        root = requested_root.resolve(strict=True)

        candidate = Path(path).absolute()
        if expected_name.fullmatch(candidate.name) is None:
            raise ArtifactError("staged file is not a plugin-created temporary")
        if candidate.parent != root or candidate.parent.resolve(strict=True) != root:
            raise ArtifactError("staged file is outside private storage")
        try:
            file_info = candidate.lstat()
        except FileNotFoundError:
            return None
        if (not stat.S_ISREG(file_info.st_mode) or stat.S_ISLNK(file_info.st_mode)
                or _is_reparse_point(file_info)):
            raise ArtifactError("staged file must be a regular file")
        return candidate
    except ArtifactError:
        raise
    except (OSError, ValueError) as exc:
        raise ArtifactError("staged file is unavailable") from exc


def discard_staged_artifact(artifact: StagedArtifact,
                            staging_dir: str | os.PathLike[str], *,
                            selected_plate: StagedPlate | None = None) -> bool:
    """Remove only a plugin-named staged copy, optionally with its extracted plate.

    This operation accepts Python-created handles, never browser paths. Both
    files must be direct regular-file children of the caller's real staging
    directory, and their generated names must match this module's staging
    formats. User source files and retained outbox files are outside this API.
    Missing files are treated as already discarded so callers can retry safely.
    """
    if not isinstance(artifact, StagedArtifact):
        raise ArtifactError("invalid staged artifact handle")
    artifact_id = artifact.manifest.get("artifact_id")
    try:
        if str(uuid.UUID(str(artifact_id))) != str(artifact_id):
            raise ValueError
    except (ValueError, TypeError, AttributeError) as exc:
        raise ArtifactError("invalid staged artifact id") from exc
    targets = [
        _discardable_stage_path(artifact.path, staging_dir, _ARTIFACT_STAGE_NAME),
    ]
    if selected_plate is not None:
        if (not isinstance(selected_plate, StagedPlate)
                or selected_plate.source_artifact_id != artifact_id):
            raise ArtifactError("selected plate does not belong to this artifact")
        targets.append(_discardable_stage_path(
            selected_plate.path, staging_dir, _PLATE_STAGE_NAME,
        ))

    existing = [target for target in targets if target is not None]
    # Validate every target before changing either file. Files are in a private
    # application directory, so chmod only clears the read-only bit needed by
    # Windows before unlinking the exact checked path.
    for target in existing:
        try:
            os.chmod(target, stat.S_IREAD | stat.S_IWRITE)
            target.unlink()
        except OSError as exc:
            raise ArtifactError("could not discard staged temporary") from exc
    return bool(existing)


def discard_staged_plate(plate: StagedPlate,
                         staging_dir: str | os.PathLike[str]) -> bool:
    """Remove one extracted plate temporary without touching its source artifact."""
    if not isinstance(plate, StagedPlate):
        raise ArtifactError("invalid staged plate handle")
    try:
        if str(uuid.UUID(str(plate.source_artifact_id))) != str(plate.source_artifact_id):
            raise ValueError
    except (ValueError, TypeError, AttributeError) as exc:
        raise ArtifactError("invalid source artifact id") from exc
    target = _discardable_stage_path(plate.path, staging_dir, _PLATE_STAGE_NAME)
    if target is None:
        return False
    try:
        os.chmod(target, stat.S_IREAD | stat.S_IWRITE)
        target.unlink()
    except OSError as exc:
        raise ArtifactError("could not discard staged plate temporary") from exc
    return True


def _hash_stream(stream, chunk_bytes: int, max_bytes: int) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    while True:
        block = stream.read(chunk_bytes)
        if not block:
            break
        size += len(block)
        if size > max_bytes:
            raise ArtifactError("artifact exceeds size limit")
        digest.update(block)
    return size, digest.hexdigest()


def verify_staged_plate(plate: StagedPlate, *, chunk_bytes: int = DEFAULT_CHUNK_BYTES,
                        max_bytes: int = MAX_ZIP_MEMBER_BYTES) -> bool:
    _validate_limits(max_bytes, chunk_bytes)
    if not isinstance(plate, StagedPlate):
        return False
    try:
        if plate.path.is_symlink() or not stat.S_ISREG(plate.path.stat().st_mode):
            return False
        size, digest = _hash_file(plate.path, chunk_bytes, max_bytes)
        return size == plate.size_bytes and digest == plate.sha256
    except (OSError, ArtifactError, ValueError, TypeError):
        return False
