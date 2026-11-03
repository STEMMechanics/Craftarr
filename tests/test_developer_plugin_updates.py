import os
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from app import developer_plugin_updates as updates


def test_safe_build_environment_limits_gradle_and_maven(tmp_path):
    environment = updates._safe_build_environment(tmp_path)

    assert environment["GRADLE_USER_HOME"] == str(tmp_path / "gradle-cache")
    assert "-Xmx768m" in environment["GRADLE_OPTS"]
    assert "-Dorg.gradle.workers.max=1" in environment["GRADLE_OPTS"]
    assert "-Xmx768m" in environment["MAVEN_OPTS"]
    assert "-XX:ActiveProcessorCount=1" in environment["MAVEN_OPTS"]
    assert f"-Dmaven.repo.local={tmp_path / 'maven-cache'}" in environment["MAVEN_OPTS"]


def test_prioritise_build_command_uses_nice(monkeypatch):
    monkeypatch.setattr(updates, "BUILD_NICE", 10)
    monkeypatch.setattr(updates, "shutil_which", lambda command: "/usr/bin/nice" if command == "nice" else None)

    assert updates._prioritise_build_command(["bash", "./gradlew", "build"]) == [
        "/usr/bin/nice", "-n", "10", "bash", "./gradlew", "build",
    ]


def test_run_idle_timeout_kills_the_build_process_group():
    with pytest.raises(subprocess.TimeoutExpired) as failure:
        updates._run(["bash", "-c", "printf start; sleep 30"], timeout=10, idle_timeout=1)

    assert getattr(failure.value, "idle_timeout", False) is True


def test_queue_job_clears_log_from_previous_build(monkeypatch):
    state = SimpleNamespace(
        source_key="owner/repository#main",
        latest_commit="old-commit",
        installed_commit="old-commit",
        status="failed",
        error="old error",
        build_log="old build output",
        build_report_html="old report",
        build_report_path="old-report.html",
        checked_at=None,
        updated_at=None,
    )

    class FakeDB:
        def get(self, *_args):
            return state

        def commit(self):
            pass

    server = SimpleNamespace(id=42)
    setting = SimpleNamespace(project="Owner/Repository", development_branch="main", plugin_name="STEMCraft")
    plugin = {"name": "STEMCraft", "filename": "STEMCraft.jar"}
    monkeypatch.setattr(updates._worker_pool, "submit", lambda *_args: None)
    updates._active_jobs.discard((server.id, "stemcraft"))

    try:
        assert updates._queue_job(server, setting, plugin, "new-commit", updates.datetime.utcnow(), FakeDB()) is True
        assert state.build_log is None
        assert state.build_report_html is None
        assert state.build_report_path is None
    finally:
        updates._active_jobs.discard((server.id, "stemcraft"))


@pytest.mark.skipif(os.name != "posix" or shutil.which("bash") is None, reason="requires POSIX bash process groups")
def test_run_timeout_kills_the_entire_build_process_group():
    with pytest.raises(subprocess.TimeoutExpired):
        updates._run(["bash", "-c", "sleep 30 & wait"], timeout=1)
