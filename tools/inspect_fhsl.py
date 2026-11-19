#!/usr/bin/env python3
"""Read-only FHSL inspection. Python 3.9+, standard library only.

Usage: python inspect_fhsl.py --data-dir PATH > fhsl-report.json
Paths are relative to orca_plugins; no logs, plugin code or global settings are read.
"""
import argparse
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import re
import struct
import zipfile

EXPECTED = {
    "filamenthub_surface_hard_surface": 53,
    "filamenthub_surface_nature_organics": 38,
    "filamenthub_surface_textiles_materials": 48,
}
SHORT_MODULES = {
    "fhslh": "filamenthub_surface_hard_surface",
    "fhsln": "filamenthub_surface_nature_organics",
    "fhslt": "filamenthub_surface_textiles_materials",
}
MODULE_IDENTITIES = {name: name for name in EXPECTED}
MODULE_IDENTITIES.update(SHORT_MODULES)
# Folder holding the maps inside each module.
TEXTURE_DIRS = {name: ("t" if name in SHORT_MODULES else "textures") for name in MODULE_IDENTITIES}
EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp"}


def error_code(exc):
    """Never copy exception text: it can contain an absolute path or credentials."""
    return {"type": type(exc).__name__, "errno": getattr(exc, "errno", None),
            "winerror": getattr(exc, "winerror", None)}


def is_link(path):
    try:
        # Junctions and other reparse points are excluded too, including Python <3.12.
        return path.is_symlink() or bool(getattr(path.lstat(), "st_file_attributes", 0) & 0x400)
    except OSError:
        return True


def walk_files(root, errors):
    for directory, dirs, files in os.walk(root, followlinks=False,
                                          onerror=lambda e: errors.append(error_code(e))):
        base = Path(directory)
        dirs[:] = sorted(d for d in dirs if not is_link(base / d))
        for name in dirs:
            yield base / name
        for name in sorted(files):
            path = base / name
            if not is_link(path):
                yield path


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def image_header(path):
    with path.open("rb") as stream:
        data = stream.read(33)
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        if len(data) != 33 or data[8:16] != b"\x00\x00\x00\rIHDR":
            return {"format": "PNG", "header_valid": False}
        import zlib
        width, height, depth, color, compression, filtering, interlace = struct.unpack(
            ">IIBBBBB", data[16:29])
        crc_ok = zlib.crc32(data[12:29]) & 0xffffffff == int.from_bytes(data[29:33], "big")
        mode = {0: "gray", 2: "RGB", 3: "palette", 4: "gray_alpha", 6: "RGBA"}.get(color, "unknown")
        valid_depth = depth in ({0: {1, 2, 4, 8, 16}, 2: {8, 16}, 3: {1, 2, 4, 8},
                                 4: {8, 16}, 6: {8, 16}}.get(color, set()))
        return {"format": "PNG", "width": width, "height": height, "bit_depth": depth,
                "color_type": color, "mode": mode, "alpha_channel": color in (4, 6),
                "interlace": interlace, "ihdr_crc_ok": crc_ok,
                "header_valid": bool(crc_ok and width and height and valid_depth and
                                     compression == filtering == 0 and interlace in (0, 1)),
                "transparency_chunk_checked": False,
                "integrity": "IHDR_only_no_pixel_decode_or_remaining_chunk_CRC"}
    return {"format": "JPEG" if data.startswith(b"\xff\xd8\xff") else
            "BMP" if data.startswith(b"BM") else "unknown", "integrity": "signature_only"}


def native_probe(path):
    """Probe only one existing file, without reading it or changing anything."""
    if os.name != "nt":
        return {"status": "not_windows"}
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    absolute = os.path.abspath(path)
    extended = "\\\\?\\UNC\\" + absolute[2:] if absolute.startswith("\\\\") else "\\\\?\\" + absolute
    result = {}
    for label, candidate in (("normal", absolute), ("extended", extended)):
        ctypes.set_last_error(0)
        handle = kernel.CreateFileW(candidate, 0x80000000, 7, None, 3, 0x80, None)
        ok = handle != ctypes.c_void_p(-1).value
        result[label] = {"open_ok": ok, "winerror": 0 if ok else ctypes.get_last_error()}
        if ok:
            kernel.CloseHandle(handle)
    result["scope"] = "current_Python_process_not_Orca_or_Boost_runtime"
    return result


