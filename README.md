# OrcaSlicer Plugins

Custom Python plugins for **OrcaSlicer**'s plugin system (PR #14530), built on the
`orca` host API.

## Repository Layout

Each plugin lives in its own directory under `plugins/`:

*   **[`plugins/printers/`](plugins/printers/)** — cross-vendor dashboard of your networked 3D printers:
    live status, temperatures and print progress for Moonraker / OctoPrint /
    generic hosts, with each printer's web interface one click away. OrcaSlicer's
    own Multi-device tab is Bambu-only; this covers everyone. Its `vX.Y.Z`
    GitHub release and trusted Orca Cloud publication are automated.
*   **[`plugins/bumpmesh/`](plugins/bumpmesh/)** — experimental integration for
    transferring current Orca model geometry into BumpMesh by CNC Kitchen
    through the Plugin Pages API. It keeps a manual release kit pending CNC
    Kitchen's decision about taking over the integration.

Every plugin directory owns its runtime source, version, `README.md`,
`description.md`, `CHANGELOG.md`, package builder, tests and publishing notes.
Plugins are built and released independently; a release never takes notes or
assets from another plugin directory.

## Local Installation

To load a plugin into OrcaSlicer:

1. Locate the native plugins directory:
   * **Windows:** `%APPDATA%/OrcaSlicer/orca_plugins/`
   * **Linux:** `~/.config/OrcaSlicer/orca_plugins/`
2. Copy the plugin's folder (e.g. `plugins/printers/`) into that directory.
3. Restart OrcaSlicer so the built-in Plugin Manager discovers the manifest.

## Publishing

The Plugin Hub accepts manifest versions only as three numeric components in
`X.Y.Z` form (for example, `0.1.0`). Pre-release or build suffixes such as
`0.1.0-alpha.1`, `0.1.0-rc1`, and `0.1.0+build.4` are rejected. Mark alpha or
beta maturity in the listing text instead of the version field.

Orca Cloud release tags must use the exact `vX.Y.Z` form. Because Git tags are
repository-wide, a version used for a release must not be ambiguous between
plugin directories. Automated trusted publishing is currently configured only
for Print Farm. BumpMesh keeps its own manual publishing kit and instructions
until CNC Kitchen decides whether to take over the integration.

## License

Open-source under the **AGPL v3** license.
