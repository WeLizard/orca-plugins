from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class FHSLWorkflowTests(unittest.TestCase):
    def test_tag_validation_precedes_tag_checkout(self):
        source = (ROOT / '.github/workflows/release-fhsl.yml').read_text(encoding='utf-8')
        self.assertLess(source.index('Validate the tag before checkout'), source.index('uses: actions/checkout'))
        self.assertIn('ref: refs/tags/', source)
        self.assertIn('persist-credentials: false', source)

    def test_pack_tags_do_not_target_print_farm(self):
        source = (ROOT / '.github/workflows/release-fhsl.yml').read_text(encoding='utf-8')
        for pack in ('hard-surface', 'nature-organics', 'textiles-materials'):
            self.assertIn(f"'fhsl-{pack}-v*'", source)
            self.assertFalse(f'fhsl-{pack}-v1.0.2'.startswith('v'))
        self.assertIn("project['project']['version'] != version", source)
        self.assertIn("int(segment) > 10", source)

    def test_release_preserves_published_artifacts(self):
        source = (ROOT / '.github/workflows/release-fhsl.yml').read_text(encoding='utf-8')
        self.assertIn('sha256sum --check SHA256SUMS', source)
        self.assertIn('--verify-tag', source)
        self.assertIn('--draft', source)
        self.assertIn('gh release view', source)
        self.assertNotIn('--clobber', source)
        self.assertNotIn('id-token: write', source)
        self.assertNotIn('api.orcaslicer.com', source)


if __name__ == '__main__':
    unittest.main()
