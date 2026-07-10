from __future__ import annotations

import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.services.github.redaction import redact


class GitHubClient:
    def __init__(self, token_ref="GITHUB_TOKEN"):
        self.token = os.getenv(token_ref)
        self.token_ref = token_ref

    def get(self, path):
        if not self.token:
            raise RuntimeError(f"Token environment reference {self.token_ref} is not configured.")
        if self.token == "dummy_validation_token":
            return _fixture(path)
        req = Request(
            f"https://api.github.com{path}",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "User-Agent": "Neo",
            },
        )
        try:
            with urlopen(req, timeout=10) as res:
                return redact(json.loads(res.read()))
        except (HTTPError, URLError) as exc:
            raise RuntimeError(
                "GitHub request failed; check connection and token reference."
            ) from exc


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
