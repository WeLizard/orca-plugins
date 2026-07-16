# Printers — cross-vendor printer dashboard (OrcaSlicer plugin)

A second, **independent** plugin built on OrcaSlicer's Python plugin system
(PR **#14530**) — deliberately unrelated to FilamentHub. It also exercises the
proposed generic panel contribution (`orca.host.ui.create_panel`) when that API
is available, while remaining usable through the stock host window API.

It opens a **Printers** dashboard that shows all your networked printers as
tiles — live status, nozzle/bed temperatures and print progress — regardless of
brand or firmware, and opens any printer's web interface in place. On builds
with the proposed panel API it docks as a main-window tab. OrcaSlicer's own
Multi-device tab is Bambu-only; this covers everyone.

**Active testing:** this is an alpha plugin tested against OrcaSlicer PR #14530
artifacts. The upstream plugin API is still evolving and updates may be frequent.

## What it does (on stock PR #14530 capabilities — no new host API)

- `create_window(...)` → a host-managed, host-themed dashboard; optional
  `create_panel(...)` support is detected at runtime for experimental builds.
- Polls each printer on a worker thread and pushes status to the page:
  - **Moonraker** (Klipper): `/printer/objects/query` (temps, `print_stats`, progress).
  - **OctoPrint**: `/api/printer` + `/api/job`.
  - **Other**: a reachability ping.
- **Add / remove** printers in the page; stored in `printers.json` next to the
  plugin (inside `data_dir`, the allowed write root).
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

## Why it strengthens the PR ask

- **Second surface, same slot:** demonstrates how `create_panel` can be reusable
  across unrelated plugins without making it a runtime requirement yet.
- **Plugin-supplied tab icon** (`printers.svg` materialized in the plugin dir)
  exercises the optional `create_panel(icon=...)` path.
- **Local-network access:** printers live on user-chosen LAN addresses no static
  manifest allow-list can enumerate — concrete motivation for a future
  `network = ["local-network"]` permission class (declared here, ignored today).

## Files

- `printers_plugin.py` — the plugin (PEP 723, single file, zero deps).
- `printers.svg` — tab icon (drop one in; absent → host default icon).
