# Kermit — Die RAG-Pipeline

> **Was ist das?** Kermit ist eine modulare **Retrieval-Augmented-Generation-Plattform (RAG)**:
> Ein System, das beliebige Firmendokumente (PDFs, Word, CSV, CNC-Programme …) einliest,
> durchsuchbar macht und über mehrere KI-Agenten **belegte** Antworten auf Fachfragen liefert.
> Jede Aussage in einer Antwort verweist zurück auf die Quelle — keine erfundenen Fakten.

Dieses Dokument ist als **Grundlage für eine Präsentation** gedacht. Es erklärt:
1. Aus welchen Bausteinen die Pipeline besteht
2. Wie Daten reinkommen (Ingestion) — inkl. CNC-Sonderfall
3. Wie eine Frage beantwortet wird (Query / Agenten-Prozess)
4. Wie der **State** an jedem Schritt aussieht
5. Woher das **Ergebnis** kommt und wie es aufgebaut ist
6. Die einzelnen **Use-Cases** und ihre Besonderheiten

---

## 1. Das große Bild

Kermit besteht aus **7 unabhängigen Microservices**, die über HTTP miteinander reden.
Man kann sich das wie ein Fließband vorstellen: Jeder Service macht genau eine Sache gut.

| Service | Port | Aufgabe (in einem Satz) |
|---|---|---|
| **cleaning** | 8001 | Rohdatei → sauberer Text/Markdown (PDF, DOCX, CSV, CNC) |
| **data_structure** | 8002 | Text → sinnvolle Häppchen ("Chunks") mit Metadaten |
| **embedding** | 8003 | Text-Chunk → Zahlenvektor (für semantische Suche) |
| **vectordb** | 8004 | Speichert & durchsucht Vektoren (Qdrant) |
| **evaluation** | 8005 | **Das Gehirn**: Agenten, Suche, Reranking, Antwort + Quellen |
| **frontend** | 3000 | Chat-Oberfläche, Upload, Admin (SvelteKit) |
| **postgres** | 5432 | Speichert Verlauf, Konfiguration, Prompts, Memory |

Dazu extern: **Qdrant** (Vektor-Datenbank), **Ollama / OpenAI** (LLM + Embeddings),
optional **MinerU** (OCR für gescannte PDFs).

> **Kernidee "Use-Case-Isolation":** Jeder Kunde/Anwendungsfall (`gw_stpoelten`,
> `neumann`, `wiener_linien`, `ustp` …) hat eigene Datensammlungen mit eigenem
> Präfix. Ein Agent für St. Pölten kann **niemals** in den Daten von Wiener Linien
> suchen. Das ist hart im Code verdrahtet.

```
                              ┌─────────────────────────────┐
                              │   FRONTEND (Chat + Upload)  │
                              └───────┬─────────────┬───────┘
                  Ingestion-Pfad ─────┘             └───── Frage-Pfad
                          │                                  │
   ┌──────────┬──────────┼──────────┬───────────┐           │
   ▼          ▼          ▼          ▼            ▼           ▼
cleaning → data_struct → embedding → vectordb         evaluation (Agenten)
  8001        8002         8003       8004  ◄───── sucht ─── 8005
   │            │            │          │                     │
 Text      Chunks       Vektoren    Qdrant ───────────────► Antwort + Quellen
```

---

## 2. Ingestion — Wie Daten in die Pipeline kommen

Es gibt **zwei Wege**, abhängig davon, was hochgeladen wird:

### 2A. Standard-Ingestion (einzelne Datei: PDF, DOCX, CSV, TXT)

Ausgelöst im Frontend über **Upload-Button** → `POST /api/ingest`.

| Schritt | Service | Eingang (State) | Ausgang (State) |
|---|---|---|---|
| **1. Reinigen** | cleaning `/v1/clean` | Datei-Bytes | `ParsedDocument`: Volltext + Seiten + Bilder + Metadaten |
| **2. Chunking** | data_structure `/v1/structure` | Markdown + Metadaten | Liste von `Chunk` (je ~256 Token, mit Breadcrumb & Seite) |
| **3. Embedding** | embedding `/v1/embed/batch` | Text-Liste | Vektoren (z. B. 384-dim, Modellname gestempelt) |
| **4. Speichern** | vectordb `/v1/upsert` | Vektor + Metadaten | In Qdrant-Collection abgelegt |
| **5. Protokoll** | evaluation `/v1/ingest-log` | Dateiname, Hash, Anzahl | Eintrag in Postgres (nicht-blockierend) |

