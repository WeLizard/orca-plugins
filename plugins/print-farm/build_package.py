from __future__ import annotations

import argparse
import ast
import base64
import csv
import hashlib
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "printers_plugin.py"
LOCALES = ROOT / "printers_locales"
CHANGELOG = ROOT / "CHANGELOG.md"
DESCRIPTION = ROOT / "description.md"
EMBEDDED_UI_COPY_TOKEN = "_EMBEDDED_UI_COPY = {}"
HELPER_BEGIN = "# BEGIN PRINT_FARM_HELPERS"
HELPER_END = "# END PRINT_FARM_HELPERS"
# Helpers are registered once, in printers_plugin.py, as
# `_pf_<alias> = _pf_load_local_helper("<module>")`; the builder reads them there,
# so adding a helper module is a one-line change in the plugin source.
HELPER_LOAD = re.compile(
    r'^_pf_([a-z0-9_]+) = _pf_load_local_helper\("([a-z0-9_]+)"\)$',
    re.MULTILINE,
)


def _reset_output_dir(path: Path) -> None:
    if path.exists():
        # Empty the directory instead of removing it: Windows refuses to remove a
        # directory that is some process's working directory.
        for child in path.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
    path.mkdir(parents=True, exist_ok=True)


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


def current_version() -> str:
    source = SOURCE.read_text(encoding="utf-8")
    metadata = extract_metadata(source)
    version = metadata["tool"]["orcaslicer"]["plugin"]["version"]
    runtime_version = extract_runtime_version(source)
    if runtime_version != version:
        raise ValueError(
            f"Metadata version {version!r} does not match PLUGIN_VERSION {runtime_version!r}"
        )
    return version


def default_release_output_root() -> Path:
    return ROOT / "dist" / f"release-{current_version()}"


def _source_with_embedded_locales(source: str) -> str:
    """Embed reviewable locale catalogs into one installable plugin module."""
    result = source.replace("\r\n", "\n").replace("\r", "\n")
    if result.count(EMBEDDED_UI_COPY_TOKEN) != 1:
        raise ValueError("embedded locale marker is missing or duplicated")
    catalogs = {}
    for locale_path in sorted(LOCALES.glob("*.json")):
        data = json.loads(locale_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in data.items()
        ):
            raise ValueError(f"invalid locale catalog: {locale_path.name}")
        catalogs[locale_path.stem] = data
    embedded = json.dumps(
        catalogs,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    result = result.replace(
        EMBEDDED_UI_COPY_TOKEN,
        "_EMBEDDED_UI_COPY = " + embedded,
        1,
    )
    ast.parse(result, filename="printers_plugin.py[release]")
    return result


# Names a helper may not use once it is wrapped in a function: they would read the
# plugin module instead of the helper, or reach module state by name.
_HELPER_FORBIDDEN_NAMES = frozenset({
    "__name__", "__file__", "__spec__", "__package__", "__loader__", "__builtins__",
    "globals", "locals", "vars",
})


def _stringify_annotations(tree: ast.AST) -> None:
    """Keep ``from __future__ import annotations`` semantics without the import."""
    for node in ast.walk(tree):
        if isinstance(node, ast.arg) and node.annotation is not None:
            node.annotation = ast.Constant(ast.unparse(node.annotation))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.returns is not None:
            node.returns = ast.Constant(ast.unparse(node.returns))
        elif isinstance(node, ast.AnnAssign):
            node.annotation = ast.Constant(ast.unparse(node.annotation))


def _helper_definition(alias: str, name: str, helper_source: str) -> tuple[str, str]:
    """Return (source of a plain function that builds the helper namespace, docstring).

    The helper's statements become the body of an ordinary ``def``: the shipped
    plugin contains the helper as real, reviewable code, never as a string that is
    compiled and executed at import time.
    """
    tree = ast.parse(helper_source, filename=name + ".py")
    for node in ast.walk(tree):
        if isinstance(node, (ast.Global, ast.Nonlocal)):
            raise ValueError(f"{name}: global/nonlocal cannot be embedded as a function")
        if isinstance(node, ast.ImportFrom) and any(item.name == "*" for item in node.names):
            raise ValueError(f"{name}: star import cannot be embedded as a function")
        if isinstance(node, ast.Name) and node.id in _HELPER_FORBIDDEN_NAMES:
            raise ValueError(f"{name}: {node.id} cannot be used in an embedded helper")
    body = list(tree.body)
    doc = ""
    if (body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        doc = body.pop(0).value.value
    lazy_annotations = False
    kept = []
    for node in body:
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            if any(item.name != "annotations" for item in node.names):
                raise ValueError(f"{name}: unsupported __future__ import")
            lazy_annotations = True
            continue
        kept.append(node)
    if lazy_annotations:
        _stringify_annotations(ast.Module(body=kept, type_ignores=[]))
    kept.append(ast.Return(value=ast.Call(
        func=ast.Name(id="dict", ctx=ast.Load()),
        args=[ast.Call(func=ast.Name(id="locals", ctx=ast.Load()), args=[], keywords=[])],
        keywords=[],
    )))
    function = ast.FunctionDef(
        name="_pf_define_" + alias,
        args=ast.arguments(posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[]),
        body=kept, decorator_list=[], returns=None, type_params=[],
    )
    module = ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[]))
    return ast.unparse(module), doc


