# API-Referenz

> Wird mit der Implementierung der einzelnen Services ergänzt.

Die nach außen dokumentierte, bearer-gesicherte Pipeline-API (`/api/v1`) hat
eine eigene Referenz: [`public_api.md`](public_api.md).

## Shared Models

Siehe `shared/shared/models.py` für die gemeinsamen Datenstrukturen:

- `ChunkMetadata` – Metadaten eines Textabschnitts
- `Chunk` – Textabschnitt mit Metadaten
- `EmbeddedChunk` – Chunk mit Vektor
- `SearchResult` – Suchergebnis mit Score
- `APIError` – Einheitliches Fehlerformat
