from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import uuid4


class ApiUnavailableError(RuntimeError):
    pass


class ApiError(RuntimeError):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass
class NeoApiClient:
    base_url: str
    timeout: float = 10.0

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
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
                return json.loads(raw) if raw else None
        except HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            detail = _detail(raw) or f"API returned HTTP {exc.code}"
            raise ApiError(exc.code, detail) from exc
        except URLError as exc:
            raise ApiUnavailableError(str(exc.reason)) from exc

    def get(self, path: str, query: dict[str, Any] | None = None) -> Any:
        return self.request("GET", path, query=query)

    def post(self, path: str, body: dict[str, Any] | None = None) -> Any:
        return self.request("POST", path, json_body=body or {})

    def download(self, path: str) -> bytes:
        request = Request(f"{self.base_url}{path}", headers={"Accept": "application/zip"})
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return response.read()
        except HTTPError as exc:
            raise ApiError(
                exc.code, _detail(exc.read().decode("utf-8", "replace")) or "Download failed"
            ) from exc
        except URLError as exc:
            raise ApiUnavailableError(str(exc.reason)) from exc

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
                "Accept": "application/json",
            },
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
                return json.loads(raw) if raw else None
        except HTTPError as exc:
            raise ApiError(
                exc.code, _detail(exc.read().decode("utf-8", "replace")) or "Upload failed"
            ) from exc
        except URLError as exc:
            raise ApiUnavailableError(str(exc.reason)) from exc


def _detail(raw: str) -> str | None:
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        return raw.strip() or None
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, str):
        return detail
    return json.dumps(detail) if detail else raw.strip()
