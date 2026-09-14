# Öffentliche Pipeline-API

Die komplette RAG-Pipeline lässt sich nicht nur über die Chat-Oberfläche, sondern
auch per HTTP-API ansprechen. Bereitgestellt wird sie **vom Frontend** — dem
einzigen von außen erreichbaren Dienst — und intern an den evaluation-Service
durchgeschleust.

Weil ein Agentenlauf je nach Frage zehn Sekunden bis einige Minuten dauert, ist
die API **asynchron**: Man stellt eine Frage, bekommt sofort eine `request_id`
zurück und holt die Antwort ab, sobald sie fertig ist.

> **Die vollständige Endpunkt-Referenz wird aus dem Code erzeugt** und steht in
> der laufenden Instanz:
>
> - im Dashboard unter **Verwaltung → API-Dokumentation** (`/admin/api-docs`) —
>   mit Feldtabellen, Statuscodes und fertigen curl-Aufrufen
> - maschinenlesbar unter **`/api/v1/openapi.json`** (ohne Token abrufbar), z. B.
>   für Client-Generatoren
>
> Dieses Dokument erklärt Ablauf, Einrichtung und Betrieb — also das, was sich
> nicht aus dem Code ableiten lässt. Feld- und Antwortdetails werden hier
> bewusst nicht wiederholt, damit es keine zweite Fassung gibt, die veraltet.

```
POST /api/v1/chat                     → 202  { request_id, status: "queued" }
GET  /api/v1/chat/{request_id}        →      { status: queued|running|done|error }
GET  /api/v1/chat/{request_id}/result → 200  Antwort   |  409 solange unfertig
GET  /api/v1/sessions/{session_id}    →      kompletter Chat als JSON
```

---

## 1. Authentifizierung

Jede Anfrage braucht ein Bearer-Token:

```
Authorization: Bearer kmt_XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX
```

Tokens werden im Dashboard unter **Verwaltung → API-Tokens** angelegt. Beim
Anlegen wird der Schlüssel **genau einmal** angezeigt; gespeichert wird nur sein
SHA-256-Hash. Ein verlorenes Token kann daher nicht wiederhergestellt, sondern
nur ersetzt werden.

Pro Token einstellbar:

| Einstellung | Bedeutung |
|---|---|
| **Name** | Wofür das Token gedacht ist (erscheint im Anfragen-Protokoll) |
| **Use Cases** | Auf welche Use Cases das Token zugreifen darf. Nichts ausgewählt = alle |
| **Gültig für (Tage)** | Optionales Ablaufdatum |
| **Sperren** | Token sofort ungültig machen, Eintrag bleibt zur Nachvollziehbarkeit |

Ein Token, das nicht für einen Use Case freigegeben ist, bekommt `403` — und
zwar auch beim Abrufen von Ergebnissen und Transkripten, nicht nur beim Stellen
der Frage. Ein Token kann also niemals die Daten eines anderen Kunden lesen.

**Verbindungstest:**

```bash
curl -s https://kermit.example.org/api/v1/me \
  -H "Authorization: Bearer $KERMIT_TOKEN"
```

```json
{
  "token": { "id": "…", "name": "Intranet-Integration", "prefix": "kmt_a1b2c3d4" },
  "use_cases": ["gw_stpoelten"],
  "scope": "restricted",
  "expires_at": null
}
```

---

## 2. Frage stellen

```bash
curl -s -X POST https://kermit.example.org/api/v1/chat \
  -H "Authorization: Bearer $KERMIT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
        "use_case": "gw_stpoelten",
        "query": "Welches Werkzeug wird für Bohrungen in EN AW-6005A verwendet?"
      }'
```

Antwort (**HTTP 202 Accepted**):

