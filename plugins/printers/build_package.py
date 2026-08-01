from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "printers_plugin.py"
LOCALES = ROOT / "printers_locales"


def extract_metadata(source: str) -> dict[str, object]:
    lines = source.splitlines()
    try:
        start = lines.index("# /// script")
        end = lines.index("# ///", start + 1)
    except ValueError as exc:
        raise ValueError("PEP 723 metadata block is missing") from exc

    metadata_lines: list[str] = []
    for line in lines[start + 1 : end]:
        if not line.startswith("#"):
            raise ValueError("Every PEP 723 metadata line must be a comment")
        metadata_lines.append(line[2:] if line.startswith("# ") else line[1:])

    metadata = tomllib.loads("\n".join(metadata_lines))
    plugin = metadata.get("tool", {}).get("orcaslicer", {}).get("plugin", {})
    if not isinstance(plugin, dict):
        raise ValueError("[tool.orcaslicer.plugin] metadata is missing")
    if plugin.get("id") != "printers":
        raise ValueError("Plugin id must remain 'printers'")
    version = plugin.get("version")
    if not isinstance(version, str) or not version:
        raise ValueError("Plugin version is missing")
    if re.fullmatch(r"\d+\.\d+\.\d+", version) is None:
        raise ValueError("Plugin Hub version must use numeric X.Y.Z format")
    if metadata.get("dependencies") != []:
        raise ValueError("The single-file package must remain dependency-free")
    return metadata


def extract_runtime_version(source: str) -> str:
    module = ast.parse(source, filename=str(SOURCE))
    for node in module.body:
        if isinstance(node, ast.Assign):
            if any(isinstance(target, ast.Name) and target.id == "PLUGIN_VERSION" for target in node.targets):
                if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                    return node.value.value
    raise ValueError("PLUGIN_VERSION constant is missing")


def _build_wheel(source_bytes: bytes, version: str, output_root: Path) -> Path:
    build_dir = output_root / "wheel-build"
    if build_dir.exists():
        shutil.rmtree(build_dir)
    build_dir.mkdir(parents=True)
    (build_dir / "printers_plugin.py").write_bytes(source_bytes)
    shutil.copy2(ROOT / "pyproject.toml", build_dir / "pyproject.toml")
    shutil.copytree(LOCALES, build_dir / LOCALES.name)
    wheels_out = output_root / "wheels"
    wheels_out.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(wheels_out)],
        cwd=build_dir,
        check=True,
    )
    return wheels_out / f"printers-{version}-py3-none-any.whl"


def build(output_root: Path, wheel: bool = True) -> Path:
    source_bytes = SOURCE.read_bytes()
    source = source_bytes.decode("utf-8")
    ast.parse(source, filename=str(SOURCE))
    metadata = extract_metadata(source)
    plugin = metadata["tool"]["orcaslicer"]["plugin"]
    version = plugin["version"]
    runtime_version = extract_runtime_version(source)
    if runtime_version != version:
        raise ValueError(
            f"Metadata version {version!r} does not match PLUGIN_VERSION {runtime_version!r}"
        )

    package_dir = output_root / f"printers-{version}"
    package_dir.mkdir(parents=True, exist_ok=True)
    package_path = package_dir / "printers_plugin.py"
    package_path.write_bytes(source_bytes)
    package_locales = package_dir / LOCALES.name
    if package_locales.exists():
        shutil.rmtree(package_locales)
    shutil.copytree(LOCALES, package_locales)

    digest = hashlib.sha256(source_bytes).hexdigest()
    checksum_lines = [f"{digest}  printers_plugin.py"]
    for locale_path in sorted(package_locales.glob("*.json")):
        locale_digest = hashlib.sha256(locale_path.read_bytes()).hexdigest()
        checksum_lines.append(
            f"{locale_digest}  {LOCALES.name}/{locale_path.name}"
        )
    (package_dir / "SHA256SUMS").write_text(
        "\n".join(checksum_lines) + "\n", encoding="utf-8", newline="\n"
    )
    (package_dir / "package-metadata.json").write_text(
        json.dumps(
            {
                "id": plugin["id"],
                "name": plugin["name"],
                "description": plugin["description"],
                "author": plugin["author"],
                "version": version,
                "network": plugin.get("network", []),
                "requires_python": metadata.get("requires-python"),
                "dependencies": metadata.get("dependencies"),
                "entry_file": package_path.name,
                "sha256": digest,
                "locales": sorted(path.stem for path in package_locales.glob("*.json")),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    icon = ROOT / "printers.svg"
    if icon.is_file():
        shutil.copy2(icon, package_dir / icon.name)
    if wheel:
        wheel_path = _build_wheel(source_bytes, version, output_root)
        print(wheel_path)
    return package_dir


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the Print Farm OrcaSlicer plugin package")
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "dist",
        help="Output root (default: plugins/printers/dist)",
    )
    parser.add_argument(
        "--no-wheel",
        action="store_true",
        help="Only stage the package; skip building the wheel",
    )
    args = parser.parse_args()
    package_dir = build(args.output.resolve(), wheel=not args.no_wheel)
    print(package_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
