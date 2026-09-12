#!/usr/bin/env python3
"""Build README.md + SVG fetch from config.yml and GitHub GraphQL."""

from __future__ import annotations

import html
import json
import os
import sys
import time
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.yml"
README_PATH = ROOT / "README.md"
CACHE_PATH = ROOT / "cache" / "stats.json"
ASSETS_DIR = ROOT / "assets"
GRAPHQL = "https://api.github.com/graphql"
VALUE_COL = 24
STATS_LABEL_COL = 16
RULE_WIDTH = 56
FONT_SIZE = 12
LINE_HEIGHT = 16
PAD_X = 8
PAD_Y = 8
CHAR_W = 7.22
IMG_WIDTH = 460

THEMES = {
    "dark": {
        "key": "#ffa657",
        "value": "#a5d6ff",
        "muted": "#6e7681",
        "add": "#3fb950",
        "del": "#f85149",
    },
    "light": {
        "key": "#953800",
        "value": "#0550ae",
        "muted": "#656d76",
        "add": "#1a7f37",
        "del": "#cf222e",
    },
}


def load_config() -> dict:
    with CONFIG_PATH.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def token() -> str:
    value = os.environ.get("ACCESS_TOKEN", "").strip()
    if not value:
        sys.exit(
            "Falta ACCESS_TOKEN. En Actions: repo → Settings → Secrets → ACCESS_TOKEN.\n"
            "En local: export ACCESS_TOKEN=ghp_..."
        )
    return value


def username(config: dict) -> str:
    return os.environ.get("USER_NAME", config["username"]).strip()


def graphql(headers: dict, query: str, variables: dict | None = None) -> dict:
    for attempt in range(6):
        response = requests.post(
            GRAPHQL,
            json={"query": query, "variables": variables or {}},
            headers=headers,
            timeout=60,
        )
        if response.status_code == 200:
            payload = response.json()
            if payload.get("errors"):
                raise RuntimeError(payload["errors"])
            return payload["data"]
        if response.status_code in {403, 502} and attempt < 5:
            time.sleep(8 * (attempt + 1))
            continue
        raise RuntimeError(f"GraphQL {response.status_code}: {response.text[:500]}")
    raise RuntimeError("GraphQL: demasiados reintentos")


def fetch_user(headers: dict, login: str) -> dict:
    data = graphql(
        headers,
        """
        query ($login: String!) {
          user(login: $login) {
            id
            createdAt
            followers { totalCount }
            issues { totalCount }
            pullRequests { totalCount }
            repositories(ownerAffiliations: OWNER) { totalCount }
          }
        }
        """,
        {"login": login},
    )
    return data["user"]


def fetch_repo_count(headers: dict, login: str, affiliations: list[str]) -> int:
    data = graphql(
        headers,
        """
        query ($login: String!, $aff: [RepositoryAffiliation]) {
          user(login: $login) {
            repositories(ownerAffiliations: $aff) { totalCount }
          }
        }
        """,
        {"login": login, "aff": affiliations},
    )
    return int(data["user"]["repositories"]["totalCount"])


def fetch_stars(headers: dict, login: str) -> int:
    cursor = None
    total = 0
    while True:
        data = graphql(
            headers,
            """
            query ($login: String!, $cursor: String) {
              user(login: $login) {
                repositories(first: 100, after: $cursor, ownerAffiliations: OWNER) {
                  pageInfo { hasNextPage endCursor }
                  nodes { stargazerCount }
                }
              }
            }
            """,
            {"login": login, "cursor": cursor},
        )
        repos = data["user"]["repositories"]
        total += sum(node["stargazerCount"] for node in repos["nodes"] if node)
        if not repos["pageInfo"]["hasNextPage"]:
            return total
        cursor = repos["pageInfo"]["endCursor"]