```json
{
  "request_id": "9c1d3f2a-5b6e-4f70-8a91-2c3d4e5f6a7b",
  "status": "queued",
  "use_case": "gw_stpoelten",
  "session_id": "2f8b1c04-7d3e-4a11-9c62-88ab0d4e1f33",
  "role": "default",
  "query": "Welches Werkzeug wird für Bohrungen in EN AW-6005A verwendet?",
  "image_count": 0,
  "created_at": "2026-09-08T10:12:31.114+00:00",
  "started_at": null,
  "finished_at": null,
  "error": null
}
```

Pflichtfelder sind `use_case` und `query`; optional sind `images`,
`session_id`, `role`, `history` und `config`. Die genauen Typen, Standardwerte
und Erläuterungen stehen in der generierten Referenz unter
**Verwaltung → API-Dokumentation**.

### Mehrstufige Unterhaltung

Für eine Folgefrage einfach dieselbe `session_id` mitschicken. Der Verlauf wird
serverseitig aus den vorherigen Runden rekonstruiert — der Client muss nichts
mitschleppen außer der Session-ID:

```bash
curl -s -X POST https://kermit.example.org/api/v1/chat \
  -H "Authorization: Bearer $KERMIT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
        "use_case": "gw_stpoelten",
        "session_id": "2f8b1c04-7d3e-4a11-9c62-88ab0d4e1f33",
        "query": "Und mit welcher Drehzahl?"
      }'
```

---

## 3. Frage mit Bild

Bilder werden als **base64** im JSON mitgeschickt — entweder als Data-URI
(`data:image/png;base64,…`) oder als reines base64 ohne Präfix. Sie werden vom
Vision-Modell beschrieben, und die Beschreibung fließt in Planung, Suche und
Antwort ein.

```bash
IMG=$(base64 -i fehlerbild.jpg | tr -d '\n')

curl -s -X POST https://kermit.example.org/api/v1/chat \
  -H "Authorization: Bearer $KERMIT_TOKEN" \
  -H "Content-Type: application/json" \
  -d @- <<EOF
{
  "use_case": "neumann",
  "query": "Was zeigt dieses Display und was muss ich tun?",
  "images": ["data:image/jpeg;base64,$IMG"]
}
EOF
```

Grenzen: max. **10 Bilder** pro Anfrage. Der Gesamtbody darf `BODY_SIZE_LIMIT`
des Frontends nicht überschreiten (Standard 50 MB) — base64 vergrößert eine
Datei um rund ein Drittel.

> Bildanalyse braucht ein konfiguriertes Vision-Modell für den Use Case
> (Dashboard → Use Case → Einstellungen). Fehlt es, wird das Bild stillschweigend
> übergangen und nur die Textfrage beantwortet.

---

## 4. Zustand abfragen

```bash
curl -s https://kermit.example.org/api/v1/chat/$REQUEST_ID \
  -H "Authorization: Bearer $KERMIT_TOKEN"
```

```json
{
  "request_id": "9c1d3f2a-…",
  "status": "running",
  "use_case": "gw_stpoelten",
  "session_id": "2f8b1c04-…",
  "query": "Welches Werkzeug …",
  "image_count": 0,
  "created_at": "2026-09-08T10:12:31.114+00:00",
  "started_at": "2026-09-08T10:12:31.980+00:00",
  "finished_at": null,
  "error": null
}
```

| `status` | Bedeutung |
|---|---|
| `queued` | Angenommen, wartet auf einen freien Bearbeitungsplatz |
| `running` | Die Agenten arbeiten |
| `done` | Antwort liegt vor — `result_url` zeigt darauf |
| `error` | Der Lauf ist gescheitert, `error` enthält den Grund |

Diese Abfrage ist bewusst schlank (nie die Antwort selbst) und darf im
Sekundentakt gepollt werden. **Empfehlung:** alle 2–5 Sekunden.

---

## 5. Antwort abholen

```bash
curl -s https://kermit.example.org/api/v1/chat/$REQUEST_ID/result \
  -H "Authorization: Bearer $KERMIT_TOKEN"
```

