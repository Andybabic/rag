# Neuen Use Case hinzufuegen

Diese Anleitung beschreibt, wie ein neuer Use Case zur RAG Platform
hinzugefuegt wird. Als Beispiel erstellen wir den Use Case **"stadtwerke"**
fuer einen Wasser-/Abwasser-Wartungsassistenten.

---

## Architektur-Ueberblick

Ein Use Case besteht aus zwei Teilen:

1. **Plugin** im Data-Structure-Service – bestimmt, wie Dokumente
   zerlegt und mit Metadaten angereichert werden.
2. **Use-Case-Eintrag** im Evaluation-Service – definiert System-Prompt,
   erlaubte Agent-Actions und Standard-Collection.

```
Dokument-Upload                    Benutzer-Anfrage
      │                                  │
      ▼                                  ▼
┌─────────────┐                  ┌───────────────┐
│ Data Struct. │                  │  Evaluation   │
│  Plugin      │                  │  use_cases.py │
│  ↓           │                  │  ↓            │
│ enrich_meta  │                  │ system_prompt │
│ collections  │                  │ agent_actions │
│ routing      │                  │ collection    │
└─────────────┘                  └───────────────┘
```

---

## Schritt 1: Plugin erstellen

Kopieren Sie die Vorlage und benennen Sie sie um:

```bash
cd services/data_structure
cp plugins/beispiel.py plugins/stadtwerke.py
```

Bearbeiten Sie `plugins/stadtwerke.py`:

```python
"""Stadtwerke-Plugin: Wasser-/Abwasser-Wartungsassistent."""

from __future__ import annotations

import re

from shared.models import ChunkMetadata

from plugins.base import BasePlugin


class StadtwerkePlugin(BasePlugin):
    # Eindeutiges Kuerzel – wird in API-Requests als use_case uebergeben
    use_case_id = "stadtwerke"

    # Schluesselwoerter fuer automatische Kategorisierung
    _ANLAGEN_KEYWORDS = {
        "pumpwerk": ["pumpe", "pumpwerk", "foerderanlage", "druckleitung"],
        "klaeranlage": ["klaeranlage", "belebung", "nachklaerung", "schlamm"],
        "netz": ["rohrleitung", "schieber", "hydrant", "hausanschluss"],
    }

    def enrich_metadata(
        self,
        base: ChunkMetadata,
        text: str,
        raw_meta: dict,
    ) -> ChunkMetadata:
        # Anlagentyp erkennen
        text_lower = text.lower()
        anlage = "allgemein"
        best_score = 0
        for typ, keywords in self._ANLAGEN_KEYWORDS.items():
            score = sum(1 for kw in keywords if kw in text_lower)
            if score > best_score:
                best_score = score
                anlage = typ

        # Normreferenz extrahieren (DIN, DVGW, etc.)
        norm_match = re.search(
            r"(DIN\s*(?:EN\s*)?\d+|DVGW\s*[A-Z]\s*\d+)", text
        )
        norm_ref = norm_match.group(0) if norm_match else ""

        base.extra = {
            **raw_meta,
            "anlage": anlage,
            "norm_ref": norm_ref,
        }
        # Collection-Routing nach Anlagentyp
        base.collection = f"sw_{anlage}"
        return base

    def get_collections(self) -> list[str]:
        return [
            "sw_pumpwerk",
            "sw_klaeranlage",
            "sw_netz",
            "sw_allgemein",
        ]

    def get_system_prompt(self, role: str = "default") -> str:
        if role == "trainee":
            return (
                "Du bist ein Lern-Assistent fuer Auszubildende der "
                "Stadtwerke. Erklaere technische Konzepte einfach "
                "und verstaendlich. Gib immer die Quelle an."
            )
        return (
            "Du bist ein Wartungsassistent fuer Wasser- und "
            "Abwasseranlagen der Stadtwerke. Beantworte Fragen "
            "fachlich praezise. Zitiere relevante DIN/DVGW-Normen. "
            "Gib Dokument und Seitenzahl an."
        )

    def get_agent_actions(self) -> list[str]:
        return [
            "SEARCH",
            "RECALL_MEMORY",
            "LOOKUP_SOURCES",
            "FINAL_ANSWER",
        ]
```

### BasePlugin-Methoden im Detail

| Methode | Pflicht? | Beschreibung |
|---------|----------|--------------|
| `enrich_metadata(base, text, raw_meta)` | Ja | Analysiert jeden Chunk und fuellt `base.extra` |
| `get_collections()` | Nein | Gibt die Qdrant-Collections zurueck. Erster Eintrag = Default |
| `get_system_prompt(role)` | Nein | LLM-System-Prompt, optional rollenbasiert |
| `get_agent_actions()` | Nein | Verfuegbare Actions im ReAct-Loop |

