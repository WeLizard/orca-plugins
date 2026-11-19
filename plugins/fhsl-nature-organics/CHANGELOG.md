# Changelog — FilamentHub Surface Library — Nature & Organics

## 1.0.2

- Prepared the existing texture set as 2048 × 2048 8-bit grayscale PNG assets.
- Fixes maps missing on Windows. OrcaSlicer unpacks a pack under a folder named after its module, and the full plugin name appeared there twice, so most file paths ran past the 260-character limit Windows applies by default; only the maps with the shortest names loaded, or none. With the short `fhsln` module and `t` texture folder the longest path is about 210 characters.
- Shortened the internal import module to `fhsln` and its texture folder to `t` while retaining the established distribution, plugin ID, capability label, and all 38 texture names.
- Added standalone source and package validation for this pack.