```json
{
  "request_id": "9c1d3f2a-…",
  "status": "done",
  "use_case": "gw_stpoelten",
  "session_id": "2f8b1c04-…",
  "query": "Welches Werkzeug wird für Bohrungen in EN AW-6005A verwendet?",
  "answer": "Für Bohrungen in EN AW-6005A T6 wird der VHMI-BOHRER (T1) eingesetzt [1] …",
  "citations": [
    {
      "index": 1,
      "text": "Bearbeitungsschritt: Bohren, Durchmesser 9 mm …",
      "metadata": { "file_name": "112217126", "product_id": "11221.7126" }
    }
  ],
  "sufficient": true,
  "images_used": [],
  "searched_collections": ["gw_cnc_steps"],
  "created_at": "2026-09-08T10:12:31.114+00:00",
  "finished_at": "2026-09-08T10:13:04.552+00:00"
}
```

**Statuscodes:**

| Code | Bedeutung |
|---|---|
| `200` | Antwort liegt vor |
| `409` | Noch `queued` oder `running` — Body enthält den aktuellen Zustand |
| `422` | Der Lauf ist gescheitert, `detail` enthält den Grund |
| `404` | Unbekannte `request_id` |

Wer sich das Polling des Status-Endpunkts sparen will, kann direkt diesen
Endpunkt pollen und auf `409` mit erneutem Warten reagieren.

### Vollständiges Ergebnis inkl. Ablaufprotokoll

`GET /api/v1/chat/{request_id}/full` liefert das ungekürzte Pipeline-Ergebnis:
Zerlegungsplan des Managers, jeder Sub-Agent mit seinen Schritten, die
verwendeten Chunks und die Laufzeiten. Deutlich größer — deshalb ein eigener
Endpunkt und nicht Teil von `/result`.

---

## 6. Kompletten Chat als JSON laden

```bash
curl -s https://kermit.example.org/api/v1/sessions/$SESSION_ID \
  -H "Authorization: Bearer $KERMIT_TOKEN"
```

```json
{
  "session_id": "2f8b1c04-…",
  "use_case": "gw_stpoelten",
  "message_count": 2,
  "messages": [
    {
      "request_id": "9c1d3f2a-…",
      "role": "default",
      "query": "Welches Werkzeug …",
      "answer": "Für Bohrungen in EN AW-6005A T6 …",
      "citations": [ … ],
      "image_count": 0,
      "sufficient": true,
      "created_at": "2026-09-08T10:12:31.114+00:00",
      "feedback": null
    },
    { "request_id": "…", "query": "Und mit welcher Drehzahl?", … }
  ]
}
```

Lesbar sind nur Sessions, die **über diese API** entstanden sind, und nur
innerhalb des Use-Case-Bereichs des Tokens. Chats aus der Weboberfläche sind
über die API nicht abrufbar.

---

## 7. Vollständiges Beispiel

```bash
#!/usr/bin/env bash
set -euo pipefail

BASE="https://kermit.example.org/api/v1"
AUTH="Authorization: Bearer $KERMIT_TOKEN"

# 1. Frage stellen
REQUEST_ID=$(curl -sf -X POST "$BASE/chat" -H "$AUTH" \
  -H "Content-Type: application/json" \
  -d '{"use_case":"gw_stpoelten","query":"Welches Werkzeug für Bohren in Alu 6005?"}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["request_id"])')

echo "Request: $REQUEST_ID"

# 2. Warten, bis die Pipeline fertig ist
while true; do
  STATUS=$(curl -sf "$BASE/chat/$REQUEST_ID" -H "$AUTH" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])')
  echo "  … $STATUS"
  [ "$STATUS" = "done" ] || [ "$STATUS" = "error" ] && break
  sleep 3
done

# 3. Antwort abholen
curl -sf "$BASE/chat/$REQUEST_ID/result" -H "$AUTH" | python3 -m json.tool
```

Python-Variante:

