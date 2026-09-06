"""Orchestration for local model setup.

Routes stay thin: they parse and validate, this decides. Everything returned from here
is already shaped for the interface, and every model carries its prose alongside its
numbers so that no caller has to reinvent the phrasing.
"""

from __future__ import annotations

from typing import Any

from app.services.local_models import catalog, installer, probe, wording
from app.services.local_models.sizing import rank
from app.services.local_models.types import GOALS, Machine, Recommendation

# The question the wizard asks first, in the words it asks it. Deliberately phrased as
# things a person wants to do, not as capabilities a model has -- someone who does not
# know what a model is still knows whether they want help with writing or with code.
GOAL_CHOICES: list[dict[str, str]] = [
    {
        "id": "chat",
        "label": "General questions and chat",
        "detail": "Everyday help, explanations and ideas.",
    },
    {
        "id": "writing",
        "label": "Writing and editing",
        "detail": "Drafting, rewriting and shortening long text.",
    },
    {
        "id": "coding",
        "label": "Programming",
        "detail": "Reading, writing and explaining code.",
    },
    {
        "id": "reasoning",
        "label": "Difficult problems",
        "detail": "Slower answers, worked through more carefully.",
    },
    {
        "id": "images",
        "label": "Pictures and screenshots",
        "detail": "Describing and answering questions about images.",
    },
    {
        "id": "offline",
        "label": "Working with no internet",
        "detail": "Small and self-contained, for travel or privacy.",
    },
]


class LocalModelsService:
    def goals(self) -> dict[str, Any]:
        return {"goals": GOAL_CHOICES}

    def machine(self, *, fresh: bool = False) -> dict[str, Any]:
        found = probe.detect(fresh=fresh)
        return {**found.as_dict(), "summary": wording.describe_machine(found)}

    def recommendations(
        self,
        *,
        goal: str = "chat",
        limit: int = 40,
        include_unfit: bool = True,
        fresh: bool = False,
    ) -> dict[str, Any]:
        if goal not in GOALS:
            # Rejected rather than silently defaulted: a typo that quietly answers for
            # a different goal gives confident, wrong-shaped advice.
            raise ValueError(f"Unknown goal '{goal}'. Expected one of: {', '.join(GOALS)}.")

        found = probe.detect(fresh=fresh)
        models = catalog.get_models()
        ranked = rank(models, found, goal=goal, limit=limit, include_unfit=include_unfit)
        installed = installer.installed_tags()

        return {
            "machine": {**found.as_dict(), "summary": wording.describe_machine(found)},
            "goal": goal,
            "recommendations": [self._present(item, found, installed) for item in ranked],
        }

    def model(self, model_id: str, *, goal: str = "chat") -> dict[str, Any]:
        if goal not in GOALS:
            raise ValueError(f"Unknown goal '{goal}'. Expected one of: {', '.join(GOALS)}.")

        row = catalog.get_model(model_id)
        if row is None:
            raise LookupError(f"No model called '{model_id}' in the catalog.")

        found = probe.detect()
        ranked = rank([row], found, goal=goal, limit=1, include_unfit=True)
        presented = self._present(ranked[0], found, installer.installed_tags())
        presented["manual_steps"] = catalog.manual_steps(row)
        return presented

    def installed(self) -> dict[str, Any]:
        """Which catalog models are already downloaded, and whether an engine is there."""

        tags = installer.installed_tags()
        return {
            "engine_available": installer.engine_available(),
            "installed": sorted(
                model["id"]
                for model in catalog.get_models()
                if model.get("ollama") and model["ollama"] in tags
            ),
        }

    def _present(
        self, item: Recommendation, machine: Machine, installed: set[str]
    ) -> dict[str, Any]:
        payload = item.as_dict()
        payload["plain"] = wording.plain(item.model, item.fit)
        payload["install_options"] = catalog.install_options(item.model, machine)
        payload["installed"] = bool(item.model.get("ollama") and item.model["ollama"] in installed)
        return payload
