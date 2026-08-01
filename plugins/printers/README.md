# Print Farm — cross-vendor printer dashboard (OrcaSlicer plugin)

A second, **independent** plugin built on OrcaSlicer's Python plugin system —
deliberately unrelated to the FilamentHub catalog plugin.

It opens a **Print Farm** dashboard that shows supported networked printers as
tiles with live status, nozzle/bed temperatures and print progress, and can open
a printer's web interface in place. It currently uses the stable host-managed
window API. Native Pages from draft PR #14992 are tracked separately and are
not treated as a production contract yet. OrcaSlicer's own Multi-device tab is
Bambu-only.

**Active testing:** this is an alpha plugin tested against exact OrcaSlicer PR
artifacts. The upstream plugin API is still evolving and updates may be frequent.

## What it does

- `create_window(...)` → a host-managed, host-themed dashboard.
- Polls each printer on a worker thread and pushes status to the page:
  - **Moonraker** (Klipper): `/printer/objects/query` (temps, `print_stats`, progress).
  - **OctoPrint**: `/api/printer` + `/api/job`.
  - **Bambu Lab LAN**: MQTT status/control plus FTPS upload.
- Other OrcaSlicer host types remain visible as disabled planned integrations.
  They are not presented as working until status, control, and upload are
  implemented and can be tested.
- **Add / remove** printers in the page; mutable state uses
  `orca.host.plugin.storage()` when available and falls back to the install
  directory on older hosts.
- **Open** → swaps the grid for an `<iframe>` of the printer's web UI
  (Mainsail/Fluidd/OctoPrint are framable on the LAN).
- Polls live status while the tab is open and exposes Pause / Resume / Stop when
  supported by the selected printer backend.
- Sends one local G-code to one or several printers.
- Optional outbox records OrcaSlicer file-export and upload events on the matching
  printer tile. Select **Printers Outbox** in OrcaSlicer's **Slicing pipeline
  plugin** field for each process preset that should use it. Files already handed
  to a print host are marked **sent by Orca** and cannot be uploaded a second
  time; local exports retain Send / Print actions.

## Upstream integration coverage

- **Plugin page candidate:** `printers.svg` is packaged for the Pages capability
  icon path from draft PR #14992; adopting that capability remains gated by an
  exact artifact test and a host-owned tab/window presentation choice.
- **Local-network access:** printers live on user-chosen LAN addresses no static
  manifest allow-list can enumerate — concrete motivation for a future
  `network = ["local-network"]` permission class (declared here, ignored today).

## Files

- `printers_plugin.py` — the dependency-free plugin runtime.
- `printers_locales/` — bundled interface translations with English fallback.
- `printers.svg` — tab icon (drop one in; absent → host default icon).
- `validate_locales.py` / `TRANSLATING.md` — catalog validation and community workflow.
