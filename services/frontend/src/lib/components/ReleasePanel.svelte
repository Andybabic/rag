<script lang="ts">
	import { app } from '$lib/state.svelte';
	import {
		buildSnapshot,
		importSnapshot,
		snapshotToCsvString,
		snapshotPreviewChunks
	} from '$lib/release';
	import type { ChunkingStrategy } from '$lib/state.svelte';

	let showPreview = $state(false);

	const canRelease = $derived(
		!!app.parsedTable &&
			(app.pii.status === 'done' || app.pii.status === 'error') &&
			app.release.status !== 'importing'
	);

	const released = $derived(
		app.release.status === 'released' ||
			app.release.status === 'importing' ||
			app.release.status === 'imported'
	);

	function fmtTime(iso: string | null): string {
		if (!iso) return '–';
		const d = new Date(iso);
		return d.toLocaleString('de-AT', {
			year: 'numeric',
			month: '2-digit',
			day: '2-digit',
			hour: '2-digit',
			minute: '2-digit',
			second: '2-digit'
		});
	}

	async function release() {
		if (!app.parsedTable) return;
		// Snapshot is materialized exactly once and frozen on app.release.snapshot.
		// All later mutations to detections/approvals don't affect what was sent.
		const snapshot = buildSnapshot({
			table: app.parsedTable,
			mode: app.anonymizeMode,
			strategy: app.chunkingStrategy,
			detections: app.pii.detections,
			approvals: app.pii.approvals,
			placeholders: app.pii.placeholders
		});
		app.release.snapshot = snapshot;
		app.release.status = 'released';
		app.release.error = null;

		// Auto-import the released snapshot through the gate.
		app.release.status = 'importing';
		try {
			const result = await importSnapshot(snapshot, app.useCase);
			app.release.importResult = result;
			app.release.importedAt = new Date().toISOString();
			app.release.status = 'imported';
		} catch (err) {
			app.release.status = 'error';
			app.release.error =
				err instanceof Error ? err.message : 'Import fehlgeschlagen.';
		}
	}

	function downloadSnapshot() {
		if (!app.release.snapshot) return;
		const csv = snapshotToCsvString(app.release.snapshot);
		const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' });
		const url = URL.createObjectURL(blob);
		const a = document.createElement('a');
		const baseName = app.release.snapshot.fileName.replace(/\.[^.]+$/, '');
		a.href = url;
		a.download = `${baseName}.snapshot.csv`;
		a.click();
		URL.revokeObjectURL(url);
	}

	function reset() {
		app.release.status = 'idle';
		app.release.snapshot = null;
		app.release.importedAt = null;
		app.release.importResult = null;
		app.release.error = null;
	}

	function setStrategy(s: ChunkingStrategy) {
		app.chunkingStrategy = s;
	}

	const chunkPreview = $derived.by(() => {
		if (!app.parsedTable || app.chunkingStrategy !== 'per-row') return [];
		// Build a throwaway snapshot just for the preview (no releasedAt mutation
		// happens because we don't write it to app.release.snapshot).
		const snap = buildSnapshot({
			table: app.parsedTable,
			mode: app.anonymizeMode,
			strategy: 'per-row',
			detections: app.pii.detections,
			approvals: app.pii.approvals,
			placeholders: app.pii.placeholders
		});
		return snapshotPreviewChunks(snap, 2);
	});

	const strategyLabel = $derived(
		app.chunkingStrategy === 'per-row' ? 'Pro Zeile (1 Chunk pro Zeile)' : 'Ganze Datei'
	);
</script>

