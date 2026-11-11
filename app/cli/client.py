from __future__ import annotations

import json
from dataclasses import dataclass, field
from http.cookiejar import CookieJar
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, OpenerDirector, Request, build_opener
from uuid import uuid4

SESSION_COOKIE = "neo_profile_session"
CSRF_COOKIE = "neo_csrf"
CSRF_HEADER = "x-neo-csrf"


class ApiUnavailableError(RuntimeError):
    pass


class ApiError(RuntimeError):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


class NotSignedIn(ApiError):
    """The server wants a profile session and the CLI holds none that works."""

    def __init__(self, detail: str = "Not signed in. Run `neo login` first."):
        super().__init__(401, detail)


@dataclass
class NeoApiClient:
    base_url: str
    timeout: float = 10.0
    #: The profile session and CSRF token, as ``neo login`` stored them.
    session_token: str | None = None
    csrf_token: str | None = None
    _jar: CookieJar = field(default_factory=CookieJar, repr=False)
    _opener: OpenerDirector | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/")
        self._opener = build_opener(HTTPCookieProcessor(self._jar))

    @property
    def signed_in(self) -> bool:
        return bool(self.session_token)

    def _headers(self, method: str, accept: str = "application/json") -> dict[str, str]:
        headers = {"Accept": accept}
        cookies = []
        if self.session_token:
            cookies.append(f"{SESSION_COOKIE}={self.session_token}")
        if self.csrf_token:
            cookies.append(f"{CSRF_COOKIE}={self.csrf_token}")
            if method.upper() not in {"GET", "HEAD", "OPTIONS"}:
                headers[CSRF_HEADER] = self.csrf_token
        if cookies:
            headers["Cookie"] = "; ".join(cookies)
        return headers

    def _open(self, request: Request):
        assert self._opener is not None
        try:
            return self._opener.open(request, timeout=self.timeout)
        except HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            detail = _detail(raw) or f"API returned HTTP {exc.code}"
            if exc.code == 401 and detail == "authenticated_profile_required":
                raise NotSignedIn() from exc
            raise ApiError(exc.code, detail) from exc
        except URLError as exc:
            raise ApiUnavailableError(str(exc.reason)) from exc

    def request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        if query:
            clean = {key: value for key, value in query.items() if value is not None}
            if clean:
                url = f"{url}?{urlencode(clean)}"
        data = json.dumps(json_body).encode("utf-8") if json_body is not None else None
        request = Request(
            url,
            data=data,
            method=method.upper(),
            headers={"Content-Type": "application/json", **self._headers(method)},
        )
        with self._open(request) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else None

    def cookies_after(self, method: str, path: str, json_body: dict[str, Any]) -> dict[str, str]:
        """Perform a request and return the cookies the server set on it."""

        request = Request(
            f"{self.base_url}{path}",
            data=json.dumps(json_body).encode("utf-8"),
            method=method.upper(),
            headers={"Content-Type": "application/json", **self._headers(method)},
        )
        with self._open(request) as response:
            response.read()
        return {cookie.name: cookie.value or "" for cookie in self._jar}

    def get(self, path: str, query: dict[str, Any] | None = None) -> Any:
        return self.request("GET", path, query=query)

    def post(self, path: str, body: dict[str, Any] | None = None) -> Any:
        return self.request("POST", path, json_body=body or {})

    def download(self, path: str) -> bytes:
        request = Request(
            f"{self.base_url}{path}", headers=self._headers("GET", accept="application/zip")
        )
        with self._open(request) as response:
            return response.read()

    def upload(
        self, path: str, file_name: str, file_data: bytes, fields: dict[str, str] | None = None
    ) -> Any:
        boundary = f"----neo-{uuid4().hex}"
        body = bytearray()
        for key, value in (fields or {}).items():
            body.extend(
                (
                    f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n'
                    f"\r\n{value}\r\n"
                ).encode()
            )
        body.extend(
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
                f'filename="{file_name}"\r\nContent-Type: application/zip\r\n\r\n'
            ).encode()
        )
        body.extend(file_data)
        body.extend(f"\r\n--{boundary}--\r\n".encode())
        request = Request(
            f"{self.base_url}{path}",
            data=bytes(body),
            method="POST",
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                **self._headers("POST"),
            },
        )
        with self._open(request) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else None


def _detail(raw: str) -> str | None:
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        return raw.strip() or None
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, str):
        return detail
    return json.dumps(detail) if detail else raw.strip()
