"""Bizzdesign Alfabet RESTful API (v2) client: token + report queries.

Alfabet exposes data through *reports* configured in the tenant: ``POST /api/v2/objects``
runs a named report (e.g. ``Application-Mark``) with optional ``ReportArgs`` filters
(wildcards allowed, e.g. ``{"name": "*ACME*"}``) and ``Limit``/``Offset`` paging. A
bearer token comes from an OAuth2 password grant on ``/api/token``.

Credentials come only from the environment -- ``ALFABET_URL``, ``ALFABET_USERNAME``,
``ALFABET_PASSWORD`` (the local launcher fills them from the macOS Keychain). They are
never logged, never put in an error message, and never reach the browser. The report
the app imports from is ``ALFABET_REPORT``.

Run ``python -m eatools.alfabet_api --report <name> [--arg key=value ...]`` to print the
*shape* of a report's response (keys, counts, column names) -- what the entity mapping
is written against.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass, field

import httpx

log = logging.getLogger(__name__)

TIMEOUT = 60.0
DEFAULT_PROFILE = "API"
PAGE_SIZE = 1000
# A whole-tenant pull is ~2,500 applications; the cap stops a runaway loop if Alfabet
# ever reports a Count that paging never reaches.
MAX_ROWS = 20000


def is_configured() -> bool:
    return all(os.environ.get(k) for k in ("ALFABET_URL", "ALFABET_USERNAME", "ALFABET_PASSWORD", "ALFABET_REPORT"))


def selection_args(company: str = "", name: str = "", version: str = "", objectstate: str = "") -> dict[str, str]:
    """Selection criteria -> report args, using only arguments the report is known to accept.

    The report has no company argument, but Alfabet names carry the owning company as a
    suffix -- "Payroll Hub (ACME)" -- so a company becomes a name wildcard on that suffix.
    """
    company, name = company.strip(), name.strip()
    args: dict[str, str] = {}
    if company and name:
        args["name"] = f"{name}*({company})*"
    elif company:
        args["name"] = f"*({company})*"
    elif name:
        args["name"] = name
    if version.strip():
        args["version"] = version.strip()
    if objectstate.strip():
        args["objectstate"] = objectstate.strip()
    return args


class AlfabetApiError(Exception):
    """A short, user-safe message. Never carries credentials or the token."""


@dataclass(frozen=True)
class AlfabetConfig:
    url: str
    username: str
    password: str = field(repr=False)
    profile: str = DEFAULT_PROFILE

    @classmethod
    def from_env(cls) -> "AlfabetConfig":
        url = os.environ.get("ALFABET_URL", "").rstrip("/")
        username = os.environ.get("ALFABET_USERNAME", "")
        password = os.environ.get("ALFABET_PASSWORD", "")
        if not (url and username and password):
            raise AlfabetApiError(
                "Alfabet API not configured: set ALFABET_URL, ALFABET_USERNAME and ALFABET_PASSWORD."
            )
        return cls(url, username, password, os.environ.get("ALFABET_PROFILE", DEFAULT_PROFILE))


class AlfabetClient:
    def __init__(self, config: AlfabetConfig, http: httpx.Client | None = None) -> None:
        self._config = config
        self._http = http or httpx.Client(timeout=TIMEOUT)
        self._token: str | None = None

    def _fetch_token(self) -> str:
        try:
            res = self._http.post(
                f"{self._config.url}/api/token",
                data={
                    "grant_type": "password",
                    "username": self._config.username,
                    "password": self._config.password,
                },
            )
        except httpx.HTTPError as exc:
            log.warning("Alfabet token request failed: %s", type(exc).__name__)
            raise AlfabetApiError("Could not reach the Alfabet API -- check connectivity.") from None
        # Alfabet answers a bad login with 403 and a bare JSON string, not an OAuth error.
        if res.status_code in (400, 401, 403):
            raise AlfabetApiError("Alfabet rejected the API username or password.")
        if res.status_code >= 400:
            raise AlfabetApiError(f"Alfabet token request failed (HTTP {res.status_code}).")
        try:
            token = res.json().get("access_token")
        except (ValueError, AttributeError):
            token = None
        if not token:
            raise AlfabetApiError("Alfabet returned no access token.")
        return token

    def run_report(
        self,
        report: str,
        args: dict[str, str] | None = None,
        limit: int = 1000,
        offset: int = 0,
    ):
        """Run one page of a named Alfabet report and return the decoded JSON as-is."""
        body = {
            "CurrentProfile": self._config.profile,
            "CurrentMandate": "",
            "EmptyValues": True,
            "Report": report,
            "ReportResult": "DataSet",
            "Limit": limit,
            "Offset": offset,
        }
        if args:
            body["ReportArgs"] = args

        res = None
        # One retry: a 401 on a cached token usually just means it expired.
        for attempt in range(2):
            if self._token is None:
                self._token = self._fetch_token()
            try:
                res = self._http.post(
                    f"{self._config.url}/api/v2/objects",
                    json=body,
                    headers={"Authorization": f"Bearer {self._token}"},
                )
            except httpx.HTTPError as exc:
                log.warning("Alfabet report request failed: %s", type(exc).__name__)
                raise AlfabetApiError("Could not reach the Alfabet API -- check connectivity.") from None
            if res.status_code == 401 and attempt == 0:
                self._token = None
                continue
            break

        if res.status_code == 401:
            raise AlfabetApiError("Alfabet denied access to this report for the API user.")
        if res.status_code >= 400:
            raise AlfabetApiError(f"Alfabet rejected the report query (HTTP {res.status_code}): {_message(res)}")
        try:
            return res.json()
        except ValueError:
            raise AlfabetApiError("Alfabet returned a response that is not JSON.") from None

    def fetch_all(self, report: str, args: dict[str, str] | None = None, page_size: int = PAGE_SIZE) -> list[dict]:
        """Every object the report returns for `args`, following Limit/Offset paging.

        `Count` in each response is the total number of matches, not the page size.
        """
        objects: list[dict] = []
        offset = 0
        while True:
            page = self.run_report(report, args, limit=page_size, offset=offset)
            if not isinstance(page, dict) or not isinstance(page.get("Objects"), list):
                raise AlfabetApiError("Alfabet returned an unexpected report format.")
            batch = page["Objects"]
            objects.extend(batch)
            offset += len(batch)
            total = page.get("Count") if isinstance(page.get("Count"), int) else None
            if not batch or len(batch) < page_size or (total is not None and offset >= total):
                return objects
            if offset >= MAX_ROWS:
                raise AlfabetApiError(f"Selection matches more than {MAX_ROWS} objects; narrow it down.")


def fetch_selection(
    company: str = "", name: str = "", version: str = "", objectstate: str = ""
) -> tuple[list[dict], dict[str, str]]:
    """Run the configured report for the selection; returns (objects, report args used)."""
    if not is_configured():
        raise AlfabetApiError(
            "Alfabet API not configured: set ALFABET_URL, ALFABET_USERNAME, ALFABET_PASSWORD and ALFABET_REPORT."
        )
    args = selection_args(company, name, version, objectstate)
    client = AlfabetClient(AlfabetConfig.from_env())
    return client.fetch_all(os.environ["ALFABET_REPORT"], args), args


def _message(res: httpx.Response) -> str:
    """Alfabet's own error text, trimmed -- it describes the query, not the caller."""
    try:
        data = res.json()
    except ValueError:
        return res.text[:200]
    if isinstance(data, dict):
        return str(data.get("Message") or data.get("message") or data)[:200]
    return str(data)[:200]


