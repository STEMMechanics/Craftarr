import json
import time

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
