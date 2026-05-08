# Frontend Service – Entwickler-Leitfaden

## Aufgabe des Service

Das Frontend ist eine SvelteKit-Anwendung mit Tailwind CSS. Es bietet die
Chat-Oberflaeche, Datei-Upload und Use-Case-Auswahl. Server-seitige
Route-Handler proxyen Anfragen an die Backend-Services und orchestrieren
die Ingestion-Pipeline.

---

## Verzeichnisstruktur

```
services/frontend/
├── package.json             # Abhaengigkeiten (Svelte 5, SvelteKit, Tailwind v4)
├── svelte.config.js         # SvelteKit-Konfiguration (adapter-node, runes)
├── vite.config.ts           # Vite-Build-Konfiguration
├── tsconfig.json            # TypeScript-Konfiguration
├── src/
│   ├── app.d.ts             # SvelteKit-Typdefinitionen
│   ├── lib/
│   │   ├── state.svelte.ts  # Globaler App-State ($state rune)
│   │   ├── api.ts           # Client-seitige API-Funktionen
│   │   ├── server/
│   │   │   └── services.ts  # Backend-Service-URLs (env-basiert)
│   │   └── components/
│   │       ├── Sidebar.svelte
│   │       ├── Chat.svelte
│   │       ├── ChatMessage.svelte
│   │       └── FileUpload.svelte
│   └── routes/
│       ├── +page.svelte     # Hauptseite (Sidebar + Chat)
│       ├── +page.ts         # SSR deaktiviert (export const ssr = false)
│       ├── +layout.svelte   # Root-Layout
│       ├── layout.css       # Tailwind-Imports
│       ├── health/
│       │   └── +server.ts   # Health-Endpunkt
│       └── api/
│           ├── query/+server.ts       # → Evaluation /v1/agent/query
│           ├── feedback/+server.ts    # → Evaluation /v1/log
│           ├── collections/+server.ts # → VectorDB /v1/collections
│           └── ingest/+server.ts      # Pipeline: Clean → Structure → Embed → Upsert
└── tests/
```

---

## Technologie-Stack

- **Svelte 5** mit Runes (`$state`, `$props`, `$derived`)
- **SvelteKit** mit `adapter-node` fuer Produktion
- **Tailwind CSS v4** mit `@tailwindcss/forms` und `@tailwindcss/typography`
- **TypeScript**

---

## Neue Komponente erstellen

### 1. Datei anlegen

```
src/lib/components/MeineKomponente.svelte
```

### 2. Svelte-5-Syntax verwenden

```svelte
<script lang="ts">
  // Props via $props() rune (NICHT export let)
  let { title, count = 0 }: { title: string; count?: number } = $props();

  // Lokaler State via $state rune
  let isOpen = $state(false);

  // Abgeleiteter Wert via $derived rune
  let label = $derived(isOpen ? 'Schliessen' : 'Oeffnen');

  function toggle() {
    isOpen = !isOpen;
  }
</script>

<div class="p-4 bg-white rounded-lg shadow">
  <h3 class="text-lg font-semibold">{title}</h3>
  <button onclick={toggle} class="mt-2 px-3 py-1 bg-blue-600 text-white rounded">
    {label}
  </button>
  {#if isOpen}
    <p class="mt-2">Inhalt hier ({count})</p>
  {/if}
</div>
```

**Wichtige Svelte-5-Regeln:**
- `$props()` statt `export let` fuer Props.
- `$state()` statt reaktive Deklarationen.
- `onclick={fn}` statt `on:click={fn}`.
- `{@html content}` fuer HTML-Rendering (mit Vorsicht – XSS-Risiko).

### 3. In Seite einbinden

```svelte
<script lang="ts">
  import MeineKomponente from '$lib/components/MeineKomponente.svelte';
</script>

<MeineKomponente title="Titel" count={5} />
```

---

## Globalen State erweitern

Der App-State liegt in `src/lib/state.svelte.ts`:

```typescript
export const app = $state({
  useCase: 'neumann' as string,
  role: 'default' as string,
  sessionId: '' as string,
  messages: [] as Message[],
  isLoading: false,
  uploadStatus: null as { ok: boolean; msg: string } | null,
});
```

Um ein neues Feld hinzuzufuegen:

1. Property im `app`-Objekt ergaenzen.
2. Falls noetig, den `Message`-Typ erweitern.
3. In Komponenten direkt ueber `app.meinFeld` zugreifen (reaktiv dank `$state`).

---

## Neuen API-Endpunkt (Server Route) erstellen

Server Routes proxyen Anfragen an die Backend-Services. Sie laufen
serverseitig in Node.js und haben Zugriff auf Umgebungsvariablen.

