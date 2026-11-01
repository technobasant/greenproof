"""The PR comment reader (SPEC 6.6) against recorded API JSON. No network is touched."""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from greenproof import github, pipeline
from greenproof.github import HttpResponse
from tests.helpers import Repo, load_json, make_repo, policy_text
from tests.schema_check import load_schema, validate

FIXTURES = Path(__file__).parent / "fixtures" / "github"
API = "https://api.github.example/api/v3"
FP1, FP2, FP3, FP4 = "a" * 12, "b" * 12, "c" * 12, "d" * 12
PAGE1_URL = f"{API}/repos/acme/widgets/issues/7/comments?per_page=100"
PAGE2_URL = f"{API}/repos/acme/widgets/issues/7/comments?per_page=100&page=2"


def recorded(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


class FakeApi:
    """Replays recorded responses by URL and remembers every request."""

    def __init__(self, responses: Mapping[str, HttpResponse | Exception]) -> None:
        self.responses = dict(responses)
        self.requests: list[tuple[str, dict[str, str], float]] = []

    def __call__(self, url: str, headers: Mapping[str, str], timeout: float) -> HttpResponse:
        self.requests.append((url, dict(headers), timeout))
        response = self.responses[url]
        if isinstance(response, Exception):
            raise response
        return response


def paged_api() -> FakeApi:
    return FakeApi(
        {
            PAGE1_URL: HttpResponse(
                recorded("comments-page1.json"),
                f'<{PAGE2_URL}>; rel="next", <{API}/repos/acme/widgets/issues/7/comments?page=2>;'
                ' rel="last"',
            ),
            PAGE2_URL: HttpResponse(recorded("comments-page2.json"), None),
        }
    )


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"action": "opened", "pull_request": {"number": 7}}))
    return {
        "GITHUB_EVENT_NAME": "pull_request",
        "GITHUB_EVENT_PATH": str(event),
        "GITHUB_API_URL": API,
        "GITHUB_REPOSITORY": "acme/widgets",
        "GITHUB_TOKEN": "t0ken",
    }


# ---- who may accept ----------------------------------------------------------------------


def test_trusted_humans_accept_across_pages_and_bots_and_outsiders_cannot(
    env: dict[str, str],
) -> None:
    api = paged_api()
    accepted, notes = github.github_acceptances(env, api)
    assert notes == []
    # FP1/FP2: OWNER on page 1. FP4: MEMBER on page 2 (upper-case command and hex, trailing
    # prose ignored). FP3 was asked for by a bot, a contributor, a stranger and (mid-line,
    # not as a command) a collaborator, so it is not accepted.
    assert set(accepted) == {FP1, FP2, FP4}
    one = accepted[FP1]
    assert (one.by, one.at, one.source, one.reason) == (
        "maintainer",
        "2026-11-01T10:00:00Z",
        "github",
        None,
    )
    assert accepted[FP4].by == "teammate"
    assert accepted[FP4].at == "2026-11-01T11:00:00Z"


def test_a_collaborator_command_line_accepts(env: dict[str, str]) -> None:
    comments = [
        {
            "user": {"login": "c", "type": "User"},
            "author_association": "COLLABORATOR",
            "created_at": "2026-11-01T12:00:00Z",
            "body": f"/greenproof accept {FP3}",
        }
    ]
    assert set(github.parse_acceptances(comments)) == {FP3}


