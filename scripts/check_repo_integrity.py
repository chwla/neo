#!/usr/bin/env python3
from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

CRITICAL_DIRS = (
    "app",
    "app/api/routes",
    "app/services",
    "frontend/src",
    "tests",
)
PLACEHOLDER_MARKERS = (
    "placeholder",
    "todo",
    "stub",
    "not implemented",
    "replace me",
)


@dataclass
class IntegrityReport:
    root: Path
    failures: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def fail(self, message: str) -> None:
        self.failures.append(message)

    def note(self, message: str) -> None:
        self.notes.append(message)

    @property
    def ok(self) -> bool:
        return not self.failures


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise SystemExit(f"{name} must be an integer, got {raw!r}.") from None
    if value < 0:
        raise SystemExit(f"{name} must be non-negative, got {value}.")
    return value


def run_argv(
    argv: list[str],
    *,
    cwd: Path,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=cwd,
        text=True,
        capture_output=True,
        check=check,
    )


def git_lines(root: Path, *args: str) -> list[str]:
    result = run_argv(["git", *args], cwd=root)
    if result.returncode != 0:
        return []
    return [line.rstrip("\n") for line in result.stdout.splitlines()]


def check_critical_dirs(report: IntegrityReport) -> None:
    for relative in CRITICAL_DIRS:
        path = report.root / relative
        if not path.is_dir():
            report.fail(f"Critical directory missing: {relative}/")


def test_files(root: Path) -> list[Path]:
    tests_dir = root / "tests"
    if not tests_dir.is_dir():
        return []
    return sorted(tests_dir.glob("test_*.py"))


def check_test_file_count(report: IntegrityReport, min_files: int) -> None:
    files = test_files(report.root)
    report.note(f"Found {len(files)} test source file(s); minimum required is {min_files}.")
    if len(files) < min_files:
        report.fail(
            f"Too few test files: found {len(files)}, expected at least {min_files}."
        )


def parse_pytest_collected(output: str) -> int | None:
    patterns = (
        r"(?m)(\d+)\s+tests?\s+collected",
        r"(?m)collected\s+(\d+)\s+items?",
        r"(?m)(\d+)\s+items?\s+collected",
    )
    for pattern in patterns:
        match = re.search(pattern, output)
        if match:
            return int(match.group(1))
    return None


def check_pytest_collection(report: IntegrityReport, min_tests: int) -> None:
    result = run_argv(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        cwd=report.root,
    )
    combined = f"{result.stdout}\n{result.stderr}"
    if result.returncode != 0:
        report.fail(
            "pytest collection failed while checking repo integrity:\n"
            + combined.strip()[:4000]
        )
        return
    count = parse_pytest_collected(combined)
    if count is None:
        report.fail(
            "Could not parse pytest collected test count from `python -m pytest "
            "--collect-only -q` output."
        )
        return
    report.note(f"pytest collected {count} test(s); minimum required is {min_tests}.")
    if count < min_tests:
        report.fail(
            f"Suspiciously few pytest tests collected: {count}, expected at least {min_tests}."
        )


def is_cache_path(path: str) -> bool:
    normalized = path.replace("\\", "/")
    return "__pycache__/" in normalized or normalized.endswith(".pyc")


def check_git_state(report: IntegrityReport, allow_untracked_tests: bool) -> None:
    status = git_lines(report.root, "status", "--porcelain", "--untracked-files=all")
    deleted_tests: list[str] = []
    untracked_tests: list[str] = []
    staged_cache: list[str] = []
    for line in status:
        if len(line) < 4:
            continue
        index_status = line[0]
        worktree_status = line[1]
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        normalized = path.replace("\\", "/")
        if normalized.startswith("tests/") and (
            index_status == "D" or worktree_status == "D"
        ):
            deleted_tests.append(path)
        if line.startswith("?? ") and re.match(r"tests/test_.*\.py$", normalized):
            untracked_tests.append(path)
        if index_status not in {" ", "?"} and is_cache_path(path):
            staged_cache.append(path)
    if deleted_tests:
        report.fail("Deleted test source file(s): " + ", ".join(sorted(deleted_tests)))
    if untracked_tests and not allow_untracked_tests:
        report.fail(
            "Untracked test source file(s) are not allowed: "
            + ", ".join(sorted(untracked_tests))
        )
    if staged_cache:
        report.fail("Staged cache/bytecode file(s): " + ", ".join(sorted(staged_cache)))

    tracked_cache = [path for path in git_lines(report.root, "ls-files") if is_cache_path(path)]
    if tracked_cache:
        report.fail("Tracked cache/bytecode file(s): " + ", ".join(sorted(tracked_cache)))


def check_placeholder_tests(report: IntegrityReport) -> None:
    suspicious: list[str] = []
    for path in test_files(report.root):
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            suspicious.append(f"{path.relative_to(report.root)} (not UTF-8)")
            continue
        lowered = text.lower()
        meaningful_lines = [
            line.strip()
            for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        has_test_symbol = bool(
            re.search(r"(?m)^\s*def\s+test_", text)
            or re.search(r"(?m)^\s*async\s+def\s+test_", text)
            or re.search(r"(?m)^\s*class\s+Test\w*", text)
            or "unittest.TestCase" in text
        )
        marker_hit = any(marker in lowered for marker in PLACEHOLDER_MARKERS)
        only_trivial = len(meaningful_lines) <= 4 and any(
            line in {"pass", "...", "assert True", "return None"}
            for line in meaningful_lines
        )
        marker_is_suspicious = marker_hit and (len(meaningful_lines) <= 8 or only_trivial)
        if not has_test_symbol or marker_is_suspicious or only_trivial:
            reason = []
            if not has_test_symbol:
                reason.append("no test symbols")
            if marker_is_suspicious:
                reason.append("placeholder marker")
            if only_trivial:
                reason.append("trivial body")
            suspicious.append(f"{path.relative_to(report.root)} ({', '.join(reason)})")
    if suspicious:
        report.fail("Suspicious placeholder-like test file(s): " + ", ".join(suspicious))


def print_report(report: IntegrityReport) -> None:
    print("Neo repo integrity check")
    print(f"Root: {report.root}")
    print()
    if report.notes:
        print("Checks:")
        for note in report.notes:
            print(f"  PASS/INFO: {note}")
        print()
    if report.failures:
        print("Failures:")
        for failure in report.failures:
            print(f"  FAIL: {failure}")
        print()
        print("Repo integrity check failed.")
    else:
        print("Repo integrity check passed.")


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    report = IntegrityReport(root=root)
    min_files = env_int("NEO_MIN_TEST_FILES", 20)
    min_tests = env_int("NEO_MIN_PYTEST_TESTS", 100)
    allow_untracked = os.getenv("NEO_ALLOW_UNTRACKED_TESTS", "0") == "1"

    check_critical_dirs(report)
    check_test_file_count(report, min_files)
    check_placeholder_tests(report)
    check_git_state(report, allow_untracked)
    check_pytest_collection(report, min_tests)
    print_report(report)
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
