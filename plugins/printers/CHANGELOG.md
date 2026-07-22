# Print Farm plugin — changelog

Newest first. The top entry is the text pasted into the Plugin Hub on release.

## 0.0.3
- Bambu printers are now discovered over the local network (SSDP) instead of reading OrcaSlicer's config, which the plugin audit now blocks. Moonraker/OctoPrint discovery is unchanged.

## 0.0.2
- Outbox is vendor-agnostic: removed the misleading "sent by Orca" state and the separate Send button. Each sliced file has a single Print action that re-checks the printer's live status before starting.
