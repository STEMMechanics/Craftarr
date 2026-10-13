import gzip
from datetime import timezone

import pytest

from types import SimpleNamespace

from app.server_logs import (
    MAX_LOG_VIEW_BYTES, list_server_logs, read_latest_log, read_server_log,
    safe_log_path,
)
from app.web_logs import paginated_logs


def server_at(path):
    return SimpleNamespace(directory=str(path))


def test_lists_and_reads_plain_and_compressed_logs(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "latest.log").write_text("latest shutdown\n")
    with gzip.open(logs / "2026-08-11-1.log.gz", "wb") as stream:
        stream.write(b"Saving chunks\nThreadedAnvilChunkStorage: All dimensions are saved\n")

    entries = list_server_logs(server_at(tmp_path))
    compressed, truncated = read_server_log(server_at(tmp_path), "2026-08-11-1.log.gz")

    assert {entry["name"] for entry in entries} == {"latest.log", "2026-08-11-1.log.gz"}
    assert all(entry["modified"].tzinfo is timezone.utc for entry in entries)
    assert "All dimensions are saved" in compressed
    assert truncated is False


@pytest.mark.parametrize("filename", ["../latest.log", "server.properties", "", "/tmp/latest.log"])
def test_rejects_paths_outside_logs_directory(tmp_path, filename):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "latest.log").write_text("safe")

    with pytest.raises(ValueError):
        safe_log_path(server_at(tmp_path), filename)


def test_rejects_log_symlinks(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    outside = tmp_path / "outside.log"
    outside.write_text("secret")
    (logs / "linked.log").symlink_to(outside)

    with pytest.raises(ValueError):
        safe_log_path(server_at(tmp_path), "linked.log")


def test_latest_log_refresh_reads_the_most_recent_content(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "latest.log").write_bytes(b"old" * MAX_LOG_VIEW_BYTES + b"clean shutdown\n")

    content, truncated = read_latest_log(server_at(tmp_path))

    assert content.endswith("clean shutdown\n")
    assert truncated is True


def test_log_list_is_paginated_five_at_a_time(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    for index in range(12):
        path = logs / f"server-{index:02}.log"
        path.write_text(str(index))
        path.touch()

    first, page, total_pages = paginated_logs(server_at(tmp_path), 1)
    last, last_page, _ = paginated_logs(server_at(tmp_path), 99)

    assert len(first) == 5
    assert page == 1
    assert total_pages == 3
    assert len(last) == 2
    assert last_page == 3
