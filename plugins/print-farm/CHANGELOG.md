# Print Farm plugin — changelog

Newest first. The top entry is the text pasted into the Plugin Hub on release.

## 0.1.1
- Added per-printer Connection options: a separate Web UI address on the same host and an explicitly selected stock Snapmaker U1 camera mode. This camera runs only while its overlay is open, closes on cancellation or lease expiry, keeps keys out of the page and clears failed or expired frames. Tested on local emulators; hardware acceptance remains pending.
- Embedded Web UI windows show a selectable address and browser instructions when the page cannot load. Feedback provides the fixed FilamentHub site address and directions to its existing Beta Feedback button; no printer data is sent.
- Moonraker printers now show all discovered extruder temperatures and the active tool reported by the printer. Clicking a head shows its details without changing the physical tool. Missing head readings stay unknown. Tested with local fixtures; hardware acceptance remains pending.
- Added Repetier-Server with explicit printer selection by name and slug, status and temperatures, and classic server-managed job upload/start/control. New or unknown connector modes stay read-only. Start requests may wait in the server queue; physical printing is confirmed only by status. Tested with local emulators, not hardware.
- Newly entered printer keys use the system credential store (Windows Credential Manager, macOS Keychain, Linux Secret Service). Existing plaintext keys can be moved with explicit confirmation; old backups remain. An unavailable or locked credential store blocks printer connections until you retry access.
- Bambu connections now require you to confirm the printer's certificates before connecting: status and commands (MQTT) and file transfers (FTPS) are confirmed separately. Later connections check the saved certificates before sending an access code or file. A changed certificate blocks the connection until you explicitly confirm the replacement; a rejected operation is never replayed automatically. The first confirmation is a trust decision, not proof of the printer's identity.
- Background work now reports a denied OrcaSlicer thread permission immediately, so a rejected printer command does not remain queued and a later retry can start normally.
- LAN discovery still offers Bambu printers, but network announcements no longer save a printer name, serial or model for automatic slice matching. Matching uses a name you entered or an OrcaSlicer preset, or a serial/model observed through the configured printer connection; older unverified saved values are ignored for that purpose.
- The cleanup timer can be retried after a permission denial without interrupting the dashboard.
- Loading the plugin no longer starts a background thread, so OrcaSlicer's permission question about threads now appears once, when you first open Print Farm, instead of twice at start-up.
- A Bambu printer whose certificate is not confirmed yet now says so on its card and highlights **Certificates…**, instead of silently showing the printer as offline.
- The Feedback window has a **Copy** button next to the site address and confirms when the address is copied.

