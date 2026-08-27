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
