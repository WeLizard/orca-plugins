# OrcaSlicer Plugins

Custom Python plugins for **OrcaSlicer**'s plugin system (PR #14530), built on the
`orca` host API. Self-contained, single-file plugins with no third-party
dependencies.

## Repository Layout

Each plugin lives in its own directory under `plugins/`:

*   **`plugins/printers/`** — cross-vendor dashboard of your networked 3D printers:
    live status, temperatures and print progress for Moonraker / OctoPrint /
    generic hosts, with each printer's web interface one click away. OrcaSlicer's
    own Multi-device tab is Bambu-only; this covers everyone.

## Local Installation

To load a plugin into OrcaSlicer:

1. Locate the native plugins directory:
   * **Windows:** `%APPDATA%/OrcaSlicer/orca_plugins/`
   * **Linux:** `~/.config/OrcaSlicer/orca_plugins/`
2. Copy the plugin's folder (e.g. `plugins/printers/`) into that directory.
3. Restart OrcaSlicer so the built-in Plugin Manager discovers the manifest.

## License

Open-source under the **AGPL v3** license.
