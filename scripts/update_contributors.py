#!/usr/bin/env python3
"""Update the README contributor avatar grid from merged pull requests."""

from __future__ import annotations

import html
import json
import os
import sys
from collections import Counter
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
START = "<!-- CONTRIBUTORS:START -->"
END = "<!-- CONTRIBUTORS:END -->"
PER_PAGE = 100
COLUMNS = 5
EXCLUDED_USERS = {
    "stemmechanics",
    "stemmechanics-bot",
    "stemmechanics-release",
    "craftarr",
    "craftarr-bot",
    "craftarr-release",
    "release-bot",
}
EXCLUDED_USERS_CASEFOLD = {name.casefold() for name in EXCLUDED_USERS}


def fetch_merged_pr_authors(repository: str) -> Counter[str]:
    counts: Counter[str] = Counter()
    page = 1
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    while True:
        url = (
            f"https://api.github.com/repos/{repository}/pulls"
            f"?state=closed&sort=updated&direction=desc&per_page={PER_PAGE}&page={page}"
        )
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "Craftarr-contributors",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = Request(url, headers=headers)
        try:
            with urlopen(request, timeout=30) as response:
                pull_requests = json.load(response)
        except (HTTPError, URLError, TimeoutError) as exc:
            raise RuntimeError(f"GitHub pull request request failed: {exc}") from exc
        if not isinstance(pull_requests, list):
            raise RuntimeError("GitHub returned an unexpected pull request response")
        for pull_request in pull_requests:
            if not pull_request.get("merged_at"):
                continue
            user = pull_request.get("user") or {}
            login = user.get("login")
            if (
                not login
                or login.casefold() in EXCLUDED_USERS_CASEFOLD
                or login.casefold().endswith("[bot]")
            ):
                continue
            counts[login] += 1
        if len(pull_requests) < PER_PAGE:
            break
        page += 1
    return counts


def contributor_grid(counts: Counter[str]) -> str:
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0].casefold()))
    if not ordered:
        return "Contributors will appear here after the first merged pull request."

    rows = []
    for offset in range(0, len(ordered), COLUMNS):
        cells = []
        for login, total in ordered[offset : offset + COLUMNS]:
            safe_login = html.escape(login, quote=True)
            cells.append(
                "<td align=\"center\" width=\"120\">"
                f"<a href=\"https://github.com/{safe_login}\">"
                f"<img src=\"https://github.com/{safe_login}.png?size=96\" width=\"72\" "
                f"alt=\"{safe_login}\"/><br/>"
                f"<sub><b>{safe_login}</b></sub><br/>"
                f"<sub>{total} merged PR{'s' if total != 1 else ''}</sub>"
                "</a></td>"
            )
        rows.append("<tr>" + "".join(cells) + "</tr>")
    return "<table><tbody>\n" + "\n".join(rows) + "\n</tbody></table>"


def main() -> int:
    repository = os.environ.get("GITHUB_REPOSITORY", "STEMMechanics/Craftarr")
    if "/" not in repository:
        print("GITHUB_REPOSITORY must be OWNER/REPOSITORY", file=sys.stderr)
        return 2
    try:
        counts = fetch_merged_pr_authors(repository)
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 1

    source = README.read_text(encoding="utf-8")
    if source.count(START) != 1 or source.count(END) != 1:
        print("README must contain exactly one contributor marker pair", file=sys.stderr)
        return 2
    before, remainder = source.split(START, 1)
    _, after = remainder.split(END, 1)
    updated = before + START + "\n" + contributor_grid(counts) + "\n" + END + after
    if updated != source:
        README.write_text(updated, encoding="utf-8")
        print(f"Updated contributor grid with {sum(counts.values())} merged pull requests")
    else:
        print("Contributor grid is already up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
