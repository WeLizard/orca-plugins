from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_orcacloud_publish_uses_only_the_connected_release_event() -> None:
    publish = (ROOT / ".github/workflows/publish-orcacloud.yml").read_text(
        encoding="utf-8"
    )
    release = (ROOT / ".github/workflows/release-printers.yml").read_text(
        encoding="utf-8"
    )

    assert "types: [published]" in publish
    assert "workflow_dispatch:" not in publish
    assert "id-token: write" in publish
    assert "github.event.release.tag_name" in publish
    assert '--form-string "metadata=$metadata"' in publish
    assert '-F "metadata=$metadata"' not in publish
    assert '--pattern "SHA256SUMS"' in publish
    assert "GitHub wheel does not match SHA256SUMS" in publish
    assert "Runtime plugin version does not match the GitHub release" in publish
    assert "Wheel metadata version does not match the GitHub release" in publish
    assert "gh workflow run publish-orcacloud.yml" not in release
    assert "--draft" in release
    assert "--draft=false" not in release
    assert "sha256sum --check SHA256SUMS" in release