---

## Schritt 2: Plugin registrieren (Data Structure)

In `services/data_structure/plugins/__init__.py` den Import und die
Registrierung ergaenzen:

```python
from plugins.stadtwerke import StadtwerkePlugin

# ... bestehende register_plugin()-Aufrufe ...

register_plugin(StadtwerkePlugin())
```

**Testen:**

```bash
cd services/data_structure
python -c "from plugins import get_plugin; p = get_plugin('stadtwerke'); print(p.get_collections())"
# ['sw_pumpwerk', 'sw_klaeranlage', 'sw_netz', 'sw_allgemein']
```

---

## Schritt 3: Use Case im Evaluation-Service registrieren

In `services/evaluation/core/use_cases.py` den neuen Eintrag in
`_USE_CASE_META` ergaenzen:

```python
_USE_CASE_META: dict[str, dict] = {
    # ... bestehende Use Cases ...

    "stadtwerke": {
        "system_prompt": {
            "default": (
                "Du bist ein Wartungsassistent fuer Wasser- und "
                "Abwasseranlagen der Stadtwerke. Beantworte Fragen "
                "fachlich praezise. Zitiere relevante DIN/DVGW-Normen. "
                "Gib Dokument und Seitenzahl an."
            ),
            "trainee": (
                "Du bist ein Lern-Assistent fuer Auszubildende der "
                "Stadtwerke. Erklaere technische Konzepte einfach "
                "und verstaendlich. Gib immer die Quelle an."
            ),
        },
        "agent_actions": [
            "SEARCH",
            "RECALL_MEMORY",
            "LOOKUP_SOURCES",
            "FINAL_ANSWER",
        ],
        "default_collection": "sw_pumpwerk",
    },
}
```

**Wichtig:** Die Werte muessen mit dem Plugin uebereinstimmen:

- `system_prompt` → gleicher Text wie `get_system_prompt()`
- `agent_actions` → gleiche Liste wie `get_agent_actions()`
- `default_collection` → erster Eintrag aus `get_collections()`

---

## Schritt 4: Frontend erweitern (optional)

Wenn der Use Case im Frontend erscheinen soll, die Use-Case-Liste in
`services/frontend/src/lib/components/Sidebar.svelte` erweitern:

```svelte
const useCases = [
    // ... bestehende Use Cases ...
    {
        id: 'stadtwerke',
        label: 'Stadtwerke',
        desc: 'Wasser/Abwasser',
        color: 'bg-cyan-600'
    },
];
```

Falls der Use Case rollenbasierte Prompts hat, das Role-Dropdown
ebenfalls fuer den neuen Use Case aktivieren (analog zu `wiener_linien`).

---

## Schritt 5: Agent-Actions pruefen

### Standard-Actions (in allen Use Cases verfuegbar)

| Action | Beschreibung |
|--------|--------------|
| `SEARCH` | Embedding + VectorDB-Suche + Reranking |
| `RECALL_MEMORY` | Session-Gedaechtnis lesen |
| `LOOKUP_SOURCES` | Verfuegbare Collections auflisten |
| `FINAL_ANSWER` | Antwort zurueckgeben (beendet den Loop) |

### Spezial-Actions (nur bestimmte Use Cases)

| Action | Use Case | Beschreibung |
|--------|----------|--------------|
| `SEARCH_CNC` | gw_stpoelten | Cross-Collection-Suche mit CNC-Daten |
| `CLARIFY` | wiener_linien | Rueckfrage an den Benutzer stellen |

### Neue Action erstellen

Wenn Ihr Use Case eine eigene Action braucht:

1. Handler in `services/evaluation/core/actions.py` erstellen:

```python
async def action_search_norm(args: dict) -> str:
    """SEARCH_NORM – suche nach DIN/DVGW-Normen."""
    norm_id = args.get("norm_id", "")
    # ... Implementierung ...
    return f"Gefunden: {norm_id} ..."
```

2. In `ACTION_HANDLERS` registrieren:

```python
ACTION_HANDLERS["SEARCH_NORM"] = "action_search_norm"
```

3. In `get_agent_actions()` des Plugins und in `_USE_CASE_META`
   hinzufuegen.

---

## Schritt 6: Collection-Prefixes fuer LOOKUP_SOURCES

Die Action `LOOKUP_SOURCES` filtert Collections nach Use-Case-Prefix.
In `services/evaluation/core/actions.py` den Prefix ergaenzen:

