"""Orchestration for comparing models.

Routes parse and validate; this decides. Everything returned from here is already shaped
for the interface, and the plain-language wording lives here rather than in the browser
so that the API, the screen and anything built on it later all say the same thing.

The one thing this module deliberately does *not* do is pick a winner. It assembles the
configuration, runs it, and hands back measurements.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterator
from typing import Any

import requests

from app.core.config import get_settings
from app.services.llm import LLMConfig, LLMRegistry
from app.services.llm_registry.service import LLMRegistryService
from app.services.llm_registry.types import ModelCreate, ProviderCreate
from app.services.model_compare import judge, runner, tasks
from app.services.model_compare.grading import GRADER_VERSION
from app.services.model_compare.tasks import MAX_CUSTOM_PROMPTS, USE_CASE_CHOICES
from app.services.model_compare.types import (
    USE_CASES,
    Contender,
    GenerationSettings,
    JudgeSettings,
    RunConfig,
    Task,
)

_LOG = logging.getLogger(__name__)

#: How many tasks a run does unless the user says otherwise. Three is enough for one
#: model to pull ahead of another without the wait becoming something to plan around;
#: the screen exposes the knob for anyone who wants a steadier reading.
DEFAULT_DEPTH = 3

#: Fewer than two is not a comparison. More than four is four columns of small text and
#: a wait long enough that the answer arrives after the decision.
MIN_CONTENDERS = 2
MAX_CONTENDERS = 4

#: Listing what a provider has is one request, not one per model, and it happens while
#: the user is still choosing -- so it is kept short and a slow provider is reported as
#: unknown rather than holding the page.
DISCOVERY_TIMEOUT_SECONDS = 4

#: Bounds on what the user may ask a model to generate. The low end stops a setting that
#: truncates every answer into nonsense; the high end stops one run eating an afternoon.
MIN_OUTPUT_TOKENS = 32
MAX_OUTPUT_TOKENS = 4096

MAX_TEMPERATURE = 2.0
MAX_RUBRIC_LENGTH = 4000

#: Names an Ollama install reports that are embedders rather than chat models. They
#: cannot hold a conversation -- asked one, they refuse the request -- so they are never
#: offered as something to compare. Mirrors the list the LLM registry uses.
EMBEDDING_HINTS = ("embed", "bge", "gte", "minilm")


def _is_embedding_model(name: str) -> bool:
    return any(hint in name.lower() for hint in EMBEDDING_HINTS)


class ModelCompareService:
    def __init__(self, registry: LLMRegistry | None = None) -> None:
        self._registry = registry or LLMRegistry()

    # -- what the screen offers -------------------------------------------------

    def use_cases(self) -> dict[str, Any]:
        """The choices, each carrying how deep it can go."""

        return {
            "use_cases": [
                {
                    **choice,
                    "task_count": tasks.total_tasks(choice["id"]),
                    "default_depth": min(DEFAULT_DEPTH, tasks.total_tasks(choice["id"]) or 1),
                    # Whether a rule-based score is possible at all here. The custom pack
                    # says no, and the screen says so rather than showing an empty score.
                    "deterministic_available": choice["id"] != "custom",
                }
                for choice in USE_CASE_CHOICES
            ],
            "default_depth": DEFAULT_DEPTH,
            "min_models": MIN_CONTENDERS,
            "max_models": MAX_CONTENDERS,
            "max_custom_prompts": MAX_CUSTOM_PROMPTS,
            "grader_version": GRADER_VERSION,
            "defaults": GenerationSettings().as_dict(),
            "limits": {
                "min_output_tokens": MIN_OUTPUT_TOKENS,
                "max_output_tokens": MAX_OUTPUT_TOKENS,
                "max_temperature": MAX_TEMPERATURE,
            },
            "default_rubric": judge.DEFAULT_RUBRIC,
        }

    def candidates(self) -> dict[str, Any]:
        """Every model Neo is configured to talk to, and whether it looks reachable."""

        configs, active_id = self._registry.list()
        enabled = [item for item in configs if item.enabled]
        reachable = _reachability(enabled)
        rows = []
        for config in enabled:
            state = reachable.get(_endpoint(config), {})
            contender = runner.contender_from_config(
                config, version=(state.get("versions") or {}).get(config.model, "")
            )
            rows.append(
                {
                    **contender.as_dict(),
                    "active": config.id == active_id,
                    "reachable": state.get("reachable"),
                    "installed": _installed(state, config),
                    "note": _availability_note(state, config),
                }
            )
        return {
            "candidates": rows,
            "active_id": active_id,
            # Models the machine already has that Neo is not set up to talk to. Offered
            # here rather than only in Settings because this is the screen where their
            # absence is felt: being told to go somewhere else to add a second model is
            # the one thing standing between the user and a comparison.
            "discoverable": self._discoverable(enabled, reachable),
            **self.use_cases(),
        }

    def _discoverable(
        self, configs: list[LLMConfig], reachable: dict[tuple[str, str], dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Chat models a provider serves that the picker does not know about yet."""

        registered = {config.model for config in configs}
        found: list[dict[str, Any]] = []
        seen: set[str] = set()
        for (provider_type, base_url), state in reachable.items():
            if provider_type != "ollama" or not state.get("reachable"):
                continue
            versions = state.get("versions") or {}
            for name in sorted(state.get("models") or []):
                # ":latest" is recorded twice, bare and tagged, so that either spelling
                # matches an existing entry. Only the written-out form is offered.
                if not name or ":" not in name or name in registered or name in seen:
                    continue
                if _is_embedding_model(name):
                    continue
                seen.add(name)
                found.append(
                    {
                        "model": name,
                        "display_name": name,
                        "version": versions.get(name, ""),
                        "base_url": base_url,
                        "local": runner.is_local(base_url),
                    }
                )
        return found

    def add_model(self, model: str, base_url: str = "") -> dict[str, Any]:
        """Set up a model the machine already has, so it can be compared.

        Registered through the LLM registry rather than written straight into the picker
        file: the registry is what mirrors a model into the picker, the routes and
        Settings, so a model added here is a model Neo can use everywhere rather than one
        that exists only on this screen.
        """

        name = (model or "").strip()
        if not name:
            raise ValueError("Say which model to add.")
        if _is_embedding_model(name):
            raise ValueError(
                f"'{name}' turns text into numbers for search. It cannot hold a "
                "conversation, so there is nothing to compare it on."
            )

        endpoint = (base_url or get_settings().ollama_url).rstrip("/")
        probe = _probe(
            LLMConfig(
                id="probe", name="probe", provider="ollama", model=name, base_url=endpoint
            )
        )
        if not probe.get("reachable"):
            raise LookupError(f"Neo could not reach a model host at {endpoint}.")
        if name not in (probe.get("models") or set()):
            raise LookupError(f"'{name}' is not installed on this computer.")

        registry = LLMRegistryService()
        provider = _ollama_provider(registry, endpoint)
        settings = get_settings()
        registry.create_model(
            ModelCreate(
                provider_id=provider["id"],
                model_name=name,
                display_name=name,
                max_output_tokens=settings.chat_num_predict,
                enabled=True,
                metadata={"source": "model_compare"},
            )
        )
        return self.candidates()

    # -- running one -------------------------------------------------------------

    def build(
        self,
        *,
        model_ids: list[str],
        use_case: str = "coding",
        depth: int = DEFAULT_DEPTH,
        prompts: list[str] | None = None,
        judge_id: str | None = None,
        judge_rubric: str = "",
        deterministic: bool = True,
        temperature: float = 0.0,
        max_output_tokens: int | None = None,
        allow_thinking: bool = False,
        run_id: str | None = None,
    ) -> tuple[list[tuple[LLMConfig, Contender]], RunConfig, frozenset[str]]:
        """Validate everything and assemble the record of what is about to happen.

        Everything a run needs is settled here, before a single token is generated: the
        route calls this to reject a bad request while it can still return a status code,
        and calls it again -- or rather uses what it returned -- to do the run.
        """

        versions = _versions_for(self._registry, model_ids, judge_id)
        pairs = self._pairs(model_ids, versions)
        selected = self._tasks(use_case, depth, prompts or [])
        generation = self._generation(temperature, max_output_tokens, allow_thinking)
        judge_settings = self._judge(judge_id, judge_rubric, versions)

        if not deterministic and not judge_settings.enabled:
            raise ValueError(
                "With rule-based checks switched off and no judge, nothing would be "
                "evaluated. Turn one of them on."
            )
        if use_case == "custom" and not judge_settings.enabled:
            # Allowed, and worth being explicit about rather than refusing: the answers
            # side by side are a legitimate thing to want. The screen says the same.
            _LOG.debug("Custom comparison with no judge: answers will not be scored.")

        contenders = [contender for _, contender in pairs]
        warm_ids = _warm_ids(contenders)
        config = RunConfig(
            run_id=run_id or str(uuid.uuid4()),
            use_case=use_case,
            contenders=tuple(contenders),
            tasks=tuple(selected),
            generation=generation,
            judge=judge_settings,
            deterministic=deterministic,
            grader_version=GRADER_VERSION,
            parallel=runner.workers(contenders),
            estimate_seconds=runner.estimate_seconds(
                contenders,
                selected,
                generation=generation,
                judge_enabled=judge_settings.enabled,
                warm_ids=warm_ids,
            ),
        )
        return pairs, config, warm_ids

    def plan(self, **kwargs: Any) -> dict[str, Any]:
        """What a run would do, without doing it."""

        _, config, warm_ids = self.build(**kwargs)
        return {
            **config.as_dict(),
            "total_cells": len(config.contenders) * len(config.tasks),
            "warm_models": sorted(warm_ids),
        }

    def stream(self, **kwargs: Any) -> Iterator[dict[str, Any]]:
        """Run the comparison, reporting each result as it lands."""

        pairs, config, warm_ids = self.build(**kwargs)
        finalize = None
        if config.judge.enabled:
            finalize = judge.pass_over(self._config(config.judge.model_id), config.judge)
        return runner.run(pairs, config, finalize=finalize, warm_ids=warm_ids)

    def cancel(self, run_id: str) -> dict[str, Any]:
        return {"cancelled": runner.cancel(run_id)}

    # -- validation --------------------------------------------------------------

    def _config(self, model_id: str) -> LLMConfig:
        try:
            return self._registry.get(model_id)
        except ValueError as exc:
            raise LookupError(f"No model called '{model_id}' is set up.") from exc

    def _pairs(
        self, model_ids: list[str], versions: dict[str, str]
    ) -> list[tuple[LLMConfig, Contender]]:
        # Order-preserving de-duplication: the same model twice is not a comparison, and
        # the columns have to stay in the order the user picked them.
        unique = list(dict.fromkeys(model_ids))
        if len(unique) < MIN_CONTENDERS:
            raise ValueError(f"Pick at least {MIN_CONTENDERS} different models to compare.")
        if len(unique) > MAX_CONTENDERS:
            raise ValueError(f"Neo compares up to {MAX_CONTENDERS} models at a time.")
        pairs = []
        for model_id in unique:
            config = self._config(model_id)
            pairs.append(
                (config, runner.contender_from_config(config, version=versions.get(model_id, "")))
            )
        return pairs

    def _tasks(self, use_case: str, depth: int, prompts: list[str]) -> list[Task]:
        if use_case not in USE_CASES:
            raise ValueError(
                f"Unknown use case '{use_case}'. Expected one of: {', '.join(USE_CASES)}."
            )
        if use_case == "custom":
            written = [item for item in prompts if item and item.strip()]
            if not written:
                raise ValueError("Write at least one question you want the models compared on.")
            if len(written) > MAX_CUSTOM_PROMPTS:
                raise ValueError(
                    f"A custom comparison takes up to {MAX_CUSTOM_PROMPTS} questions."
                )
            return tasks.custom_tasks(written)
        selected = tasks.select(use_case, depth)
        if not selected:
            raise ValueError(f"There are no tasks for '{use_case}'.")
        return selected

    def _generation(
        self, temperature: float, max_output_tokens: int | None, allow_thinking: bool
    ) -> GenerationSettings:
        if not 0 <= temperature <= MAX_TEMPERATURE:
            raise ValueError(f"Temperature has to be between 0 and {MAX_TEMPERATURE:g}.")
        if max_output_tokens is not None and not (
            MIN_OUTPUT_TOKENS <= max_output_tokens <= MAX_OUTPUT_TOKENS
        ):
            raise ValueError(
                f"The output limit has to be between {MIN_OUTPUT_TOKENS} and "
                f"{MAX_OUTPUT_TOKENS} tokens."
            )
        return GenerationSettings(
            temperature=float(temperature),
            max_output_tokens=max_output_tokens,
            allow_thinking=bool(allow_thinking),
        )

    def _judge(
        self, judge_id: str | None, rubric: str, versions: dict[str, str]
    ) -> JudgeSettings:
        if not judge_id:
            return JudgeSettings()
        if len(rubric or "") > MAX_RUBRIC_LENGTH:
            raise ValueError("That rubric is longer than the judge can usefully read.")
        config = self._config(judge_id)
        return JudgeSettings(
            model_id=judge_id,
            display_name=config.model or config.name,
            model_version=versions.get(judge_id, ""),
            rubric=(rubric or "").strip(),
        )


