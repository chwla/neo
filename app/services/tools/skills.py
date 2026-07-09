from __future__ import annotations

from app.services.tools import store


def resolve_skill_instructions(skill_ids: list[str]) -> dict:
    instructions, warnings, skills = [], [], []
    for skill_id in skill_ids:
        skill = store.get_skill(skill_id)
        if not skill or not skill.get("enabled"):
            warnings.append(f"Skill '{skill_id}' is unavailable and was ignored.")
            continue
        skills.append(skill)
        instructions.append(f"{skill['display_name'] or skill['name']}:\n{skill['instructions']}")
    return {"skills": skills, "instructions": instructions, "warnings": warnings}
