from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import json
import os
import re
import tomllib
import zipfile
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
PACKS = {
    "fhsl-hard-surface": ("filamenthub_surface_hard_surface", "fhslh", 53, "FHSL — Hard Surface"),
    "fhsl-nature-organics": ("filamenthub_surface_nature_organics", "fhsln", 38, "FHSL — Nature & Organics"),
    "fhsl-textiles-materials": ("filamenthub_surface_textiles_materials", "fhslt", 48, "FHSL — Textiles & Materials"),
}


def valid_version(version: str) -> bool:
    return bool(re.fullmatch(r"\d+\.\d+\.\d+", version)) and all(int(part) <= 10 for part in version.split("."))


def _record_hash(data: bytes) -> str:
    return "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode("ascii").rstrip("=")


def _pep723(text: str) -> dict:
    lines = text.splitlines()
    try:
        start = lines.index("# /// script") + 1
        end = lines.index("# ///", start)
    except ValueError as exc:
        raise ValueError("Missing or malformed PEP 723 metadata block") from exc
    body = []
    for line in lines[start:end]:
        if line == "#":
            body.append("")
        elif line.startswith("# "):
            body.append(line[2:])
        else:
            raise ValueError("Non-comment line inside PEP 723 metadata block")
    return tomllib.loads("\n".join(body))


def validate_png_payload(data: bytes, filename: str) -> None:
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format != "PNG" or image.mode != "L" or image.size != (2048, 2048):
                raise ValueError(f"Not a 2048x2048 grayscale PNG: {filename}")
            if "transparency" in image.info:
                raise ValueError(f"Unexpected transparency metadata: {filename}")
            image.verify()
        with Image.open(io.BytesIO(data)) as image:
            image.load()
    except Exception as exc:
        raise ValueError(f"Invalid PNG payload {filename}: {exc}") from exc


def _project(plugin_dir: Path, distribution: str, import_name: str, expected_count: int) -> tuple[str, str]:
    config = tomllib.loads((plugin_dir / "pyproject.toml").read_text(encoding="utf-8"))
    project = config["project"]
    plugin = config["tool"]["fhsl"]
    version = project["version"]
    if not valid_version(version):
        raise ValueError(f"Invalid numeric Orca version: {version}")
    if project["name"].replace("-", "_") != distribution or plugin["plugin-id"] != distribution:
        raise ValueError(f"Project/plugin identity mismatch in {plugin_dir}")
    if plugin["import-name"] != import_name:
        raise ValueError(f"Project import-name mismatch in {plugin_dir}")
    if plugin["texture-count"] != expected_count:
        raise ValueError(f"Expected texture count mismatch in {plugin_dir}")
    runtime = (plugin_dir / import_name / "__init__.py").read_text(encoding="utf-8")
    orca_plugin = _pep723(runtime)["tool"]["orcaslicer"]["plugin"]
    if orca_plugin.get("id") != distribution or orca_plugin.get("version") != version:
        raise ValueError(f"PEP 723 id/version mismatch in {plugin_dir}")
    if orca_plugin.get("name") != PACKS[plugin_dir.name][3]:
        raise ValueError(f"PEP 723 display name mismatch in {plugin_dir}")
    changelog = (plugin_dir / "CHANGELOG.md").read_text(encoding="utf-8")
    versions = re.findall(r"^## (\d+\.\d+\.\d+)\s*$", changelog, re.MULTILINE)
    if not versions or versions[0] != version:
        raise ValueError(f"Newest changelog version must equal candidate {version} in {plugin_dir}")
    return version, project["name"]


def validate_source(plugin_dir: Path, distribution: str, import_name: str, expected_count: int) -> list[Path]:
    _project(plugin_dir, distribution, import_name, expected_count)
    module_dir = plugin_dir / import_name
    init_py = module_dir / "__init__.py"
    images = sorted((module_dir / "t").glob("*.png"), key=lambda p: p.name.casefold())
    manifest = json.loads((plugin_dir / "texture-manifest.json").read_text(encoding="utf-8"))
    expected = manifest.get("textures", {})
    if len(images) != expected_count or set(expected) != {p.name for p in images}:
        raise ValueError(f"{plugin_dir.name}: texture set differs from the checked-in manifest")
    for path in images:
        if not path.stem.isascii() or not re.fullmatch(r"[a-z0-9_]+", path.stem):
            raise ValueError(f"Non-ASCII or unexpected texture filename: {path.name}")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != expected[path.name]:
            raise ValueError(f"Texture hash mismatch: {path.name}")
        validate_png_payload(data, path.name)
    return [init_py, *images]


