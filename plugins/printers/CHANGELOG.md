# Print Farm plugin — changelog

Newest first. The top entry is the text pasted into the Plugin Hub on release.

## Unreleased

## 0.0.5
- Added bounded local print history for each printer, covering jobs started by Print Farm and jobs observed while the dashboard is open.
- Added complete interface translations for every current OrcaSlicer language; printer protocols and raw diagnostics remain in English.
- Added a native OrcaSlicer plugin tab while keeping the dashboard available on supported older builds.
- Preserved printers, history, and queued files across plugin updates and stopped queued LAN actions when the plugin is disabled or updated.
- Kept embedded printer interfaces inside their selected HTTP(S) site so links cannot replace the OrcaSlicer page.

## 0.0.4
- Made add, remove, refresh and printer polling update independently without stale or returning tiles.
- Moonraker, OctoPrint and Bambu Lab now use complete status/control/upload backends; unfinished host types remain visible but disabled instead of pretending to work.
- Uses OrcaSlicer's private plugin storage when available, reuses bounded background workers and avoids temporary upload files that trigger extra permission requests.

## 0.0.3
- Bambu printers are now discovered over the local network (SSDP) instead of reading OrcaSlicer's config, which the plugin audit now blocks. Moonraker/OctoPrint discovery is unchanged.

## 0.0.2
- Outbox is vendor-agnostic: removed the misleading "sent by Orca" state and the separate Send button. Each sliced file has a single Print action that re-checks the printer's live status before starting.
