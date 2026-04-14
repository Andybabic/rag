# Cleaning Service – Entwickler-Leitfaden

## Aufgabe des Service

Der Cleaning Service extrahiert Text aus hochgeladenen Dokumenten (PDF, DOCX,
CSV, TXT, MD) und liefert sauberen Markdown zurueck. Optional werden PII-Daten
entfernt. Er ist der erste Schritt in der Ingestion-Pipeline.

---

## Verzeichnisstruktur

```
services/cleaning/
├── main.py              # FastAPI-App, Middleware, Health-Endpoint
├── config.py            # Pydantic-Settings (Env-Variablen)
├── models.py            # Request-/Response-Modelle
├── router/
│   └── v1.py            # Endpunkte: /v1/clean, /v1/clean/batch, /v1/formats
├── core/
│   ├── cleaner.py       # Zentrale clean()-Funktion (delegiert an Handler)
│   ├── handlers.py      # BaseHandler + konkrete Handler pro Dateityp
│   ├── batch.py         # Parallele Verarbeitung mehrerer Dateien
│   └── pii.py           # Regex-basierte PII-Entfernung
└── tests/
    └── ...              # Pytest-Tests fuer Handler, Endpunkte, PII
```

---

## Neuen Dateityp unterstuetzen

### 1. Handler implementieren

In `core/handlers.py` eine neue Klasse erstellen, die `BaseHandler` erbt:

```python
class XlsxHandler(BaseHandler):
    """Handler fuer Excel-Dateien."""

    extensions = {".xlsx", ".xls"}

    async def parse(
        self, file_bytes: bytes, filename: str
    ) -> list[dict]:
        # file_bytes enthaelt den rohen Dateiinhalt.
        # Rueckgabe: Liste von Seiten-Dicts mit {"page": int, "text": str}
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(file_bytes))
        pages = []
        for i, sheet in enumerate(wb.worksheets, 1):
            rows = []
            for row in sheet.iter_rows(values_only=True):
                rows.append(" | ".join(str(c or "") for c in row))
            pages.append({"page": i, "text": "\n".join(rows)})
        return pages
```

**Regeln:**
- `extensions` muss ein Set aus Dateiendungen mit Punkt sein.
- `parse()` ist `async` und gibt eine Liste von Seiten-Dicts zurueck.
- Jedes Dict braucht `"page"` (int) und `"text"` (str).

### 2. Handler registrieren

Am Ende von `core/handlers.py` den neuen Handler in `HANDLER_REGISTRY`
eintragen:

```python
HANDLER_REGISTRY[".xlsx"] = XlsxHandler()
HANDLER_REGISTRY[".xls"] = XlsxHandler()
```

Danach wird der Handler automatisch von `cleaner.py` und dem
`/v1/formats`-Endpunkt erkannt.

### 3. Abhaengigkeiten ergaenzen

Falls ein neues Package noetig ist (z.B. `openpyxl`), in
`services/cleaning/requirements.txt` ergaenzen.

### 4. Tests schreiben

In `tests/` einen neuen Test erstellen:

```python
import pytest
from core.handlers import XlsxHandler

@pytest.mark.anyio
async def test_xlsx_handler():
    handler = XlsxHandler()
    # Test-Datei als bytes erzeugen oder aus fixtures laden
    pages = await handler.parse(xlsx_bytes, "test.xlsx")
    assert len(pages) >= 1
    assert "page" in pages[0]
    assert "text" in pages[0]
```

---

## PII-Muster erweitern

In `core/pii.py` sind die Regex-Muster definiert:

```python
_PATTERNS = {
    "EMAIL": re.compile(r"..."),
    "PHONE": re.compile(r"..."),
    "IP": re.compile(r"..."),
    # Neues Muster ergaenzen:
    "IBAN": re.compile(r"\b[A-Z]{2}\d{2}\s?\d{4}\s?\d{4}\s?\d{4}\s?\d{4}\s?\d{0,2}\b"),
}
```

Jeder Treffer wird durch `[IBAN_REMOVED]` ersetzt. Der Platzhalter-Name leitet
sich automatisch vom Dict-Key ab.

---

## Batch-Verarbeitung anpassen

In `core/batch.py` steuern zwei Config-Werte die Parallelitaet:

- `BATCH_PARSE_WORKERS` – Max. gleichzeitige Parse-Vorgaenge (Semaphore).
- `LOG_DIR` – Verzeichnis fuer `batch_errors.jsonl`.

Um die Fehlerbehandlung zu aendern (z.B. Retry-Logik), die Funktion
`process_batch()` erweitern. Aktuell wird jeder Fehler isoliert – ein
fehlgeschlagener Parse stoppt nicht die restlichen Dateien.

---

## MinerU-Integration (PDF)

Der `PDFHandler` unterstuetzt zwei Modi:

1. **MinerU** (primary) – Externer Dienst fuer Layout-Erkennung.
   Konfiguriert ueber `USE_MINERU=true` und `MINERU_API_URL`.
2. **PyMuPDF** (fallback) – Lokale Textextraktion mit `fitz`.

Wenn MinerU nach `MAX_RETRIES` Versuchen fehlschlaegt, wird automatisch auf
PyMuPDF gewechselt. Um das Fallback-Verhalten zu aendern, in
`core/handlers.py` die Methode `PDFHandler.parse()` anpassen.

---

## Endpunkt hinzufuegen

### 1. Modell definieren

In `models.py` Request- und Response-Modelle erstellen:

```python
class MyRequest(BaseModel):
    param: str

class MyResponse(BaseModel):
    result: str
    request_id: str = ""
```

### 2. Route hinzufuegen

In `router/v1.py`:

```python
@router.post("/v1/my-endpoint", response_model=MyResponse)
async def my_endpoint(body: MyRequest, request: Request):
    request_id = getattr(request.state, "request_id", "")
    # Logik aufrufen
    result = await my_function(body.param)
    return MyResponse(result=result, request_id=request_id)
```

**Wichtig:**
- Immer `request_id` aus `request.state` holen und zurueckgeben.
- Fehler werden automatisch vom globalen Exception-Handler behandelt.
- Fuer `ValueError` → 400, `FileNotFoundError` → 404, sonst → 500.

---

## Tests ausfuehren

```bash
cd services/cleaning
python -m pytest tests/ -v
```

Alle Tests nutzen `httpx.AsyncClient` mit `httpx.ASGITransport`, sodass kein
laufender Server noetig ist.

---

## Konfiguration (Env-Variablen)

| Variable | Default | Beschreibung |
|----------|---------|--------------|
| `USE_MINERU` | `false` | MinerU fuer PDF-Parsing verwenden |
| `MINERU_API_URL` | `http://mineru:8000` | MinerU-Service-URL |
| `MINERU_PAGE_BY_PAGE` | `false` | PDFs seitenweise an MinerU senden |
| `BATCH_PARSE_WORKERS` | `4` | Max. parallele Parse-Vorgaenge |
| `MAX_RETRIES` | `3` | Max. MinerU-Wiederholungsversuche |
| `RETRY_BACKOFF_BASE` | `2` | Backoff-Faktor (Sekunden) |
| `LOG_LEVEL` | `INFO` | Log-Level |
| `LOG_DIR` | `logs` | Verzeichnis fuer Fehler-Logs |