def audit_wheel(path: Path, distribution: str, import_name: str, version: str, expected_count: int) -> None:
    with zipfile.ZipFile(path) as wheel:
        if wheel.testzip() is not None:
            raise ValueError(f"Corrupt wheel archive: {path}")
        names = wheel.namelist()
        if len(names) != len(set(names)):
            raise ValueError(f"Wheel contains duplicate archive paths: {path}")
        if any("__pycache__" in name or name.endswith(".pyc") for name in names):
            raise ValueError(f"Wheel contains bytecode: {path}")
        images = [name for name in names if name.startswith(f"{import_name}/t/") and name.endswith(".png")]
        if len(images) != expected_count:
            raise ValueError(f"Wheel texture count mismatch: {path}")
        for name in images:
            validate_png_payload(wheel.read(name), Path(name).name)
        metadata_name = next(name for name in names if name.endswith(".dist-info/METADATA"))
        metadata = wheel.read(metadata_name).decode("utf-8")
        expected_name = distribution.replace("_", "-")
        if f"Name: {expected_name}\n" not in metadata or f"Version: {version}\n" not in metadata:
            raise ValueError(f"Wheel version metadata mismatch: {path}")
        if "Requires-Python: >=3.12\n" not in metadata:
            raise ValueError(f"Wheel Python requirement metadata mismatch: {path}")
        top_level_name = next(name for name in names if name.endswith(".dist-info/top_level.txt"))
        if wheel.read(top_level_name).decode("ascii") != f"{import_name}\n":
            raise ValueError(f"Wheel import-name metadata mismatch: {path}")
        record_name = next(name for name in names if name.endswith(".dist-info/RECORD"))
        rows = list(csv.reader(wheel.read(record_name).decode("utf-8").splitlines()))
        if len(rows) != len(names):
            raise ValueError(f"Wheel RECORD inventory mismatch: {path}")
        row_names = [row[0] for row in rows]
        if len(row_names) != len(set(row_names)) or set(row_names) != set(names):
            raise ValueError(f"Wheel RECORD paths are duplicate or incomplete: {path}")
        for filename, digest_field, size in rows:
            if not digest_field:
                if filename != record_name or size:
                    raise ValueError(f"Unexpected unhashed RECORD entry: {filename}")
                continue
            data = wheel.read(filename)
            algo, expected_digest = digest_field.split("=", 1)
            actual = base64.urlsafe_b64encode(hashlib.new(algo, data).digest()).decode("ascii").rstrip("=")
            if actual != expected_digest or str(len(data)) != size:
                raise ValueError(f"Wheel RECORD hash/size mismatch: {filename}")


def build_one(key: str, output_dir: Path | None = None) -> Path:
    if key not in PACKS:
        raise ValueError(f"Unknown plugin: {key}")
    distribution, import_name, count, display_name = PACKS[key]
    plugin_dir = ROOT / "plugins" / key
    inputs = validate_source(plugin_dir, distribution, import_name, count)
    version, metadata_name = _project(plugin_dir, distribution, import_name, count)
    release_dir = output_dir or plugin_dir / "dist" / f"release-{version}"
    target_dir = release_dir / "wheels"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{distribution}-{version}-py3-none-any.whl"
    dist_name = f"{distribution}-{version}.dist-info"
    payload: dict[str, bytes] = {}
    for source in inputs:
        rel = source.relative_to(plugin_dir).as_posix()
        payload[f"{import_name}/{rel.split('/', 1)[1]}"] = source.read_bytes()
    payload[f"{dist_name}/METADATA"] = (
        "Metadata-Version: 2.1\n"
        f"Name: {metadata_name}\nVersion: {version}\n"
        "Requires-Python: >=3.12\n"
        f"Summary: {display_name} height maps for OrcaSlicer\nAuthor: FilamentHub\n\n"
    ).encode("utf-8")
    payload[f"{dist_name}/WHEEL"] = b"Wheel-Version: 1.0\nGenerator: fhsl_package.py\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    payload[f"{dist_name}/top_level.txt"] = f"{import_name}\n".encode("ascii")
    record = f"{dist_name}/RECORD"
    rows = [(name, _record_hash(data), str(len(data))) for name, data in sorted(payload.items())]
    rows.append((record, "", ""))
    stream = io.StringIO(newline="")
    csv.writer(stream, lineterminator="\n").writerows(rows)
    payload[record] = stream.getvalue().encode("utf-8")
    temp_target = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    try:
        with zipfile.ZipFile(temp_target, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as wheel:
            for name, data in sorted(payload.items()):
                info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_STORED
                info.external_attr = 0o644 << 16
                wheel.writestr(info, data)
        audit_wheel(temp_target, distribution, import_name, version, count)
        temp_target.replace(target)
    finally:
        if temp_target.exists():
            temp_target.unlink()
    release_dir.mkdir(parents=True, exist_ok=True)
    changelog = (plugin_dir / "CHANGELOG.md").read_text(encoding="utf-8")
    section = changelog.split(f"## {version}", 1)[1].split("\n## ", 1)[0].strip()
    (release_dir / "RELEASE_NOTES.md").write_text(f"# {display_name} {version}\n\n{section}\n", encoding="utf-8", newline="\n")
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    (release_dir / "SHA256SUMS").write_text(f"{digest}  wheels/{target.name}\n", encoding="ascii", newline="\n")
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description="Build one FHSL Orca texture-library wheel.")
    parser.add_argument("--plugin", choices=PACKS, help="Build just this plugin; default builds each independently.")
    parser.add_argument("--output", type=Path, help="Override this plugin's release root; wheel, notes and sums go inside it.")
    args = parser.parse_args()
    if args.output and not args.plugin:
        parser.error("--output requires --plugin")
    keys = [args.plugin] if args.plugin else list(PACKS)
    for key in keys:
        wheel = build_one(key, args.output)
        print(f"{wheel}\t{wheel.stat().st_size}\t{hashlib.sha256(wheel.read_bytes()).hexdigest()}")


if __name__ == "__main__":
    main()
