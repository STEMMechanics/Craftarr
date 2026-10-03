import io
import tarfile

import pytest

from app import update_manager
from app.update_manager import (
    _safe_extract,
    install_release,
    normalize_version,
    release_asset_urls,
    rollback_release,
)


def test_normalize_version_handles_release_prefix():
    assert normalize_version("v1.2.3") == (1, 2, 3)


@pytest.mark.parametrize("tag", ["0.1.1", "v0.1.1"])
def test_release_tags_support_existing_and_prefixed_conventions(tag):
    assert update_manager.RELEASE_TAG_PATTERN.fullmatch(tag)


def test_normalize_version_rejects_non_numeric_release():
    assert normalize_version("not-a-version") == (0,)


def test_latest_release_check_uses_github_redirect_without_rest_api(monkeypatch):
    class RedirectResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def geturl(self):
            return "https://github.com/STEMMechanics/Craftarr/releases/tag/0.4.16"

    requests = []

    def fake_urlopen(request, timeout):
        requests.append((request.full_url, timeout))
        return RedirectResponse()

    monkeypatch.setattr(update_manager, "_release_check_cache", None)
    monkeypatch.setattr(update_manager.urllib.request, "urlopen", fake_urlopen)

    result = update_manager.get_latest_release()

    assert requests == [(update_manager.GITHUB_LATEST_RELEASE, 10)]
    assert result["tag"] == "0.4.16"
    assert result["latest_version"] == "0.4.16"
    assert result["update_available"] is True


def test_release_asset_urls_match_release_workflow_names():
    archive, checksum = release_asset_urls("v0.4.14")

    assert archive == (
        "https://github.com/STEMMechanics/Craftarr/releases/download/"
        "v0.4.14/craftarr-console-0.4.14.tar.gz"
    )
    assert checksum == archive + ".sha256"


@pytest.mark.parametrize("tag", ["v1", "v../latest", "latest", "v1/2.0", "v1.2.3;id"])
def test_install_release_rejects_unsafe_tag_before_network(tag, tmp_path):
    with pytest.raises(ValueError, match="Invalid release tag"):
        install_release(tag, tmp_path)


def test_safe_extract_rejects_parent_path(tmp_path):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz") as archive:
        member = tarfile.TarInfo("../outside")
        member.size = 1
        archive.addfile(member, io.BytesIO(b"x"))
    with pytest.raises(ValueError, match="unsafe path"):
        _safe_extract(data.getvalue(), tmp_path)


def test_rollback_release_restores_snapshot(monkeypatch, tmp_path):
    for name in ("app", "migrations"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "value.txt").write_text("new")
    for name in ("alembic.ini", "requirements.txt"):
        (tmp_path / name).write_text("new")
    backup = tmp_path / ".updates" / "20260808T120000Z"
    backup.mkdir(parents=True)
    for name in ("app", "migrations"):
        (backup / name).mkdir()
        (backup / name / "value.txt").write_text("old")
    for name in ("alembic.ini", "requirements.txt"):
        (backup / name).write_text("old")
    monkeypatch.setattr(update_manager.subprocess, "run", lambda *args, **kwargs: None)

    result = rollback_release("20260808T120000Z", tmp_path)

    assert result["restart_required"] is True
    assert (tmp_path / "app" / "value.txt").read_text() == "old"
    assert (tmp_path / "requirements.txt").read_text() == "old"


def test_rollback_release_rejects_path_traversal(tmp_path):
    with pytest.raises(ValueError, match="Invalid rollback identifier"):
        rollback_release("../20260808T120000Z", tmp_path)


def test_rollback_restores_saved_monitoring_defaults(monkeypatch, tmp_path):
    for name in ('app', 'migrations'):
        (tmp_path / name).mkdir()
    for name in ('alembic.ini', 'requirements.txt', 'plugin-monitoring.yml'):
        (tmp_path / name).write_text('new')
    backup = tmp_path / '.updates' / '20260918T120000Z'
    backup.mkdir(parents=True)
    for name in ('app', 'migrations'):
        (backup / name).mkdir()
    for name in ('alembic.ini', 'requirements.txt', 'plugin-monitoring.yml'):
        (backup / name).write_text('saved')
    monkeypatch.setattr(update_manager.subprocess, 'run', lambda *args, **kwargs: None)
    rollback_release('20260918T120000Z', tmp_path)
    assert (tmp_path / 'plugin-monitoring.yml').read_text() == 'saved'