def owned_repos(headers: dict, login: str) -> list[dict]:
    cursor = None
    edges: list[dict] = []
    while True:
        data = graphql(
            headers,
            """
            query ($login: String!, $cursor: String) {
              user(login: $login) {
                repositories(first: 60, after: $cursor, ownerAffiliations: OWNER) {
                  pageInfo { hasNextPage endCursor }
                  nodes {
                    nameWithOwner
                    isFork
                    defaultBranchRef {
                      target {
                        ... on Commit {
                          history { totalCount }
                        }
                      }
                    }
                  }
                }
              }
            }
            """,
            {"login": login, "cursor": cursor},
        )
        page = data["user"]["repositories"]
        edges.extend(page["nodes"])
        if not page["pageInfo"]["hasNextPage"]:
            return edges
        cursor = page["pageInfo"]["endCursor"]


def commit_page(
    headers: dict,
    owner: str,
    repo: str,
    cursor: str | None,
) -> dict | None:
    data = graphql(
        headers,
        """
        query ($owner: String!, $name: String!, $cursor: String) {
          repository(owner: $owner, name: $name) {
            defaultBranchRef {
              target {
                ... on Commit {
                  history(first: 100, after: $cursor) {
                    pageInfo { hasNextPage endCursor }
                    nodes {
                      additions
                      deletions
                      author { user { id } }
                    }
                  }
                }
              }
            }
          }
        }
        """,
        {"owner": owner, "name": repo, "cursor": cursor},
    )
    ref = data["repository"]["defaultBranchRef"]
    if not ref:
        return None
    return ref["target"]["history"]


def loc_for_repo(headers: dict, full_name: str, owner_id: str) -> dict:
    owner, name = full_name.split("/", 1)
    additions = deletions = my_commits = 0
    cursor = None
    while True:
        history = commit_page(headers, owner, name, cursor)
        if history is None:
            return {"commit_count": 0, "my_commits": 0, "additions": 0, "deletions": 0}
        for node in history["nodes"]:
            user = (node.get("author") or {}).get("user")
            if not user or user.get("id") != owner_id:
                continue
            my_commits += 1
            additions += node.get("additions") or 0
            deletions += node.get("deletions") or 0
        if not history["pageInfo"]["hasNextPage"]:
            return {
                "commit_count": 0,
                "my_commits": my_commits,
                "additions": additions,
                "deletions": deletions,
            }
        cursor = history["pageInfo"]["endCursor"]


def load_cache() -> dict:
    if not CACHE_PATH.exists():
        return {"repos": {}}
    with CACHE_PATH.open(encoding="utf-8") as fh:
        return json.load(fh)


