import os
import requests
import xml.etree.ElementTree as ET
from pathlib import Path
from datetime import datetime, timezone
import hashlib


GITHUB_TOKEN = os.environ["GH_TOKEN"]
USER_NAME = os.environ["GH_USER_NAME"]
SVG_PATH = Path("light-mode-profile.svg")

HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Content-Type": "application/json",
}


# ---------------------------
# Cache file for LOC data
# ---------------------------

def get_cache_file(username, scope="loc"):
    """Return a deterministic cache file path for a user and scope."""
    os.makedirs("cache", exist_ok=True)
    key = f"{username}:{scope}".encode()
    hash_id = hashlib.sha256(key).hexdigest()[:8]
    return f"cache/{scope}_{hash_id}.txt"


def load_cache(cache_file):
    """
    Load the plain-text table cache.
    Returns a dict mapping repo_hash -> stats dict.
    """
    cache = {}
    if not os.path.exists(cache_file):
        return cache

    with open(cache_file, "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            try:
                repo_hash = parts[0]
                total_commits = int(parts[1]) if len(parts) > 1 else 0
                my_commits = int(parts[2]) if len(parts) > 2 else 0
                loc_add = int(parts[3]) if len(parts) > 3 else 0
                loc_del = int(parts[4]) if len(parts) > 4 else 0
                pushed_at = parts[5] if len(parts) > 5 else ""  

                cache[repo_hash] = {
                    "total_commits": total_commits,
                    "my_commits": my_commits,
                    "loc_add": loc_add,
                    "loc_del": loc_del,
                    "pushed_at": pushed_at
                }
            except ValueError:
                # Skip malformed lines
                continue
    return cache


def save_cache(cache_file, cache):
    """
    Save the cache as a plain-text table.
    Each line: repo_hash total_commits my_commits loc_add loc_del pushed_at
    """
    with open(cache_file, "w") as f:
        # Optional header
        f.write("# repository_hash total_commits my_commits loc_add loc_del pushed_at\n")
        for repo_hash, stats in cache.items():
            line = (
                f"{repo_hash} "
                f"{stats.get('total_commits', 0)} "
                f"{stats.get('my_commits', 0)} "
                f"{stats.get('loc_add', 0)} "
                f"{stats.get('loc_del', 0)} "
                f"{stats.get('pushed_at','')}\n"
            )
            f.write(line)



# ==== GRAPHQL HELPER ====
def query_request(func_name, query, variables):
    resp = requests.post(
        "https://api.github.com/graphql",
        json={"query": query, "variables": variables},
        headers=HEADERS,
    )
    if resp.status_code == 200:
        return resp
    raise Exception(func_name, "failed", resp.status_code, resp.text)

# Query GraphQL for stars
def stars_counter(edges):
    """
    Count total stars in repositories owned by me.
    Expects: repositories { edges { node { stargazers { totalCount } } } }.
    """
    total_stars = 0
    for edge in edges:
        total_stars += edge["node"]["stargazers"]["totalCount"]
    return total_stars

#Query GraphQL for profile stats
def graph_profile_stats():
    """
    Get public repos, followers, and total stars on my public repos.
    """
    query = """
    query($login: String!, $after: String) {
      user(login: $login) {
        repositories(
          first: 100
          after: $after
          ownerAffiliations: OWNER
          isFork: false
          privacy: PUBLIC
        ) {
          totalCount
          pageInfo {
            hasNextPage
            endCursor
          }
          edges {
            node {
              stargazers {
                totalCount
              }
            }
          }
        }
        followers {
          totalCount
        }
      }
    }
    """

    repos_count = 0
    followers = 0
    total_stars = 0
    after = None

    while True:
        variables = {"login": USER_NAME, "after": after}
        r = query_request("graph_profile_stats", query, variables)
        user = r.json()["data"]["user"]

        repos = user["repositories"]
        followers = user["followers"]["totalCount"]
        repos_count = repos["totalCount"]

        edges = repos["edges"]
        total_stars += stars_counter(edges)

        if not repos["pageInfo"]["hasNextPage"]:
            break
        after = repos["pageInfo"]["endCursor"]

    return repos_count, followers, total_stars

# get total contributions from start date to present
def graph_contributions_all_time():
    """
    Contributions from 'the beginning' until now (all-time).
    GitHub uses contributionsCollection(from:, to:) in GraphQL. [web:29][web:36]
    """
    query = """
    query($start_date: DateTime!, $end_date: DateTime!, $login: String!) {
      user(login: $login) {
        contributionsCollection(from: $start_date, to: $end_date) {
          contributionCalendar {
            totalContributions
          }
        }
      }
    }
    """
    start_iso = "2025-01-01T00:00:00Z"
    now = datetime.now(timezone.utc)
    end_iso = now.replace(microsecond=0).isoformat().replace("+00:00", "Z")

    variables = {
        "start_date": start_iso,
        "end_date": end_iso,
        "login": USER_NAME,
    }
    r = query_request("graph_contributions_all_time", query, variables)
    total = r.json()["data"]["user"]["contributionsCollection"][
        "contributionCalendar"
    ]["totalContributions"]
    return int(total)

# ==== GATHER STATS FOR SVG ====
def get_stats_for_svg():
    repos, followers, stars = graph_profile_stats()
    contributions = graph_contributions_all_time()

    loc_data = get_total_loc_cached(USER_NAME)

    return {
        "repos": repos,
        "followers": followers,
        "stars": stars,
        "commits": loc_data["total_commits"],
        "loc_total": loc_data["loc_total"],
        "loc_add": loc_data["loc_add"],
        "loc_del": loc_data["loc_del"],
        "contributed": contributions,
    }


def compute_repo_loc(owner, repo, max_commits=200):
    commits_url = f"https://api.github.com/repos/{owner}/{repo}/commits?per_page={min(max_commits, 100)}"
    commits = requests.get(commits_url, headers=HEADERS).json()

    loc_add = loc_del = total_commits = my_commits = 0

    for commit in commits[:max_commits]:
        sha = commit["sha"]
        commit_url = f"https://api.github.com/repos/{owner}/{repo}/commits/{sha}"
        data = requests.get(commit_url, headers=HEADERS).json()

        stats = data.get("stats", {})
        loc_add += stats.get("additions", 0)
        loc_del += stats.get("deletions", 0)
        total_commits += 1
        if data.get("author") and data["author"].get("login") == owner:
            my_commits += 1

    return loc_add, loc_del, total_commits, my_commits


# get total lines of code added and deleted in all repos using REST API
def get_total_loc_cached(username):
    cache_file = get_cache_file(username)
    cache = load_cache(cache_file)

    repos_url = f"https://api.github.com/users/{username}/repos"
    repos = requests.get(repos_url, headers=HEADERS).json()

    updated = False
    total_add = total_del = total_commits = my_commits_total = 0

    for repo in repos:
        repo_id = str(repo["id"])
        pushed_at = repo["pushed_at"]

        cached = cache.get(repo_id)

        if not cached or cached["pushed_at"] != pushed_at:
            loc_add, loc_del, repo_total_commits, repo_my_commits = compute_repo_loc(username, repo["name"])

            cache[repo_id] = {
                "name": repo["name"],
                "pushed_at": pushed_at,
                "loc_add": loc_add,
                "loc_del": loc_del,
                "total_commits": repo_total_commits,
                "my_commits": repo_my_commits
            }
            updated = True

        total_add += cache[repo_id]["loc_add"]
        total_del += cache[repo_id]["loc_del"]
        total_commits += cache[repo_id]["total_commits"]
        my_commits_total += cache[repo_id]["my_commits"]

    if updated:
        save_cache(cache_file, cache)

    return {
        "loc_add": total_add,
        "loc_del": total_del,
        "loc_total": total_add + total_del,
        "total_commits": total_commits,
        "my_commits": my_commits_total
    }



# UPDATE SVG
def update_svg(svg_path: Path, stats: dict):
    ET.register_namespace("", "http://www.w3.org/2000/svg")
    tree = ET.parse(svg_path)
    root = tree.getroot()

    def strip_ns(tag):
        return tag.split("}", 1)[-1]

    for elem in root.iter():
        if strip_ns(elem.tag) != "tspan":
            continue
        el_id = elem.attrib.get("id", "")

        if el_id == "repo_data":
            elem.text = str(stats["repos"])
        elif el_id == "contrib_data":
            elem.text = str(stats["contributed"])
        elif el_id == "star_data":
            elem.text = str(stats["stars"])
        elif el_id == "commit_data":
            elem.text = str(stats["commits"])
        elif el_id == "follower_data":
            elem.text = str(stats["followers"])
        elif el_id == "loc_data":
            elem.text = str(stats["loc_total"])
        elif el_id == "loc_add":
            elem.text = str(stats["loc_add"])
        elif el_id == "loc_del":
            elem.text = str(stats["loc_del"])

    tree.write(svg_path, encoding="utf-8", xml_declaration=True)

if __name__ == "__main__":
    stats = get_stats_for_svg()
    update_svg(SVG_PATH, stats)
    
