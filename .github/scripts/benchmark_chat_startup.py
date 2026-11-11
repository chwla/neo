"""Compare cold and prepared local prompts without touching user chat storage."""

import argparse
import json
import sys
import time
from pathlib import Path
from unittest.mock import patch

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from check_chat_latency import service  # noqa: E402

from app.services.chat import chat_prompt_prefix  # noqa: E402
from app.services.llm import OllamaClient, forget_loaded_models  # noqa: E402
from app.services.provider_runtime.service import ProviderRuntimeService  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="llama3.2:3b")
    parser.add_argument("--effort", choices=("low", "high"), default="low")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    high = args.effort == "high"
    chat = service(high=high)
    messages = chat.build_messages(
        "HELLO",
        [],
        chat.build_context("HELLO"),
        project_context="No project context loaded.",
        task_context="No task context loaded.\n\nNo file context loaded.\n\n"
        "No codebase index context requested.\n\nNo symbol navigation context requested.\n\n\n\n",
    )
    prefix = chat_prompt_prefix(reasoning=high, carries_workspace=True, timezone="Asia/Calcutta")
    assert messages[0].content.startswith(prefix)
    base = "http://127.0.0.1:11434"
    context = 8192 if high else 4096
    route = {
        "model": args.model,
        "provider_record": {
            "provider_type": "ollama",
            "base_url": base,
            "enabled": True,
            "timeout_seconds": 240,
        },
        "model_record": {"model_name": args.model, "enabled": True, "context_window": 32768},
    }

    class SeededClient(OllamaClient):
        def _options(self, temperature, num_predict):
            return {**super()._options(temperature, num_predict), "seed": 42}

    client = SeededClient(
        args.model,
        base,
        240,
        3072 if high else 2048,
        num_ctx=context,
        keep_alive="30m",
        disable_thinking=not high,
    )
    results = []
    for prepared in (False, True):
        requests.post(
            base + "/api/generate", json={"model": args.model, "keep_alive": 0}, timeout=30
        ).raise_for_status()
        forget_loaded_models()
        warm = None
        if prepared:
            with patch("app.services.provider_runtime.service.select", return_value=route):
                warm = ProviderRuntimeService(initialize=False).warm(prefix=prefix, reasoning=high)
            assert warm["status"] == "warmed", warm
            print(json.dumps({"preparation": warm}), flush=True)
        started = time.perf_counter()
        first = None
        reply = ""
        final = {}
        for event in client.chat_stream(messages, temperature=0.2):
            if event["type"] == "chunk":
                if first is None:
                    first = round((time.perf_counter() - started) * 1000)
                reply += event["content"]
            if event["type"] == "done":
                final = event
        result = {
            "model": args.model,
            "effort": args.effort,
            "prepared": prepared,
            "preparation": warm,
            "first_content_ms": first,
            "total_ms": round((time.perf_counter() - started) * 1000),
            "reply": reply,
            "usage": final,
        }
        results.append(result)
        Path(args.output).write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
