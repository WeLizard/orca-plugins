# Print Farm — cross-vendor printer dashboard (OrcaSlicer plugin)

A second, **independent** plugin built on OrcaSlicer's Python plugin system —
deliberately unrelated to the FilamentHub catalog plugin.

It opens a native **Print Farm** plugin page that shows supported networked
printers as tiles with live status, nozzle/bed temperatures and print progress,
and can open a printer's web interface in a confined host window. Older
OrcaSlicer builds retain the host-managed Script window fallback. OrcaSlicer's
own Multi-device tab is Bambu-only.

**Active testing:** this is an alpha plugin tested against exact OrcaSlicer PR
artifacts. The upstream plugin API is still evolving and updates may be frequent.

## Known OrcaSlicer UI limitation

The available-printer picker currently uses the native drop-down supplied by
OrcaSlicer's embedded WebView. If that drop-down is open while the OrcaSlicer
window is moved, its popup may remain at its original screen position until it
is closed. Selecting an item, pressing Esc, or clicking outside closes it; the
dashboard and printer data are unaffected.

## What it does

- Native `orca.pages.PagesPluginCapabilityBase` dashboard, with
  `create_window(...)` retained for hosts that do not expose Pages.
- Polls each printer on a worker thread and pushes status to the page:
  - **Moonraker** (Klipper): `/printer/objects/query` (temps, `print_stats`, progress).
  - **OctoPrint**: `/api/printer` + `/api/job`.
  - **Bambu Lab LAN**: MQTT status/control plus FTPS upload.
  - **Duet / RepRapFirmware (standalone and DSF)**: status, T0/T1 tool
    temperatures, upload to `0:/gcodes/` checked by size, and start (`M32`) only
    after a verified upload and the usual per-print confirmations. The protocol
    (RRF or DSF) seen on the first good poll is remembered per printer and a
    later change is refused instead of silently switching; saving the printer
    again re-detects it. Pause / resume / stop are not available (their wire
    form is not verified). Tested only against loopback emulators, not on a
    real Duet.
  - **Creality (stock firmware, Ender 5 Max style)**: read-only status over the
    printer's WebSocket (state, progress, file name, layers; the confirmed
    protocol has no temperatures, so they show as "—"). Enter the printer's
    upload address in the URL field (for example `http://192.168.x.x:PORT`) and,
    if needed, the optional WebSocket port. **The default WebSocket port 9999 is
    unconfirmed**; change it if your printer uses another one. No password is
    needed. Upload stays disabled ("not verified on hardware") because the
    writable upload target has not been confirmed on a real printer; start,
    pause and stop do not exist for this type. Tested only against loopback
    emulators.
  - **FlashForge (Adventurer 5M, 5M Pro, AD5X)**: read-only status over the
    printer's LAN HTTP API (port 8898): model (identified from the printer's
    model id, never from its editable name), firmware, state, nozzle and bed
    temperatures, progress, file name. Enter the printer address in the URL
    field, its **serial number** and its **check code** (both are on the
    printer screen) and turn **LAN mode on** at the printer. The check code is
    stored as a secret and is never sent back to the page; the serial number is
    an ordinary field. Other FlashForge models are shown as unsupported.
    **Closed until verified on hardware:** upload ("not verified on hardware"),
    starting a print, and pause / resume / stop (the plugin shows the reason, not
    an error). The upload and start code exists but the driver keeps it switched
    off until a model is confirmed on a real printer. The TCP fallback (port
    8899) is not used. Tested only against loopback emulators and synthetic
    fixtures; no real printer was contacted.
- Other OrcaSlicer host types remain visible as disabled planned integrations.
  They are not presented as working until status, control, and upload are
  implemented and can be tested.
- **Add / remove** printers in the page; mutable state uses
  `orca.host.plugin.storage()` when available and falls back to a stable
  directory outside the replaceable plugin package on older hosts.
- Authenticated OctoPrint instances can be added with an API key. Credentials
  remain in Python/private plugin storage; saved keys and Bambu access codes are
  never sent back to the dashboard page.
- **Open** → opens a sandboxed `<iframe>` restricted to that printer's exact
  HTTP(S) origin (Mainsail/Fluidd/OctoPrint remain framable on the LAN).
- Polls live status while the tab is open and exposes Pause / Resume / Stop when
  supported by the selected printer backend.
- Keeps the latest 500 locally observed print records. The dashboard button
  opens a searchable timeline across printers, with the file, printer, start
  time, result and recorded totals. Each printer tile opens the same journal
  for that printer.
  It records jobs observed while the dashboard is open; a successful upload or
  start request alone is not counted as a physical print. It cannot reconstruct
  jobs missed while OrcaSlicer was offline. The journal stays in local plugin
  storage.
- Sends local G-code or a selected plate from a sliced 3MF to one or several
  printers. Import, inspection and upload use bounded chunks; unsliced 3MF
  files are rejected. Upload does not start a print.
- Starting a print checks the file against each target and shows the result per
  printer: compatible, check manually, or incompatible, with the reason.
  Incompatible targets (unsliced project 3MF, a plate that is not in the file,
  G-code sent to a printer that starts sliced 3MF only, connections that cannot
  start prints yet) are excluded. A file is compatible when its printer profile
  is the OrcaSlicer preset the card was adopted from; renaming the card does not
  change this. Otherwise you confirm that the file is sliced for that exact
  printer. Every new print also needs a bed-clear confirmation, and each
  approval works once and only for the exact file, plate and target.
- Records a local operation intent before sending a file or control command.
  Uncertain requests are held for review and are not sent again automatically.
- Optional outbox records OrcaSlicer file-export and upload events on the matching
  printer tile. Select **Printers Outbox** in OrcaSlicer's **Slicing pipeline
  plugin** field for each process preset that should use it. The tile shows three
  files at a time with scrolling; its disclosure control expands the list to ten.
  Retained copies can be uploaded explicitly and removed separately.
  Copies are stored in separate folders named by each printer's stable target ID;
  files whose target cannot be determined stay in `outbox/unassigned`. Renaming
  a printer does not move its copies. The dashboard shows each printer's copy
  count and size and warns when retained storage needs review. Files queued
  for a printer are removed one by one. Leftovers (loose files, prepared
  uploads, legacy recovery copies and the index backup) can be deleted with one
  button after a confirmation, only while no upload or print operation is active
  and the file index is valid. Nothing is deleted automatically. Prepared import
  copies are temporary and are released after sending, on cancellation, or when
  the dashboard closes.
- Storage tied to OrcaSlicer's signed-in plugin account fails closed when that
  storage is unavailable. A previous shared local store is imported only after
  an explicit preview and confirmation; the source and recovery copies remain
  available for manual inspection.

## Upstream integration coverage

- **Plugin page:** Pages is the primary capability when present; capability
  detection keeps the Script window working on supported older hosts.
- **Local-network access:** printers live on user-chosen LAN addresses no static
  manifest allow-list can enumerate — concrete motivation for a future
  `network = ["local-network"]` permission class (declared here, ignored today).

## Files

- `printers_plugin.py` — the dependency-free plugin runtime. The wheel embeds
  the focused helper modules and requires no additional user-installed Python
  packages.
- `printers_locales/` — bundled interface translations for every current
  OrcaSlicer UI locale, with per-key English fallback.
- `printers.svg` — tab icon (drop one in; absent → host default icon).
- `validate_locales.py` / `TRANSLATING.md` — catalog validation and community workflow.
- `description.md` — Orca Cloud listing text.
- `CHANGELOG.md` — release notes owned by this plugin.
- `build_package.py` / `PUBLISHING.md` — deterministic package and release workflow.
