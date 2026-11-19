# FHSL — Nature & Organics

Part of the FilamentHub Surface Library (FHSL). This standalone OrcaSlicer texture-library plugin installs 38 seamless 2048 × 2048, 8-bit grayscale PNG height maps. Existing distribution/plugin identity, capability label, and every texture stem are retained. The internal import package is shortened to `fhsln` and its texture folder to `t`, keeping OrcaSlicer's extracted file paths well below the 260-character Windows limit.

## Install

Install the wheel for this pack in an OrcaSlicer build that supports texture-library plugins. The maps appear under **Texture libraries → FilamentHub Surface Library — Nature & Organics**.

This package is a new candidate. It has not yet been accepted in the target OrcaSlicer build.

## Candidate smoke check

Before installing a local candidate, disable the older Cloud/diagnostic copy of this same pack in OrcaSlicer to avoid loading two copies with the same plugin identity. Install the 1.0.2 candidate wheel, reopen the texture selector, and confirm that all 38 maps appear under **Texture libraries → FilamentHub Surface Library — Nature & Organics**. Then test the first, a middle, and the last map by loading each onto a model.

This is a new short import path (`fhsln/t`) with the established wheel distribution and plugin ID. Confirm the exact candidate in OrcaSlicer before publishing it.