def _endpoint(config: LLMConfig) -> tuple[str, str]:
    return (config.provider, config.base_url)


def _ollama_provider(registry: LLMRegistryService, base_url: str) -> dict[str, Any]:
    """The registry's Ollama provider for this address, created if there is not one."""

    for provider in registry.list_providers():
        if provider.get("provider_type") != "ollama":
            continue
        if str(provider.get("base_url") or "").rstrip("/") == base_url:
            return provider
    return registry.create_provider(
        ProviderCreate(
            name="Ollama",
            provider_type="ollama",
            base_url=base_url,
            metadata={"created_by": "model_compare"},
        )
    )


def _versions_for(
    registry: LLMRegistry, model_ids: list[str], judge_id: str | None
) -> dict[str, str]:
    """The exact build behind each chosen model's tag, keyed by registry id.

    A tag like "latest" moves. Without this a stored result could not say which build it
    actually measured, which is most of what makes an old result worth keeping.
    """

    wanted = [item for item in [*model_ids, judge_id] if item]
    configs, _ = registry.list()
    chosen = [item for item in configs if item.id in set(wanted)]
    if not chosen:
        return {}
    probed = _reachability(chosen)
    return {
        config.id: (probed.get(_endpoint(config), {}).get("versions") or {}).get(config.model, "")
        for config in chosen
    }


