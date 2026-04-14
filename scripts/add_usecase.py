#!/usr/bin/env python3
"""Legt einen neuen Use Case an – idempotent.

Usage:
    python scripts/add_usecase.py <use_case_id> [--prefix <p>] [--collection <c>]
                                                 [--role default,trainee]
                                                 [--description "Kurztext"]

Was passiert:
    1. Prompt-Datei pro Rolle unter
       services/evaluation/core/prompts/use_cases/<id>/<role>.txt
    2. Eintrag in services/evaluation/core/prompts/use_cases_registry.json
    3. Plugin-Datei services/data_structure/plugins/<id>.py (Vorlage)
    4. Import + register_plugin() in services/data_structure/plugins/__init__.py
    5. Prefix-Eintrag in services/evaluation/core/use_cases.py

Existierende Einträge werden nicht überschrieben – das Script ist sicher
mehrfach aufrufbar.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

REGISTRY = ROOT / "services/evaluation/core/prompts/use_cases_registry.json"
PROMPTS_DIR = ROOT / "services/evaluation/core/prompts/use_cases"
PLUGINS_DIR = ROOT / "services/data_structure/plugins"
PLUGIN_INIT = PLUGINS_DIR / "__init__.py"
USE_CASES_PY = ROOT / "services/evaluation/core/use_cases.py"
FRONTEND_USE_CASES_TS = ROOT / "services/frontend/src/lib/use-cases.ts"

# Rotation of Tailwind colors for new use cases (fallback after built-ins).
_COLOR_PALETTE = [
    ("bg-orange-600", "#ea580c"),
    ("bg-rose-600", "#e11d48"),
    ("bg-cyan-600", "#0891b2"),
    ("bg-amber-600", "#d97706"),
    ("bg-teal-600", "#0d9488"),
    ("bg-indigo-600", "#4f46e5"),
]

ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def die(msg: str) -> None:
    print(f"✖ {msg}", file=sys.stderr)
    sys.exit(1)


def class_name_from_id(use_case_id: str) -> str:
    return "".join(part.capitalize() for part in use_case_id.split("_")) + "Plugin"


def ensure_prompt_files(use_case_id: str, roles: list[str]) -> list[Path]:
    created: list[Path] = []
    uc_dir = PROMPTS_DIR / use_case_id
    uc_dir.mkdir(parents=True, exist_ok=True)
    for role in roles:
        path = uc_dir / f"{role}.txt"
        if path.exists():
            continue
        path.write_text(
            f"# System-Prompt für Use Case '{use_case_id}' (Rolle: {role}).\n"
            f"# Beschreibe hier Zielgruppe, Tonalität und Antwortformat.\n\n"
            "Du bist ein hilfreicher Assistent. Beantworte Fragen ausschließlich\n"
            "auf Basis des bereitgestellten Kontexts. Zitiere Quellen mit [n].\n"
            "Wenn die Antwort nicht im Kontext steht, sage das klar.\n",
            encoding="utf-8",
        )
        created.append(path)
    return created


def ensure_registry_entry(
    use_case_id: str,
    roles: list[str],
    collection: str,
    prefix: str,
    actions: list[str],
) -> bool:
    data = json.loads(REGISTRY.read_text(encoding="utf-8"))
    if use_case_id in data:
        return False
    data[use_case_id] = {
        "roles": roles,
        "agent_action_names": actions,
        "default_collection": collection,
        "collection_prefixes": [prefix],
    }
    # Stable key ordering: alphabetical
    data = {k: data[k] for k in sorted(data.keys())}
    REGISTRY.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return True


PLUGIN_TEMPLATE = '''"""Plugin für Use Case '{use_case_id}'.