**State nach Schritt 1 (`ParsedDocument`):**
```json
{
  "text": "## Wartung\n### Fehlerdiagnose\nPrüfe zuerst...",
  "pages": [{"page": 1, "text": "..."}],
  "images": [{"id": "img1", "base64": "...", "alt_text": "...", "context": "..."}],
  "metadata": {
    "format": "pdf",
    "file_hash": "ab12...",
    "stored_path": "/data/originale/handbuch.pdf",
    "total_pages": 42,
    "parser": "mineru"     // oder "pymupdf" als Fallback
  }
}
```

**State nach Schritt 2 (ein `Chunk`):**
```json
{
  "id": "chunk-uuid",
  "text": "[handbuch.pdf | Wartung > Fehlerdiagnose]\n\nPrüfe zuerst die Stromversorgung...",
  "metadata": {
    "file_name": "handbuch.pdf",
    "page": 7,
    "doc_type": "pdf",
    "use_case": "neumann",
    "collection": "neumann_machines",
    "extra": { /* use-case-spezifische Felder vom Plugin */ }
  }
}
```

> **Warum der "Breadcrumb" (`Wartung > Fehlerdiagnose`)?** Damit ein einzelnes Häppchen
> seinen Kontext behält. Das Embedding "weiß" dann, dass dieser Text zur Fehlerdiagnose
> gehört — die Suche wird präziser.

**Wichtige Qualitäts-Sicherungen in der Ingestion:**
- **Modell-Stempel:** Jeder Vektor trägt den Namen des Embedding-Modells. Wird später mit
  einem anderen Modell gesucht, wird das erkannt und abgelehnt (sonst kollabiert die
  Ähnlichkeit auf ~0 und die Suche liefert Müll).
- **Boilerplate-Filter:** Inhaltsverzeichnisse, Kopfzeilen, Seiten mit zu wenig echtem
  Text werden aussortiert.
- **OCR ehrlich:** Gescannte PDFs ohne Textebene werden mit klarer Fehlermeldung abgelehnt,
  statt einen leeren, nicht durchsuchbaren Index zu bauen.

---

### 2B. Ordner-/CNC-Ingestion (ZIP mit ganzem Produkt-Ordner) — *Use-Case GW St. Pölten*

Das ist der spannende Spezialfall. Hier wird **kein Fließtext**, sondern ein ganzer
**Produkt-Ordner als ZIP** hochgeladen → `POST /api/ingest/folder` →
data_structure `/v1/structure/folder`.

Ein solcher Ordner sieht typisch so aus:
```
11221.7126/
├── 112217126            ← CNC-Programm (Sinumerik/Siemens MPF, oft ohne Endung)
├── Stückliste.csv       ← Material-Info (Bill of Materials)
├── Einstellblatt 3.pdf  ← Rüstblatt (wird separat als PDF behandelt)
├── 7126_1.jpg           ← Referenzbild
└── 112217126_VORLAGE    ← Vorlage → wird übersprungen
```

**Schritt für Schritt, was hier passiert:**

1. **Gruppieren** (`folder_ingest.py`): ZIP entpacken, jede Datei klassifizieren
   (CNC / Stückliste / Einstellblatt / Bild / überspringen) und nach **Produkt-ID**
   bündeln. Die Produkt-ID (`11221.7126`) ist der gemeinsame Schlüssel.
   - Vorlagen (`*VORLAGE*`), CAM-Arbeitsverzeichnisse (`Hypermill/`) und Temp-Dateien
     werden übersprungen.
   - Byte-identische CNC-Kopien werden per SHA1-Hash dedupliziert.

2. **Material extrahieren** (`stueckliste.py`): Aus der CSV wird die Alu-Legierung
   per Regex gezogen, z. B. `EN AW-6005A T6`. Das wird später jedem Bearbeitungsschritt
   beigemischt → man kann nach "Werkzeug für Material X" suchen.

