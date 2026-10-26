# Print Farm

Manage networked 3D printers from a single OrcaSlicer page.

> **OrcaSlicer will ask for permission.** When you install or update the plugin, OrcaSlicer asks whether it may run work in the background. Please allow it: checking printer status and uploading files run in the background, so the slicer does not freeze while it waits for the network.

**Connections**

- Moonraker / Klipper, OctoPrint and Bambu Lab (LAN mode): live status, nozzle
  and bed temperatures, progress, thumbnails, file upload, start, pause,
  resume and cancel.
- Duet (RepRapFirmware standalone and DSF, including IDEX tool temperatures):
  status, file upload and start. Pause, resume and cancel are off until their
  commands are confirmed.
- Creality Ender 5 Max (stock firmware) and FlashForge Adventurer 5M / 5M Pro
  and AD5X: status only. Upload and start stay closed until they are verified
  on real printers.
- PrusaLink: status and upload only.

These new connections were tested against emulators, not hardware yet.

**Printing**

- Send one sliced file to one or several printers. Each printer is checked
  separately: compatible, needs your manual check, or incompatible.
- Before every start you confirm that the bed is free.
- Slices are kept per printer, with a button to remove leftover files.
- Print journal with search across all printers.
- Open each printer's web interface without leaving the plugin page.
- Interface in every language shipped by OrcaSlicer.

Printer addresses and credentials stay in OrcaSlicer's private plugin storage.
Printer protocols and raw diagnostic messages remain in English.

## What the plugin does on your computer

- **Who it talks to.** Only your printers: the ones you added and the ones you adopted from your OrcaSlicer printer profiles, at the addresses stored there. It uses no internet service, has no account of its own and sends no telemetry or usage data to anyone.
- **What it sends to a printer, only when you click.** Sending the file you chose, starting it (after you confirm the bed is free) and, where the printer supports it, pause, resume and stop (stop needs a second click). Bambu Lab: the file over FTPS and the commands over MQTT. Moonraker and OctoPrint: upload, start, pause, resume and stop over HTTP. Duet: upload and an `M32` start. PrusaLink: upload only. Creality and FlashForge printers are only read. Printer status is read every 15 seconds by default (5, 15, 30 or 60 in Settings) while the Print Farm page is open and visible; reading a Bambu printer also asks it for a full status report.
- **Finding Bambu printers.** The plugin only listens for the announcements Bambu printers broadcast (UDP port 2021) and sends nothing. It listens for 2 to 3 seconds when you open the Print Farm page or press Refresh, and after a status update when a Bambu card still shows only an address as its name.
- **Certificates.** Bambu printers use self-signed certificates, so the plugin does not verify them. Other HTTPS printers are verified unless you turn that off for a printer.
- **What it reads from OrcaSlicer.** Your printer profiles that have a print host address: name, type, address and the key or password saved there. A key taken from a profile is kept in memory only. For every slice it also reads the G-code and the printer name, model and host from the slicing settings (never a key) to keep a copy (setting **Keep copies of sliced files**, on by default) and pick the right printer card; OrcaSlicer's file is not changed. It also receives OrcaSlicer's export, send and print events for the print journal, and the interface language.
- **Where it stores data.** In the private plugin storage folder that OrcaSlicer provides (on builds without it, in a `.print-farm` folder inside the OrcaSlicer data folder): the printer list, print journal, operation log and copies of your slices. Keys and access codes that you type in are saved there as plain text, readable by anyone who can open that folder; the plugin does not encrypt them or change the folder's permissions. Old state from earlier versions is imported only after you confirm, and a backup of it is kept next to the storage folder. Copies are deleted only by you, or by the optional automatic cleanup, which is off by default. Everything the plugin writes is inside that folder or the backup next to it.
- **What starts when it loads.** It opens its storage, reads its settings and marks operations that were interrupted as unknown (nothing is re-sent). It starts background worker threads when they are needed and a cleanup timer that does nothing unless automatic cleanup is on. No printer is contacted and nothing is scanned until you open the Print Farm page.
- **What it opens.** The Print Farm page, a plugin window with a printer's own web interface (**Open**) and a plugin window with its camera. These show only the printer's own address. The plugin never starts a browser or any other program.

**Development status:** Alpha. The OrcaSlicer Python Plugin API is still
evolving, and some capabilities use compatibility fallbacks on older builds.

**Known OrcaSlicer UI limitation:** if the native available-printer drop-down
is open while the OrcaSlicer window is moved, its popup may remain at its
original screen position until it is closed. Selecting an item, pressing Esc,
or clicking outside closes it; the dashboard and printer data are unaffected.
