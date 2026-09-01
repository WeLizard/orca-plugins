# Publishing Print Farm

Print Farm is released independently from every other plugin in this
repository. Its version is defined by `PLUGIN_VERSION` and the PEP 723 metadata
in `printers_plugin.py`; both values must match the newest numeric section in
`CHANGELOG.md`.

## Build

```powershell
python build_package.py --release-notes dist/RELEASE_NOTES.md
```

The command creates the installable wheel under `dist/wheels/`, a staged package
under `dist/printers-X.Y.Z/`, checksums and release notes derived only from this
directory's changelog. The source checkout is the development form; Print Farm
has no separate localhost/prod backend contour, so a second byte-identical dev
wheel is intentionally not produced. As with the FilamentHub plugin, the wheel
contains one Python module with the validated locale catalogs embedded; the
staged package also keeps the standalone JSON files for review and translation.

## Release

1. Verify the wheel in a compatible OrcaSlicer build.
2. Confirm no other plugin directory currently declares the same release
   version.
3. Create and push the exact Orca Cloud tag `vX.Y.Z`.
   The tag must contain the current trusted-publishing workflow. Published tags
   are immutable: if an older tag contains an invalid workflow, create a new
   plugin version instead of repointing or republishing that tag.
4. The Print Farm workflow tests and builds only `plugins/printers/`, creates
   the matching draft GitHub release and uploads the verified assets.
5. After checking the draft assets, publish that GitHub release through the
   owner's authenticated session. The resulting `release.published` event
   starts the connected Orca Cloud trusted-publishing workflow.
6. Verify the GitHub release, Orca Cloud card and downloadable wheel before
   announcing the update.

The owner performs the push and final publication checks.
