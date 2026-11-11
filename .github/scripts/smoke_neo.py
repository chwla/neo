"""Start Neo with disposable data and check the public Tools menu API."""

import http.cookiejar
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPCookieProcessor, ProxyHandler, Request, build_opener


class OllamaStub(BaseHTTPRequestHandler):
    """Exercise the real provider adapter without requiring a downloaded model."""

    def log_message(self, *args):
        pass

    def respond(self, data):
        payload = json.dumps(data).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        self.respond({"models": [{"name": "qwen3:8b"}]})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/api/show":
            self.respond({"capabilities": ["completion", "tools"]})
            return
        messages = body["messages"]
        tools = {schema["function"]["name"] for schema in body.get("tools", [])}
        created = any("Verified directory A/" in message["content"] for message in messages)
        message = {"role": "assistant"}
        if messages[0]["content"].startswith("You judge"):
            message["content"] = json.dumps({
                "complete": created, "blocked": not created, "unmet_criteria": [],
                "summary": "Folder verified." if created else "Attach a folder to continue.",
            })
        elif created:
            message["content"] = "I created the folder A."
        elif "create_directory" in tools:
            message["content"] = ""
            message["tool_calls"] = [
                {"function": {"name": "create_directory", "arguments": {"path": "A"}}}
            ]
        else:
            message["content"] = "Choose Open a folder to attach your Desktop."
        self.respond({"message": message, "done": True, "done_reason": "stop"})