def _source_with_embedded_helpers(source: str) -> str:
    """Keep the shipped plugin self-contained while retaining focused source tests."""
    if source.count(HELPER_BEGIN) != 1 or source.count(HELPER_END) != 1:
        raise ValueError("helper embed markers are missing or duplicated")
    before, rest = source.split(HELPER_BEGIN, 1)
    _, after = rest.split(HELPER_END, 1)
    registered = HELPER_LOAD.findall(rest.split(HELPER_END, 1)[0])
    if not registered:
        raise ValueError("no helper modules are registered")
    lines = [
        "import sys as _pf_sys",
        "import types as _pf_types",
        "",
        "",
        "def _pf_embedded_helper(name, define, doc):",
        "    module_name = __name__ + '_' + name",
        "    module = _pf_types.ModuleType(module_name, doc)",
        "    module.__dict__.update(define())",
        "    _pf_sys.modules[module_name] = module",
        "    return module",
        "",
        "",
    ]
    assignments = []
    for alias, name in registered:
        helper_path = ROOT / (name + ".py")
        helper_source = helper_path.read_text(encoding="utf-8")
        definition, doc = _helper_definition(alias, name, helper_source)
        lines.extend([definition, "", ""])
        assignments.append(
            "_pf_%s = _pf_embedded_helper(%r, _pf_define_%s, %r)" % (alias, name, alias, doc)
        )
    result = before + "\n".join(lines + assignments) + after
    ast.parse(result, filename="printers_plugin.py[embedded helpers]")
    return result


# What the shipped plugin may not contain. The OrcaCloud security scan reads the
# plugin statically, so these are refused at build time instead of being found later.
RELEASE_FORBIDDEN_IMPORTS = frozenset({
    "ctypes", "subprocess", "webbrowser", "importlib", "pickle", "marshal", "shelve",
    "runpy", "imp", "socketserver", "http.server", "getpass", "platform", "code", "codeop",
})
RELEASE_FORBIDDEN_CALLS = frozenset({"exec", "eval", "compile", "__import__"})
RELEASE_FORBIDDEN_OS_ATTRIBUTES = frozenset({"environ", "getenv", "putenv", "startfile", "system", "popen"})


def _forbidden_module(name: str) -> bool:
    return name in RELEASE_FORBIDDEN_IMPORTS or name.split(".")[0] in RELEASE_FORBIDDEN_IMPORTS


def release_source_violations(source: str) -> list[str]:
    """Constructs the shipped plugin must not contain (an empty list means clean)."""
    found = []
    tree = ast.parse(source, filename="printers_plugin.py[release]")
    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        if isinstance(node, ast.Import):
            found.extend(f"line {line}: import {item.name}" for item in node.names
                         if _forbidden_module(item.name))
        elif isinstance(node, ast.ImportFrom):
            if _forbidden_module(node.module or ""):
                found.append(f"line {line}: from {node.module} import ...")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in RELEASE_FORBIDDEN_CALLS:
                found.append(f"line {line}: call to {node.func.id}()")
            elif (node.func.id in {"getattr", "setattr", "delattr"} and node.args
                    and isinstance(node.args[0], ast.Name) and node.args[0].id in {"os", "sys"}):
                found.append(f"line {line}: indirect {node.func.id}() on {node.args[0].id}")
        elif (isinstance(node, ast.Attribute) and node.attr in RELEASE_FORBIDDEN_OS_ATTRIBUTES
                and isinstance(node.value, ast.Name) and node.value.id == "os"):
            found.append(f"line {line}: os.{node.attr}")
    return found


