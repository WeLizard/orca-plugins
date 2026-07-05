# OrcaSlicer Plugins

A personal repository for developing and testing custom Python plugins for **OrcaSlicer**.

## Repository Layout

Each plugin is located in its own directory:

*   **`plugins/filamenthub/`** — Cloud-native material catalog and spool management extension.

## Local Installation

To load a plugin into OrcaSlicer:

1. Locate the native plugins configuration directory:
   * **Windows:** `%APPDATA%/OrcaSlicer/orca_plugins/`
   * **Linux:** `~/.config/OrcaSlicer/orca_plugins/`
2. Copy the specific plugin folder directly into that directory.
3. Restart OrcaSlicer to let the built-in Plugin Manager discover the manifest.

## License

Open-source under the **AGPL v3** license.
