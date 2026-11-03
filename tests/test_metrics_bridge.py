import json
import time

from app import metrics_bridge
from app.metrics_bridge import metrics_snapshot_path, read_metrics_snapshot


def test_metrics_snapshot_returns_validated_paper_values(tmp_path):
    path = metrics_snapshot_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({
            "schema": 1,
            "recorded_at": time.time(),
            "tps": 19.98,
            "mspt": 24.6,
            "source": "paper-api",
        }),
        encoding="utf-8",
    )

    snapshot = read_metrics_snapshot(tmp_path)

    assert snapshot["tps"] == 19.98
    assert snapshot["mspt"] == 24.6
    assert snapshot["source"] == "paper-api"


def test_metrics_snapshot_rejects_stale_or_invalid_values(tmp_path):
    path = metrics_snapshot_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({
            "recorded_at": time.time() - 181,
            "tps": 19.0,
            "mspt": 20.0,
        }),
        encoding="utf-8",
    )
    assert read_metrics_snapshot(tmp_path) is None

    path.write_text(
        json.dumps({
            "recorded_at": time.time(),
            "tps": 100,
            "mspt": -1,
        }),
        encoding="utf-8",
    )
    snapshot = read_metrics_snapshot(tmp_path)
    assert snapshot["tps"] is None
    assert snapshot["mspt"] is None


def test_bridge_install_failure_is_persisted_for_performance_page(monkeypatch, tmp_path):
    monkeypatch.setattr(metrics_bridge, "_java_tool", lambda _java_path, _name: None)

    assert not metrics_bridge.ensure_metrics_bridge(tmp_path, tmp_path / "paper.jar")

    status = metrics_bridge.read_metrics_bridge_status(tmp_path)
    assert status["state"] == "error"
    assert "JDK" in status["message"]
    assert metrics_bridge.metrics_bridge_status_path(tmp_path).is_file()


def test_stale_bridge_installation_state_is_reported_as_an_error(tmp_path):
    path = metrics_bridge.metrics_bridge_status_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({
            "state": "installing",
            "message": "Craftarr is installing the Paper API bridge before the server starts.",
            "checked_at": time.time() - metrics_bridge.MAX_INSTALLING_STATUS_AGE_SECONDS - 1,
        }),
        encoding="utf-8",
    )

    status = metrics_bridge.read_metrics_bridge_status(tmp_path)

    assert status["state"] == "error"
    assert "did not finish" in status["message"]