<section class="space-y-4 rounded-lg border border-gray-200 bg-white p-4 shadow-sm">
	<div class="flex flex-wrap items-center justify-between gap-3">
		<div>
			<h3 class="text-sm font-semibold text-gray-800">Freigabe & Import</h3>
			<p class="text-[11px] text-gray-400">
				Erst nach Klick auf <span class="font-medium">"Freigeben"</span> verlassen Daten dieses Gerät.
			</p>
		</div>

		{#if app.release.status === 'idle' || app.release.status === 'released'}
			<button
				onclick={release}
				disabled={!canRelease}
				class="rounded-lg bg-emerald-600 px-4 py-2 text-sm font-semibold text-white transition-colors hover:bg-emerald-700 disabled:cursor-not-allowed disabled:opacity-40"
			>
				Freigeben & importieren
			</button>
		{:else if app.release.status === 'importing'}
			<button
				disabled
				class="rounded-lg bg-emerald-600 px-4 py-2 text-sm font-semibold text-white opacity-70"
			>
				Importiere …
			</button>
		{:else if app.release.status === 'imported'}
			<button
				onclick={reset}
				class="rounded-lg border border-gray-300 bg-white px-3 py-2 text-xs text-gray-600 hover:bg-gray-50"
			>
				Zurücksetzen
			</button>
		{:else if app.release.status === 'error'}
			<button
				onclick={release}
				class="rounded-lg bg-amber-600 px-4 py-2 text-sm font-semibold text-white hover:bg-amber-700"
			>
				Erneut versuchen
			</button>
		{/if}
	</div>

	<!-- Audit trail -->
	<dl class="grid grid-cols-1 gap-1 rounded-md border border-gray-100 bg-gray-50 p-3 text-[11px] sm:grid-cols-2">
		<div class="flex items-center justify-between gap-3">
			<dt class="text-gray-500">Datei geladen</dt>
			<dd class="font-mono text-gray-700">{fmtTime(app.audit.fileLoadedAt)}</dd>
		</div>
		<div class="flex items-center justify-between gap-3">
			<dt class="text-gray-500">Scan abgeschlossen</dt>
			<dd class="font-mono text-gray-700">{fmtTime(app.audit.scanCompletedAt)}</dd>
		</div>
		<div class="flex items-center justify-between gap-3">
			<dt class="text-gray-500">Anonymisierung</dt>
			<dd class="font-mono text-gray-700">
				{app.anonymizeMode === 'off'
					? 'Aus (Original)'
					: app.anonymizeMode === 'auto'
					? 'Vollständig'
					: 'Selektiv'}
				{#if app.pii.status === 'done'}
					· {app.pii.approvals.size}/{app.pii.detections.length} maskiert
				{/if}
			</dd>
		</div>
		<div class="flex items-center justify-between gap-3">
			<dt class="text-gray-500">Chunking</dt>
			<dd class="font-mono text-gray-700">{strategyLabel}</dd>
		</div>
		<div class="flex items-center justify-between gap-3">
			<dt class="text-gray-500">Freigegeben</dt>
			<dd class="font-mono text-gray-700">{fmtTime(app.release.snapshot?.releasedAt ?? null)}</dd>
		</div>
		<div class="flex items-center justify-between gap-3">
			<dt class="text-gray-500">Importiert</dt>
			<dd class="font-mono text-gray-700">{fmtTime(app.release.importedAt)}</dd>
		</div>
		<div class="flex items-center justify-between gap-3">
			<dt class="text-gray-500">Status</dt>
			<dd>
				<span
					class="rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wider
						{app.release.status === 'imported'
							? 'bg-emerald-100 text-emerald-700'
							: app.release.status === 'importing'
							? 'bg-blue-100 text-blue-700'
							: app.release.status === 'released'
							? 'bg-amber-100 text-amber-700'
							: app.release.status === 'error'
							? 'bg-red-100 text-red-700'
							: 'bg-gray-200 text-gray-600'}"
				>
					{app.release.status}
				</span>
			</dd>
		</div>
	</dl>

	<!-- Chunking strategy picker -->
	{#if app.parsedTable && (app.release.status === 'idle' || app.release.status === 'error')}
		<div class="space-y-2 rounded-md border border-gray-200 bg-white p-3">
			<div class="flex items-center justify-between gap-3">
				<div>
					<div class="text-xs font-semibold text-gray-700">Chunking-Strategie</div>
					<div class="text-[11px] text-gray-400">
						Wie soll die Tabelle ins RAG geladen werden?
					</div>
				</div>
				<div class="flex gap-1">
					<button
						class="rounded-full px-3 py-1 text-xs font-medium transition-colors
							{app.chunkingStrategy === 'per-row'
								? 'bg-blue-600 text-white'
								: 'border border-gray-300 bg-white text-gray-600 hover:bg-gray-50'}"
						onclick={() => setStrategy('per-row')}
					>
						Pro Zeile
					</button>
					<button
						class="rounded-full px-3 py-1 text-xs font-medium transition-colors
							{app.chunkingStrategy === 'whole-file'
								? 'bg-blue-600 text-white'
								: 'border border-gray-300 bg-white text-gray-600 hover:bg-gray-50'}"
						onclick={() => setStrategy('whole-file')}
					>
						Ganze Datei
					</button>
				</div>
			</div>

			{#if app.chunkingStrategy === 'per-row'}
				<p class="text-[11px] text-gray-500">
					Jede Zeile wird zu einem eigenen Chunk im Vektor-Store. Format pro Zeile:
					<span class="font-mono text-gray-600">Spalte: Wert</span> pro Zeilenumbruch. Gut für
					Lookups einzelner Datensätze.
				</p>
				{#if chunkPreview.length > 0}
					<details class="text-[11px]">
						<summary class="cursor-pointer text-gray-500 hover:text-gray-700">
							Beispiel-Chunks anzeigen ({chunkPreview.length}/{app.parsedTable?.rows.length ?? 0})
						</summary>
						<div class="mt-1 space-y-1">
							{#each chunkPreview as chunk, i}
								<pre class="overflow-auto rounded border border-gray-100 bg-gray-50 p-2 text-[10px] text-gray-700">Chunk {i + 1}:
{chunk}</pre>
							{/each}
						</div>
					</details>
				{/if}
			{:else}
				<p class="text-[11px] text-gray-500">
					Die gesamte Datei läuft durch die Standard-Pipeline (Cleaning → Strukturierung →
					Embedding). Die Strukturierung entscheidet, wie chunked wird. Gut für freie Suche im
					gesamten Inhalt.
				</p>
			{/if}
		</div>
	{/if}

	{#if !canRelease && app.release.status === 'idle'}
		<div class="rounded-md border border-amber-200 bg-amber-50 p-3 text-xs text-amber-800">
			{#if !app.parsedTable}
				Bitte zuerst eine CSV/Excel-Datei laden.
			{:else if app.pii.status === 'idle'}
				Bitte zuerst einen PII-Scan starten — auch wenn das Ergebnis ohne Maskierung importiert werden soll.
			{:else if app.pii.status === 'loading-model' || app.pii.status === 'scanning'}
				Scan läuft … bitte warten.
			{/if}
		</div>
	{/if}

	{#if app.release.status === 'error' && app.release.error}
		<div class="rounded-md border border-red-200 bg-red-50 p-3 text-xs text-red-700">
			Fehler beim Import: {app.release.error}
		</div>
	{/if}

	{#if released && app.release.snapshot}
		<div class="rounded-md border border-emerald-200 bg-emerald-50 p-3 text-xs">
			<div class="font-medium text-emerald-800">
				{app.release.status === 'imported'
					? 'Daten erfolgreich importiert.'
					: app.release.status === 'importing'
					? 'Sende Daten ans RAG …'
					: 'Daten freigegeben.'}
			</div>
			<div class="mt-1 text-emerald-700">
				{app.release.snapshot.rowCount} Zeilen · {app.release.snapshot.columnCount} Spalten ·
				{app.release.snapshot.anonymized ? 'maskiert' : 'unmaskiert'} ·
				{app.release.snapshot.chunkingStrategy === 'per-row' ? 'pro Zeile' : 'ganze Datei'}
				{#if app.release.importResult?.chunks !== undefined}
					· {app.release.importResult.chunks} Chunks
				{/if}
			</div>
			<div class="mt-2 flex flex-wrap gap-2">
				<button
					onclick={() => (showPreview = !showPreview)}
					class="rounded border border-emerald-300 bg-white px-2 py-1 text-[11px] text-emerald-800 hover:bg-emerald-50"
				>
					{showPreview ? 'Snapshot ausblenden' : 'Snapshot anzeigen'}
				</button>
				<button
					onclick={downloadSnapshot}
					class="rounded border border-emerald-300 bg-white px-2 py-1 text-[11px] text-emerald-800 hover:bg-emerald-50"
				>
					Snapshot als CSV laden
				</button>
			</div>
		</div>

		{#if showPreview}
			<pre class="max-h-72 overflow-auto rounded-md border border-gray-200 bg-gray-900 p-3 text-[11px] leading-snug text-gray-100">{snapshotToCsvString(app.release.snapshot)}</pre>
		{/if}
	{/if}
</section>
