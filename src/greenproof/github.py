"""Acceptances from pull request comments (SPEC 6.6, the authoritative path in CI).

A comment line `/greenproof accept FP [FP ...]` accepts those fingerprints when the comment's
`author_association` is OWNER, MEMBER or COLLABORATOR and the author's `user.type` is `User`.
Bots and outside contributors cannot accept. Any failure yields no acceptances and a note.

The HTTP fetch is injectable so tests replay recorded API JSON without a network.
"""

from __future__ import annotations

import json
import re
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from greenproof.accept import Acceptance, is_fingerprint

TIMEOUT_SECONDS = 10.0
MAX_PAGES = 20
PR_EVENTS = ("pull_request", "pull_request_target")
TRUSTED_ASSOCIATIONS = ("OWNER", "MEMBER", "COLLABORATOR")
DEFAULT_API_URL = "https://api.github.com"
_REPOSITORY = re.compile(r"[\w.-]+/[\w.-]+")
_ACCEPT_LINE = re.compile(r"^\s*/greenproof\s+accept\s+(.+)$", re.IGNORECASE)
_NEXT_LINK = re.compile(r'<([^>]+)>\s*;[^,]*\brel="?next"?')


class GithubError(Exception):
    """The PR comments could not be read."""


@dataclass(frozen=True)
class HttpResponse:
    body: bytes
    link: str | None = None


Fetcher = Callable[[str, Mapping[str, str], float], HttpResponse]


def urllib_fetch(url: str, headers: Mapping[str, str], timeout: float) -> HttpResponse:
    request = urllib.request.Request(url, headers=dict(headers))
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return HttpResponse(response.read(), response.headers.get("Link"))


def is_pr_event(env: Mapping[str, str]) -> bool:
    return env.get("GITHUB_EVENT_NAME", "") in PR_EVENTS


def _pr_number(env: Mapping[str, str]) -> int:
    path = env.get("GITHUB_EVENT_PATH", "")
    if not path:
        raise GithubError("GITHUB_EVENT_PATH is not set")
    try:
        with open(path) as handle:
            event = json.load(handle)
    except (OSError, ValueError) as exc:
        raise GithubError(f"cannot read the event payload: {exc}") from exc
    pull = event.get("pull_request") if isinstance(event, dict) else None
    number = pull.get("number") if isinstance(pull, dict) else None
    if isinstance(number, bool) or not isinstance(number, int):
        raise GithubError("the event payload has no pull request number")
    return number


def _next_url(link: str | None) -> str | None:
    match = _NEXT_LINK.search(link) if link else None
    return match.group(1) if match else None


def _same_origin(a: str, b: str) -> bool:
    left, right = urlsplit(a), urlsplit(b)
    return (left.scheme, left.netloc) == (right.scheme, right.netloc)


def fetch_comments(env: Mapping[str, str], fetch: Fetcher) -> list[dict[str, Any]]:
    """Every issue comment on the PR, following `Link: rel="next"` pagination."""
    api = env.get("GITHUB_API_URL", "").strip().rstrip("/") or DEFAULT_API_URL
    repository = env.get("GITHUB_REPOSITORY", "")
    if not _REPOSITORY.fullmatch(repository):
        raise GithubError(f"GITHUB_REPOSITORY is not owner/name: {repository!r}")
    number = _pr_number(env)
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "greenproof",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = env.get("GITHUB_TOKEN", "")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = f"{api}/repos/{repository}/issues/{number}/comments?per_page=100"
    comments: list[dict[str, Any]] = []
    for _ in range(MAX_PAGES):
        try:
            response = fetch(url, headers, TIMEOUT_SECONDS)
            page = json.loads(response.body)
        except (OSError, ValueError) as exc:  # URLError and HTTPError are OSErrors
            raise GithubError(str(exc)) from exc
        if not isinstance(page, list):
            raise GithubError("unexpected response: expected a list of comments")
        comments.extend(c for c in page if isinstance(c, dict))
        following = _next_url(response.link)
        if following is None:
            return comments
        if not _same_origin(following, api):  # never send the token to another host
            raise GithubError(f"refusing to follow a pagination link to another host: {following}")
        url = following
    raise GithubError(f"more than {MAX_PAGES} pages of comments")


def parse_acceptances(comments: list[dict[str, Any]]) -> dict[str, Acceptance]:
    """Fingerprint to acceptance, from trusted human comments only."""
    out: dict[str, Acceptance] = {}
    for comment in comments:
        user = comment.get("user")
        if not isinstance(user, dict) or user.get("type") != "User":
            continue
        if comment.get("author_association") not in TRUSTED_ASSOCIATIONS:
            continue
        body = comment.get("body")
        if not isinstance(body, str):
            continue
        by = user.get("login")
        at = comment.get("created_at")
        for line in body.splitlines():
            match = _ACCEPT_LINE.match(line)
            if not match:
                continue
            for token in match.group(1).split():
                if not is_fingerprint(token.lower()):
                    break
                out[token.lower()] = Acceptance(
                    by if isinstance(by, str) else "unknown",
                    at if isinstance(at, str) else "",
                    "github",
                )
    return out


def github_acceptances(
    env: Mapping[str, str], fetch: Fetcher | None = None
) -> tuple[dict[str, Acceptance], list[str]]:
    """(acceptances, report notes). Any failure is no acceptances and a note (fail closed)."""
    if not is_pr_event(env):
        return {}, ["--github-accept: not a pull_request event, so no findings were accepted"]
    try:
        comments = fetch_comments(env, fetch or urllib_fetch)
    except GithubError as exc:
        return {}, [f"could not read PR comments: {exc}; no findings were accepted"]
    return parse_acceptances(comments), []
