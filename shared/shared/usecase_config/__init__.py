"""Per-usecase configuration + prompt resolver.

Reads overrides from the ``usecase_config`` and ``usecase_prompts`` tables
in Postgres; falls back to env vars / hardcoded defaults if no row is set
or if the DB is unreachable.

Public API::

    from shared.usecase_config import resolve_config, resolve_prompt
    from shared.usecase_config import invalidate, set_pool

    cfg = await resolve_config(use_case="neumann")
    prompt = await resolve_prompt("neumann", "agent.system.default", default="…")
"""

from shared.usecase_config.resolver import (
    GLOBAL_USE_CASE,
    Skill,
    SkillExecutionBundle,
    SkillFunction,
    UseCase,
    clear_skill_code,
    create_skill,
    delete_skill,
    delete_use_case,
    get_execution_bundle,
    get_skill,
    get_skill_code,
    get_skill_variables,
    get_use_case,
    invalidate,
    invalidate_all,
    invalidate_use_cases,
    list_deleted_use_cases,
    list_functions_for_skill,
    list_prompts,
    list_skill_executions,
    list_skill_functions,
    list_skills,
    list_use_cases,
    log_skill_execution,
    resolve_config,
    resolve_prompt,
    set_function_permission,
    set_pool,
    set_skill_code,
    set_skill_variables,
    update_skill,
    upsert_config,
    upsert_prompt,
    upsert_use_case,
)

__all__ = [
    "GLOBAL_USE_CASE",
    "Skill",
    "SkillExecutionBundle",
    "SkillFunction",
    "UseCase",
    "clear_skill_code",
    "create_skill",
    "delete_skill",
    "delete_use_case",
    "list_deleted_use_cases",
    "get_execution_bundle",
    "get_skill",
    "get_skill_code",
    "get_skill_variables",
    "get_use_case",
    "invalidate",
    "invalidate_all",
    "invalidate_use_cases",
    "list_prompts",
    "list_functions_for_skill",
    "list_skill_executions",
    "list_skill_functions",
    "list_skills",
    "list_use_cases",
    "log_skill_execution",
    "resolve_config",
    "resolve_prompt",
    "set_pool",
    "set_function_permission",
    "set_skill_code",
    "set_skill_variables",
    "update_skill",
    "upsert_config",
    "upsert_prompt",
    "upsert_use_case",
]
