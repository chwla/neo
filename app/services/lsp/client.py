"""A small, mockable JSON-RPC client for allowlisted LSP processes."""

from __future__ import annotations

import queue
import subprocess
import threading
import time
from collections.abc import Callable
from typing import Any

from app.services.lsp.protocol import decode, encode


class JsonRpcClient:
    """JSON-RPC-over-stdio client with bounded output and deterministic cleanup."""

    STDERR_LIMIT = 64 * 1024

    def __init__(
        self,
        argv: list[str],
        cwd: str,
        timeout: float = 10,
        *,
        process_factory: Callable[..., Any] = subprocess.Popen,
        notification_handler: Callable[[str, dict], None] | None = None,
    ):
        self.process = process_factory(
            argv,
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
        )
        self.timeout = timeout
        self.next_id = 1
        self.notification_handler = notification_handler
        self._messages: queue.Queue[dict | BaseException] = queue.Queue()
        self._pending: dict[int, dict] = {}
        self._request_lock = threading.Lock()
        self._closed = False
        self._stderr = bytearray()
        self._reader = threading.Thread(target=self._read_messages, daemon=True)
        self._stderr_reader = threading.Thread(target=self._drain_stderr, daemon=True)
        self._reader.start()
        self._stderr_reader.start()

    @property
    def stderr(self) -> str:
        return bytes(self._stderr).decode("utf-8", errors="replace")

    def _read_messages(self) -> None:
        assert self.process.stdout is not None
        try:
            while True:
                self._messages.put(decode(self.process.stdout))
        except BaseException as exc:  # delivered to the blocked request, if any
            self._messages.put(exc)

    def _drain_stderr(self) -> None:
        stream = self.process.stderr
        if stream is None:
            return
        while True:
            chunk = stream.read(4096)
            if not chunk:
                return
            available = self.STDERR_LIMIT - len(self._stderr)
            if available > 0:
                self._stderr.extend(chunk[:available])

    def _send(self, message: dict) -> None:
        if self._closed:
            raise RuntimeError("LSP client is closed")
        if self.process.poll() is not None:
            raise RuntimeError("LSP process is not running")
        assert self.process.stdin is not None
        self.process.stdin.write(encode(message))
        self.process.stdin.flush()

    def request(self, method: str, params: dict) -> object:
        with self._request_lock:
            ident = self.next_id
            self.next_id += 1
            self._send({"jsonrpc": "2.0", "id": ident, "method": method, "params": params})
            deadline = time.monotonic() + self.timeout
            while True:
                response = self._pending.pop(ident, None)
                if response is not None:
                    return self._result(response)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._kill()
                    raise TimeoutError(f"LSP request timed out: {method}")
                try:
                    message = self._messages.get(timeout=remaining)
                except queue.Empty as exc:
                    self._kill()
                    raise TimeoutError(f"LSP request timed out: {method}") from exc
                if isinstance(message, BaseException):
                    raise RuntimeError("LSP process closed its protocol stream") from message
                if "id" in message:
                    message_id = message["id"]
                    if message_id == ident:
                        return self._result(message)
                    if isinstance(message_id, int):
                        self._pending[message_id] = message
                elif "method" in message:
                    self._handle_notification(message)

    @staticmethod
    def _result(message: dict) -> object:
        if "error" in message:
            raise RuntimeError(f"LSP request failed: {message['error']}")
        return message.get("result")

    def _handle_notification(self, message: dict) -> None:
        if self.notification_handler:
            params = message.get("params")
            self.notification_handler(message["method"], params if isinstance(params, dict) else {})

    def notify(self, method: str, params: dict) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _kill(self) -> None:
        if self.process.poll() is None:
            self.process.kill()
        self._closed = True

    def close(self) -> None:
        if self._closed:
            return
        try:
            self.request("shutdown", {})
            self.notify("exit", {})
            self.process.wait(timeout=min(self.timeout, 1))
        except Exception:
            self._kill()
        finally:
            self._closed = True