def smoke(directory: Path, model_port: int) -> None:
    desktop = directory / "Desktop"
    desktop.mkdir()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {
        **os.environ,
        "NEO_DATA_DIR": str(directory / "data"),
        "NEO_WORKSPACE_LIVE_ROOTS": str(directory),
        "NEO_WEB_SEARCH_PROVIDER": "disabled",
        "OLLAMA_BASE_URL": f"http://127.0.0.1:{model_port}",
        "NEO_DEFAULT_MODEL": "qwen3:8b",
        "NO_PROXY": "localhost,127.0.0.1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def api(path, data=None, method=None):
        request = Request(
            f"http://127.0.0.1:{port}/api{path}",
            data=json.dumps(data).encode("utf-8") if data is not None else None,
            headers={"Content-Type": "application/json"},
            method=method,
        )
        try:
            with opener.open(request, timeout=15) as response:
                return json.load(response)
        except HTTPError as error:
            # Detailed readiness returns 503 when optional providers are offline.
            if path == "/health/ready" and error.code == 503:
                return json.load(error)
            raise RuntimeError(f"{path}: HTTP {error.code}: {error.read().decode()}") from error

    log_path = directory / "neo.log"
    try:
        with log_path.open("w", encoding="utf-8") as log, subprocess.Popen(
            [sys.executable, "-u", "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
             "--port", str(port), "--timeout-graceful-shutdown", "5"],
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            # A console group of its own, so Ctrl+Break reaches only the server.
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        ) as server:
            try:
                deadline = time.monotonic() + 100
                while time.monotonic() < deadline and server.poll() is None:
                    try:
                        api("/health/live")
                        break
                    except URLError:
                        time.sleep(0.5)
                else:
                    raise RuntimeError("Neo did not start.")
                assert api("/health/ready")["status"] == "ready"
                api("/account-profiles", {"username": "ci", "password": "ci-password"})
                assert api("/health/ready")["checks"]["connector_key"]["ok"]

                chat = api("/chats", {})["id"]
                endpoint = f"/chats/{chat}"

                def directory_tool():
                    return next(
                        tool for tool in api(f"{endpoint}/tools")["tools"]
                        if tool["name"] == "create_directory"
                    )

                def start_agent():
                    result = api(f"{endpoint}/generations", {
                        "prompt": "CREATE A FOLDER IN DESKTOP NAMED 'A'",
                        "mode": "agent",
                        "memory_enabled": False,
                    })
                    return f"/agent-sessions/{result['agent_session_id']}"

                def wait_for(run, *statuses):
                    deadline = time.monotonic() + 30
                    while time.monotonic() < deadline:
                        detail = api(run)
                        if detail["session"]["status"] in statuses:
                            return detail
                        time.sleep(0.25)
                    raise AssertionError(f"Agent did not reach {statuses}: {detail['session']}")

                tool = directory_tool()
                assert tool["enabled"] and not tool["available"]
                assert tool["unavailable_reason"] == "folder_required"
                blocked = wait_for(start_agent(), "completed", "failed")
                assert blocked["session"]["stop_reason"] == "blocked", blocked["session"]
                assert blocked["tool_calls"] == []
                repo = api("/repos/register", {"path": str(desktop), "confirm": True})["repo"]
                api(endpoint, {"repo_id": repo["id"]}, method="PATCH")
                assert directory_tool()["available"]
                api(endpoint, {"disabled_tools": ["create_directory"]}, method="PATCH")
                tool = directory_tool()
                assert not tool["enabled"] and not tool["available"]
                api(endpoint, {"disabled_tools": []}, method="PATCH")
                run = start_agent()
                pending = wait_for(run, "waiting_approval", "completed", "failed")
                approval = pending["pending_approval"]
                assert approval and approval["tool_name"] == "create_directory", pending
                assert not (desktop / "A").exists()
                api(f"{run}/approvals/{approval['id']}", {"decision": "allow_once"})
                completed = wait_for(run, "completed", "failed")
                assert completed["session"]["stop_reason"] == "verified_complete", completed
                assert list((desktop / "A").iterdir()) == []
                (desktop / "A").rmdir()
                desktop.rmdir()
                assert directory_tool()["unavailable_reason"] == "folder_required"

                # Editing a message right after a reply used to self-deadlock on a
                # fresh profile ("database is locked", 30 s, then 500).
                started = api(f"{endpoint}/generations", {"prompt": "Say ONE.", "mode": "chat"})
                generation = started["generation"]["id"] if "generation" in started else None
                deadline = time.monotonic() + 30
                while generation and time.monotonic() < deadline:
                    state = api(f"{endpoint}/generations/{generation}")
                    if state["status"] not in {"queued", "running"}:
                        break
                    time.sleep(0.25)
                messages = api(endpoint)["messages"]
                user_message = next(m for m in messages if m["role"] == "user")
                edit_started = time.monotonic()
                edited = api(f"{endpoint}/messages/{user_message['id']}",
                             {"content": "Say TWO."}, method="PATCH")
                assert edited["content"] == "Say TWO.", edited
                assert time.monotonic() - edit_started < 10

                # The Settings "Test" button path: a labelled query, never a 500.
                config = api("/search/config", {"provider": "duckduckgo"})
                assert config["provider"] == "duckduckgo", config
                tested = api("/search/test", {"query": "neo smoke", "count": 1})
                assert "success" in tested and "error" in tested, tested
                api("/search/config", {"provider": "disabled"})

                # Git init and a checkpoint on a managed copy, on every OS.
                project = directory / "project"
                project.mkdir()
                (project / "main.py").write_text("print('hi')\n", encoding="utf-8")
                managed = api("/repos/register", {"path": str(project), "confirm": True,
                                                  "access": "managed"})["repo"]
                init = api(f"/git/repos/{managed['id']}/init", {"confirm": True})
                assert init["git_repo"]["git_initialized"], init
                assert init["checkpoint"], init  # the files were committed at init
                # The managed copy lives under the data directory's workspaces tree.
                copy = next(p for p in (directory / "data" / "workspaces").rglob(managed["id"])
                            if p.is_dir())
                assert (copy / ".git" / "info" / "exclude").is_file()
                # A change inside the managed copy, as the agent's write tool would make.
                (copy / "main.py").write_text("print('changed')\n", encoding="utf-8")
                created = api(f"/git/repos/{managed['id']}/checkpoints",
                              {"title": "Smoke checkpoint", "confirm": True})
                assert created["checkpoint"]["commit_sha"], created
                listed = api(f"/git/repos/{managed['id']}/checkpoints")
                assert len(listed) >= 2, listed

                # An over-limit body gets a readable 413, not a dropped connection.
                oversize = Request(
                    f"http://127.0.0.1:{port}/api/notes",
                    data=json.dumps({"title": "big", "content": "x" * (9 * 1024 * 1024)}).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                try:
                    opener.open(oversize, timeout=30)
                    raise AssertionError("an oversize body was accepted")
                except HTTPError as error:
                    assert error.code == 413, error.code
                    assert "too large" in json.load(error)["detail"], "no readable 413 body"

                # The CLI signs in with a stored session and then reads as that profile.
                cli_env = {**env, "NEO_CLI_SESSION_FILE": str(directory / "cli-session.json")}
                cli = [sys.executable, "-m", "app.cli.main", "--api-url",
                       f"http://127.0.0.1:{port}"]
                before = subprocess.run([*cli, "status", "--json"], env=cli_env,
                                        capture_output=True, text=True, encoding="utf-8")
                assert before.returncode == 0, before.stderr
                denied = subprocess.run([*cli, "memory", "list"], env=cli_env,
                                        capture_output=True, text=True, encoding="utf-8")
                assert denied.returncode == 4, (denied.returncode, denied.stderr)
                login = subprocess.run([*cli, "login", "--profile", "ci", "--password-stdin"],
                                       input="ci-password\n", env=cli_env,
                                       capture_output=True, text=True, encoding="utf-8")
                assert login.returncode == 0, login.stderr
                for command in (["memory", "list"], ["status"], ["agent", "list"],
                                ["tests", "list", "--repo", managed["id"]]):
                    done = subprocess.run([*cli, *command, "--json"], env=cli_env,
                                          capture_output=True, text=True, encoding="utf-8")
                    assert done.returncode == 0, (command, done.returncode, done.stderr)
                logout = subprocess.run([*cli, "logout"], env=cli_env,
                                        capture_output=True, text=True, encoding="utf-8")
                assert logout.returncode == 0, logout.stderr

                # A held-open event stream must not stop the server from exiting.
                tail = Request(f"http://127.0.0.1:{port}/api/chat-events",
                               headers={"Accept": "application/x-ndjson"})
                stream = opener.open(tail, timeout=15)
                stream.readline()
                stopped_at = time.monotonic()
                if os.name == "nt":
                    server.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    server.send_signal(signal.SIGTERM)
                try:
                    server.wait(timeout=8)
                except subprocess.TimeoutExpired as exc:
                    raise AssertionError("Neo did not stop within 8 s with a stream open") from exc
                assert time.monotonic() - stopped_at < 8
                stream.close()
            finally:
                server.terminate()
                try:
                    server.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait()
    finally:
        logs = log_path.read_text(encoding="utf-8", errors="replace")
        print(logs)
    assert "Traceback" not in logs and "ERROR" not in logs
    print("Startup, Tools API, blocked run, and approved directory creation passed.")


if __name__ == "__main__":
    # macOS defaults to /var, a system location Neo intentionally will not attach.
    with tempfile.TemporaryDirectory(prefix="neo-smoke-", dir=Path.cwd()) as scratch:
        with ThreadingHTTPServer(("127.0.0.1", 0), OllamaStub) as model:
            thread = threading.Thread(target=model.serve_forever, daemon=True)
            thread.start()
            try:
                smoke(Path(scratch).resolve(), model.server_port)
            finally:
                model.shutdown()
                thread.join()
