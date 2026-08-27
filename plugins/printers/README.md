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

## What it does

- Native `orca.pages.PagesPluginCapabilityBase` dashboard, with
  `create_window(...)` retained for hosts that do not expose Pages.
- Polls each printer on a worker thread and pushes status to the page:
  - **Moonraker** (Klipper): `/printer/objects/query` (temps, `print_stats`, progress).
  - **OctoPrint**: `/api/printer` + `/api/job`.
  - **Bambu Lab LAN**: MQTT status/control plus FTPS upload.
- Other OrcaSlicer host types remain visible as disabled planned integrations.
  They are not presented as working until status, control, and upload are
  implemented and can be tested.
- **Add / remove** printers in the page; mutable state uses
  `orca.host.plugin.storage()` when available and falls back to a stable
  directory outside the replaceable plugin package on older hosts.
- **Open** → opens a sandboxed `<iframe>` restricted to that printer's exact
  HTTP(S) origin (Mainsail/Fluidd/OctoPrint remain framable on the LAN).
- Polls live status while the tab is open and exposes Pause / Resume / Stop when
  supported by the selected printer backend.
- Keeps a bounded local history for each printer. It records jobs started from
  Print Farm and jobs actually observed while the dashboard is open; it does
  not invent jobs that ran while OrcaSlicer was offline.
- Sends one local G-code to one or several printers.
- Optional outbox records OrcaSlicer file-export and upload events on the matching
  printer tile. Select **Printers Outbox** in OrcaSlicer's **Slicing pipeline
  plugin** field for each process preset that should use it. Files already handed
  to a print host are marked **sent by Orca** and cannot be uploaded a second
  time; local exports retain Send / Print actions.

## Upstream integration coverage

- **Plugin page:** Pages is the primary capability when present; capability
  detection keeps the Script window working on supported older hosts.
- **Local-network access:** printers live on user-chosen LAN addresses no static
  manifest allow-list can enumerate — concrete motivation for a future
  `network = ["local-network"]` permission class (declared here, ignored today).

## Files

- `printers_plugin.py` — the dependency-free plugin runtime.
- `printers_locales/` — bundled interface translations for every current
  OrcaSlicer UI locale, with per-key English fallback.
- `printers.svg` — tab icon (drop one in; absent → host default icon).
- `validate_locales.py` / `TRANSLATING.md` — catalog validation and community workflow.
- `description.md` — Orca Cloud listing text.
- `CHANGELOG.md` — release notes owned by this plugin.
- `build_package.py` / `PUBLISHING.md` — deterministic package and release workflow.
