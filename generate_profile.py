#!/usr/bin/env python3
"""Build README.md from config.yml + GitHub GraphQL (owned repos only)."""

from __future__ import annotations

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
GRAPHQL = "https://api.github.com/graphql"
VALUE_COL = 26
STATS_COL = 22
STATS_LEFT_WIDTH = 36
RULE_WIDTH = 62


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
                  nodes { stargazers { totalCount } }
                }
              }
            }
            """,
            {"login": login, "cursor": cursor},
        )
        repos = data["user"]["repositories"]
        total += sum(node["stargazers"]["totalCount"] for node in repos["nodes"])
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


def dotted(label: str, value: str, col: int = VALUE_COL) -> str:
    prefix = f"{label}:"
    gap = col - len(prefix)
    dots = "." * max(gap, 2)
    return f"{prefix} {dots} {value}"


def pair(left_label: str, left_val: str, right_label: str, right_val: str) -> str:
    left = dotted(left_label, left_val, STATS_COL).ljust(STATS_LEFT_WIDTH)
    right = dotted(right_label, right_val, STATS_COL)
    return f"{left}{right}"


def rule(title: str, width: int = RULE_WIDTH) -> str:
    body = f" {title} "
    pad = width - len(body) - 2
    return f"-{body}{'─' * max(pad, 1)}-"


def render(config: dict, user: dict, stars: int, loc: dict) -> str:
    login = config["username"]
    lab = config["homelab"]
    header = f"{login}@github  " + "─" * 47
    lines = [
        "```text",
        header,
        dotted("OS", config["os"]),
        dotted("Role", config["role"]),
        dotted("Location", config["location"]),
        dotted("Editor", config["editor"]),
        dotted("Terminal", config["terminal"]),
        "",
        dotted("Languages.Programming", config["languages_programming"]),
        dotted("Frontend", config["frontend"]),
        dotted("Backend", config["backend"]),
        dotted("Databases", config["databases"]),
        dotted("Infrastructure", config["infrastructure"]),
        dotted("Tools", config["tools"]),
        "",
        rule("Homelab"),
        dotted("Server", lab["server"]),
        dotted("Deployments", lab["deployments"]),
        dotted("Proxy", lab["proxy"]),
        dotted("Network", lab["network"]),
        dotted("Containers", lab["containers"]),
        "",
        rule("GitHub Stats"),
        pair(
            "Repos",
            fmt_int(user["repositories"]["totalCount"]),
            "Stars",
            fmt_int(stars),
        ),
        pair(
            "Commits",
            fmt_int(loc["commits"]),
            "Followers",
            fmt_int(user["followers"]["totalCount"]),
        ),
        pair(
            "Pull Requests",
            fmt_int(user["pullRequests"]["totalCount"]),
            "Issues",
            fmt_int(user["issues"]["totalCount"]),
        ),
        dotted(
            "Lines of Code",
            f"{fmt_int(loc['net'])}   (+{fmt_int(loc['additions'])} / -{fmt_int(loc['deletions'])})",
            STATS_COL,
        ),
        "```",
        "",
        "<!-- generated by generate_profile.py — edit config.yml, not this file -->",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    config = load_config()
    login = username(config)
    headers = {"Authorization": f"bearer {token()}"}

    print("Fetching profile…", flush=True)
    user = fetch_user(headers, login)
    stars = fetch_stars(headers, login)
    print("Counting lines of code (owned repos, cache-aware)…", flush=True)
    loc = lines_of_code(headers, login, user["id"])

    README_PATH.write_text(render(config, user, stars, loc), encoding="utf-8")
    print(f"Wrote {README_PATH}", flush=True)
    print(
        f"repos={user['repositories']['totalCount']} stars={stars} "
        f"commits={loc['commits']} loc={loc['net']}",
        flush=True,
    )


if __name__ == "__main__":
    main()
