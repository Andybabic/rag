# Use Case übertragen (Export / Import)

Ein kompletter Use Case lässt sich als eine ZIP-Datei exportieren und auf einer
anderen Instanz — oder auf derselben unter neuem Namen — wieder importieren.

**Dashboard → Verwaltung → Use Cases**: pro Use Case **Exportieren**, oben
**Use Case importieren**.

---

## Was übertragen wird

| Bereich | Inhalt |
|---|---|
| Use Case | Label, Slug, Rollen, Aktionen, Default-Collection, Präfixe |
| Prompts | alle Prompts des Use Case (`usecase_prompts`) |
| Konfiguration | Provider, Modelle, Parameter — **ohne API-Keys** |
| Skills | Texte, Code-Datei, Funktionen inkl. Rückfrage-Override — **Variablen nur mit Namen, ohne Werte** |
| Wissensbestand | Dokumentliste, alle Collections des Use Case **mit Vektoren**, Agent-Gedächtnis (`mem_<id>`) |
| Dateien | Originale, extrahierte Bilder, Bild-Sidecars |
| Verlauf | Anfragen/Chats, Feedback, Evaluation-Runs, Skill-Audit-Log |
| Benutzer | Zuordnungen per Benutzername (nur zu Benutzern, die auf dem Ziel existieren) |

Nicht übertragen: Benutzerkonten selbst, API-Tokens und API-Jobs, globale
Prompts (`use_case = '*'`).

## Import-Ablauf

1. ZIP auswählen → wird in 16-MB-Teilen hochgeladen (unter `BODY_SIZE_LIMIT`).
2. Die Instanz prüft das Archiv und zeigt Inhalt, Konflikte und Hinweise.
3. **Existiert die ID bereits, muss ein neuer Name vergeben werden.** Dann wird
   alles mit umbenannt: `use_case` in allen Zeilen, Collections (`<neu>_…`,
   `mem_<neu>`), Präfix (`<neu>_`), Dateiablage und `stored_path`, sowie
   Verweise in Vektor-Payloads und JSON-Spalten (Zitate, Bilder, Agent-Schritte).
4. Import läuft als Hintergrund-Job; Fortschritt im Dialog.

Alle Zeilen-IDs (Anfragen, Sessions, Skills, …) werden bei jedem Import neu
vergeben — ein Klon kollidiert nie mit seinem Original.

### Was den Import verhindert

Alles wird **vor** dem ersten Schreibzugriff geprüft:

* ID bereits vergeben oder ungültig
* eine Ziel-Collection existiert schon
* **Präfix-Überschneidung.** Die Suche ist per Collection-Präfix isoliert.
  `neumann_kopie` als Klon von `neumann` ergäbe `neumann_kopie_machines` — das
  passt auf den Präfix `neumann_` des Originals, das Original würde die Daten
  des Klons mitdurchsuchen. Deshalb abgelehnt; `neumann2` geht.
* Dateiablage des Ziel-Namens nicht leer (Reste eines gelöschten Use Case)
* **anderes Embedding-Modell.** Vektoren werden 1:1 kopiert, nicht neu
  berechnet. Nutzt das Ziel (Use-Case-Konfiguration aus dem Archiv, sonst
  `EMBEDDING_MODEL` der Instanz) ein anderes Modell oder eine andere Dimension,
  wären die Vektoren nicht vergleichbar → Abbruch mit Nennung beider Modelle.

Scheitert der Import unterwegs, werden angelegte Collections und Dateien wieder
entfernt; die Datenbank wird in einer einzigen Transaktion geschrieben.

### Nach dem Import

Der Dialog listet, was noch zu tun ist:

* API-Keys in der Use-Case-Konfiguration neu eintragen
* Skill-Variablen befüllen
* Benutzer, die auf dem Ziel fehlen, anlegen und zuordnen
* **Nur bei Umbenennung:** `SEARCH_CNC` und `SEARCH_TOOL` suchen in fest
  benannten Collections (`gw_cnc_steps`, `gw_ruest_data`,
  `gw_stpoelten_default`) und greifen im Klon weiter auf die Daten des
  Originals zu.

## Archivformat

```
manifest.json               Format, Version, Zähler, Collections, Ausgelassenes
db.json                     Tabelle → Zeilen (alle Spalten)
vectors/<collection>.jsonl  {"id", "v": base64(float32 LE), "payload"} je Zeile
files/<pfad>                relativ zu <FILE_STORAGE_DIR>/<use_case>/
```

Vektoren als float32 statt JSON-Zahlen: exakt (Qdrant speichert float32) und
ein Drittel so groß. Der Import schreibt nur Spalten, die das Ziel-Schema kennt.

## Betrieb

| Variable | Default | Bedeutung |
|---|---|---|
| `TRANSFER_DIR` | `/tmp/kermit-transfer` | Export-Archive und Uploads im evaluation-Container; nach 24 h gelöscht. Braucht Platz für den größten Use Case. |
| `CLEANING_SERVICE_URL` | `http://cleaning:8001` | evaluation liest/schreibt die Dateiablage über den cleaning-Service |

Datenbank: Migration **0012** macht `ingestion_log` pro Use Case eindeutig
(`UNIQUE (use_case, file_hash)` statt `UNIQUE (file_hash)`) — sonst könnte
dieselbe Datei nicht in Original und Klon liegen. `make migrate`.

### Dateien

| Pfad | Rolle |
|---|---|
| `services/evaluation/core/transfer.py` | Export, Konfliktprüfung, Umbenennung, Import, Rollback |
| `services/evaluation/router/transfer.py` | `/v1/admin/transfer/*` |
| `services/vectordb/…` `/v1/points/export`, `/v1/points/import` | Collections samt Vektoren |
| `services/cleaning/…` `/v1/transfer/{use_case}/…` | Dateiablage lesen/schreiben (pfadsicher) |
| `services/frontend/src/routes/api/admin/transfer/[...path]` | Proxy (admin-only, streamend) |
| `services/frontend/src/routes/admin/use-cases/+page.svelte` | Buttons + Import-Dialog |
| `db/alembic/versions/0012_ingestion_log_per_use_case.py` | Migration |
