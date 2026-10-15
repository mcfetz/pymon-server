#!/usr/bin/env python3
"""github.py — GitHub repository statistics and security findings. Uses only
the stdlib (urllib).

For every configured repository (``owner/repo``) the plugin reports:

  <repo>:open_issues        open issues (pull requests are excluded)
  <repo>:open_prs           open pull requests
  <repo>:stars              stargazers
  <repo>:watches            watchers (subscribers)
  <repo>:forks              forks
  <repo>:code_findings      open code scanning alerts (e.g. CodeQL)
  <repo>:security_findings  open Dependabot alerts
  <repo>:secret_findings    open secret scanning alerts

Plus the summaries:

  repos_checked / repos_failed

GitHub counts pull requests as issues, so ``open_issues`` is derived from the
repository's ``open_issues_count`` minus the number of open pull requests.

The three finding metrics are only emitted when the corresponding feature is
enabled and readable by the token; a 404 (feature unavailable) is omitted
instead of being reported as zero. A personal access token is required for
private repositories and to read code/secret scanning and Dependabot alerts.
Without a token the anonymous rate limit (60 requests/hour) applies, which is
easily exhausted by a handful of repositories.
"""

import json
import re
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

__schema__ = {
    "label": "GitHub",
    "description": (
        "Repository statistics: open issues/PRs, stars, watchers and "
        "code/security scanning findings"
    ),
    "fields": [
        {
            "key": "sleep",
            "label": "Interval (s)",
            "type": "number",
            "default": 900,
            "min": 60,
        },
        {
            "key": "token",
            "label": "GitHub token (needed for private repos and scanning)",
            "type": "password",
            "optional": True,
        },
        {
            "key": "repos",
            "label": "Repositories (owner/repo)",
            "type": "array:string",
            "default": [],
        },
        {
            "key": "timeout",
            "label": "Timeout per request (s)",
            "type": "number",
            "default": 10,
            "min": 1,
            "max": 30,
        },
    ],
}

API = "https://api.github.com"
MAX_PARALLEL_CHECKS = 6
MAX_PLUGIN_RUNTIME = 25
REQUESTS_PER_REPO = 5
TIMEOUT_DEFAULT = 10
TIMEOUT_MAX = 30

LINK_LAST_RE = re.compile(r'[?&]page=(\d+)>;\s*rel="last"')


def _http(url, token, timeout):
    """GET a GitHub API URL. Returns (status, lowercase headers, body)."""
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "pymon-github",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response_headers = {k.lower(): v for k, v in response.headers.items()}
            return response.status, response_headers, response.read()
    except urllib.error.HTTPError as error:
        response_headers = {k.lower(): v for k, v in (error.headers or {}).items()}
        return error.code, response_headers, b""
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return 0, {}, b""


def _count_items(url, token, timeout):
    """Return the number of items behind a paginated GitHub list endpoint.

    Requests a single item (``per_page=1``) and reads the ``rel="last"`` link,
    so no page is actually downloaded. Returns None when the endpoint is
    unavailable — e.g. a 404 for a disabled feature or a missing permission.
    """
    separator = "&" if "?" in url else "?"
    status, headers, body = _http(f"{url}{separator}per_page=1", token, timeout)
    if status != 200:
        return None
    match = LINK_LAST_RE.search(headers.get("link", ""))
    if match:
        return int(match.group(1))
    try:
        data = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return len(data) if isinstance(data, list) else None


def _repo_metrics(full_name, token, timeout):
    """Collect the metrics for a single repository.

    Returns a metric dict, or None when the repository could not be read.
    """
    status, _, body = _http(f"{API}/repos/{full_name}", token, timeout)
    if status != 200:
        return None
    try:
        meta = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(meta, dict):
        return None

    metrics = {}
    stars = meta.get("stargazers_count")
    if isinstance(stars, int):
        metrics[f"{full_name}:stars"] = stars
    # GitHub's REST API reports watchers_count as a synonym for stars; the
    # actual watcher count lives in subscribers_count.
    watches = meta.get("subscribers_count")
    if isinstance(watches, int):
        metrics[f"{full_name}:watches"] = watches
    forks = meta.get("forks_count")
    if isinstance(forks, int):
        metrics[f"{full_name}:forks"] = forks

    open_prs = _count_items(f"{API}/repos/{full_name}/pulls?state=open", token, timeout)
    if open_prs is not None:
        metrics[f"{full_name}:open_prs"] = open_prs
        # open_issues_count includes pull requests, so subtract them.
        open_total = meta.get("open_issues_count")
        if isinstance(open_total, int):
            metrics[f"{full_name}:open_issues"] = max(0, open_total - open_prs)

    code = _count_items(
        f"{API}/repos/{full_name}/code-scanning/alerts?state=open", token, timeout
    )
    if code is not None:
        metrics[f"{full_name}:code_findings"] = code
    security = _count_items(
        f"{API}/repos/{full_name}/dependabot/alerts?state=open", token, timeout
    )
    if security is not None:
        metrics[f"{full_name}:security_findings"] = security
    secret = _count_items(
        f"{API}/repos/{full_name}/secret-scanning/alerts?state=open", token, timeout
    )
    if secret is not None:
        metrics[f"{full_name}:secret_findings"] = secret

    return metrics


def _normalize_repos(raw_repos):
    """Keep only well-formed ``owner/repo`` entries."""
    if not isinstance(raw_repos, list):
        return []
    repos = []
    for entry in raw_repos:
        name = str(entry or "").strip().strip("/")
        parts = name.split("/")
        if len(parts) == 2 and all(parts):
            repos.append(name)
    return repos


def main():
    config = json.load(sys.stdin)

    repos = _normalize_repos(config.get("repos", []))
    token = str(config.get("token") or "").strip()
    try:
        timeout = max(1, min(TIMEOUT_MAX, int(config.get("timeout", TIMEOUT_DEFAULT))))
    except (TypeError, ValueError):
        timeout = TIMEOUT_DEFAULT

    metrics = {"repos_checked": 0, "repos_failed": 0}
    if repos:
        workers = min(MAX_PARALLEL_CHECKS, len(repos)) or 1
        batches = (len(repos) + workers - 1) // workers
        # Every repository makes REQUESTS_PER_REPO sequential requests; keep the
        # whole run inside the agent's plugin timeout.
        budget = MAX_PLUGIN_RUNTIME // max(1, batches * REQUESTS_PER_REPO)
        effective_timeout = min(timeout, max(1, budget))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            results = executor.map(
                lambda repo: _repo_metrics(repo, token, effective_timeout), repos
            )
            for result in results:
                if result is None:
                    metrics["repos_failed"] += 1
                else:
                    metrics["repos_checked"] += 1
                    metrics.update(result)

    print(json.dumps(metrics))


if __name__ == "__main__":
    main()
