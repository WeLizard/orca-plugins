# Publishing FHSL — Hard Surface

This plugin lives in the shared `orca-plugins` monorepo and has an independent numeric version, changelog, and wheel. The import package (`fhslh`) is intentionally shorter than its stable distribution and plugin ID (`filamenthub_surface_hard_surface`). Its GitHub Release tag is `fhsl-hard-surface-vX.Y.Z`; the matching workflow builds and attaches only this plugin's wheel.

Build locally from the repository root with Python 3.12+ and Pillow installed:

```powershell
python tools/fhsl_package.py --plugin fhsl-hard-surface
```

The wheel, `SHA256SUMS`, and `RELEASE_NOTES.md` are written under `plugins/fhsl-hard-surface/dist/release-X.Y.Z/`. CI can pass `--output <release-root>` to write that same layout elsewhere.

GitHub releases are independent and do not by themselves update Orca Cloud. Current Cloud repository binding does not route one monorepo to multiple catalog entries; after the owner accepts the exact wheel in OrcaSlicer, upload it to the existing catalog entry manually until monorepo routing is supported.