def test_untrusted_or_malformed_comments_accept_nothing() -> None:
    body = f"/greenproof accept {FP1}"
    ok_user = {"login": "u", "type": "User"}
    comments: list[dict[str, Any]] = [
        {"user": {"login": "x", "type": "Bot"}, "author_association": "OWNER", "body": body},
        {
            "user": {"login": "x", "type": "Organization"},
            "author_association": "OWNER",
            "body": body,
        },
        {"user": ok_user, "author_association": "CONTRIBUTOR", "body": body},
        {"user": ok_user, "author_association": "FIRST_TIME_CONTRIBUTOR", "body": body},
        {"user": ok_user, "author_association": "OWNER", "body": 3},
        {"user": None, "author_association": "OWNER", "body": body},
        {"user": ok_user, "body": body},
        {"user": ok_user, "author_association": "OWNER", "body": "/greenproof accept nothex"},
        {"user": ok_user, "author_association": "OWNER", "body": f"/greenproof accept {FP1}0"},
    ]
    assert github.parse_acceptances(comments) == {}


def test_missing_login_and_timestamp_do_not_break_parsing() -> None:
    comments = [
        {
            "user": {"type": "User"},
            "author_association": "OWNER",
            "body": f"/greenproof accept {FP1}",
        }
    ]
    accepted = github.parse_acceptances(comments)
    assert (accepted[FP1].by, accepted[FP1].at) == ("unknown", "")


# ---- requests ------------------------------------------------------------------------------


def test_requests_follow_link_pagination_with_auth_and_a_ten_second_timeout(
    env: dict[str, str],
) -> None:
    api = paged_api()
    github.github_acceptances(env, api)
    assert [r[0] for r in api.requests] == [PAGE1_URL, PAGE2_URL]
    for _url, headers, timeout in api.requests:
        assert timeout == 10
        assert headers["Authorization"] == "Bearer t0ken"
        assert headers["Accept"] == "application/vnd.github+json"
        assert headers["User-Agent"] == "greenproof"


def test_without_a_token_the_request_is_unauthenticated(env: dict[str, str]) -> None:
    del env["GITHUB_TOKEN"]
    api = FakeApi({PAGE1_URL: HttpResponse(b"[]")})
    assert github.github_acceptances(env, api) == ({}, [])
    assert "Authorization" not in api.requests[0][1]


def test_default_api_url(env: dict[str, str]) -> None:
    del env["GITHUB_API_URL"]
    url = "https://api.github.com/repos/acme/widgets/issues/7/comments?per_page=100"
    api = FakeApi({url: HttpResponse(b"[]")})
    assert github.github_acceptances(env, api) == ({}, [])


def test_the_pull_request_target_event_is_read_too(env: dict[str, str]) -> None:
    env["GITHUB_EVENT_NAME"] = "pull_request_target"
    api = FakeApi({PAGE1_URL: HttpResponse(b"[]")})
    assert github.github_acceptances(env, api) == ({}, [])


# ---- failures are no acceptances plus a note -------------------------------------------------


def failing(env: dict[str, str], api: FakeApi) -> str:
    accepted, notes = github.github_acceptances(env, api)
    assert accepted == {}
    assert len(notes) == 1
    assert notes[0].startswith("could not read PR comments: ")
    assert notes[0].endswith("no findings were accepted")
    return notes[0]


def test_an_http_error_accepts_nothing_and_leaves_a_note(env: dict[str, str]) -> None:
    error = urllib.error.HTTPError(PAGE1_URL, 403, "rate limit exceeded", {}, None)
    assert "403" in failing(env, FakeApi({PAGE1_URL: error}))


def test_a_timeout_or_connection_failure_accepts_nothing(env: dict[str, str]) -> None:
    assert "timed out" in failing(env, FakeApi({PAGE1_URL: TimeoutError("timed out")}))
    assert "refused" in failing(
        env, FakeApi({PAGE1_URL: urllib.error.URLError(ConnectionRefusedError("refused"))})
    )


def test_a_failure_on_a_later_page_discards_the_earlier_acceptances(env: dict[str, str]) -> None:
    api = FakeApi(
        {
            PAGE1_URL: HttpResponse(recorded("comments-page1.json"), f'<{PAGE2_URL}>; rel="next"'),
            PAGE2_URL: OSError("connection reset"),
        }
    )
    assert "connection reset" in failing(env, api)