def extract_changelog_section(version: str) -> str:
    lines = CHANGELOG.read_text(encoding="utf-8").splitlines()
    headings = [
        (index, match.group("version"))
        for index, line in enumerate(lines)
        if (match := re.fullmatch(r"## (?P<version>\d+\.\d+\.\d+)", line.strip()))
    ]
    if not headings:
        raise ValueError("No numeric release sections found in CHANGELOG.md")
    if headings[0][1] != version:
        raise ValueError(
            f"Top CHANGELOG.md section is {headings[0][1]}, expected {version}"
        )

    start = headings[0][0] + 1
    end = headings[1][0] if len(headings) > 1 else len(lines)
    body = "\n".join(lines[start:end]).strip()
    if not body:
        raise ValueError(f"CHANGELOG.md section {version} is empty")
    return body


def render_release_notes(version: str) -> str:
    notes = extract_changelog_section(version)
    return (
        f"## Print Farm {version}\n\n"
        f"{notes}\n\n"
        "Package checksums are available in `SHA256SUMS`.\n"
    )


def _wheel_record_digest(payload: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(payload).digest())
    return "sha256=" + digest.rstrip(b"=").decode("ascii")


def _canonical_core_metadata(version: str) -> bytes:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]
    authors = project.get("authors")
    if not isinstance(authors, list) or len(authors) != 1:
        raise ValueError("The wheel must declare exactly one project author")
    author = authors[0].get("name") if isinstance(authors[0], dict) else None
    fields = {
        "Name": project.get("name"),
        "Summary": project.get("description"),
        "Author": author,
        "Requires-Python": project.get("requires-python"),
    }
    if not all(isinstance(value, str) and value for value in fields.values()):
        raise ValueError("Wheel project metadata is incomplete")
    return (
        "Metadata-Version: 2.4\n"
        f"Name: {fields['Name']}\n"
        f"Version: {version}\n"
        f"Summary: {fields['Summary']}\n"
        f"Author: {fields['Author']}\n"
        f"Requires-Python: {fields['Requires-Python']}\n"
        "\n"
    ).encode("utf-8")


def _canonical_wheel_file() -> bytes:
    return (
        "Wheel-Version: 1.0\n"
        "Generator: print-farm build_package.py\n"
        "Root-Is-Purelib: true\n"
        "Tag: py3-none-any\n"
        "\n"
    ).encode("utf-8")


def _normalize_wheel_metadata(wheel_path: Path, version: str) -> None:
    """Rewrite the wheel as one cross-platform, byte-reproducible artifact."""
    with zipfile.ZipFile(wheel_path, "r") as archive:
        original = {name: archive.read(name) for name in archive.namelist()}

    dist_info = f"printers-{version}.dist-info"
    metadata_path = f"{dist_info}/METADATA"
    wheel_metadata_path = f"{dist_info}/WHEEL"
    top_level_path = f"{dist_info}/top_level.txt"
    record_path = f"{dist_info}/RECORD"
    expected = {
        "printers_plugin.py",
        metadata_path,
        wheel_metadata_path,
        top_level_path,
        record_path,
    }
    if set(original) != expected:
        raise ValueError(
            "Wheel contents differ from the dependency-free plugin contract: "
            f"{sorted(original)}"
        )

    payloads = {
        "printers_plugin.py": original["printers_plugin.py"],
        metadata_path: _canonical_core_metadata(version),
        wheel_metadata_path: _canonical_wheel_file(),
        top_level_path: b"printers_plugin\n",
    }
    rows = [
        [name, _wheel_record_digest(payloads[name]), str(len(payloads[name]))]
        for name in sorted(payloads)
    ]
    rows.append([record_path, "", ""])
    record_buffer = io.StringIO(newline="")
    csv.writer(record_buffer, lineterminator="\n").writerows(rows)
    payloads[record_path] = record_buffer.getvalue().encode("utf-8")

    temporary = wheel_path.with_suffix(wheel_path.suffix + ".tmp")
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_STORED) as archive:
            for name in sorted(payloads):
                entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                entry.compress_type = zipfile.ZIP_STORED
                entry.create_system = 3
                entry.external_attr = 0o100644 << 16
                archive.writestr(entry, payloads[name])
        temporary.replace(wheel_path)
    finally:
        temporary.unlink(missing_ok=True)