def metadata_fields(text):
    result = {}
    for line in text.splitlines():
        for key in ("Name", "Version"):
            if line.startswith(key + ":"):
                value = line.split(":", 1)[1].strip()
                # These are package identifiers, not arbitrary text to echo into reports.
                if re.fullmatch(r"[A-Za-z0-9_.+!-]{1,150}", value):
                    result[key.lower()] = value
    return result


def inspect_copy(texture_dir, root):
    module = texture_dir.parent.name
    identity = MODULE_IDENTITIES[module]
    parts = texture_dir.relative_to(root).parts
    index = parts.index("__whl_extracted__")
    install_root = root.joinpath(*parts[:index])
    extraction_root = root.joinpath(*parts[:index + 2])
    report = {"package": identity, "import_module": module, "expected_count": EXPECTED[identity],
              "copy": "/".join(parts), "cloud_copy": "_subscribed" in parts,
              "errors": [], "images": [], "wheels": []}
    # Remove account UUID and install directory identifiers from the displayed relative path.
    report["copy"] = "/".join("<account>" if i and parts[i - 1] == "_subscribed" else p
                                  for i, p in enumerate(parts))
    for sidecar in (".install_state.json", "plugin.json"):
        path = install_root / sidecar
        if path.is_file() and not is_link(path):
            try:
                if path.stat().st_size > 1024 * 1024:
                    raise ValueError("oversized sidecar")
                value = json.loads(path.read_text("utf-8-sig"))
                if not isinstance(value, dict):
                    raise ValueError("invalid sidecar")
                safe = {}
                for key in ("installed_version", "version"):
                    val = value.get(key)
                    if isinstance(val, str) and re.fullmatch(r"[A-Za-z0-9_.+!-]{1,100}", val):
                        safe[key] = val
                if isinstance(value.get("enabled"), bool):
                    safe["enabled"] = value["enabled"]
                capabilities = value.get("capabilities")
                if isinstance(capabilities, list):
                    safe["capabilities"] = [
                        {name: flag} for item in capabilities if isinstance(item, dict)
                        for name, flag in item.items()
                        if isinstance(name, str) and name.startswith("FilamentHub Surface Library")
                        and len(name) <= 150 and isinstance(flag, bool)]
                report[sidecar] = safe
            except (OSError, ValueError) as exc:
                report["errors"].append({"operation": sidecar, **error_code(exc)})
    try:
        files = sorted(p for p in texture_dir.iterdir()
                       if not is_link(p) and p.is_file() and p.suffix.lower() in EXTENSIONS)
        for path in files:
            row = {"filename": path.name, "full_path_characters": len(str(path.absolute())),
                   "relative_path_characters": len(str(path.relative_to(root))),
                   "non_ascii_full_path": not str(path.absolute()).isascii()}
            try:
                row.update(image_header(path))
            except OSError as exc:
                row["error"] = error_code(exc)
            report["images"].append(row)
        report["image_count"] = len(files)
        report["count_matches"] = len(files) == EXPECTED[identity]
        report["max_full_path_characters"] = max((r["full_path_characters"] for r in report["images"]), default=0)
        report["at_or_above_260_characters"] = sum(r["full_path_characters"] >= 260 for r in report["images"])
        report["classification"] = ("all_PNG_L8" if files and all(
            r.get("format") == "PNG" and r.get("header_valid") and r.get("bit_depth") == 8
            and r.get("color_type") == 0 for r in report["images"]) else "mixed_or_non_L8_or_unreadable")
        if files:
            # Probe longest path: an ordinary short path is a poor test of path-length trouble.
            report["native_open_probe"] = native_probe(max(files, key=lambda p: len(str(p))))
    except OSError as exc:
        report["errors"].append({"operation": "enumerate_images", **error_code(exc)})
    for wheel in sorted(install_root.glob("*.whl")):
        if is_link(wheel):
            continue
        entry = {"filename": wheel.name}
        try:
            entry["sha256"] = sha256_file(wheel)
            with zipfile.ZipFile(wheel) as archive:
                infos = archive.infolist()
                entry["archive_entries"] = len(infos)
                entry["duplicate_paths"] = len(infos) - len({i.filename for i in infos})
                # testzip streams every member; it never imports or extracts package code.
                entry["zip_crc_ok"] = archive.testzip() is None
                metadata = [i for i in infos if i.filename.endswith(".dist-info/METADATA")]
                if len(metadata) == 1 and metadata[0].file_size <= 1024 * 1024:
                    entry["metadata"] = metadata_fields(archive.read(metadata[0]).decode("utf-8", errors="replace"))
                    normalized_name = entry["metadata"].get("name", "").lower().replace("-", "_").replace(".", "_")
                    entry["distribution_identity_matches"] = normalized_name == identity
                    installed = report.get(".install_state.json", {}).get("installed_version")
                    if installed and entry["metadata"].get("version"):
                        entry["sidecar_version_differs_from_wheel"] = installed != entry["metadata"]["version"]
                records = [i for i in infos if i.filename.endswith(".dist-info/RECORD")]
                if len(records) == 1 and records[0].file_size <= 4 * 1024 * 1024:
                    missing, mismatched, unsafe = [], [], 0
                    for name, digest, size in csv.reader(io.StringIO(archive.read(records[0]).decode("utf-8"))):
                        parts_name = name.replace("\\", "/").split("/")
                        if not name.startswith(module + "/" + TEXTURE_DIRS[module] + "/"):
                            continue
                        if any(x in ("..", "") for x in parts_name) or ":" in name:
                            unsafe += 1
                            continue
                        target = extraction_root.joinpath(*parts_name)
                        if not target.is_file() or any(is_link(p) for p in [target, *target.parents]
                                                      if p != root.parent):
                            missing.append(Path(name).name)
                        elif digest.startswith("sha256="):
                            import base64
                            actual = base64.urlsafe_b64encode(bytes.fromhex(sha256_file(target))).decode().rstrip("=")
                            if actual != digest[7:] or (size and target.stat().st_size != int(size)):
                                mismatched.append(Path(name).name)
                    entry["extracted_RECORD"] = {"missing_textures": missing, "mismatched_textures": mismatched,
                                                 "unsafe_rows": unsafe, "scope": "texture_rows_only"}
        except (OSError, ValueError, zipfile.BadZipFile, RuntimeError) as exc:
            entry["error"] = error_code(exc)
        report["wheels"].append(entry)
    return report


def inspect(data_dir):
    root = Path(data_dir).absolute() / "orca_plugins"
    result = {"schema": 1, "read_only": True, "path_scope": "relative_to_orca_plugins_account_redacted",
              "image_integrity": "PNG_IHDR_only_no_pixel_decode", "packs": [], "errors": [],
              "interpretation": "Counts describe disk copies, not loaded capabilities or successful Orca thumbnails. Native probes run in Python, not Orca."}
    if is_link(root) or not root.is_dir():
        result["errors"].append({"operation": "plugin_root", "type": "missing_or_linked_directory"})
        return result
    directories = set()
    for path in walk_files(root, result["errors"]):
        if (path.parent.name in MODULE_IDENTITIES and path.name == TEXTURE_DIRS[path.parent.name] and path.is_dir()
                and "__whl_extracted__" in path.relative_to(root).parts):
            directories.add(path)
    for directory in sorted(directories):
        result["packs"].append(inspect_copy(directory, root))
    result["enumeration_may_be_incomplete"] = bool(result["errors"])
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True, help="OrcaSlicer data directory (contains orca_plugins)")
    args = parser.parse_args()
    print(json.dumps(inspect(args.data_dir), ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