def test_an_unparseable_or_unexpected_body_accepts_nothing(env: dict[str, str]) -> None:
    assert "Expecting value" in failing(env, FakeApi({PAGE1_URL: HttpResponse(b"<html>")}))
    assert "expected a list" in failing(
        env, FakeApi({PAGE1_URL: HttpResponse(b'{"message": "Not Found"}')})
    )


def test_a_pagination_link_to_another_host_is_refused_so_the_token_stays_home(
    env: dict[str, str],
) -> None:
    other = "https://evil.example/steal"
    api = FakeApi({PAGE1_URL: HttpResponse(b"[]", f'<{other}>; rel="next"')})
    assert "another host" in failing(env, api)
    assert [r[0] for r in api.requests] == [PAGE1_URL]


def test_endless_pagination_is_cut_off(env: dict[str, str]) -> None:
    api = FakeApi({PAGE1_URL: HttpResponse(b"[]", f'<{PAGE1_URL}>; rel="next"')})
    assert "pages" in failing(env, api)
    assert len(api.requests) == github.MAX_PAGES


def test_bad_environment_accepts_nothing(env: dict[str, str], tmp_path: Path) -> None:
    api = FakeApi({})
    for key, value in (
        ("GITHUB_REPOSITORY", "not-a-repo"),
        ("GITHUB_REPOSITORY", ""),
        ("GITHUB_EVENT_PATH", ""),
        ("GITHUB_EVENT_PATH", str(tmp_path / "missing.json")),
    ):
        bad = {**env, key: value}
        failing(bad, api)
    garbage = tmp_path / "garbage.json"
    for text in ("{", "[]", '{"pull_request": {}}', '{"pull_request": {"number": true}}'):
        garbage.write_text(text)
        failing({**env, "GITHUB_EVENT_PATH": str(garbage)}, api)
    assert api.requests == []


def test_outside_a_pull_request_event_nothing_is_read(env: dict[str, str]) -> None:
    for name in ("push", "issue_comment", ""):
        env["GITHUB_EVENT_NAME"] = name
        api = FakeApi({})
        accepted, notes = github.github_acceptances(env, api)
        assert accepted == {}
        assert "not a pull_request event" in notes[0]
        assert api.requests == []


# ---- the real fetch ---------------------------------------------------------------------------


class _Response(io.BytesIO):
    def __init__(self, body: bytes, headers: dict[str, str]) -> None:
        super().__init__(body)
        self.headers = headers

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def test_urllib_fetch_returns_the_body_and_the_link_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    def fake_urlopen(request: urllib.request.Request, timeout: float) -> _Response:
        seen["url"], seen["timeout"] = request.full_url, timeout
        seen["auth"] = request.get_header("Authorization")
        return _Response(b"[1]", {"Link": '<x>; rel="next"'})

    monkeypatch.setattr(github.urllib.request, "urlopen", fake_urlopen)
    response = github.urllib_fetch("https://api.example/x", {"Authorization": "Bearer t"}, 10.0)
    assert (response.body, response.link) == (b"[1]", '<x>; rel="next"')
    assert seen == {"url": "https://api.example/x", "timeout": 10.0, "auth": "Bearer t"}


# ---- through the check pipeline ----------------------------------------------------------------


def ci_repo(tmp_path: Path) -> Repo:
    repo = make_repo(tmp_path / "ci", policy_text("true"))
    repo.remove("tests/test_app.py")  # tests.deleted blocks
    return repo


def ci_opts(
    repo: Repo, env: dict[str, str], api: FakeApi | None, **kw: Any
) -> pipeline.CheckOptions:
    return pipeline.CheckOptions(
        cwd=repo.path,
        mode="ci",
        github_accept=True,
        env={**env, "GITHUB_BASE_REF": ""},
        fetch=api,
        **kw,
    )


