"""Tests for the github plugin's metric mapping.

Everything is mocked: no network and no token are required. The tests pin the
two easy-to-get-wrong guarantees:

* ``open_issues`` excludes pull requests (GitHub's ``open_issues_count``
  includes them), and
* the scanning metrics are only emitted when the endpoint is actually
  available (a 404 is omitted, not reported as zero).

Run with:  python tests/test_github.py
"""

import importlib.util
import json
import os
import sys
from contextlib import contextmanager

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PLUGIN_PATH = os.path.join(ROOT, "plugins", "github.py")


def _load_plugin():
    spec = importlib.util.spec_from_file_location("github_plugin", PLUGIN_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


github = _load_plugin()


@contextmanager
def patched(**attrs):
    """Temporarily replace module-level attributes and restore them."""
    saved = {}
    for key, value in attrs.items():
        saved[key] = getattr(github, key)
        setattr(github, key, value)
    try:
        yield
    finally:
        for key, value in saved.items():
            setattr(github, key, value)


def _router(responses):
    """Build a fake _http that answers the first matching URL fragment.

    ``responses`` must be ordered most-specific first, because fragments such
    as ``repos/owner/repo`` also match the scanning URLs.
    """

    def http(url, token, timeout):
        for fragment, reply in responses:
            if fragment in url:
                return reply
        return 404, {}, b""

    return http


REPO_META = json.dumps(
    {
        "open_issues_count": 7,
        "stargazers_count": 12,
        "subscribers_count": 3,
        "forks_count": 4,
    }
).encode("utf-8")

LINK = {
    "link": '<https://api.github.com/repos/o/r/pulls?state=open&per_page=1&page=2>; rel="last"'
}


# ---------------------------------------------------------------------------
# _count_items
# ---------------------------------------------------------------------------


def test_count_items_reads_last_page_from_link_header():
    with patched(_http=lambda url, token, timeout: (200, LINK, b"[]")):
        assert github._count_items("https://api.github.com/x", "", 5) == 2


def test_count_items_counts_single_page_without_link():
    with patched(_http=lambda url, token, timeout: (200, {}, b'[{"n": 1}]')):
        assert github._count_items("https://api.github.com/x", "", 5) == 1


def test_count_items_returns_none_when_unavailable():
    with patched(_http=lambda url, token, timeout: (404, {}, b"")):
        assert github._count_items("https://api.github.com/x", "", 5) is None


# ---------------------------------------------------------------------------
# _repo_metrics
# ---------------------------------------------------------------------------


def test_repo_metrics_open_issues_exclude_pull_requests():
    responses = [
        ("code-scanning", (404, {}, b"")),
        ("secret-scanning", (200, {}, b"[]")),
        ("dependabot", (200, {}, b"[]")),
        ("/pulls", (200, LINK, b"[]")),
        ("repos/o/r", (200, {}, REPO_META)),
    ]
    with patched(_http=_router(responses)):
        metrics = github._repo_metrics("o/r", "", 5)

    assert metrics["o/r:open_prs"] == 2
    assert metrics["o/r:open_issues"] == 5  # 7 total - 2 pull requests
    assert metrics["o/r:stars"] == 12
    assert metrics["o/r:watches"] == 3
    assert metrics["o/r:forks"] == 4
    assert metrics["o/r:secret_findings"] == 0
    assert metrics["o/r:security_findings"] == 0


def test_scanning_metrics_omitted_when_endpoint_unavailable():
    responses = [
        ("code-scanning", (404, {}, b"")),
        ("secret-scanning", (404, {}, b"")),
        ("dependabot", (404, {}, b"")),
        ("/pulls", (200, {}, b"[]")),
        ("repos/o/r", (200, {}, REPO_META)),
    ]
    with patched(_http=_router(responses)):
        metrics = github._repo_metrics("o/r", "", 5)

    assert "o/r:code_findings" not in metrics
    assert "o/r:security_findings" not in metrics
    assert "o/r:secret_findings" not in metrics
    assert metrics["o/r:open_prs"] == 0
    assert metrics["o/r:open_issues"] == 7


def test_repo_metrics_none_when_repo_unreadable():
    with patched(_http=lambda url, token, timeout: (404, {}, b"")):
        assert github._repo_metrics("o/r", "", 5) is None


# ---------------------------------------------------------------------------
# _normalize_repos
# ---------------------------------------------------------------------------


def test_normalize_repos_keeps_only_owner_repo():
    assert github._normalize_repos(["o/r", " o/r ", "o/r/", "bad", "", 3, "a/b/c"]) == [
        "o/r",
        "o/r",
        "o/r",
    ]


def main():
    """Run every test_* function; pytest is not installed in the venv."""
    tests = [
        (name, value)
        for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    ]
    failed = []
    for name, test in tests:
        try:
            test()
            print(f"  PASS  {name}")
        except Exception as exc:  # noqa: BLE001 - report, do not abort the run
            failed.append(name)
            print(f"  FAIL  {name}: {exc}")
    print()
    print(f"{len(tests) - len(failed)}/{len(tests)} bestanden")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
