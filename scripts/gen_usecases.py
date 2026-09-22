#!/usr/bin/env python3
"""Erzeugt die Usecase-Compose-Dateien aus docker-compose.yml.

Einziger Unterschied zum Basis-Stack sind die pro Usecase eindeutigen
Namen (Volumes + Egress-Netz). Basis aendern -> Skript neu laufen lassen.
"""
import pathlib, sys

USECASES = ["jrz", "gw", "wienerlinien", "neumann", "ustp"]
NAMES = ["kermit_postgres", "kermit_qdrant", "kermit_documents",
         "kermit_hf_cache", "kermit_ollama", "kermit_skill_egress"]

root = pathlib.Path(__file__).resolve().parent.parent  # Repo-Wurzel
base = (root / "docker-compose.yml").read_text()

HEADER = """# ══════════════════════════════════════════════════════════════
# Usecase-Stack: {uc}
#
# GENERIERT aus docker-compose.yml von scripts/gen_usecases.py.
# Nicht von Hand editieren – Aenderungen gehoeren in docker-compose.yml,
# danach `python3 scripts/gen_usecases.py` ausfuehren.
#
# Einziger Unterschied zum Basis-Stack: alle persistenten Volumes und das
# Skill-Egress-Netz tragen den Praefix `kermit_{uc}_`, damit mehrere
# Usecases auf demselben Coolify-Server datentechnisch getrennt bleiben.
#
# Pro Usecase in Coolify eigene .env setzen: ORIGIN, AUTH_SECRET,
# SANDBOX_TOKEN, POSTGRES_PASSWORD.
# ══════════════════════════════════════════════════════════════

"""

for uc in USECASES:
    out = base
    for n in NAMES:
        out = out.replace(n, n.replace("kermit_", f"kermit_{uc}_", 1))
    # Original-Kopfkommentar (bis zur ersten Leerzeile nach dem Rahmen) ersetzen
    body = out.split("\nservices:\n", 1)[1]
    out = HEADER.format(uc=uc) + "services:\n" + body
    path = root / f"docker-compose.{uc}.yml"
    path.write_text(out)
    print(f"geschrieben: {path.name}")
