import io
import json
from datetime import date

from scripts import update_star_history


def test_fetch_stargazers_uses_weekly_history_api(monkeypatch):
    payload = [{"week": 1_759_046_400, "total": 3, "days": [1, 0, 2, 0, 0, 0, 0]}]
    captured = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["headers"] = request.headers
        captured["timeout"] = timeout
        return io.BytesIO(json.dumps(payload).encode())

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setattr(update_star_history, "urlopen", fake_urlopen)

    dates = update_star_history.fetch_stargazers("STEMMechanics/Craftarr")

    assert dates == [date(2025, 9, 28), date(2025, 9, 30), date(2025, 9, 30)]
    assert captured["url"] == (
        "https://api.github.com/repos/STEMMechanics/Craftarr/stargazers/history"
        "?per_page=100&page=1"
    )
    assert captured["headers"]["X-github-api-version"] == "2026-03-10"
    assert captured["timeout"] == 30
