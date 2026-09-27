from pathlib import Path
import os
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def test_install_and_upgrade_put_helper_on_sudo_safe_path():
    expected = 'install -m 0755 "$SOURCE_DIR/deploy/craftarr-console" /usr/bin/craftarr-console'

    assert expected in (ROOT / "scripts/install.sh").read_text()
    assert expected in (ROOT / "scripts/upgrade.sh").read_text()


def test_uninstall_removes_craftarr_helper_paths():
    script = (ROOT / "scripts/uninstall.sh").read_text()

    assert "rm -f /usr/bin/craftarr-console /usr/local/sbin/craftarr-console" in script


def test_fresh_install_bind_prompt_and_fallback_share_all_interfaces_default():
    script = (ROOT / "scripts/install.sh").read_text()

    assert "DEFAULT_BIND_HOST=0.0.0.0" in script
    assert 'BIND_HOST=${BIND_HOST:-${CONFIGURED_HOST:-$DEFAULT_BIND_HOST}}' in script
    assert "Bind address [%s]:" in script
    assert "then 0.0.0.0" in script


def test_installer_preserves_existing_java_and_installs_only_selected_versions():
    script = (ROOT / "scripts/install.sh").read_text()

    assert "openjdk-21-jre" not in script
    assert "java-21-openjdk" not in script
    assert "--java-version" in script
    assert "Java versions to install if missing" in script
    assert 'java_major_installed "$version"' in script
    assert '"java-$version-amazon-corretto-jdk"' in script
    assert '"java-$version-amazon-corretto-devel"' in script


def test_minecraft_service_allows_supervisor_to_stop_java_gracefully():
    unit = (ROOT / "deploy/craftarr-server@.service").read_text()

    assert "KillSignal=SIGTERM" in unit
    assert "KillMode=mixed" in unit
    assert "TimeoutStopSec=90" in unit


def test_stopped_server_status_dot_has_a_stable_default_colour():
    stylesheet = (ROOT / "app/static/style.css").read_text()
    rule = stylesheet.split(".server-status-dot {", 1)[1].split("}", 1)[0]

    assert "background: var(--danger);" in rule


def test_helper_self_elevates_with_resolved_absolute_path(tmp_path):
    sudo = tmp_path / "sudo"
    sudo.write_text("#!/usr/bin/env bash\nprintf '%s\\n' \"$@\"\n")
    sudo.chmod(0o755)
    helper = ROOT / "deploy/craftarr-console"
    environment = os.environ | {"PATH": f"{tmp_path}:{os.environ['PATH']}"}

    result = subprocess.run(
        [str(helper), "restart"], capture_output=True, text=True,
        env=environment, check=True,
    )

    arguments = result.stdout.splitlines()
    assert arguments[0] == "--"
    assert arguments[1] == str(helper.resolve())
    assert arguments[2:] == ["restart"]
