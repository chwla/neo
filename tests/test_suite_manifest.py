from __future__ import annotations

import ast
import json
import shlex
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "tests" / "test_manifest.json"
PHASE_NAMES = ("legacy", "phase0", "phase1", "phase2", "phase3")


def _manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _is_pytest_mark_skip(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr in {"skip", "skipif"}
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "mark"
        and isinstance(node.value.value, ast.Name)
        and node.value.value.id == "pytest"
    )


def _has_unconditional_module_skip(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for statement in tree.body:
        if isinstance(statement, (ast.Assign, ast.AnnAssign)):
            targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
            assigns_pytestmark = any(
                isinstance(target, ast.Name) and target.id == "pytestmark" for target in targets
            )
            if assigns_pytestmark:
                value = statement.value
                contains_skip = value is not None and any(
                    _is_pytest_mark_skip(node) for node in ast.walk(value)
                )
                if contains_skip:
                    return True
        if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call):
            call = statement.value
            if (
                isinstance(call.func, ast.Attribute)
                and call.func.attr == "skip"
                and isinstance(call.func.value, ast.Name)
                and call.func.value.id == "pytest"
                and any(
                    keyword.arg == "allow_module_level"
                    and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value is True
                    for keyword in call.keywords
                )
            ):
                return True
    return False


def _collect(modules: list[str]) -> tuple[set[str], str]:
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", *modules],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    output = completed.stdout + completed.stderr
    assert completed.returncode == 0, output
    node_ids = {
        line.strip()
        for line in completed.stdout.splitlines()
        if line.startswith("tests/") and "::" in line
    }
    return node_ids, output


def test_manifest_requires_modules_and_behavioral_categories_for_every_phase() -> None:
    manifest = _manifest()
    assert manifest["schema_version"] == 1
    assert tuple(manifest["phases"]) == PHASE_NAMES

    for phase in PHASE_NAMES:
        entry = manifest["phases"][phase]
        modules = entry["required_modules"]
        categories = entry["behavioral_categories"]
        assert modules
        assert categories
        assert set(categories.values()) <= set(modules)
        for module in modules:
            path = ROOT / module
            assert path.is_file(), f"required test module is missing: {module}"
            assert not _has_unconditional_module_skip(path), (
                f"required test module is unconditionally skipped: {module}"
            )


def test_pytest_configuration_collects_the_complete_tests_tree() -> None:
    configuration = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    pytest_options = configuration.get("tool", {}).get("pytest", {}).get("ini_options", {})
    assert pytest_options.get("testpaths") == ["tests"]

    addopts = shlex.split(str(pytest_options.get("addopts", "")))
    assert not any(option.startswith("--ignore") for option in addopts)
    excluded = set(pytest_options.get("norecursedirs", []))
    assert {"legacy", "memory_v2"}.isdisjoint(excluded)

    for conftest in (ROOT / "tests").rglob("conftest.py"):
        source = conftest.read_text(encoding="utf-8")
        assert "pytest_collection_modifyitems" not in source
        assert "collect_ignore" not in source


def test_pytest_discovers_required_modules_categories_and_all_phase2_tests() -> None:
    manifest = _manifest()
    for phase in PHASE_NAMES:
        entry = manifest["phases"][phase]
        modules = entry["required_modules"]
        node_ids, output = _collect(modules)
        assert len(node_ids) >= entry["minimum_collected"], output
        collected_modules = {node_id.split("::", 1)[0] for node_id in node_ids}
        assert set(modules) <= collected_modules
        for category, module in entry["behavioral_categories"].items():
            assert module in collected_modules, f"{phase} category not collected: {category}"
