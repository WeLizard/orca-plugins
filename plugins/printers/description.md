# Print Farm

Manage supported networked 3D printers from a single OrcaSlicer page.

- View live status, nozzle and bed temperatures, progress and thumbnails.
- Connect Moonraker, OctoPrint and Bambu Lab printers on the local network.
- Send one sliced G-code file to one or several printers.
- Open each printer's web interface without letting it replace the OrcaSlicer
  plugin page or navigate to unrelated sites.
- Review bounded local print history for jobs started by Print Farm or observed
  while its dashboard was open.
- Use the interface in every language currently shipped by OrcaSlicer.

Printer addresses and credentials stay in OrcaSlicer's private plugin storage,
or in the durable compatibility location used by older plugin-enabled builds.
Printer protocols and raw diagnostic messages remain in English.

**Development status:** Alpha. The OrcaSlicer Python Plugin API is still
evolving, and some capabilities use compatibility fallbacks on older builds.

**Known OrcaSlicer UI limitation:** if the native available-printer drop-down
is open while the OrcaSlicer window is moved, its popup may remain at its
original screen position until it is closed. Selecting an item, pressing Esc,
or clicking outside closes it; the dashboard and printer data are unaffected.