def describe(obj, indent: int = 0, max_keys: int = 60) -> list[str]:
    """Shape of a JSON value: keys, types, list lengths, first element. Values shortened."""
    pad = "  " * indent
    if isinstance(obj, dict):
        lines = [f"{pad}object with {len(obj)} keys"]
        for key in list(obj)[:max_keys]:
            value = obj[key]
            if isinstance(value, (dict, list)):
                lines.append(f"{pad}  {key!r}:")
                lines.extend(describe(value, indent + 2, max_keys))
            else:
                lines.append(f"{pad}  {key!r}: {_scalar(value)}")
        return lines
    if isinstance(obj, list):
        lines = [f"{pad}list of {len(obj)}"]
        if obj:
            lines.append(f"{pad}  [0]:")
            lines.extend(describe(obj[0], indent + 2, max_keys))
        return lines
    return [f"{pad}{_scalar(obj)}"]


def _scalar(value) -> str:
    text = json.dumps(value, ensure_ascii=False)
    return f"{type(value).__name__} {text[:60]}{'...' if len(text) > 60 else ''}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print the shape of an Alfabet report response.")
    parser.add_argument("--report", required=True, help="Alfabet report name, e.g. Application-Mark")
    parser.add_argument("--arg", action="append", default=[], help="ReportArgs filter key=value (repeatable)")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--save", help="Also write the raw JSON response to this file")
    opts = parser.parse_args(argv)

    args = {}
    for item in opts.arg:
        key, sep, value = item.partition("=")
        if not sep:
            parser.error(f"--arg must be key=value, got {item!r}")
        args[key] = value

    try:
        result = AlfabetClient(AlfabetConfig.from_env()).run_report(opts.report, args, limit=opts.limit)
    except AlfabetApiError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print("\n".join(describe(result)))
    if opts.save:
        with open(opts.save, "w", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False, indent=2)
        print(f"raw response written to {opts.save}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
