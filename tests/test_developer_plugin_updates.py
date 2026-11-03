import os
import shutil
import subprocess

import pytest

from app import developer_plugin_updates as updates


def test_safe_build_environment_limits_gradle_and_maven(tmp_path):
    environment = updates._safe_build_environment(tmp_path)

    assert environment["GRADLE_USER_HOME"] == str(tmp_path / "gradle-cache")
    assert "-Xmx1024m" in environment["GRADLE_OPTS"]
    assert "-Dorg.gradle.workers.max=2" in environment["GRADLE_OPTS"]
    assert "-Xmx1024m" in environment["MAVEN_OPTS"]
    assert "-XX:ActiveProcessorCount=2" in environment["MAVEN_OPTS"]
    assert f"-Dmaven.repo.local={tmp_path / 'maven-cache'}" in environment["MAVEN_OPTS"]


def test_prioritise_build_command_uses_nice(monkeypatch):
    monkeypatch.setattr(updates, "BUILD_NICE", 10)
    monkeypatch.setattr(updates, "shutil_which", lambda command: "/usr/bin/nice" if command == "nice" else None)

    assert updates._prioritise_build_command(["bash", "./gradlew", "build"]) == [
        "/usr/bin/nice", "-n", "10", "bash", "./gradlew", "build",
    ]


@pytest.mark.skipif(os.name != "posix" or shutil.which("bash") is None, reason="requires POSIX bash process groups")
def test_run_timeout_kills_the_entire_build_process_group():
    with pytest.raises(subprocess.TimeoutExpired):
        updates._run(["bash", "-c", "sleep 30 & wait"], timeout=1)
