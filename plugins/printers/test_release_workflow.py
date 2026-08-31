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
    assert "gh workflow run publish-orcacloud.yml" not in release
    assert "--draft" in release
    assert "--draft=false" not in release
