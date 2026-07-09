from __future__ import annotations

import uuid

from app.services.llm_registry.service import LLMRegistryService
from app.services.rules import store
from app.services.rules.safety import HARD_FORBIDDEN, LIST_FIELDS, sanitize_rules
from app.services.rules.types import RuleResolveRequest

SCOPE_RANK = {"workspace": 0, "global": 0, "project": 1, "repo": 2, "task": 3, "coding_run": 4}


class RuleResolver:
    def resolve(self, request: RuleResolveRequest, *, persist: bool = True) -> dict:
        store.initialize_rule_tables()
        warnings: list[str] = []
        profiles, _ = store.list_profiles(enabled=True, limit=1000)
        matches = []
        ids = {
            "project": request.project_id,
            "repo": request.repo_id,
            "task": request.task_id,
            "coding_run": request.coding_run_id,
        }
        for profile in profiles:
            scope = profile["scope_type"]
            if scope in {"workspace", "global"} or (
                ids.get(scope) and profile.get("scope_id") == ids[scope]
            ):
                matches.append(profile)
        matches.sort(
            key=lambda p: (
                SCOPE_RANK.get(p["scope_type"], -1),
                p["priority"],
                p["created_at"],
                p["id"],
            )
        )
        resolved = {field: [] for field in LIST_FIELDS}
        resolved.update(
            {
                "test_preferences": [],
                "model_routes": {},
                "approval_defaults": {},
                "patch_constraints": {},
            }
        )
        applied = []
        for profile in matches:
            clean = sanitize_rules(profile.get("rules", {}), warnings, profile["name"])
            self._merge(resolved, clean, warnings)
            applied.append(
                {
                    "id": profile["id"],
                    "name": profile["name"],
                    "scope_type": profile["scope_type"],
                    "scope_id": profile.get("scope_id"),
                    "priority": profile["priority"],
                    "source_type": profile["source_type"],
                }
            )
        if request.override_rules:
            self._merge(
                resolved,
                sanitize_rules(request.override_rules, warnings, "coding-run override"),
                warnings,
            )
            applied.append(
                {
                    "id": "coding-run-override",
                    "name": "Coding run override",
                    "scope_type": "coding_run",
                    "scope_id": request.coding_run_id,
                    "priority": 10000,
                    "source_type": "override",
                }
            )
        self._enforce_safety(resolved, warnings)
        self._validate_routes(resolved, warnings)
        result = {"resolved_rules": resolved, "applied_profiles": applied, "warnings": warnings}
        if persist:
            store.insert_log(
                {
                    "id": str(uuid.uuid4()),
                    "context_type": request.context_type,
                    "context_id": request.context_id or request.coding_run_id,
                    "project_id": request.project_id,
                    "task_id": request.task_id,
                    "repo_id": request.repo_id,
                    "applied_profiles": applied,
                    "resolved_rules": resolved,
                    "warnings": warnings,
                    "created_at": store.now_iso(),
                }
            )
        return result

    @staticmethod
    def _merge(target: dict, source: dict, warnings: list[str]) -> None:
        for field in LIST_FIELDS | {"test_preferences"}:
            for item in source.get(field, []):
                if item not in target[field]:
                    target[field].append(item)
        for field in ("checkpoint_template",):
            if field in source:
                target[field] = source[field]
        target["model_routes"].update(source.get("model_routes", {}))
        target["approval_defaults"].update(source.get("approval_defaults", {}))
        constraints = source.get("patch_constraints", {})
        if isinstance(constraints.get("max_files"), int) and constraints["max_files"] > 0:
            current = target["patch_constraints"].get("max_files")
            target["patch_constraints"]["max_files"] = (
                min(current, constraints["max_files"]) if current else constraints["max_files"]
            )
        for key in ("prefer_existing_files", "allow_new_files"):
            if key in constraints:
                value = bool(constraints[key])
                target["patch_constraints"][key] = (
                    target["patch_constraints"].get(key, True) and value
                )
        if source.get("metadata"):
            target.setdefault("metadata", {}).update(source["metadata"])

    @staticmethod
    def _enforce_safety(resolved: dict, warnings: list[str]) -> None:
        for path in HARD_FORBIDDEN:
            if path not in resolved["forbidden_paths"]:
                resolved["forbidden_paths"].append(path)
        approvals = resolved["approval_defaults"]
        for key in (
            "require_patch_approval",
            "require_test_approval",
            "require_checkpoint_approval",
        ):
            if approvals.get(key) is False:
                warnings.append(f"Safety override ignored: {key} cannot be disabled.")
            approvals[key] = True
        constraints = resolved["patch_constraints"]
        if constraints.get("max_files", 8) > 8:
            warnings.append("Safety override ignored: patch max_files cannot exceed 8.")
        constraints["max_files"] = min(int(constraints.get("max_files", 8)), 8)
        constraints.setdefault("prefer_existing_files", True)
        constraints.setdefault("allow_new_files", True)

    @staticmethod
    def _validate_routes(resolved: dict, warnings: list[str]) -> None:
        service = LLMRegistryService()
        for consumer, route_name in list(resolved["model_routes"].items()):
            route = service.get_route(route_name)
            provider = service.get_provider(route["provider_id"]) if route else None
            model = service.get_model(route["model_id"]) if route else None
            compatible = bool(
                route
                and route.get("enabled")
                and provider
                and provider.get("enabled")
                and model
                and model.get("enabled")
            )
            if consumer == "embedding" and compatible:
                compatible = bool(model.get("supports_embeddings"))
            if not compatible:
                warnings.append(
                    f"Model route '{route_name}' for {consumer} is missing, disabled, "
                    "or incompatible; "
                    "normal routing will be used."
                )
                del resolved["model_routes"][consumer]

    @staticmethod
    def prompt_context(result: dict) -> str:
        rules = result["resolved_rules"]
        lines = [*rules.get("instructions", []), *rules.get("coding_style", [])]
        if rules.get("preferred_paths"):
            lines.append("Prefer paths: " + ", ".join(rules["preferred_paths"]))
        if rules.get("forbidden_paths"):
            lines.append("Never edit paths: " + ", ".join(rules["forbidden_paths"]))
        return "\n".join(f"- {line}" for line in lines)

    @staticmethod
    def route_name(result: dict, consumer: str, default: str) -> str:
        return result.get("resolved_rules", {}).get("model_routes", {}).get(consumer, default)

    @staticmethod
    def research_context(result: dict) -> str:
        rules = result["resolved_rules"]
        lines = [*rules.get("instructions", [])]
        for field, label in (
            ("research_preferences", "Research preference"),
            ("source_preferences", "Source preference"),
        ):
            lines.extend(f"{label}: {item}" for item in rules.get(field, []))
        return "\n".join(f"- {line}" for line in lines)