def _warm_ids(contenders: list[Contender]) -> frozenset[str]:
    """Which of these models a provider already has loaded, where it will say."""

    resident: dict[str, set[str]] = {}
    warm: set[str] = set()
    for contender in contenders:
        if contender.provider != "ollama":
            # No equivalent question to ask a hosted provider. Treated as cold, which
            # only ever makes the estimate more conservative.
            continue
        if contender.base_url not in resident:
            resident[contender.base_url] = runner.resident_models(contender.base_url)
        if contender.model in resident[contender.base_url]:
            warm.add(contender.id)
    return frozenset(warm)


def _reachability(configs: list[LLMConfig]) -> dict[tuple[str, str], dict[str, Any]]:
    """Ask each distinct provider once what it has, rather than each model separately.

    Ollama lists every installed model in one call, so a comparison of four local models
    costs one request instead of four -- which is the difference between the picker
    appearing at once and appearing after a visible pause.
    """

    found: dict[tuple[str, str], dict[str, Any]] = {}
    for config in configs:
        key = _endpoint(config)
        if key in found:
            continue
        found[key] = _probe(config)
    return found


def _ollama_version(entry: dict[str, Any]) -> str:
    """The build behind a tag, in the terms Ollama reports it."""

    details = entry.get("details") or {}
    parts = [
        str(details.get("parameter_size") or ""),
        str(details.get("quantization_level") or ""),
        str(entry.get("digest") or "")[:7],
    ]
    return " · ".join(part for part in parts if part)