## 0.0.10
- Removed the system `curl` fallback for Bambu uploads. Files already went to Bambu printers over the plugin's own FTPS client, so nothing changes for you; the plugin no longer starts any other program.
- Security review of what the plugin does: the shipped plugin no longer contains dynamic code execution (the bundled helper modules are plain code now), indirect `os` calls, environment reads or `ftplib` loaded at start-up (it loads on the first Bambu upload). The build refuses a plugin that contains such constructs.
- Added a "What the plugin does on your computer" section to the description: which addresses it contacts, what it sends, what it reads from OrcaSlicer, where it stores data and keys (plain text), what runs at load.
- Merged the storage block into the print journal: one list, newest first. A print or send made from Print Farm that has a stored copy offers "Send again…" and "Delete file"; slice copies that were never sent appear as "Slice, not sent" with "Send to printer…" and "Delete". A copy is tied to a journal entry only by the SHA-256 that Print Farm recorded when it sent that exact file; Orca events are never matched to files by guesswork. Every send keeps the same approval, bed-clear and file checks, and the journal search finds copies by name. The bottom storage block and the "queued files match no printer" status line are gone: a quiet link to the journal remains, the 500 MB size warning shows only when exceeded, and "Delete leftovers" appears in the journal only when there are leftovers. The newest three copies under each printer card are unchanged (now newest first).
- A Bambu printer that reports a finished job can take the next one (select, Print… and mass send), still with the clear-bed confirmation and every other check; failed and error states stay blocked, and the state is shown as completed. Cards no longer leave an empty gap where Pause and Stop are hidden.
- Printer cards are more compact: when a printer is not printing, "Select", "Print history", "Print…", the camera icon and "Open" share one wrapping row instead of two; while it prints or is paused, "Print history" stays beside Pause/Stop.
- Added a Settings tab for both capabilities (checkboxes and lists instead of raw JSON, localized): keep slice copies, record Orca sends/prints in the journal, the profile hint, optional deletion of copies older than 30/90/180/365 days (off by default), print history on/off, printer polling interval 5/15/30/60 s, pause polling while the panel is hidden, and a confirmed "Clear print history" button. Defaults keep the previous behaviour.
- Slices are easier to tell apart: the fallback name of a slice now includes seconds, and each copy under a printer card shows its size and the time it was added. New setting "Keep only the newest slices per printer" (All by default, or 3/5/10/20): when chosen, storing a new slice deletes that printer's oldest unsent copies beyond the limit; copies tied to the print journal and anything in use by an active operation are never removed. Existing copies are not renamed.
- If the plugin cannot save a slice (storage error), it now skips quietly instead of failing, so OrcaSlicer's export or upload still goes through.
- A new slice is matched to a printer card by the preset's printer address (or FlashForge serial) before the profile name; an address shared by several cards leaves it unassigned. The printer profile is read from OrcaSlicer's slicing context when available, otherwise from the G-code. No keys are read.
- A slice for a Bambu printer is matched to its card by the model code (from the printer's SSDP announcement or MQTT report, or X1 aliases) when nothing else identifies it: exactly one Bambu card with that model, any nozzle size; several cards of one model leave it unassigned. The start check stays "check manually". Not checked on a real printer.
- An identical file for the same printer (or unassigned) is stored once; repeated hook calls just refresh it.
- The interface language is read from OrcaSlicer's app info when available.
- API keys of printers adopted from OrcaSlicer presets are no longer saved in the plugin's printer list; the key comes from the preset (a key already saved stays until the preset supplies one).
- Files in "Not linked to a printer" can now be sent once to a printer you pick (online, idle, accepts uploads): "Upload only" or "Upload and start", with an in-page check that the file was sliced for that printer (and a clear bed before a start); nothing is linked. The printer profile is also read from the start of the G-code, where OrcaSlicer 2.5 writes it.
- Added Duet / RepRapFirmware (standalone and DSF) with tool temperatures, size-verified upload and a confirmed start that runs only after a verified upload. The detected protocol is pinned per printer; pause, resume and stop stay unavailable. Verified on loopback emulators only, not on a real printer.
- Added read-only Creality (stock firmware) status over WebSocket with an optional port (9999 is an unconfirmed default). Upload is shown as unavailable until the upload target is verified on hardware; verified on loopback emulators only.
- Added FlashForge (Adventurer 5M, 5M Pro, AD5X) status over the LAN HTTP API: model, firmware, temperatures and progress, using the printer serial number and check code (LAN mode must be on). Upload, start, pause, resume and stop stay off until verified on a real printer, and the card shows why. Verified on loopback emulators and synthetic fixtures only.
- Bambu (LAN) cards now show what the printer already reports: time left, layer, AMS spools (type, colour, remaining %) and a printer error code; each row appears only when the data is present. Not checked on a real printer.
- Printing and paused cards of every type now show time left and layer when the printer reports them (Moonraker: estimated from print time and progress; OctoPrint: time left only; Duet: from the object model; Creality, FlashForge, Bambu: as reported); nothing is shown when the data is missing. Not checked on real printers.
- Moonraker and OctoPrint cards have a camera button that opens a live view in an overlay (MJPEG, or a snapshot refreshed every 2 s); the camera address is read only on click, must be on the printer's own host, and the stream stops as soon as the overlay closes. If the picture does not load in Orca's window, "Open camera in window" shows it in a separate one. Not checked on real printers.
- Print Farm cards hide "select" and "Print..." while a printer is printing or paused (other reasons keep the disabled button with an explanation); OctoPrint without a configured webcam, or Moonraker without a usable one, now says "no camera" instead of guessing an address, and the camera icon disappears from that card until the panel reloads; the storage block is collapsed to one line by default and lists files newest first per printer (5 at a time) with "Delete all" for files not linked to a printer.
- New slices get a readable file name instead of the slicer's hidden temporary one, the storage block no longer lists service files or repeats totals, files not linked to any printer are shown as a compact list with Delete (confirmed in the page) and the reason they cannot be sent; Moonraker thumbnails in subfolders and after a failed first read now load.
- Marked operations left half-sent by a crash as unknown when the plugin starts (nothing is re-sent); they stay on hold until you release them from the print journal, and a blocked printer now says so.
- Made the Duet protocol pin strict: a printer pinned to standalone RRF never sends its password to the DSF API, and a session the firmware opens with an unusable key is closed. IDEX tool temperatures are read from tool numbers, not array positions.
- Cancelled an operation plan that never reached dispatch (a failed dispatch claim, or one left over from a crash) so it no longer blocks its printer or "Delete leftovers".
- Made "use AMS" an explicit opt-in: an absent or non-true value means off both when a start is approved and when it is sent.
- Not verified on real hardware: Creality, FlashForge, Duet (RRF, DSF, IDEX) and PrusaLink were tested against loopback emulators and synthetic fixtures only; no printer was contacted.
- Kept plugin state out of the installation directory until OrcaSlicer provides its storage location.
- Checked compatibility before a print starts and showed it per printer
  (compatible, check manually, incompatible) with the exact reason. A file is
  compatible only when its printer profile matches the OrcaSlicer preset the
  card was adopted from; an editable card name is never evidence. Unknown
  targets need an explicit "sliced for this printer" check, every new print needs
  a bed-clear confirmation, and both are part of the approved plan and the
  operation journal. Unsliced projects, wrong plates and unsuitable file kinds
  are blocked. Upload without starting remains available.
- Preserved retained outbox copies after transport acceptance; only explicit
  removal deletes them. Released temporary import copies after sending,
  cancellation, or dashboard close; uncertain outcomes remain in the journal.
- Accepted sliced G-code and multi-plate 3MF files through bounded transfers, preserved the selected plate, and streamed uploads without loading the whole file into memory.
- Recorded printer actions before sending them and held uncertain outcomes for operator review instead of automatically repeating a request.
- Let actions for different printers proceed independently, so a long upload does not delay a control request for another printer.
- Verified TLS certificates by default for HTTPS printers with a per-printer exception. Bambu LAN endpoints present a self-signed certificate, so they keep working without verification as before.
- Added a searchable print journal across printers with a dashboard button,
  printer-specific views and a summary of locally recorded jobs.
- Stored retained slices in separate stable printer folders, kept ambiguous files unassigned, and added per-printer size/count warnings.
- Added a "Delete leftovers" action for loose files, prepared uploads, legacy recovery copies and the index backup. It asks first, runs only while no upload or print operation is active and the migrated file index is valid, never follows links or leaves the plugin storage, and never touches queued printer files or the index.
- Built the connection list and the page's backend checks from one registry, so enabling a connection takes a single entry; the release builder now embeds every helper module the plugin loads.
- Made legacy local-storage import an explicit, snapshot-bound action with recoverable copies and no automatic cross-account fallback.
- Kept distinct OrcaSlicer presets visible when they use the same server address.
- Separated transport, device, job, authorization and freshness observations; kept incomplete readings unknown and blocked writes when the selected backend or target binding changed.
- Stopped status and command requests from switching to another printer protocol after an authentication or route failure.
- Gave saved printers stable target identities and binding revisions, preserved separate targets on one server, and rejected stale or ambiguous actions before dispatch.
- Kept outbox files bound to an exact target when known, and rejected unsafe stored paths.
- Kept account-denied storage from activating background work or reading previous account files; preserved damaged outbox indexes.
- Limited dashboard payloads to the fields used by the interface so legacy or nested private fields stay in local storage.
- Left queued files unassigned when printer names are only similar or duplicated, and rejected a start targeting a different printer.
- Blocked OctoPrint print starts when the live job response is missing, incomplete, or disagrees with an idle printer state.
- Kept missing progress values unknown instead of displaying a false zero percent.
- Preserved malformed printer records instead of silently dropping them during a later save.

- Required a confirmed idle state from the configured printer backend before starting a print; incomplete status and backend mismatches now block the action.
- Sent the saved Moonraker API key with status, thumbnail, upload and control requests without retrying authentication failures through another backend.
- Kept damaged printer records untouched and stopped storage fallback when the host denies its account storage.
- Updated the Print Farm icon.
- Kept printer cards compact: queued files show three rows by default, scroll to the rest, and expand to ten rows on demand.
- Matched dropdown arrows across the dashboard and showed Bambu connection failures as a short offline state with details on hover.
- Grouped the compact Name and IP fields with the other add-printer controls.
- The journal now also lists files sent or printed from OrcaSlicer itself ("reported by OrcaSlicer"): a send shows as "sent" (not a print), a print by its outcome. The printer is matched by the device serial; an unknown device is still listed, without a printer.
- A short line appears when a slice finished but the Printers Outbox plugin is not selected in the process profile (Slicing pipeline plugin), and disappears once it runs.
- A saved slice takes the model name reported by OrcaSlicer instead of a dated placeholder when the export name is only a temporary one.

## 0.0.9
- Moved printer removal away from the Open action, made it visually quieter, and added a second-click confirmation.
- Made card action buttons size themselves from translated labels and wrap cleanly on narrow cards.
- Made unavailable Select controls visibly muted and explain why selection is disabled.

## 0.0.8
- Restored the available-printer list after plugin lifecycle reloads: Orca-configured printers appear immediately and LAN Bambu discovery resumes automatically.

## 0.0.7
- Fixed OrcaCloud release delivery; plugin behavior is unchanged from 0.0.6.

## 0.0.6
- Restored OrcaCloud delivery through the connected GitHub release workflow; plugin behavior is unchanged from 0.0.5.

## 0.0.5
- Fixed Print Farm wheel installation and startup on Windows builds of OrcaSlicer.
- Fixed dashboard rendering and printer selection so saved Voron/Bambu devices appear immediately, repeated choices do not create duplicates, and the native tab always has its Print Farm icon.
- Added bounded local print history for each printer, covering jobs started by Print Farm and jobs observed while the dashboard is open.
- Added complete interface translations for every current OrcaSlicer language; printer protocols and raw diagnostics remain in English.
- Added a native OrcaSlicer plugin tab while keeping the dashboard available on supported older builds.
- Preserved printers, history, and queued files across plugin updates and stopped queued LAN actions when the plugin is disabled or updated.
- Kept embedded printer interfaces inside their selected HTTP(S) site so links cannot replace the OrcaSlicer page.
- Authenticated OctoPrint instances can now be added manually, while saved OctoPrint and Bambu credentials remain in Python private storage and are never reflected into the dashboard page.
- Available OrcaSlicer and LAN printers now populate when Print Farm opens or a printer is removed, and the manual connection fields stay in a compact expandable row.

## 0.0.4
- Made add, remove, refresh and printer polling update independently without stale or returning tiles.
- Moonraker, OctoPrint and Bambu Lab now use complete status/control/upload backends; unfinished host types remain visible but disabled instead of pretending to work.
- Uses OrcaSlicer's private plugin storage when available, reuses bounded background workers and avoids temporary upload files that trigger extra permission requests.

## 0.0.3
- Bambu printers are now discovered over the local network (SSDP) instead of reading OrcaSlicer's config, which the plugin audit now blocks. Moonraker/OctoPrint discovery is unchanged.

## 0.0.2
- Outbox is vendor-agnostic: removed the misleading "sent by Orca" state and the separate Send button. Each sliced file has a single Print action that re-checks the printer's live status before starting.