def _build_wheel(source_bytes: bytes, version: str, output_root: Path) -> Path:
    wheels_out = output_root / "wheels"
    wheels_out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="print-farm-wheel-") as temporary:
        build_dir = Path(temporary)
        (build_dir / "printers_plugin.py").write_bytes(source_bytes)
        shutil.copy2(ROOT / "pyproject.toml", build_dir / "pyproject.toml")
        subprocess.run(
            [sys.executable, "-m", "build", "--wheel", "--outdir", str(wheels_out)],
            cwd=build_dir,
            check=True,
        )
    wheel_path = wheels_out / f"printers-{version}-py3-none-any.whl"
    _normalize_wheel_metadata(wheel_path, version)
    return wheel_path


def build(output_root: Path, wheel: bool = True) -> Path:
    source = SOURCE.read_text(encoding="utf-8")
    ast.parse(source, filename=str(SOURCE))
    metadata = extract_metadata(source)
    plugin = metadata["tool"]["orcaslicer"]["plugin"]
    version = plugin["version"]
    runtime_version = extract_runtime_version(source)
    if runtime_version != version:
        raise ValueError(
            f"Metadata version {version!r} does not match PLUGIN_VERSION {runtime_version!r}"
        )
    if not DESCRIPTION.is_file():
        raise FileNotFoundError(f"Plugin Hub description is missing: {DESCRIPTION}")
    release_text = _source_with_embedded_helpers(_source_with_embedded_locales(source))
    violations = release_source_violations(release_text)
    if violations:
        raise ValueError("release plugin contains forbidden constructs: " + "; ".join(violations))
    release_bytes = release_text.encode("utf-8")

    package_dir = output_root / f"printers-{version}"
    _reset_output_dir(package_dir)
    package_path = package_dir / "printers_plugin.py"
    package_path.write_bytes(release_bytes)
    package_locales = package_dir / LOCALES.name
    if package_locales.exists():
        shutil.rmtree(package_locales)
    shutil.copytree(LOCALES, package_locales)
    package_description = package_dir / DESCRIPTION.name
    shutil.copy2(DESCRIPTION, package_description)

    digest = hashlib.sha256(release_bytes).hexdigest()
    checksum_lines = [f"{digest}  printers_plugin.py"]
    for locale_path in sorted(package_locales.glob("*.json")):
        locale_digest = hashlib.sha256(locale_path.read_bytes()).hexdigest()
        checksum_lines.append(
            f"{locale_digest}  {LOCALES.name}/{locale_path.name}"
        )
    checksum_lines.append(
        f"{hashlib.sha256(package_description.read_bytes()).hexdigest()}  {DESCRIPTION.name}"
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
        wheel_path = _build_wheel(release_bytes, version, output_root)
        print(wheel_path)
    return package_dir


def stage_release_metadata(output_root: Path, wheel_path: Path, version: str) -> tuple[Path, Path]:
    notes_path = output_root / "RELEASE_NOTES.md"
    notes_path.write_text(
        render_release_notes(version), encoding="utf-8", newline="\n"
    )
    relative_wheel = wheel_path.relative_to(output_root).as_posix()
    digest = hashlib.sha256(wheel_path.read_bytes()).hexdigest()
    checksum_path = output_root / "SHA256SUMS"
    checksum_path.write_text(
        f"{digest}  {relative_wheel}\n",
        encoding="utf-8",
        newline="\n",
    )
    return notes_path, checksum_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the Print Farm OrcaSlicer plugin package")
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output root (default: plugins/print-farm/dist/release-<current-version>)",
    )
    parser.add_argument(
        "--no-wheel",
        action="store_true",
        help="Only stage the package; skip building the wheel",
    )
    parser.add_argument(
        "--release-notes",
        type=Path,
        help="Write GitHub release notes from the current changelog section",
    )
    args = parser.parse_args()
    version = current_version()
    default_output = args.output is None
    output_root = (
        default_release_output_root()
        if default_output
        else args.output.resolve()
    )
    if default_output:
        _reset_output_dir(output_root)
    package_dir = build(output_root, wheel=not args.no_wheel)
    if not args.no_wheel:
        wheel_path = output_root / "wheels" / f"printers-{version}-py3-none-any.whl"
        notes_path, checksum_path = stage_release_metadata(
            output_root, wheel_path, version
        )
        print(notes_path)
        print(checksum_path)
    if args.release_notes is not None:
        notes_path = args.release_notes.resolve()
        notes_path.parent.mkdir(parents=True, exist_ok=True)
        notes_path.write_text(
            render_release_notes(version), encoding="utf-8", newline="\n"
        )
    print(package_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