3. **CNC parsen** (`cnc_parser.py`) — **der Kern**:
   - Erkennt automatisch den **Dialekt**: `simple` (handgeschrieben, ein T-Aufruf pro
     Block) oder `structured` (CAM-Export mit Werkzeugliste + Operations-Index).
   - Zerlegt das Programm in **einzelne Operationen** — **eine Operation = ein Chunk**.
   - Repariert kaputte Umlaute aus dem CAM-Postprozessor (`Frdsen` → `Fräsen`,
     `FrC$sen` → `Fräsen`).
   - Klassifiziert jede Operation (Bohren / Fräsen / Taschenfräsen / …) und jedes
     Werkzeug (Bohrer / Fräser / Anbohrer / Sonderwerkzeug).

**State: eine geparste Operation → ein CNC-Chunk in der Collection `gw_cnc_steps`:**
```json
{
  "id": "uuid",
  "text": "Bearbeitungsschritt: Bohren, Durchmesser 9 mm.\nWerkzeug: VHMI-BOHRER (Bohrer), Werkzeug-Nr. T1.\nMaterial: EN AW-6005A T6.\nBauteil 11221.7126, 1.BEARBEITUNG.\nSchnittparameter: Drehzahl 10900, Vorschub 3000.",
  "metadata": {
    "doc_type": "cnc",
    "use_case": "gw_stpoelten",
    "collection": "gw_cnc_steps",
    "extra": {
      "product_id": "11221.7126",
      "operation_type": "Bohren",
      "tool_id": "T1",
      "tool_type": "VHMI-BOHRER",
      "tool_class": "Bohrer",
      "diameter": 9.0,
      "spindle_speed": 10900,
      "feed": 3000,
      "cycle": "CYCLE82",
      "material_class": "EN AW-6005A T6"
    }
  }
}
```

> **Der Clou:** Aus einem rohen Maschinen-Programm werden strukturierte, durchsuchbare
> "Erfahrungs-Bausteine". Später kann die Frage *"Welches Werkzeug für Bohren in Alu 6005?"*
> beantwortet werden, indem alle historischen Operationen aggregiert werden — und das
> System sagt sogar, **in wie vielen Bauteilen** jedes Werkzeug eingesetzt wurde.

**Antwort der Ordner-Ingestion:**
```json
{
  "status": "ok",
  "products": [{
    "product_id": "11221.7126",
    "cnc_files": 1,
    "operations": 2,
    "material_class": "EN AW-6005A T6",
    "einstellblaetter": ["Einstellblatt 3.pdf"],
    "images": 1
  }],
  "total_chunks": 3,
  "skipped_noncanonical": ["11221.7126/112217126_VORLAGE", "..."]
}
```

---

## 3. Query — Wie eine Frage beantwortet wird (der Agenten-Prozess)

Das ist das Herzstück und für die Präsentation am wichtigsten. Eine Frage durchläuft eine
**zweistufige Agenten-Architektur**: ein **Manager** plant, mehrere **Sub-Agenten** arbeiten.

```
                    Frage des Nutzers
                          │
              ┌───────────▼────────────┐
              │   MANAGER-AGENT         │   1) Frage in 1–3 Teilaufgaben zerlegen
              │   (manager.py)          │      → wählt Rolle(n) + Merge-Strategie
              └───────────┬────────────┘
                          │  Fan-Out (parallel)
        ┌─────────────────┼─────────────────┐
        ▼                 ▼                 ▼
   Sub-Agent          Sub-Agent          Sub-Agent      2) Jeder läuft einen
   Rolle "facts"      Rolle "procedure"  Rolle "context"   ReAct-Loop (s. u.)
        │                 │                 │
        └─────────────────┼─────────────────┘
                          ▼
              ┌────────────────────────┐
              │   SYNTHESE              │   3) Teil-Antworten + globaler Chunk-Pool
              │   (manager.py)          │      → zu einer Antwort verschmolzen
              └───────────┬────────────┘
                          ▼
              ┌────────────────────────┐
              │   COMPLIANCE-CHECK      │   4) Prüft: jede Aussage belegt?
              │                         │      keine Halluzination? → OK / REWRITE / REFUSE
              └───────────┬────────────┘
                          ▼
                Endantwort + Quellen + voller Trace
```