def _probe(config: LLMConfig) -> dict[str, Any]:
    base = config.base_url.rstrip("/")
    try:
        if config.provider == "ollama":
            response = requests.get(f"{base}/api/tags", timeout=DISCOVERY_TIMEOUT_SECONDS)
            response.raise_for_status()
            names: set[str] = set()
            versions: dict[str, str] = {}
            for entry in response.json().get("models") or []:
                name = str(entry.get("name") or entry.get("model") or "")
                if not name:
                    continue
                names.add(name)
                versions[name] = _ollama_version(entry)
                # Ollama answers to "qwen3:8b" whether or not the tag was written out, so
                # record the bare name too or a registry entry that omits ":latest" reads
                # as missing when it is right there.
                if name.endswith(":latest"):
                    bare = name.rsplit(":", 1)[0]
                    names.add(bare)
                    versions[bare] = versions[name]
            return {"reachable": True, "models": names, "versions": versions}
        headers = (
            {"Authorization": f"Bearer {config.resolved_api_key()}"}
            if config.resolved_api_key()
            else {}
        )
        response = requests.get(
            f"{base}/models", headers=headers, timeout=DISCOVERY_TIMEOUT_SECONDS
        )
        response.raise_for_status()
        return {
            "reachable": True,
            "models": {str(item.get("id") or "") for item in response.json().get("data") or []},
            "versions": {},
        }
    except requests.RequestException as exc:
        _LOG.debug("Could not list models at %s: %s", base, exc)
        return {"reachable": False, "models": set(), "versions": {}}
    except ValueError:
        # Reachable, but not answering in a shape Neo understands. Listing is a
        # convenience; the model may still work, so this is not called unreachable.
        return {"reachable": True, "models": set(), "versions": {}}


def _installed(state: dict[str, Any], config: LLMConfig) -> bool | None:
    if not state.get("reachable"):
        return None
    models = state.get("models") or set()
    if not models:
        return None
    return config.model in models


def _availability_note(state: dict[str, Any], config: LLMConfig) -> str:
    """Why a model might not be worth picking, in a sentence, or nothing at all."""

    if state.get("reachable") is False:
        where = (
            "on this computer"
            if runner.is_local(config.base_url)
            else "at the address it is set up with"
        )
        return f"Neo cannot reach this one {where} right now."
    if _installed(state, config) is False:
        return "This one is set up but not downloaded yet."
    return ""