def save_cache(cache: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CACHE_PATH.open("w", encoding="utf-8") as fh:
        json.dump(cache, fh, indent=2)
        fh.write("\n")


def lines_of_code(headers: dict, login: str, owner_id: str) -> dict:
    cache = load_cache()
    cached_repos: dict = cache.get("repos", {})
    fresh: dict[str, dict] = {}
    additions = deletions = commits = 0

    for node in owned_repos(headers, login):
        if node.get("isFork"):
            continue
        full_name = node["nameWithOwner"]
        branch = node.get("defaultBranchRef")
        commit_count = 0
        if branch and branch.get("target"):
            commit_count = branch["target"]["history"]["totalCount"]

        previous = cached_repos.get(full_name)
        if previous and previous.get("commit_count") == commit_count:
            loc = previous
        elif commit_count == 0:
            loc = {"commit_count": 0, "my_commits": 0, "additions": 0, "deletions": 0}
        else:
            loc = loc_for_repo(headers, full_name, owner_id)
            loc["commit_count"] = commit_count
            print(f"  counted {full_name}: {loc['my_commits']} commits", flush=True)

        fresh[full_name] = loc
        commits += loc["my_commits"]
        additions += loc["additions"]
        deletions += loc["deletions"]

    save_cache({"repos": fresh})
    return {
        "commits": commits,
        "additions": additions,
        "deletions": deletions,
        "net": additions - deletions,
    }


def fmt_int(value: int) -> str:
    return f"{value:,}"


def set_github_output(name: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(f"{name}={value}\n")


def commit_message(user: dict, loc: dict) -> str:
    repos = user["repositories"]["totalCount"]
    followers = user["followers"]["totalCount"]
    return (
        f"stats: {fmt_int(loc['commits'])} commits, "
        f"{fmt_int(followers)} followers, "
        f"{fmt_int(repos)} repos"
    )


def dotted_parts(label: str, value: str, col: int = VALUE_COL) -> list[tuple[str, str]]:
    prefix = f"{label}:"
    dots = "." * max(col - len(prefix), 2)
    return [("key", prefix), ("muted", f" {dots} "), ("value", value)]


def rule_parts(title: str, width: int = RULE_WIDTH) -> list[tuple[str, str]]:
    body = f" {title} "
    pad = max(width - len(body) - 2, 1)
    return [("muted", f"-{body}{'─' * pad}-")]


def header_parts(login: str) -> list[tuple[str, str]]:
    name = f"{login}@github"
    rest = max(RULE_WIDTH - len(name) - 2, 8)
    return [("key", name), ("muted", "  " + "─" * rest)]


def join_columns(
    left: list[tuple[str, str]],
    right: list[tuple[str, str]],
    left_width: int,
) -> list[tuple[str, str]]:
    pad = max(left_width - len(line_text(left)), 2)
    return left + [("muted", " " * pad)] + right


def stats_rows(
    user: dict,
    stars: int,
    loc: dict,
    contributed: int,
) -> list[list[tuple[str, str]]]:
    owned = user["repositories"]["totalCount"]
    lefts = [
        dotted_parts("Repos", fmt_int(owned), STATS_LABEL_COL)
        + [
            ("muted", " {Contributed: "),
            ("value", fmt_int(contributed)),
            ("muted", "}"),
        ],
        dotted_parts("Commits", fmt_int(loc["commits"]), STATS_LABEL_COL),
        dotted_parts(
            "Pull Requests",
            fmt_int(user["pullRequests"]["totalCount"]),
            STATS_LABEL_COL,
        ),
        dotted_parts("Lines of Code", fmt_int(loc["net"]), STATS_LABEL_COL),
    ]
    rights = [
        dotted_parts("Stars", fmt_int(stars), STATS_LABEL_COL),
        dotted_parts(
            "Followers",
            fmt_int(user["followers"]["totalCount"]),
            STATS_LABEL_COL,
        ),
        dotted_parts(
            "Issues",
            fmt_int(user["issues"]["totalCount"]),
            STATS_LABEL_COL,
        ),
        [
            ("muted", "("),
            ("add", f"{fmt_int(loc['additions'])}++"),
            ("muted", ", "),
            ("del", f"{fmt_int(loc['deletions'])}--"),
            ("muted", ")"),
        ],
    ]
    left_width = max(len(line_text(parts)) for parts in lefts) + 3
    return [join_columns(left, right, left_width) for left, right in zip(lefts, rights)]


def build_lines(
    config: dict,
    user: dict,
    stars: int,
    loc: dict,
    contributed: int,
) -> list[list[tuple[str, str]]]:
    lab = config["homelab"]
    empty: list[tuple[str, str]] = []
    return [
        header_parts(config["username"]),
        dotted_parts("OS", config["os"]),
        dotted_parts("Role", config["role"]),
        dotted_parts("Location", config["location"]),
        dotted_parts("Editor", config["editor"]),
        dotted_parts("Terminal", config["terminal"]),
        empty,
        dotted_parts("Languages.Programming", config["languages_programming"]),
        dotted_parts("Frontend", config["frontend"]),
        dotted_parts("Backend", config["backend"]),
        dotted_parts("Databases", config["databases"]),
        dotted_parts("Infrastructure", config["infrastructure"]),
        dotted_parts("Tools", config["tools"]),
        empty,
        rule_parts("Homelab"),
        dotted_parts("Server", lab["server"]),
        dotted_parts("Deployments", lab["deployments"]),
        dotted_parts("Proxy", lab["proxy"]),
        dotted_parts("Network", lab["network"]),
        dotted_parts("Containers", lab["containers"]),
        empty,
        rule_parts("GitHub Stats"),
        *stats_rows(user, stars, loc, contributed),
    ]


def line_text(parts: list[tuple[str, str]]) -> str:
    return "".join(text for _, text in parts)


def svg_dimensions(lines: list[list[tuple[str, str]]]) -> tuple[int, int]:
    max_len = max((len(line_text(parts)) for parts in lines), default=40)
    width = int(PAD_X * 2 + max_len * CHAR_W)
    height = int(PAD_Y * 2 + len(lines) * LINE_HEIGHT)
    return width, height


def render_svg(lines: list[list[tuple[str, str]]], theme: str) -> str:
    colors = THEMES[theme]
    width, height = svg_dimensions(lines)
    rows: list[str] = []
    y = PAD_Y + FONT_SIZE
    for parts in lines:
        if not parts:
            y += LINE_HEIGHT
            continue
        spans = []
        for kind, text in parts:
            spans.append(
                f'<tspan class="{kind}">{html.escape(text)}</tspan>'
            )
        rows.append(
            f'<text x="{PAD_X}" y="{y}" class="row">{"".join(spans)}</text>'
        )
        y += LINE_HEIGHT
    style = f"""
    .row, tspan {{
      font-family: Consolas, ui-monospace, SFMono-Regular, Menlo, monospace;
      font-size: {FONT_SIZE}px;
      white-space: pre;
    }}
    .key {{ fill: {colors['key']}; }}
    .value {{ fill: {colors['value']}; }}
    .muted {{ fill: {colors['muted']}; }}
    .add {{ fill: {colors['add']}; }}
    .del {{ fill: {colors['del']}; }}
    """
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-label="ttiziu@github">\n'
        f"<style>{style}</style>\n"
        + "\n".join(rows)
        + "\n</svg>\n"
    )


def render_readme(login: str, height: int) -> str:
    base = f"https://raw.githubusercontent.com/{login}/{login}/main/assets"
    return f"""\
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="{base}/profile-dark.svg">
  <img alt="{login}@github" src="{base}/profile-light.svg" width="{IMG_WIDTH}" height="{height}">
</picture>

<!-- generated by generate_profile.py — edit config.yml, not this file -->
"""


def write_outputs(
    config: dict,
    user: dict,
    stars: int,
    loc: dict,
    contributed: int,
) -> None:
    lines = build_lines(config, user, stars, loc, contributed)
    svg_w, svg_h = svg_dimensions(lines)
    img_h = max(1, round(svg_h * IMG_WIDTH / svg_w))
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    for theme in ("dark", "light"):
        (ASSETS_DIR / f"profile-{theme}.svg").write_text(
            render_svg(lines, theme), encoding="utf-8"
        )
    README_PATH.write_text(render_readme(username(config), img_h), encoding="utf-8")


def main() -> None:
    config = load_config()
    login = username(config)
    headers = {"Authorization": f"bearer {token()}"}

    print("Fetching profile…", flush=True)
    user = fetch_user(headers, login)
    stars = fetch_stars(headers, login)
    owned = user["repositories"]["totalCount"]
    try:
        contributed = fetch_repo_count(
            headers,
            login,
            ["OWNER", "COLLABORATOR", "ORGANIZATION_MEMBER"],
        )
    except Exception as exc:
        print(f"Contributed count fallback to owned ({exc})", flush=True)
        contributed = owned

    print("Counting lines of code (owned repos, cache-aware)…", flush=True)
    loc = lines_of_code(headers, login, user["id"])

    write_outputs(config, user, stars, loc, contributed)
    message = commit_message(user, loc)
    set_github_output("commit_message", message)
    print(f"Wrote {README_PATH} and assets/profile-{{dark,light}}.svg", flush=True)
    print(message, flush=True)
    print(
        f"repos={owned} contributed={contributed} stars={stars} "
        f"commits={loc['commits']} loc={loc['net']}",
        flush=True,
    )


if __name__ == "__main__":
    main()
