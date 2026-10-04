#!/usr/bin/env python3
"""Fetch current GitHub stargazer timestamps and render light/dark SVG charts."""

from __future__ import annotations

import calendar
import json
import os
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "assets" / "star-history"
WIDTH = 1000
HEIGHT = 390
LEFT = 82
RIGHT = 34
TOP = 44
BOTTOM = 62

THEMES = {
    "light": {
        "background": "#ffffff",
        "foreground": "#193126",
        "muted": "#607368",
        "grid": "#dce9e0",
        "line": "#16884c",
        "fill": "#16884c",
    },
    "dark": {
        "background": "#0d1117",
        "foreground": "#e6edf3",
        "muted": "#9aa7b2",
        "grid": "#303b45",
        "line": "#4fce83",
        "fill": "#4fce83",
    },
}


def fetch_stargazers(repository: str) -> list[date]:
    """Fetch star dates from GitHub's weekly star-history endpoint."""
    dates: list[date] = []
    page = 1
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    while True:
        url = (
            f"https://api.github.com/repos/{repository}/stargazers/history"
            f"?per_page=100&page={page}"
        )
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "Craftarr-star-history",
            "X-GitHub-Api-Version": "2026-03-10",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = Request(url, headers=headers)
        try:
            with urlopen(request, timeout=30) as response:
                records = json.load(response)
        except (HTTPError, URLError, TimeoutError) as exc:
            raise RuntimeError(f"GitHub stargazer request failed: {exc}") from exc
        if not isinstance(records, list):
            raise RuntimeError("GitHub returned an unexpected stargazer response")
        for record in records:
            if not isinstance(record, dict):
                continue
            week = record.get("week")
            daily_counts = record.get("days")
            if not isinstance(week, int) or not isinstance(daily_counts, list):
                continue
            try:
                week_start = datetime.fromtimestamp(week, tz=timezone.utc).date()
            except (OverflowError, OSError, ValueError):
                continue
            for offset, count in enumerate(daily_counts[:7]):
                if isinstance(count, int) and count > 0:
                    dates.extend([week_start + timedelta(days=offset)] * count)
        if len(records) < 100:
            break
        page += 1
    return dates


def month_sequence(start: date, end: date) -> list[str]:
    months: list[str] = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        months.append(f"{year:04d}-{month:02d}")
        if month == 12:
            year, month = year + 1, 1
        else:
            month += 1
    return months


def render_chart(theme: dict[str, str], dates: list[date]) -> str:
    now = datetime.now(timezone.utc).date()
    plot_width = WIDTH - LEFT - RIGHT
    plot_height = HEIGHT - TOP - BOTTOM
    if dates:
        months = month_sequence(min(dates), max(now, max(dates)))
        month_counts = Counter(item.strftime("%Y-%m") for item in dates)
        running = 0
        values = []
        for month in months:
            running += month_counts[month]
            values.append(running)
        maximum = max(values, default=1)
    else:
        months = [now.strftime("%Y-%m")]
        values = [0]
        maximum = 1

    def point(index: int, value: int) -> tuple[float, float]:
        x = LEFT + (plot_width * index / max(1, len(values) - 1))
        y = TOP + plot_height - (plot_height * value / maximum)
        return x, y

    grid_lines = []
    tick_values = sorted(set([0, max(1, maximum // 2), maximum]))
    for value in tick_values:
        y = TOP + plot_height - (plot_height * value / maximum)
        grid_lines.append(
            f'<line x1="{LEFT}" y1="{y:.1f}" x2="{WIDTH - RIGHT}" y2="{y:.1f}" '
            f'stroke="{theme["grid"]}" stroke-width="1"/>'
            f'<text x="{LEFT - 14}" y="{y + 5:.1f}" text-anchor="end" '
            f'font-size="13" fill="{theme["muted"]}">{value}</text>'
        )

    labels = []
    label_count = min(6, len(months))
    label_indices = sorted(set(round(i * (len(months) - 1) / max(1, label_count - 1)) for i in range(label_count)))
    for index in label_indices:
        year, month = months[index].split("-")
        x = LEFT + plot_width * index / max(1, len(months) - 1)
        label = f"{calendar.month_abbr[int(month)]} {year}"
        labels.append(
            f'<text x="{x:.1f}" y="{HEIGHT - 24}" text-anchor="middle" '
            f'font-size="13" fill="{theme["muted"]}">{label}</text>'
        )

    coordinates = [point(index, value) for index, value in enumerate(values)]
    path = " ".join(("M" if index == 0 else "L") + f"{x:.1f},{y:.1f}" for index, (x, y) in enumerate(coordinates))
    baseline = TOP + plot_height
    area_path = f"{path} L {coordinates[-1][0]:.1f},{baseline:.1f} L {coordinates[0][0]:.1f},{baseline:.1f} Z"
    count = len(dates)
    title = "Craftarr star history"
    description = f"Cumulative current stargazers by month. Current total: {count}."
    if not dates:
        message = (
            f'<text x="{WIDTH / 2:.0f}" y="{TOP + plot_height / 2 + 8:.1f}" '
            f'text-anchor="middle" font-size="18" fill="{theme["muted"]}">No stargazers yet</text>'
        )
    else:
        message = ""

    background = theme["background"]
    foreground = theme["foreground"]
    muted = theme["muted"]
    line = theme["line"]
    fill = theme["fill"]
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" viewBox="0 0 {WIDTH} {HEIGHT}" role="img" aria-labelledby="title desc">
  <title id="title">{title}</title>
  <desc id="desc">{description}</desc>
  <rect width="100%" height="100%" rx="16" fill="{background}"/>
  <text x="{LEFT}" y="28" font-family="system-ui, sans-serif" font-size="16" font-weight="650" fill="{foreground}">Stars over time</text>
  <text x="{WIDTH - RIGHT}" y="28" text-anchor="end" font-family="system-ui, sans-serif" font-size="13" fill="{muted}">{count} current stars</text>
  {''.join(grid_lines)}
  <path d="{area_path}" fill="{fill}" opacity=".12"/>
  <path d="{path}" fill="none" stroke="{line}" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>
  {''.join(labels)}
  {message}
</svg>
'''


def main() -> int:
    repository = os.environ.get("GITHUB_REPOSITORY", "STEMMechanics/Craftarr")
    if "/" not in repository:
        print("GITHUB_REPOSITORY must be OWNER/REPOSITORY", file=sys.stderr)
        return 2
    try:
        dates = fetch_stargazers(repository)
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 1
    OUTPUT.mkdir(parents=True, exist_ok=True)
    for name, theme in THEMES.items():
        path = OUTPUT / f"{name}.svg"
        path.write_text(render_chart(theme, dates), encoding="utf-8")
        print(f"Wrote {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
