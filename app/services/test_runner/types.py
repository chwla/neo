from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

TestRunStatus = Literal["queued", "running", "passed", "failed", "timed_out", "cancelled", "error"]


class TestCommandCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    command: list[str] = Field(min_length=1, max_length=64)
    working_directory: str = Field(default=".", min_length=1, max_length=1000)
    timeout_seconds: int = Field(default=120, ge=1, le=600)
    project_id: str | None = None


class TestCommandUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    command: list[str] | None = Field(default=None, min_length=1, max_length=64)
    working_directory: str | None = Field(default=None, min_length=1, max_length=1000)
    timeout_seconds: int | None = Field(default=None, ge=1, le=600)
    enabled: bool | None = None


class TestCommand(BaseModel):
    id: str
    repo_id: str
    project_id: str | None = None
    name: str
    command: list[str]
    working_directory: str
    timeout_seconds: int
    enabled: bool
    created_at: str
    updated_at: str


class TestCommandSuggestion(BaseModel):
    name: str
    command: list[str]
    working_directory: str = "."
    timeout_seconds: int = 120


class TestRunRequest(BaseModel):
    confirm: bool = False
    task_id: str | None = None
    agent_run_id: str | None = None
    patch_application_id: str | None = None


class TestRun(BaseModel):
    id: str
    repo_id: str
    project_id: str | None = None
    task_id: str | None = None
    agent_run_id: str | None = None
    patch_application_id: str | None = None
    test_command_id: str | None = None
    name: str
    command: list[str]
    working_directory: str
    status: TestRunStatus
    exit_code: int | None = None
    stdout_text: str = ""
    stderr_text: str = ""
    combined_output: str = ""
    duration_ms: int | None = None
    timeout_seconds: int
    error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    started_at: str | None = None
    completed_at: str | None = None