```python
import base64, time, requests

BASE = "https://kermit.example.org/api/v1"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


def ask(use_case: str, query: str, image_path: str | None = None,
        session_id: str | None = None) -> dict:
    payload = {"use_case": use_case, "query": query}
    if session_id:
        payload["session_id"] = session_id
    if image_path:
        data = base64.b64encode(open(image_path, "rb").read()).decode()
        payload["images"] = [f"data:image/jpeg;base64,{data}"]

    started = requests.post(f"{BASE}/chat", json=payload, headers=HEADERS)
    started.raise_for_status()
    request_id = started.json()["request_id"]

    while True:
        result = requests.get(f"{BASE}/chat/{request_id}/result", headers=HEADERS)
        if result.status_code == 409:       # noch nicht fertig
            time.sleep(3)
            continue
        result.raise_for_status()           # 422 = Lauf gescheitert
        return result.json()
```

---

## 8. Fehlerformat

Alle Fehler kommen als JSON:

```json
{ "detail": "Token is not authorised for use case 'wiener_linien'" }
```

| Code | Ursache |
|---|---|
| `400` | Ungültiger Body, unbekannter Use Case, keine UUID |
| `401` | Token fehlt, ist unbekannt, gesperrt oder abgelaufen |
| `403` | Token darf diesen Use Case nicht verwenden |
| `404` | Unbekannte `request_id` oder `session_id` |
| `409` | Ergebnis abgerufen, obwohl der Lauf noch läuft |
| `422` | Der Pipeline-Lauf ist gescheitert (Grund in `detail`) |
| `502` | Die Pipeline ist nicht erreichbar |

Unbekannte, gesperrte und abgelaufene Tokens liefern absichtlich dieselbe
`401`-Meldung — die Unterscheidung würde verraten, welche Schlüssel es einmal
gab.

---

## 9. Betrieb

### Einmalig: Migration einspielen

Die API braucht die Tabellen `api_tokens` und `api_jobs` (Alembic-Revision
`0009`). Bei einer **neuen** Datenbank kommen sie aus `db/init.sql`; eine
**bestehende** Datenbank braucht einmalig:

```bash
cd db && alembic upgrade head
```

Danach den evaluation-Service neu starten. Fehlen die Tabellen, bleibt der
Rest der Plattform unberührt: Der Service startet normal, protokolliert eine
Warnung und die API-Endpunkte antworten mit `503`.

### Laufzeit

Jobs liegen in der Postgres-Tabelle `api_jobs` und überleben damit einen
Neustart: Ein Lauf, der beim Herunterfahren unterbrochen wurde, wandert zurück
in die Warteschlange statt als Fehler zu enden.

Ein Hintergrund-Worker im evaluation-Service holt sich Jobs mit
`FOR UPDATE SKIP LOCKED`, sodass mehrere Replicas denselben Job nie doppelt
ausführen.

| Umgebungsvariable | Standard | Wirkung |
|---|---|---|
| `API_JOB_CONCURRENCY` | `2` | Gleichzeitige Agentenläufe pro Prozess. Das LLM-Backend teilt sich mit dem interaktiven Chat — höher setzen heißt, dass API-Last die Oberfläche verlangsamt |
| `API_JOB_TIMEOUT_S` | `900` | Zeitlimit pro Lauf. Ohne Limit würde ein hängendes LLM einen Bearbeitungsplatz dauerhaft blockieren |
| `API_JOB_POLL_INTERVAL_S` | `5` | Wie oft der Worker nachsieht, ob eine andere Replica etwas eingestellt hat |
| `API_JOB_HISTORY_TURNS` | `10` | Wie viele frühere Runden als Verlauf nachgeladen werden, wenn der Client keinen mitschickt |
| `API_CORS_ORIGIN` | `*` | `Access-Control-Allow-Origin` für `/api/v1`. Bearer-Tokens werden vom Browser nicht automatisch mitgeschickt, deshalb ist `*` hier unkritisch |

Im Dashboard zeigt **Verwaltung → API-Tokens** unter der Token-Liste die letzten
API-Anfragen mit Status, Use Case und verwendetem Token.
