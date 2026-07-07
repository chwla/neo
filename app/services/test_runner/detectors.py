from __future__ import annotations

import json
from pathlib import Path

from app.services.test_runner.types import TestCommandSuggestion


def detect_commands(workspace_path: Path) -> list[TestCommandSuggestion]:
    suggestions: list[TestCommandSuggestion] = []
    has_tests = (workspace_path / "tests").is_dir()
    has_pytest = any(
        (workspace_path / name).is_file()
        for name in ("pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini")
    )
    if has_tests or has_pytest:
        suggestions.append(
            TestCommandSuggestion(name="Python tests", command=["python", "-m", "pytest", "-q"])
        )
    if has_tests:
        suggestions.append(
            TestCommandSuggestion(
                name="Python unittest discovery",
                command=["python", "-m", "unittest", "discover", "-s", "tests", "-v"],
            )
        )

    package_file = workspace_path / "package.json"
    if package_file.is_file():
        try:
            payload = json.loads(package_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            payload = {}
        scripts = payload.get("scripts") if isinstance(payload, dict) else {}
        if isinstance(scripts, dict):
            for script in ("test", "build", "lint", "typecheck"):
                if script in scripts:
                    command = ["npm", "test"] if script == "test" else ["npm", "run", script]
                    suggestions.append(
                        TestCommandSuggestion(
                            name=f"npm {script}", command=command, timeout_seconds=180
                        )
                    )
    return suggestions
