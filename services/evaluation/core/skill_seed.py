"""Seed the reference skills shipped with the platform (``config/skills/``).

Runs at startup, next to :func:`core.seed.seed_use_cases`, and follows the
same create-if-missing contract: a skill that already exists in the database is
left alone, so anything an admin edited in the dashboard survives a restart.

The code file is a separate question from the skill row. A skill that exists
but has no code file yet gets one — that is how an existing deployment picks
up the plugin layer without anyone re-creating skills by hand. A skill whose
code file was *replaced* in the dashboard keeps the replacement: the seed only
writes source when there is none.

Variables are never seeded. They hold API keys, they belong to a deployment
rather than to the repository, and a default value in version control is
exactly the kind of credential nobody remembers to change.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from shared.skills import SkillCodeError, detect_language, parse_skill_code
from shared.usecase_config import (
    create_skill,
    list_skills,
    list_use_cases,
    set_skill_code,
)

logger = logging.getLogger(__name__)

_DEFAULT_DIR = Path(__file__).resolve().parent.parent / "config" / "skills"


def _skills_dir() -> Path:
    override = os.getenv("SKILLS_CONFIG_DIR")
    return Path(override) if override else _DEFAULT_DIR


async def _target_use_cases(declared: list[str]) -> list[str]:
    """Resolve a manifest entry's ``use_cases`` to concrete ids."""
    if "*" in declared:
        return [uc.id for uc in await list_use_cases(only_enabled=True)]
    return list(declared)


async def seed_skills(directory: str | Path | None = None) -> int:
    """Create missing reference skills and import their code files.

    Returns the number of skills whose code file was written (created or
    back-filled). Never raises: a failing seed must not stop the service, it
    just means the reference skill is missing until someone looks at the log.
    """
    base = Path(directory) if directory else _skills_dir()
    manifest_path = base / "manifest.json"
    if not manifest_path.exists():
        logger.info("No skill manifest at %s — skipping skill seed", manifest_path)
        return 0

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        logger.error("Could not read skill manifest %s: %s", manifest_path, exc)
        return 0

    imported = 0
    for entry in manifest.get("skills", []):
        name = entry.get("name")
        code_file = entry.get("code_file")
        if not name or not code_file:
            logger.warning("Skipping skill manifest entry without name/code_file: %r", entry)
            continue

        source_path = base / code_file
        if not source_path.exists():
            logger.warning("Skill %r references missing file %s", name, source_path)
            continue
        try:
            source = source_path.read_text(encoding="utf-8")
            language = detect_language(code_file)
            functions = parse_skill_code(source, language)
        except (OSError, SkillCodeError) as exc:
            logger.error("Skill %r could not be parsed: %s", name, exc)
            continue

        for use_case in await _target_use_cases(entry.get("use_cases") or ["*"]):
            try:
                existing = {s.name: s for s in await list_skills(use_case)}
                skill = existing.get(name)
                if skill is None:
                    skill = await create_skill(
                        use_case,
                        name,
                        entry.get("overview", ""),
                        entry.get("detailed_task", ""),
                        enabled=bool(entry.get("enabled", True)),
                        position=int(entry.get("position", 0)),
                    )
                elif skill.has_code:
                    # Someone already put a code file here — possibly a
                    # modified copy of this one. Leave it.
                    continue

                await set_skill_code(
                    skill.id,
                    language=language,
                    file_name=code_file,
                    source=source,
                    functions=[
                        {
                            "name": f.name,
                            "signature": f.signature(),
                            "description": f.description,
                            "params": list(f.params),
                            "ask_for_permission": f.ask_for_permission,
                            "allowed_domains": list(f.allowed_domains),
                        }
                        for f in functions
                    ],
                )
                imported += 1
                logger.info(
                    "Seeded skill %r for use case %r with %d function(s)",
                    name, use_case, len(functions),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Could not seed skill %r for use case %r: %s", name, use_case, exc
                )

    return imported
