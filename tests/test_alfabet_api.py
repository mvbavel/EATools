"""Alfabet API client against a mocked transport -- no network, no real credentials."""

import json
import sys
from pathlib import Path
from urllib.parse import parse_qs

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eatools.alfabet_api import AlfabetApiError, AlfabetClient, AlfabetConfig, describe  # noqa: E402

FAKE_PASSWORD = "not-a-real-password-123"
CONFIG = AlfabetConfig("https://alfabet.example", "API_TEST", FAKE_PASSWORD)


def _client(handler):
    return AlfabetClient(CONFIG, http=httpx.Client(transport=httpx.MockTransport(handler)))


def test_token_then_bearer_report_query():
    seen = []

    def handler(req):
        seen.append(req)
        if req.url.path == "/api/token":
            form = parse_qs(req.content.decode())
            assert form["grant_type"] == ["password"] and form["username"] == ["API_TEST"]
            return httpx.Response(200, json={"access_token": "tok-1"})
        assert req.headers["Authorization"] == "Bearer tok-1"
        body = json.loads(req.content)
        assert body["Report"] == "Application-Mark" and body["ReportArgs"] == {"name": "*GDC*"}
        assert body["Limit"] == 10 and body["CurrentProfile"] == "API"
        return httpx.Response(200, json={"rows": []})

    result = _client(handler).run_report("Application-Mark", {"name": "*GDC*"}, limit=10)

    assert result == {"rows": []}
    assert [r.url.path for r in seen] == ["/api/token", "/api/v2/objects"]


def test_expired_token_is_refreshed_once():
    tokens = iter(["old", "new"])
    calls = []

    def handler(req):
        if req.url.path == "/api/token":
            return httpx.Response(200, json={"access_token": next(tokens)})
        calls.append(req.headers["Authorization"])
        return httpx.Response(401 if req.headers["Authorization"] == "Bearer old" else 200, json=[])

    assert _client(handler).run_report("R") == []
    assert calls == ["Bearer old", "Bearer new"]


def test_bad_login_maps_to_safe_message():
    def handler(req):
        return httpx.Response(403, json="The user name or password is not correct.")

    try:
        _client(handler).run_report("R")
    except AlfabetApiError as exc:
        assert "username or password" in str(exc)
        assert FAKE_PASSWORD not in str(exc)
    else:
        raise AssertionError("expected AlfabetApiError")


def test_query_error_carries_alfabet_message():
    def handler(req):
        if req.url.path == "/api/token":
            return httpx.Response(200, json={"access_token": "t"})
        return httpx.Response(400, json={"Message": "Report 'Nope' not found."})

    try:
        _client(handler).run_report("Nope")
    except AlfabetApiError as exc:
        assert "Report 'Nope' not found." in str(exc) and "HTTP 400" in str(exc)
    else:
        raise AssertionError("expected AlfabetApiError")


def test_unreachable_api_maps_to_connectivity_message():
    def handler(req):
        raise httpx.ConnectError("boom", request=req)

    try:
        _client(handler).run_report("R")
    except AlfabetApiError as exc:
        assert "connectivity" in str(exc)
    else:
        raise AssertionError("expected AlfabetApiError")


def test_password_never_in_repr():
    assert FAKE_PASSWORD not in repr(CONFIG)


def test_missing_config_is_reported(monkeypatch=None):
    import os

    saved = {k: os.environ.pop(k, None) for k in ("ALFABET_URL", "ALFABET_USERNAME", "ALFABET_PASSWORD")}
    try:
        AlfabetConfig.from_env()
    except AlfabetApiError as exc:
        assert "ALFABET_PASSWORD" in str(exc)
    else:
        raise AssertionError("expected AlfabetApiError")
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


def test_fetch_all_follows_paging_until_count():
    offsets = []

    def handler(req):
        if req.url.path == "/api/token":
            return httpx.Response(200, json={"access_token": "t"})
        body = json.loads(req.content)
        offsets.append(body["Offset"])
        start, limit = body["Offset"], body["Limit"]
        objs = [{"RefStr": f"326-{i}-0"} for i in range(start, min(start + limit, 5))]
        return httpx.Response(200, json={"Objects": objs, "Count": 5, "Name": "R"})

    objs = _client(handler).fetch_all("R", page_size=2)

    assert [o["RefStr"] for o in objs] == [f"326-{i}-0" for i in range(5)]
    assert offsets == [0, 2, 4]


def test_fetch_all_rejects_unexpected_format():
    def handler(req):
        if req.url.path == "/api/token":
            return httpx.Response(200, json={"access_token": "t"})
        return httpx.Response(200, json=[1, 2])

    try:
        _client(handler).fetch_all("R")
    except AlfabetApiError as exc:
        assert "unexpected report format" in str(exc)
    else:
        raise AssertionError("expected AlfabetApiError")


def test_selection_args_map_company_to_name_suffix():
    from eatools.alfabet_api import selection_args

    assert selection_args(company="ACME") == {"name": "*(ACME)*"}
    assert selection_args(company="ACME", name="Open*") == {"name": "Open**(ACME)*"}
    assert selection_args(name="*SAP*", version="SaaS", objectstate="Active") == {
        "name": "*SAP*", "version": "SaaS", "objectstate": "Active"}
    assert selection_args() == {}


def test_describe_shows_shape():
    lines = describe({"Count": 2, "Rows": [{"Name": "A", "Ref": "1"}, {"Name": "B", "Ref": "2"}]})
    text = "\n".join(lines)
    assert "'Count': int 2" in text and "list of 2" in text and "'Name': str \"A\"" in text


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  OK  {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL  {name}: {exc}")
            except Exception as exc:  # noqa: BLE001 - surface the crash under test
                failures += 1
                print(f"FAIL  {name}: {type(exc).__name__}: {exc}")
    print("all passed" if not failures else f"{failures} failure(s)")
    sys.exit(1 if failures else 0)