### 3.1 Schritt 1 — Manager zerlegt die Frage

Der Manager fragt das LLM: *Welche Teilfragen stecken hier drin, und welcher Spezialist
soll sie beantworten?* Er gibt drei **Rollen** zur Auswahl:

| Rolle | Spezialität | Antwortstil | Schritt-Budget |
|---|---|---|---|
| **facts** | Konkrete Werte, Zahlen, Namen, Codes | Knapp, eine Zeile pro Fakt + `[n]` | 3 |
| **procedure** | Abläufe, Schritt-für-Schritt, Workflows | Nummerierte Liste, je Schritt `[n]` | 3 |
| **context** | Hintergrund, Definitionen, Rahmen | 2–4 Sätze, je `[n]` | 3 |

**Merge-Strategien:** `complementary` (ergänzend), `comparative` (gegenüberstellend),
`fallback` (dieselbe Frage auf zwei Wegen).

**State: der Manager-Plan**
```json
{
  "rationale": "Frage will technischen Wert (facts) UND Vorgehen (procedure)",
  "merge_strategy": "complementary",
  "subtasks": [
    {"role": "facts",     "sub_query": "Betriebstemperaturbereich?", "focus": "Exakte Werte"},
    {"role": "procedure", "sub_query": "Reset-Ablauf bei Überhitzung?", "focus": "Schritte"}
  ]
}
```

### 3.2 Schritt 2 — Sub-Agent läuft den ReAct-Loop

Jeder Sub-Agent arbeitet nach dem Muster **Reason → Act → Observe** ("ReAct"):
Denken → Handeln → Beobachten, wiederholt bis eine Antwort steht (max. 3–5 Schritte).

Bei jedem Schritt produziert das LLM:
```
THOUGHT: [Begründung, was als Nächstes zu tun ist]
ACTION: SEARCH({"query": "Betriebstemperatur Maschine X"})
```

**Verfügbare Aktionen** (welche genau, hängt vom Use-Case ab):

| Aktion | Zweck |
|---|---|
| **SEARCH** | Frage einbetten, in den (use-case-eigenen!) Collections suchen, hybrid + reranken |
| **SEARCH_CNC** | *Nur GW St. Pölten*: Werkzeug-Empfehlung — Operation + Material → historische Werkzeuge, aggregiert nach Häufigkeit |
| **REFINE_QUERY** | Suche mit umformulierter (breiterer) Query wiederholen, falls leer |
| **CLARIFY** | Rückfrage stellen statt zu raten |
| **RECALL_MEMORY** | Frühere Q&A dieses Use-Cases aus dem Gedächtnis holen |
| **LOOKUP_SOURCES** | Auflisten, welche Collections dieser Use-Case hat |
| **FINAL_ANSWER** | Endgültige Teil-Antwort abgeben |

**State: ein einzelner Agenten-Schritt** (genau das, was die Oberfläche als Live-Trace zeigt):
```json
{
  "step": 1,
  "thought": "Ich brauche den exakten Temperaturwert, suche gezielt.",
  "action": "SEARCH",
  "args": {"query": "Betriebstemperatur Maschine X"},
  "observation": "[1] Betriebstemperatur 5–40 °C ... [2] ...",
  "chunks": [ /* die abgerufenen Quell-Häppchen */ ]
}
```

**Wie die SEARCH-Aktion intern arbeitet (Hybrid-Retrieval):**
1. Frage → Vektor (embedding-Service)
2. **Dense-Suche** in Qdrant (semantische Ähnlichkeit) → Top-Kandidaten
3. **BM25** (klassische Stichwort-Suche) on top → fängt seltene Eigennamen/Codes
4. **RRF-Fusion**: beide Rankings verschmelzen
5. **Cross-Encoder-Reranking** (`bge-reranker-v2-m3`): bewertet Frage+Chunk paarweise neu
6. Deduplizieren, Top 7 zurück, mit Quellen `[1] [2] …`

