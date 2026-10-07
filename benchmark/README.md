# XRAG-Benchmark für Kermit

Misst die ganze Pipeline – Ingest, Suche, Agenten, Antwort – an
[AmazonScience/XRAG](https://huggingface.co/datasets/AmazonScience/XRAG):
Nachrichtenfragen mit Gold-Antwort und je 8 Artikeln (2 hilfreich, 6 Ablenker).

```bash
make dev                       # Stack starten
cp benchmark/.env.example benchmark/.env   # Judge eintragen (macht make benchmark sonst selbst)
make benchmark                 # alles; Ergebnis: benchmark/runs/<zeit>/summary.md
```

Kleiner Probelauf: `make benchmark BENCH_ARGS="--mono 10 --multi 5 --negative 3 --distractors 1"`.

Datenmenge (gemessen am Testset, Artikel im Index):

| Fragen (mono + multi) | alle Ablenker (Standard) | 2 Ablenker | 0 Ablenker |
|---|---|---|---|
| 10 + 5 | 119 (1.1 MB) | 56 (0.5 MB) | 26 (0.2 MB) |
| 20 + 10 | 234 (2.2 MB) | 112 (1.0 MB) | 52 (0.4 MB) |
| 50 + 25 | 555 (4.9 MB) | 268 (2.3 MB) | 123 (1.1 MB) |
| 100 + 50 | 1099 (9.7 MB) | 533 (4.6 MB) | 244 (2.0 MB) |

## Ablauf

| Phase | Was passiert |
|---|---|
| `check` | Verbindung zu Kermit (evaluation, vectordb, Frontend) und Judge-LLM testen – eine kleine JSON-Anfrage prüft URL, Key und Modell. Einzeln: `make benchmark-check` |
| `prepare` | XRAG-Split einmalig laden (nur deutsche Zeilen → `.cache/`), Stichprobe ziehen, jeden Artikel als `xrag_<hash>.md` schreiben |
| `setup` | Use Case `xrag_bench` anlegen/aktualisieren (Prompt: `prompt.md`, Aktionen nur `SEARCH`, `REFINE_QUERY`, `FINAL_ANSWER`) |
| `ingest` | Artikel über das Frontend `/api/ingest` hochladen – voller Weg cleaning → structure → embed → upsert. Bereits indexierte werden übersprungen; Fremdbestand aus einer anderen Stichprobe wird vorher entfernt |
| `query` | Jede Frage mit neuer Session an `evaluation /v1/agent/query`; Gedächtnis des Use Case wird pro Variante geleert |
| `score` | Kennzahlen ohne LLM + Judge-LLM |
| `report` | `summary.md` / `summary.json`, mit Veränderung zum vorherigen Lauf |
| `clean` | *nicht Teil von `all`*: alle Benchmark-Daten wieder aus Kermit entfernen (siehe Aufräumen) |

Einzelne Phasen: `python3 benchmark/run.py <phase> [--run benchmark/runs/<zeit>]`.
Abgebrochene Läufe setzen dort fort, wo sie aufgehört haben.

**Korpus:** Die Artikel *aller* gezogenen Fragen landen in einem gemeinsamen
Index (Standard ≈ 150 Fragen → ≈ 1100 Artikel). Die Suche muss die zwei
richtigen also unter Hunderten finden – nicht unter acht.

## Varianten

| Variante | Frage | Artikel | Zweck |
|---|---|---|---|
| `de` | deutsch | englisch | Hauptfall: sprachübergreifendes RAG |
| `en` | englisch (gleiche Fragen wie `de`) | englisch | Vergleichswert → **Sprachlücke DE ↔ EN** |
| `multi` | deutsch | deutsch + englisch | gemischtsprachiger Bestand |
| `neg` | deutsch | *nicht indexiert* | Kermit soll ablehnen statt erfinden |

## Kennzahlen

**Steht die Antwort drin?**
- *Judge-Score*: richtig = 1, teilweise = 0,5, falsch = 0 (LLM gegen Gold-Antwort)
- *Fakten-Abdeckung*: Gold-Antwort in Einzelfakten zerlegt (pro Judge-Modell gecacht), Anteil davon in der Antwort
- *Zahlen-Treffer*: Zahlen/Daten der Gold-Antwort in der Antwort, ohne LLM. „14.000“ und „14,000“ zählen gleich

**Wie viel Text zusätzlich?**
- *Länge vs. Gold*: Wörter der Antwort ÷ Wörter der Gold-Antwort (Median)
- *Token-Precision*: Anteil der Antwort-Wörter (ohne Stoppwörter), die in der Gold-Antwort vorkommen; 1 − Precision ist der Überschuss
- *Zusatzaussagen*: vom Judge gezählte Aussagen, die für die Frage nicht nötig sind

**Suche und Belege**
- *Retrieval Recall@k / Pool*: hilfreiche Artikel (`is_support > 0`) unter den Chunks, die der Agent gesehen hat
- *Zitat-Precision / -Recall*, *Zitate aus Ablenkern*: zitierte Dateien gegen die hilfreichen Artikel

**Sonst:** Antwortsprache korrekt, fälschlich abgelehnt, Compliance-Verdikte, Latenz p50/p95.
Aufschlüsselung nach Fragetyp (`multihop`, `aggregation`, `comparison`, `set`)
und Dokumentlage (`crossdoc_type`); die zehn schwächsten Fälle mit
`request_id` (vollständiger Trace im Admin unter Abfragen).

## Konfiguration (`benchmark/.env`)

| Variable | Standard | |
|---|---|---|
| `BENCH_JUDGE_PROVIDER` | `openai` | `openai` (OpenAI-kompatibel) oder `ollama` (native API `/api/chat`) |
| `BENCH_JUDGE_BASE_URL` | je Provider | leer → `https://api.openai.com/v1` bzw. `http://localhost:11434` |
| `BENCH_JUDGE_API_KEY` | – | openai: Bearer-Token · ollama: `X-API-Key` fürs Gateway |
| `BENCH_JUDGE_MODEL` | `gpt-4o-mini` | leer → nur Kennzahlen ohne LLM |
| `BENCH_JUDGE_NUM_CTX` | 32768 | nur Ollama: Kontextfenster je Request |
| `KERMIT_FRONTEND_URL` | automatisch | leer → `localhost:5173` (`make dev`), sonst `localhost:3000` (`make up`) |
| `KERMIT_USERNAME/PASSWORD` | `ADMIN_*` aus `.env` | Login für den Upload |
| `XRAG_SPLIT` | `test` | `dev` zum Tunen, damit das Testset unverbraucht bleibt |
| `BENCH_MONO` / `BENCH_MULTI` / `BENCH_NEGATIVE` | 100 / 50 / 10 | Stichprobe |
| `BENCH_DISTRACTORS` | alle (meist 6) | Ablenk-Artikel pro Frage im Index; weniger = weniger Upload, leichtere Suche |
| `BENCH_VARIANTS` | `de,en,multi,neg` | |
| `BENCH_QUERY_CONCURRENCY` | 2 | teilt sich das LLM mit dem Chat |

Nach einem Judge-Wechsel genügt `make benchmark-score` – Kermit wird nicht erneut gefragt.

## Aufräumen

Die Daten bleiben nach einem Lauf in Kermit – so überspringt der nächste Lauf
den Upload, und die Traces zu den `request_id`s im Bericht bleiben abrufbar.

```bash
make benchmark-clean                         # Benchmark-Daten entfernen
make benchmark BENCH_ARGS="--cleanup"        # oder direkt nach dem Bericht
```

Entfernt werden die Collections `xrag_bench_*` und `mem_xrag_bench`, die
Einträge der Dokumentliste, die gespeicherten Originaldateien und der Use Case.
Es bleiben das Abfrage-Log in Postgres (Tabelle `queries`) und die Läufe unter
`benchmark/runs/`.

## Hinweise

- **Lizenz:** XRAG steht unter CC BY-NC 4.0 – nur für nicht-kommerzielle
  Evaluation. Datensatz und Läufe sind gitignored und gehören nicht in
  Kunden-Deployments.
- Gold-Antworten sind teils maschinell übersetzt und nicht fehlerfrei; deshalb
  bewertet der Judge Bedeutung, nicht Wortlaut, und jede Zeile in
  `scored.jsonl` trägt seine Begründung.
- `make benchmark-test` prüft die Kennzahlen-Logik ohne laufenden Stack.