### 1. Route-Datei anlegen

```
src/routes/api/mein-endpunkt/+server.ts
```

### 2. Handler implementieren

```typescript
import { json } from '@sveltejs/kit';
import type { RequestHandler } from './$types';
import { SERVICES } from '$lib/server/services';

export const POST: RequestHandler = async ({ request }) => {
  const body = await request.json();

  const resp = await fetch(`${SERVICES.evaluation}/v1/my-endpoint`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-Request-ID': request.headers.get('X-Request-ID') ?? crypto.randomUUID(),
    },
    body: JSON.stringify(body),
  });

  const data = await resp.json();
  return json(data, { status: resp.status });
};
```

**Regeln:**
- Immer `X-Request-ID` weiterleiten fuer Tracing.
- Service-URLs aus `$lib/server/services.ts` verwenden (nicht hardcoden).
- Fehler-Status vom Backend durchreichen.

### 3. Client-Funktion erstellen

In `src/lib/api.ts`:

```typescript
export async function myFunction(param: string): Promise<MyResponse> {
  const resp = await fetch('/api/mein-endpunkt', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ param }),
  });
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  return resp.json();
}
```

---

## Use Case im Frontend hinzufuegen

In `src/lib/components/Sidebar.svelte` die Use-Case-Liste erweitern:

```typescript
const useCases = [
  // ... bestehende ...
  {
    id: 'mein_usecase',
    label: 'Mein Use Case',
    desc: 'Kurzbeschreibung',
    color: 'bg-purple-600',
  },
];
```

Falls der Use Case rollenbasierte Prompts hat, das Role-Dropdown ebenfalls
fuer den neuen Use Case aktivieren (analog zu `wiener_linien`).

---

## Ingestion-Pipeline verstehen

Der Endpunkt `POST /api/ingest` orchestriert die gesamte Pipeline:

```
1. File → Cleaning Service (/v1/clean)
   ↓ Markdown + Pages
2. Markdown → Data Structure Service (/v1/structure)
   ↓ Chunks (gruppiert nach Collection)
3. Chunks → Embedding Service (/v1/embed/batch) [pro Collection]
   ↓ Vektoren
4. Vektoren → VectorDB Service (/v1/upsert) [pro Collection]
```

Chunks werden nach `collection`-Feld gruppiert, sodass jede Collection
ihren eigenen Embed- und Upsert-Aufruf bekommt.

---

## Styling mit Tailwind CSS v4

Die Tailwind-Konfiguration liegt in `src/routes/layout.css`:

```css
@import 'tailwindcss';
@plugin '@tailwindcss/forms';
@plugin '@tailwindcss/typography';
```

- Tailwind v4 nutzt `@plugin` statt `plugins: []` in einer Config-Datei.
- Utility-Klassen direkt im HTML verwenden.
- Fuer eigene Styles: Standard-CSS in Svelte `<style>`-Bloecken.

---

## Entwicklungsserver starten

```bash
cd services/frontend
npm install
npm run dev
```

Der Dev-Server laeuft auf `http://localhost:5173` mit Hot-Reload.

**Wichtig:** Die Backend-Services muessen erreichbar sein. Env-Variablen
in `.env` setzen:

```
EVALUATION_SERVICE_URL=http://localhost:8005
CLEANING_SERVICE_URL=http://localhost:8001
DATA_STRUCTURE_SERVICE_URL=http://localhost:8002
EMBEDDING_SERVICE_URL=http://localhost:8003
VECTORDB_SERVICE_URL=http://localhost:8004
```

---

## Build und Produktion

```bash
npm run build       # Erzeugt build/ Verzeichnis
node build/index.js # Startet den Produktions-Server auf Port 3000
```

Im Docker-Container (`Dockerfile.frontend`):
- Multi-Stage-Build: `npm run build` → `node build/index.js`.
- adapter-node erzeugt einen eigenstaendigen Node.js-Server.

---

## Type-Checking und Linting

```bash
npm run check       # svelte-check (TypeScript + Svelte)
npm run lint        # ESLint
```

---

## Konfiguration (Env-Variablen)

| Variable | Default | Beschreibung |
|----------|---------|--------------|
| `PORT` | `3000` | Server-Port |
| `EVALUATION_SERVICE_URL` | `http://evaluation:8005` | Evaluation Service |
| `CLEANING_SERVICE_URL` | `http://cleaning:8001` | Cleaning Service |
| `DATA_STRUCTURE_SERVICE_URL` | `http://data_structure:8002` | Data Structure Service |
| `EMBEDDING_SERVICE_URL` | `http://embedding:8003` | Embedding Service |
| `VECTORDB_SERVICE_URL` | `http://vectordb:8004` | VectorDB Service |