Generiert von scripts/add_usecase.py. Ergänzen Sie enrich_metadata() mit
Ihrer use-case-spezifischen Logik. Prompt, Actions und Default-Collection
werden aus prompts/use_cases_registry.json gelesen.
"""

from __future__ import annotations

from shared.models import ChunkMetadata

from plugins.base import BasePlugin


class {class_name}(BasePlugin):
    use_case_id = "{use_case_id}"

    def enrich_metadata(
        self,
        base: ChunkMetadata,
        text: str,
        raw_meta: dict,
    ) -> ChunkMetadata:
        """Use-Case-spezifische Metadaten.

        Standard: alle Request-Felder nach base.extra übernehmen.
        Hier eigene Regeln ergänzen, z.B.:
            base.extra["kategorie"] = raw_meta.get("kategorie", "unbekannt")
        """
        base.extra = {{**(base.extra or {{}}), **raw_meta}}
        return base

    def get_collections(self) -> list[str]:
        return ["{collection}"]
'''


def ensure_plugin_file(use_case_id: str, collection: str) -> bool:
    path = PLUGINS_DIR / f"{use_case_id}.py"
    if path.exists():
        return False
    path.write_text(
        PLUGIN_TEMPLATE.format(
            use_case_id=use_case_id,
            class_name=class_name_from_id(use_case_id),
            collection=collection,
        ),
        encoding="utf-8",
    )
    return True


def ensure_plugin_registered(use_case_id: str) -> bool:
    """Patch plugins/__init__.py: add import + register_plugin() idempotently."""
    text = PLUGIN_INIT.read_text(encoding="utf-8")
    class_name = class_name_from_id(use_case_id)
    import_line = f"from plugins.{use_case_id} import {class_name}"
    register_line = f"register_plugin({class_name}())"

    changed = False
    if import_line not in text:
        # Insert after the last existing "from plugins." import
        lines = text.splitlines()
        last_import = max(
            (i for i, ln in enumerate(lines) if ln.startswith("from plugins.")),
            default=-1,
        )
        if last_import == -1:
            die("Konnte Import-Block in plugins/__init__.py nicht finden.")
        lines.insert(last_import + 1, import_line)
        text = "\n".join(lines) + ("\n" if not text.endswith("\n") else "")
        changed = True

    if register_line not in text:
        text = text.rstrip() + f"\n{register_line}\n"
        changed = True

    if changed:
        PLUGIN_INIT.write_text(text, encoding="utf-8")
    return changed


_USE_CASES_ARRAY_RE = re.compile(
    r"(export const USE_CASES:\s*UseCaseDef\[\]\s*=\s*\[)(.*?)(\]\s*;)",
    re.DOTALL,
)


def ensure_frontend_entry(use_case_id: str, label: str, description: str) -> bool:
    """Insert a new entry into the frontend USE_CASES array. Idempotent by apiId."""
    text = FRONTEND_USE_CASES_TS.read_text(encoding="utf-8")
    m = _USE_CASES_ARRAY_RE.search(text)
    if not m:
        die("Konnte USE_CASES-Array in use-cases.ts nicht finden.")
    body = m.group(2)
    if f"apiId: '{use_case_id}'" in body or f'apiId: "{use_case_id}"' in body:
        return False

    existing = len(re.findall(r"apiId:\s*'[^']+'", body))
    color, accent = _COLOR_PALETTE[existing % len(_COLOR_PALETTE)]

    slug = use_case_id.replace("_", "-")
    entry = (
        "\t{\n"
        f"\t\tslug: '{slug}',\n"
        f"\t\tapiId: '{use_case_id}',\n"
        f"\t\tlabel: '{label}',\n"
        f"\t\tdesc: '{description}',\n"
        f"\t\tcolor: '{color}',\n"
        f"\t\taccent: '{accent}'\n"
        "\t}"
    )
    # Append before the closing bracket, with a comma on the previous last entry
    trimmed = body.rstrip()
    if trimmed.endswith("}"):
        new_body = trimmed + ",\n" + entry + "\n"
    else:
        new_body = trimmed + "\n" + entry + "\n"
    new_text = text[: m.start(2)] + new_body + text[m.end(2) :]
    FRONTEND_USE_CASES_TS.write_text(new_text, encoding="utf-8")
    return True


_PREFIX_DICT_RE = re.compile(
    r"(_USE_CASE_PREFIXES:\s*dict\[str,\s*list\[str\]\]\s*=\s*\{)([^}]*)(\})",
    re.DOTALL,
)


def ensure_prefix_entry(use_case_id: str, prefix: str) -> bool:
    text = USE_CASES_PY.read_text(encoding="utf-8")
    m = _PREFIX_DICT_RE.search(text)
    if not m:
        die("Konnte _USE_CASE_PREFIXES in use_cases.py nicht finden.")
    body = m.group(2)
    if f'"{use_case_id}"' in body:
        return False
    new_entry = f'    "{use_case_id}": ["{prefix}"],\n'
    # Append before closing brace, preserving existing content
    new_body = body.rstrip() + "\n" + new_entry
    new_text = text[: m.start(2)] + new_body + text[m.end(2) :]
    USE_CASES_PY.write_text(new_text, encoding="utf-8")
    return True


def main() -> None:
    p = argparse.ArgumentParser(description="Neuen Use Case anlegen (idempotent).")
    p.add_argument("use_case_id", help="z.B. 'ustp' – nur [a-z0-9_], muss mit Buchstabe starten.")
    p.add_argument("--prefix", help="Collection-Prefix (Default: <id>_)")
    p.add_argument("--collection", help="Default-Collection (Default: <prefix>default)")
    p.add_argument(
        "--roles",
        default="default",
        help="Komma-Liste von Rollen, z.B. 'default,trainee' (Default: default).",
    )
    p.add_argument(
        "--actions",
        default="SEARCH,CLARIFY,RECALL_MEMORY,LOOKUP_SOURCES,FINAL_ANSWER",
        help="Komma-Liste erlaubter Agent-Actions.",
    )
    p.add_argument("--label", help="Anzeigename im Frontend (Default: <id>).")
    p.add_argument(
        "--description",
        default="Neuer Use Case",
        help="Kurzbeschreibung im Frontend (Default: 'Neuer Use Case').",
    )
    args = p.parse_args()

    uc_id = args.use_case_id.strip().lower()
    if not ID_RE.match(uc_id):
        die(f"Ungültige use_case_id '{uc_id}' – erlaubt: [a-z][a-z0-9_]*")

    prefix = args.prefix or f"{uc_id}_"
    if not prefix.endswith("_"):
        prefix += "_"
    collection = args.collection or f"{prefix}default"
    roles = [r.strip() for r in args.roles.split(",") if r.strip()]
    actions = [a.strip() for a in args.actions.split(",") if a.strip()]

    for pth in (REGISTRY, PLUGIN_INIT, USE_CASES_PY, FRONTEND_USE_CASES_TS):
        if not pth.exists():
            die(f"Erwartete Datei fehlt: {pth}")

    label = args.label or uc_id.replace("_", " ").title()

    print(f"→ Lege Use Case '{uc_id}' an (prefix={prefix}, collection={collection})")

    steps: list[tuple[str, bool]] = []
    created_prompts = ensure_prompt_files(uc_id, roles)
    steps.append((
        f"Prompt-Dateien ({len(created_prompts)} neu)",
        bool(created_prompts),
    ))
    steps.append((
        "Eintrag in use_cases_registry.json",
        ensure_registry_entry(uc_id, roles, collection, prefix, actions),
    ))
    steps.append((
        f"Plugin-Datei plugins/{uc_id}.py",
        ensure_plugin_file(uc_id, collection),
    ))
    steps.append((
        "Registrierung in plugins/__init__.py",
        ensure_plugin_registered(uc_id),
    ))
    steps.append((
        "Prefix-Eintrag in use_cases.py",
        ensure_prefix_entry(uc_id, prefix),
    ))
    steps.append((
        "Frontend-Eintrag in use-cases.ts",
        ensure_frontend_entry(uc_id, label, args.description),
    ))

    for label, changed in steps:
        mark = "✓ neu" if changed else "• vorhanden"
        print(f"  {mark}: {label}")

    print()
    print("Nächste Schritte:")
    for role in roles:
        print(f"  • Prompt anpassen: services/evaluation/core/prompts/use_cases/{uc_id}/{role}.txt")
    print(f"  • enrich_metadata() anpassen: services/data_structure/plugins/{uc_id}.py")
    print("  • Services neu starten, dann: POST /v1/structure mit use_case="
          f"'{uc_id}'")


if __name__ == "__main__":
    main()