**Eingebaute Sicherheitsnetze:**
- **Such-Zwang:** Im ersten Schritt **muss** gesucht werden — der Agent darf nicht aus dem
  Gesprächsverlauf/Trainingswissen "antworten" (Halluzinations-Schutz).
- **"Nicht gefunden"-Erkennung:** Sagt der Agent "steht nicht in den Dokumenten", wird
  **automatisch eine breitere Suche erzwungen**, bevor aufgegeben wird.
- **Absolutes Halluzinations-Verbot:** Jede Aussage braucht einen `[n]`-Beleg aus einem
  echten Chunk. Kein "öffentliches Wissen", kein Raten.

### 3.3 Schritt 3 — Synthese

Der Manager sammelt alle Teil-Antworten und einen **globalen Chunk-Pool** (alle gefundenen
Quellen, dedupliziert und neu durchnummeriert `[1] [2] …`). Ein Synthese-LLM-Aufruf
verschmilzt das gemäß Merge-Strategie zu **einer** zusammenhängenden Antwort.

### 3.4 Schritt 4 — Compliance-Check

Ein letzter LLM-Aufruf prüft die fertige Antwort gegen die Quellen:
- Ist **jeder Fakt** belegt? Stützen die zitierten Chunks die Aussage wirklich?
- Gibt es unbelegte Behauptungen (Halluzination)?
- Werden die Use-Case-Regeln eingehalten?

**Verdikt:** `OK` (durchlassen) · `REWRITE` (Synthese mit Hinweisen wiederholen) ·
`REFUSE` (durch "kann ich nicht zuverlässig beantworten" ersetzen).

---

## 4. Das Ergebnis — woher es kommt und wie es aufgebaut ist

Das Endergebnis (`run_manager`) ist **vollständig nachvollziehbar** — die Oberfläche kann
jeden Schritt anzeigen. Aufbau:

```json
{
  "answer": "Die Betriebstemperatur beträgt 5–40 °C [1]. Bei Überhitzung: 1. Gerät ausschalten [2] ...",
  "citations": [
    {"ref": "[1]", "file_name": "handbuch.pdf", "page": 12, "excerpt": "Betriebstemperatur 5–40 °C...", "score": 0.87, "stored_path": "/data/..."},
    {"ref": "[2]", "file_name": "handbuch.pdf", "page": 30, "excerpt": "Reset: zuerst..."}
  ],
  "sufficient": true,

  "manager_plan": { /* der Plan aus 3.1 */ },
  "subagents": [
    {"role": "facts",     "sub_query": "...", "answer": "...", "chunks": [...]},
    {"role": "procedure", "sub_query": "...", "answer": "...", "chunks": [...]}
  ],
  "global_chunks": [ /* der deduplizierte Quell-Pool, [1] [2] ... */ ],
  "agent_steps": [ /* voller ReAct-Trace, s. 3.2 */ ],
  "compliance": {"verdict": "OK", "issues": [], "guidance": ""},

  "audit": {"model": "...", "llm_provider": "ollama", "temperature": 0.7, "processing_ms": 4210}
}
```

**Kernpunkte für die Präsentation:**
- **`answer`** ist die menschenlesbare Antwort mit `[n]`-Verweisen.
- **`citations`** macht jeden `[n]`-Verweis anklickbar → öffnet das echte Quelldokument
  auf der richtigen Seite. Verweise ohne echte Quelle werden vorher entfernt.
- **`agent_steps` + `subagents` + `manager_plan`** = der komplette Denkweg. Nichts ist
  Blackbox; man kann live zusehen, wie das System sucht und entscheidet.
- **`sufficient: false`** signalisiert ehrlich, wenn die Datenlage für eine sichere
  Antwort nicht reicht (statt zu raten).

**Streaming:** In der Oberfläche kommen diese Schritte **live** rein (Server-Sent Events) —
"Denke…", "Suche…", "Schritt 1…", "Endantwort". Der Nutzer sieht die Arbeit in Echtzeit.

---

## 5. Die Use-Cases im Überblick

Alle teilen dieselbe Pipeline, unterscheiden sich aber in Daten, Collections und Prompt.

