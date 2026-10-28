from __future__ import annotations

import re

from app.services.disclosure import (
    Approval,
    Destination,
    GrantStore,
    Label,
    Labeled,
    join,
    label_of,
    require,
)
from app.services.disclosure.egress import EgressTransportError
from app.services.disclosure.egress import request as http_request
from app.services.github.redaction import redact


class GitHubClient:
    def __init__(self, connection_id: str, *, token: str | None = None):
        self.token = token
        self.connection_id = connection_id

    def payload(self, path: Labeled) -> Labeled:
        value = path.value if isinstance(path, Labeled) else path
        if not isinstance(value, str) or not re.fullmatch(
            r"/(?:user|repos/[^/?#]+/[^/?#]+/(?:issues|pulls)/[1-9][0-9]*)",
            value,
        ):
            raise RuntimeError("Unsupported GitHub read endpoint.")
        if not self.token:
            raise RuntimeError("The configured GitHub credential is unavailable.")
        return Labeled(
            {
                "method": "GET",
                "url": f"https://api.github.com{value}",
                "headers": {
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {self.token}",
                    "User-Agent": "Neo",
                },
            },
            join(label_of(path), Label.private(f"credential:github:{self.connection_id}")),
        )

    def get(
        self,
        path: Labeled,
        *,
        destination: Destination | None = None,
        grants: GrantStore | None = None,
        approval: Approval | None = None,
    ) -> Labeled:
        payload = self.payload(path)
        destination = destination or Destination("unscoped-github")
        # The existing offline validation fixture obeys the same authorization.
        if self.token == "dummy_validation_token":
            require("http", payload, destination, grants=grants, approval=approval)
            return Labeled(
                _fixture(path.value if isinstance(path, Labeled) else path), payload.label,
            )
        try:
            result = http_request(
                payload, destination, grants=grants, approval=approval, timeout=10
            )
            response = result.value
            if response.status_code < 200 or response.status_code >= 300:
                raise RuntimeError("GitHub rejected the request.")
            value = response.json()
            if not isinstance(value, dict):
                raise ValueError("invalid response")
            return Labeled(redact(value), result.label)
        except (EgressTransportError, ValueError):
            raise RuntimeError("GitHub request failed; check the connection.") from None


def _fixture(path: str) -> dict:
    if path == "/user":
        return {"login": "neo-validation"}
    is_pr = "/pulls/" in path
    number = path.rsplit("/", 1)[-1]
    return {
        "id": f"fixture-{number}",
        "title": f"Fixture {'PR' if is_pr else 'issue'} #{number}",
        "state": "open",
        "body": "Safe local validation fixture; no credentials or repository content.",
        "labels": [{"name": "fixture"}],
        "html_url": f"https://github.invalid/neo/demo/{'pull' if is_pr else 'issues'}/{number}",
        "user": {"login": "neo-validation"},
        "changed_files": 1 if is_pr else None,
        "comments": 0,
    }
