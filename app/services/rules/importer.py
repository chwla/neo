from __future__ import annotations

import json
import uuid
from pathlib import Path

from app.services.repos.store import get_repo
from app.services.rules import store
from app.services.rules.safety import sanitize_rules

RULE_FILES = ("AGENTS.md", "NEO_RULES.md", ".neo/rules.json")


class RepoRuleImporter:
    def import_repo(self, repo_id: str) -> dict:
        repo = get_repo(repo_id)
        if not repo:
            raise LookupError("Managed repository not found.")
        root = Path(repo["workspace_path"]).resolve()
        imported = []
        warnings = []
        for relative in RULE_FILES:
            path = (root / relative).resolve()
            if root not in path.parents or not path.is_file():
                continue
            enabled = True
            try:
                text = path.read_text(encoding="utf-8")
                if relative.endswith(".json"):
                    payload = json.loads(text)
                    local_warnings = []
                    rules = sanitize_rules(payload, local_warnings, relative)
                    warnings.extend(local_warnings)
                else:
                    rules = {"instructions": [text.strip()]}
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                enabled = False
                rules = {"metadata": {"import_error": str(exc)}}
                warnings.append(f"{relative}: invalid rule file; imported disabled ({exc}).")
            existing = store.find_source_profile(repo_id, relative)
            now = store.now_iso()
            data = {
                "scope_type": "repo",
                "scope_id": repo_id,
                "name": f"Repo rules: {relative}",
                "description": "Imported from Neo's managed repository copy.",
                "enabled": enabled,
                "priority": 100,
                "rules": rules,
                "source_type": "file",
                "source_path": relative,
                "updated_at": now,
            }
            if existing:
                profile = store.update_profile(existing["id"], data)
            else:
                profile = store.insert_profile({"id": str(uuid.uuid4()), **data, "created_at": now})
            imported.append(profile)
        return {"profiles": imported, "warnings": warnings}