```python
def _use_case_prefixes(use_case: str) -> list[str]:
    prefix_map = {
        "neumann": ["neumann_"],
        "gw_stpoelten": ["gw_"],
        "wiener_linien": ["wl_"],
        "stadtwerke": ["sw_"],       # ← NEU
    }
    return prefix_map.get(use_case, [use_case + "_"])
```

---

## Schritt 7: Tests schreiben

### Plugin-Tests

Erstellen Sie `services/data_structure/tests/test_stadtwerke.py`:

```python
from plugins.stadtwerke import StadtwerkePlugin
from shared.models import ChunkMetadata


def _meta() -> ChunkMetadata:
    return ChunkMetadata(
        chunk_id="test-1",
        file_name="test.pdf",
        page=1,
        doc_type="pdf",
        use_case="stadtwerke",
        collection="sw_allgemein",
    )


def test_anlage_detection():
    plugin = StadtwerkePlugin()
    meta = plugin.enrich_metadata(
        _meta(),
        "Die Pumpe im Pumpwerk 3 zeigt erhoehten Druck.",
        {},
    )
    assert meta.extra["anlage"] == "pumpwerk"
    assert meta.collection == "sw_pumpwerk"


def test_norm_extraction():
    plugin = StadtwerkePlugin()
    meta = plugin.enrich_metadata(
        _meta(),
        "Gemaess DIN EN 1610 muss die Dichtheit geprueft werden.",
        {},
    )
    assert meta.extra["norm_ref"] == "DIN EN 1610"


def test_collections():
    plugin = StadtwerkePlugin()
    cols = plugin.get_collections()
    assert "sw_pumpwerk" in cols
    assert len(cols) == 4
```

### Use-Case-Tests

Ergaenzen Sie in `services/evaluation/tests/test_use_cases.py`:

```python
def test_stadtwerke_system_prompt():
    prompt = get_system_prompt("stadtwerke")
    assert "Wartungsassistent" in prompt

def test_stadtwerke_actions():
    actions = get_agent_actions("stadtwerke")
    assert "SEARCH" in actions
    assert "FINAL_ANSWER" in actions

def test_stadtwerke_default_collection():
    assert get_default_collection("stadtwerke") == "sw_pumpwerk"
```

**Tests ausfuehren:**

```bash
cd services/data_structure && python -m pytest tests/ -v
cd services/evaluation && python -m pytest tests/ -v
```

---

## Schritt 8: Deployment

Nur zwei Services muessen neu gebaut werden:

```bash
# Server A – Data Structure Service neu bauen
docker compose -f docker-compose.core.yml build data_structure
docker compose -f docker-compose.core.yml up -d data_structure

# Server B – Evaluation Service neu bauen
docker compose -f docker-compose.inference.yml build evaluation
docker compose -f docker-compose.inference.yml up -d evaluation

# Server C – Frontend neu bauen (wenn Sidebar geaendert)
docker compose -f docker-compose.frontend.yml build frontend
docker compose -f docker-compose.frontend.yml up -d frontend
```

---

## Checkliste

- [ ] Plugin-Datei erstellt (`plugins/mein_usecase.py`)
- [ ] `use_case_id` gesetzt
- [ ] `enrich_metadata()` implementiert
- [ ] `get_collections()` definiert
- [ ] `get_system_prompt()` geschrieben
- [ ] `get_agent_actions()` geprueft
- [ ] Plugin in `plugins/__init__.py` registriert
- [ ] Use Case in `evaluation/core/use_cases.py` eingetragen
- [ ] Collection-Prefix in `actions.py::_use_case_prefixes()` ergaenzt
- [ ] Frontend `Sidebar.svelte` erweitert (optional)
- [ ] Plugin-Tests geschrieben und gruen
- [ ] Use-Case-Tests geschrieben und gruen
- [ ] Betroffene Services neu gebaut und deployt

---

## Referenz: Bestehende Use Cases

| Use Case | ID | Collections | Spezial-Actions | Rollen |
|----------|-------------|-------------|-----------------|--------|
| Firma Neumann | `neumann` | `neumann_machines` | – | default |
| GW St. Poelten | `gw_stpoelten` | `gw_cnc_steps`, `gw_ruest_data`, `gw_material_info` | `SEARCH_CNC` | default |
| Wiener Linien | `wiener_linien` | `wl_fahrzeug`, `wl_strecke`, `wl_betrieb`, `wl_recht`, `wl_pruefung` | `CLARIFY` | default, trainee |
| Beispiel | `beispiel` | `beispiel_docs` | – | default |
