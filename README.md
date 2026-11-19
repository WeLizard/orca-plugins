# OrcaSlicer Plugins

Custom Python plugins for **OrcaSlicer**'s plugin system (PR #14530), built on the
`orca` host API.

## Repository Layout

Each plugin lives in its own directory under `plugins/`:

*   **[`plugins/print-farm/`](plugins/print-farm/)** — cross-vendor dashboard of your networked 3D printers:
    live status, temperatures and print progress for Moonraker / OctoPrint /
    generic hosts, with each printer's web interface one click away. OrcaSlicer's
    own Multi-device tab is Bambu-only; this covers everyone. Its `vX.Y.Z`
    GitHub release and trusted Orca Cloud publication are automated.
*   **[`plugins/bumpmesh/`](plugins/bumpmesh/)** — experimental integration for
    transferring current Orca model geometry into BumpMesh by CNC Kitchen
    through the Plugin Pages API. It keeps a manual release kit pending CNC
    Kitchen's decision about taking over the integration.

*   **FHSL texture libraries** — three independent texture packs with checked image inventories and deterministic builds.
    * [Hard Surface](plugins/fhsl-hard-surface/) — 53 height maps.
    * [Nature & Organics](plugins/fhsl-nature-organics/) — 38 height maps.
    * [Textiles & Materials](plugins/fhsl-textiles-materials/) — 48 height maps.
    Their prefixed GitHub tags build one pack per draft release.

Every plugin directory owns its runtime source, version, `README.md`,
`description.md`, `CHANGELOG.md`, package builder, tests and publishing notes.
Plugins are built and released independently; a release never takes notes or
assets from another plugin directory.

## Local Installation

To load a plugin into OrcaSlicer:

1. Locate the native plugins directory:
   * **Windows:** `%APPDATA%/OrcaSlicer/orca_plugins/`
   * **Linux:** `~/.config/OrcaSlicer/orca_plugins/`
2. Copy the plugin's folder (e.g. `plugins/print-farm/`) into that directory.
3. Restart OrcaSlicer so the built-in Plugin Manager discovers the manifest.

## Publishing

The Plugin Hub accepts manifest versions only as three numeric components in
`X.Y.Z` form (for example, `0.1.0`). Pre-release or build suffixes such as
`0.1.0-alpha.1`, `0.1.0-rc1`, and `0.1.0+build.4` are rejected. Mark alpha or
beta maturity in the listing text instead of the version field.

Orca Cloud release tags must use the exact `vX.Y.Z` form. Because Git tags are
repository-wide, a version used for a release must not be ambiguous between
plugin directories. FHSL releases use `fhsl-hard-surface-vX.Y.Z`, `fhsl-nature-organics-vX.Y.Z`,
and `fhsl-textiles-materials-vX.Y.Z`. These prefixes leave Print Farm releases
independent. Automated trusted publishing is currently configured only
for Print Farm. BumpMesh keeps its own manual publishing kit and instructions
until CNC Kitchen decides whether to take over the integration.

## FHSL builds and diagnostics

The release builder needs Python 3.12+ and Pillow 11.3.0. From this checkout:

```powershell
python tools/fhsl_package.py --plugin fhsl-hard-surface
python -m unittest discover -s tests -p 'test_fhsl*.py' -v
```

Each build writes `wheels/`, `SHA256SUMS` and `RELEASE_NOTES.md` under the
selected plugin's `dist/release-X.Y.Z/`. The builder checks the full image
contents and exact texture manifest before auditing the wheel. The internal
import modules are short (`fhslh`, `fhsln`, `fhslt`, maps in `t/`) to keep installed
Windows paths well below 260 characters. Distribution identities and existing library capability
names are preserved. PNG L8 format
alone does not establish that every OrcaSlicer installation loads the full pack.

For missing-texture reports, run the read-only inspector with the data directory
shown by OrcaSlicer's configuration-folder command:

```powershell
python tools/inspect_fhsl.py --data-dir '<OrcaSlicer data directory>' > fhsl-report.json
```

The report lists installed copies, their enabled state and versions, texture
counts and wheel/extracted-file integrity. Account IDs are redacted and paths
are relative. PNG pixel decoding and the running host's capability registry
are outside the inspector's scope. A failed Windows native open probe is
reported separately from OrcaSlicer's own behavior.

Orca Cloud currently permits one connected repository per plugin. FHSL GitHub
release preparation is available here. Automatic delivery of these three
independent listings from this monorepo awaits upstream routing support.
After testing the exact candidate, the owner can upload its wheel manually.

## License

Open-source under the **AGPL v3** license.
