# Print Farm

Monitor and manage your networked 3D printers from one OrcaSlicer page. The toolbar counts printers that are **Printing**, **Idle**, or **Need attention**. Click a count to filter the cards, then click it again to show all printers.

> **OrcaSlicer asks for printer access and background work.** One initial prompt covers Bambu discovery (UDP port 2021) and background work. OrcaSlicer can ask for an individual printer address when you first connect. After allowing access, click **Refresh** on that printer's card. Refusing the initial request prevents the plugin from loading.

**Connections**

- Moonraker / Klipper, OctoPrint and Bambu Lab (LAN mode): live status, nozzle
  and bed temperatures, progress, thumbnails, file upload, start, pause,
  resume and cancel. Moonraker printers show the temperatures of all discovered
  heads and the active tool reported by the printer. Clicking a head only
  selects its displayed details; it sends no tool-change command.
- Duet (RepRapFirmware standalone and DSF, including IDEX tool temperatures):
  status, file upload and start. Pause, resume and cancel are off until their
  commands are confirmed.
- Creality Ender 5 Max (stock firmware) and FlashForge Adventurer 5M / 5M Pro
  and AD5X: status only. Upload and start stay closed until they are verified
  on real printers.
- PrusaLink: status and upload only.
- Repetier-Server: choose a printer by its name and slug. Reads status and
  temperatures. Classic server-managed jobs with confirmed readiness support
  upload, start, pause, resume and cancel. New or unknown connector modes are
  read-only. A start request can wait in the server queue; printing is confirmed
  only by later status. Resume may need another click after reheating.

These new connections were tested against emulators, not hardware yet.

**Printing**

- Send one sliced file to one or several printers. Each printer is checked
  separately: compatible, needs your manual check, or incompatible.
- Before every start you confirm that the bed is free.
- Slices are kept per printer, with a button to remove leftover files.
- Print journal with search across all printers.
- Open each printer's web interface without leaving the plugin page.
- Interface in every language shipped by OrcaSlicer.

Printer addresses and state stay in OrcaSlicer's private plugin storage.
New keys use the operating system credential store; the printer list keeps only bound references.
Printer protocols and raw diagnostic messages remain in English.

## What the plugin does on your computer

