# /// script
# requires-python = ">=3.12"
# dependencies = []
#
# [tool.orcaslicer.plugin]
# id = "filamenthub_surface_hard_surface"
# name = "FHSL — Hard Surface"
# description = "Seamless grayscale height maps for OrcaSlicer texture displacement."
# author = "FilamentHub"
# version = "1.0.2"
# ///

from pathlib import Path
import orca


class SurfaceTexturePack(orca.resource_texture.ResourceTexturePluginCapabilityBase):
    """Texture-library capability backed by this package's PNG height maps."""

    def get_name(self):
        return 'FilamentHub Surface Library — Hard Surface'

    def get_texture_dir(self):
        return str(Path(__file__).parent / "t")


@orca.plugin
class SurfaceTexturePlugin(orca.base):
    def register_capabilities(self):
        orca.register_capability(SurfaceTexturePack)