def comments_for(fingerprint: str, who: str = "maintainer", assoc: str = "OWNER") -> bytes:
    return json.dumps(
        [
            {
                "user": {"login": who, "type": "User"},
                "author_association": assoc,
                "created_at": "2026-11-01T09:00:00Z",
                "body": f"/greenproof accept {fingerprint}",
            }
        ]
    ).encode()


def test_ci_run_a_blocking_finding_shows_the_comment_to_post_then_accepts_it(
    tmp_path: Path, env: dict[str, str]
) -> None:
    repo = ci_repo(tmp_path)
    first = pipeline.run_check(ci_opts(repo, env, FakeApi({PAGE1_URL: HttpResponse(b"[]")})))
    assert first.exit_code == 1
    from greenproof.report import render_markdown

    fp = first.findings[0].fingerprint
    assert f"/greenproof accept {fp}" in render_markdown(first)

    api = FakeApi({PAGE1_URL: HttpResponse(comments_for(fp))})
    second = pipeline.run_check(ci_opts(repo, env, api))
    assert second.exit_code == 0
    assert second.verdict == "pass"
    assert second.accepted[fp].by == "maintainer"
    assert f"/greenproof accept {fp}" not in render_markdown(second)


def test_ci_run_an_unrelated_accept_leaves_the_finding_blocking(
    tmp_path: Path, env: dict[str, str]
) -> None:
    repo = ci_repo(tmp_path)
    api = FakeApi({PAGE1_URL: HttpResponse(comments_for(FP1))})
    assert pipeline.run_check(ci_opts(repo, env, api)).exit_code == 1


def test_ci_run_an_api_failure_fails_closed_with_a_note(
    tmp_path: Path, env: dict[str, str]
) -> None:
    repo = ci_repo(tmp_path)
    api = FakeApi({PAGE1_URL: OSError("boom")})
    report = pipeline.run_check(ci_opts(repo, env, api))
    assert report.exit_code == 1
    assert any("could not read PR comments: boom" in n for n in report.notes)


def test_ci_mode_ignores_the_local_acceptances_file(tmp_path: Path, env: dict[str, str]) -> None:
    from greenproof import accept

    repo = ci_repo(tmp_path)
    local = pipeline.run_check(pipeline.CheckOptions(cwd=repo.path, mode="local"))
    fp = local.findings[0].fingerprint
    accept.accept(repo.path, [fp], None)
    assert pipeline.run_check(pipeline.CheckOptions(cwd=repo.path, mode="local")).exit_code == 0
    # ci without --github-accept reads no comments and honours no local file.
    plain = pipeline.CheckOptions(cwd=repo.path, mode="ci", env={"GITHUB_BASE_REF": ""})
    assert pipeline.run_check(plain).exit_code == 1


def test_cli_github_accept_flag_uses_the_real_fetch_hook(
    tmp_path: Path, env: dict[str, str], run, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = ci_repo(tmp_path)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    fp = load_fingerprint(repo, run, tmp_path)
    api = FakeApi({PAGE1_URL: HttpResponse(comments_for(fp))})
    monkeypatch.setattr(github, "urllib_fetch", api)
    out = tmp_path / "r.json"
    result = run(
        repo.path,
        "check",
        "--mode",
        "ci",
        "--github-accept",
        "--github-annotations",
        "--json",
        str(out),
    )
    assert result.code == 0, result.out
    assert "ACCEPTED" in result.out
    assert "::notice file=tests/test_app.py,title=tests.deleted::" in result.out
    data = load_json(out)
    assert validate(data, load_schema()) == []
    accepted = data["findings"][0]["accepted"]
    assert accepted["source"] == "github"
    assert accepted["by"] == "maintainer"
    assert data["verdict"] == "pass"


def load_fingerprint(repo: Repo, run, tmp_path: Path) -> str:
    path = tmp_path / "first.json"
    run(repo.path, "check", "--json", str(path))
    return str(load_json(path)["findings"][0]["fingerprint"])