- **Who it talks to.** Only your printers: the ones you added and the ones you adopted from your OrcaSlicer printer profiles, at the addresses stored there. It uses no internet service, has no account of its own and sends no telemetry or usage data to anyone.
- **What it sends to a printer, only when you click.** Sending the file you chose, starting it (after you confirm the bed is free) and, where the printer supports it, pause, resume and stop (stop needs a second click). Bambu Lab: the file over FTPS and the commands over MQTT. Moonraker and OctoPrint: upload, start, pause, resume and stop over HTTP. Duet: upload and an `M32` start. Repetier-Server: upload to the selected printer, an approved start request (which can wait in the queue), pause, resume and cancel over HTTP for supported classic server-managed jobs. PrusaLink: upload only. Creality and FlashForge printers are only read. Printer status is read every 15 seconds by default (5, 15, 30 or 60 in Settings) while the Print Farm page is open and visible; reading a Bambu printer also asks it for a full status report.
- **Camera and Web UI options.** Use the camera icon to view the camera. On Klipper cards, the camera overlay also lets you select its mode. Where the printer has a Web UI, **Open** shows it in a separate window. Use that window’s address action to set an optional Web UI address on the same printer host, with its port entered explicitly. Relative webcam addresses use this Web UI origin. The address and browser instructions stay visible if the embedded Web UI cannot load.
- **Stock Snapmaker U1 camera, only when selected and opened.** Select this mode in the Klipper camera overlay. While its camera overlay is open, one worker keeps one `/websocket` connection and sends only `camera.start_monitor` (`domain=lan`, `interval=0`) every two seconds with correlated requests. With an API key, a same-origin HTTP request obtains a one-shot token for that websocket; the persistent key stays in Python HTTP headers, never in the page, URL or WebSocket handshake. JPEG snapshots from `/server/files/camera/monitor.jpg` are limited to 2 MiB and up to four per second. Another snapshot waits until the page acknowledges loading the previous image, so a slow page cannot accumulate frames. There is no proxy, port scan, redirect, arbitrary camera command or global stop-monitor call. Closing the overlay, changing its binding, stopping the plugin or expiry of a short page lease closes the connections. No camera traffic runs while idle. Last-Modified is checked when available; otherwise the time shown is receipt time, not proof of capture time. Errors or frame expiry clear the image. Tested on loopback emulators, not hardware.
- **Finding Bambu printers.** The plugin only listens for the announcements Bambu printers broadcast (UDP port 2021) and sends nothing. It listens for 2 to 3 seconds when you open the Print Farm page or press Refresh. Announced names appear as discovery hints; they are not saved as a trusted printer name or model for automatic file matching.
- **Certificates.** Bambu LAN checks the Bambu CA chain and saved device serial automatically for status, commands and file transfers. The FTPS data connection must present the same certificate as the control connection. The card shows **Certificates…** only when a certificate needs attention, for example if it is unrecognized or a saved certificate changed. Saved fingerprints are checked before credentials or files are sent. Confirming a certificate never retries a print or upload.
- **What it reads from OrcaSlicer.** Your printer profiles that have a print host address: name, type, address and the key or password saved there. A key taken from a profile is kept in memory only. For every slice it also reads the G-code and the printer name, model and host from the slicing settings (never a key) to keep a copy (setting **Keep copies of sliced files**, on by default) and pick the right printer card; OrcaSlicer's file is not changed. It also receives OrcaSlicer's export, send and print events for the print journal, and the interface language.
- **Where it stores data.** In the private plugin storage folder that OrcaSlicer provides (on builds without it, in a `.print-farm` folder inside the OrcaSlicer data folder): the printer list, print journal, operation log and copies of your slices. New keys and access codes are stored in the operating system credential store; the printer list holds bound references. Older plaintext keys remain until you explicitly move them. Supported legacy state is imported only after you confirm, with a backup next to the storage folder. Stores with scoped credentials, operation journals or incoming transfers are blocked instead of partially imported. Local wheel versions may have separate host storage folders; they are not merged automatically. Copies are deleted only by you, or by the optional automatic cleanup, which is off by default. Printer files stay inside that folder or the legacy backup next to it; new secrets are written to the operating system credential store.
- **What starts when it loads.** It asks the one permission question above, opens its storage, reads its settings, loads the module of your operating system's credential store (no key is read from it yet) and marks operations that were interrupted as unknown (nothing is re-sent). It starts background worker threads when they are needed and a cleanup timer that does nothing unless automatic cleanup is on. No printer is contacted and nothing is scanned until you open the Print Farm page.
- **What it opens.** The Print Farm page, confined windows for a printer’s own web interface and camera, and a Feedback window containing only the fixed address `https://filamenthub.ru` and instructions to copy it into a browser and use the site’s **Beta Feedback** button. No printer data or diagnostics are sent to that site. The plugin never starts a browser or any other program.

**Development status:** Alpha. The OrcaSlicer Python Plugin API is still
evolving, and some capabilities use compatibility fallbacks on older builds.

**Known OrcaSlicer UI limitation:** if the native available-printer drop-down
is open while the OrcaSlicer window is moved, its popup may remain at its
original screen position until it is closed. Selecting an item, pressing Esc,
or clicking outside closes it; the dashboard and printer data are unaffected.

Printer keys entered manually are saved in Windows Credential Manager, macOS Keychain or Linux Secret Service through keyring 25.7.0. OrcaSlicer needs dependency installation support and package access for the initial keyring installation; installation failure may prevent the plugin from loading. After loading, an unavailable system store blocks saving new keys and connections using stored keys while the dashboard remains available. Use **Retry access** after unlocking the system store. Existing plaintext keys remain until you confirm **Move keys**. Old backups are not removed and may still contain plaintext keys. Keys supplied by OrcaSlicer presets stay in memory. Stored keys are bound to their printer, endpoint and account storage; moving the JSON file alone does not move the keys.