| Use-Case | Domäne | Collections | Besonderheit |
|---|---|---|---|
| **gw_stpoelten** | CNC-Rüstung / Fertigung | `gw_cnc_steps`, `gw_ruest_data`, `gw_material_info` | ZIP/CNC-Ingestion + `SEARCH_CNC` für Werkzeug-Empfehlungen nach Häufigkeit |
| **neumann** | Maschinenwartung | `neumann_machines` | Fehlerdiagnose & Reparatur aus Handbüchern |
| **wiener_linien** | Verkehrsbetrieb | `wl_default` | zwei Rollen (`default`, `trainee`), z. B. Prüfungsvorbereitung, `§`-Belege |
| **ustp** | Universität / Info | `ustp_default` (`ustp_`-Präfix) | strikte Beleg-Pflicht für jede Info (Adresse, Raum, Telefon) |

> Neue Use-Cases werden per Skript `scripts/add_usecase.py` angelegt — das erzeugt
> automatisch Prompt, Registry-Eintrag, data_structure-Plugin, Collection-Isolation und
> den UI-Eintrag.

### Beispiel-Workflow je Use-Case (komprimiert)

**GW St. Pölten** — *"Welches Werkzeug zum Bohren in EN AW-6005?"*
→ Manager → Sub-Agent ruft `SEARCH_CNC(operation="Bohren", material="EN AW-6005")`
→ aggregiert alle historischen Bohr-Operationen in diesem Material
→ Antwort: *"VHMI-BOHRER T1 (in 7 Bauteilen), Alternativ GUE VHMI D4.9 (in 3) … [1][2]"*.

**Neumann** — *"Wie setze ich Maschine X nach Überhitzung zurück?"*
→ Manager erkennt eine `procedure`-Teilfrage → Sub-Agent sucht nach "Reset/Ablauf"
→ nummerierte Schritte, jeder mit Seitenbeleg.

**Wiener Linien** — *"Was gilt bei Signalstörung?"*
→ Rolle `default`/`trainee` → Suche in Regelwerk → Antwort mit `§`-Verweisen.

---

## 6. Glossar (für Nicht-Techniker in der Präsentation)

- **RAG (Retrieval-Augmented Generation):** Das LLM erfindet nicht, sondern bekommt erst
  echte Dokument-Ausschnitte gesucht und antwortet **nur** daraus.
- **Chunk:** Ein sinnvolles Text-Häppchen eines Dokuments (mit Quelle, Seite, Kontext).
- **Embedding / Vektor:** Eine Zahlenrepräsentation von Text, mit der man "Bedeutungs-
  Ähnlichkeit" messen kann.
- **Collection:** Ein durchsuchbarer Topf von Vektoren in der Vektor-DB (Qdrant).
- **Agent / ReAct-Loop:** Eine KI, die in Schritten *denkt → handelt (sucht) → beobachtet*,
  bis sie eine belegte Antwort hat.
- **Reranking (Cross-Encoder):** Ein zweites, genaueres Modell, das die Suchtreffer noch
  einmal nach echter Relevanz sortiert.
- **Citation / Beleg `[n]`:** Verweis von einer Aussage zurück auf das Quelldokument.
- **Compliance-Check:** Eine Endkontrolle, die prüft, ob die Antwort wirklich belegt und
  halluzinationsfrei ist.

---

## 7. Die wichtigsten Verkaufsargumente (für die Folie "Was kann es?")

1. **Belegt statt erfunden** — jede Aussage zeigt die Quelle; harter Halluzinations-Schutz.
2. **Versteht Fachformate** — nicht nur PDFs, sondern auch **CNC-Programme** werden in
   strukturiertes Erfahrungswissen verwandelt.
3. **Mehrere Spezialisten parallel** — Manager + Rollen-Agenten zerlegen komplexe Fragen.
4. **Volle Nachvollziehbarkeit** — jeder Denk- und Suchschritt ist live sichtbar und
   gespeichert (Audit).
5. **Mandantenfähig & erweiterbar** — strikte Daten-Isolation pro Use-Case; neuer Use-Case
   per Skript in Minuten.
6. **Hybride Suche + Reranking** — findet sowohl semantisch als auch über exakte Codes/
   Eigennamen, dann nochmal nach Relevanz sortiert.
```
