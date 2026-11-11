"""Run real streamed chat on disposable data, without touching user chats."""

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--model", default="llama3.2:3b")
    parser.add_argument("--effort", choices=("low", "high"), default="low")
    parser.add_argument("--original-prompt", action="store_true")
    parser.add_argument("--prompts", nargs="+", default=["what can you di", "hello"])
    args = parser.parse_args()
    sys.path.insert(0, str(Path(args.source).resolve()))
    # Keep the disposable run inspectable locally; it contains only synthetic turns.
    scratch = tempfile.mkdtemp(prefix="neo-latency-")
    os.environ.update(
        {
            "NEO_DATA_DIR": scratch,
            "NEO_DATABASE_URL": f"sqlite:///{scratch}/neo.db",
            "NEO_CHAT_MODEL": args.model,
            "NEO_DEFAULT_MODEL": args.model,
            "NEO_WEB_SEARCH_PROVIDER": "disabled",
            "NEO_MEMORY_ENABLED": "false",
            "NEO_CHAT_MODEL_WARMUP": "false",
        }
    )
    from app.db.session import SessionLocal, initialize_database
    from app.repositories.app_store import AppStore
    from app.services.chat import NeoChatService
    from app.services.llm import OllamaClient
    from app.services.llm_registry.service import LLMRegistryService
    from app.services.provider_runtime.service import ProviderRuntimeService

    if args.original_prompt:
        import importlib.util
        path = Path(__file__).resolve().parents[2] / "before-latency-fix/app/services/chat.py"
        spec = importlib.util.spec_from_file_location("neo_original_chat", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        NeoChatService = module.NeoChatService
    options = OllamaClient._options
    OllamaClient._options = lambda self, temperature, num_predict: {
        **options(self, temperature, num_predict), "seed": 42,
    }

    initialize_database()
    LLMRegistryService()
    runtime = ProviderRuntimeService()
    results = []
    with SessionLocal() as db:
        store = AppStore(db)
        thread = store.create_chat(title="Disposable latency benchmark")
        db.commit()
        for prompt in args.prompts:
            chat = NeoChatService(db, memory_enabled=False, effort=args.effort)
            started = time.perf_counter()
            first = None
            final = {}
            for event in chat.stream_message(thread.id, prompt, timezone="Asia/Calcutta"):
                if event["type"] == "chunk" and event.get("content") and first is None:
                    first = round((time.perf_counter() - started) * 1000)
                    print(json.dumps({"prompt": prompt, "first_content_ms": first}), flush=True)
                if event["type"] == "done":
                    final = event
            timings = chat._timings_snapshot(chat.last_search_intent)
            requests = []
            for request_id in timings["request_ids"]:
                request = runtime.request(request_id)
                requests.append(
                    {
                        key: request.get(key)
                        for key in (
                            "model_name",
                            "latency_ms",
                            "provider_usage",
                            "retry_count",
                        )
                    }
                )
            result = {
                "prompt": prompt,
                "reply": final.get("reply"),
                "first_content_ms": first,
                "timings": timings,
                "requests": requests,
            }
            results.append(result)
            Path(args.output).write_text(json.dumps(results, indent=2), encoding="utf-8")
            print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
