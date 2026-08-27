# Print Farm plugin — changelog

Newest first. The top entry is the text pasted into the Plugin Hub on release.

## Unreleased
- Added a per-printer local history modal for jobs started by Print Farm or observed while the dashboard is open, with explicit offline-coverage limits.
- Bundled complete interface catalogs for every current OrcaSlicer UI locale; printer protocols and raw backend diagnostics remain unchanged.

## 0.0.4
- Made add, remove, refresh and printer polling update independently without stale or returning tiles.
- Moonraker, OctoPrint and Bambu Lab now use complete status/control/upload backends; unfinished host types remain visible but disabled instead of pretending to work.
- Uses OrcaSlicer's private plugin storage when available, reuses bounded background workers and avoids temporary upload files that trigger extra permission requests.

## 0.0.3
- Bambu printers are now discovered over the local network (SSDP) instead of reading OrcaSlicer's config, which the plugin audit now blocks. Moonraker/OctoPrint discovery is unchanged.

## 0.0.2
- Outbox is vendor-agnostic: removed the misleading "sent by Orca" state and the separate Send button. Each sliced file has a single Print action that re-checks the printer's live status before starting.
