<script lang="ts">
	import { app } from '$lib/state.svelte';
	import {
		deleteDocument,
		getDocuments,
		getImageStatus,
		regenerateImages,
		reindexDocument,
		uploadFile,
		uploadFolder,
		type FolderUploadResult,
		type ImageStatus
	} from '$lib/api';
	import TablePreviewModal from './TablePreviewModal.svelte';
	import ImageGalleryModal from './ImageGalleryModal.svelte';

	const TABLE_EXTS = new Set(['csv', 'xlsx', 'xls']);
	const GALLERY_EXTS = new Set(['pdf']);

	let documents: Array<{
		id: string;
		file_name: string;
		file_hash?: string;
		stored_path?: string;
		collection: string;
		use_case: string;
		chunk_count: number;
		status: string;
		created_at: string;
	}> = $state([]);

	let collections: Array<{
		name: string;
		count: number;
		dimension: number;
	}> = $state([]);

	let loading = $state(false);
	let preview = $state<{ url: string; fileName: string } | null>(null);
	let gallery = $state<{ useCase: string; fileHash: string; fileName: string } | null>(null);
	let deletingId = $state<string | null>(null);
	let deleteError = $state<string | null>(null);

	// ── Upload state ──────────────────────────────────────────────
	let pdfUploading = $state(false);
	let pdfProgress = $state({ done: 0, total: 0 });
	let pdfResults = $state<Array<{ name: string; ok: boolean; msg: string }>>([]);

	let zipUploading = $state(false);
	let zipName = $state<string | null>(null);
	let zipResult = $state<FolderUploadResult | null>(null);
	let zipError = $state<string | null>(null);

	async function handlePdfFiles(files: FileList | null) {
		if (!files || files.length === 0) return;
		const list = Array.from(files);
		pdfUploading = true;
		pdfResults = [];
		pdfProgress = { done: 0, total: list.length };
		// Sequential – the cleaning + ingest pipeline is heavy.
		for (let i = 0; i < list.length; i++) {
			const file = list[i];
			try {
				const result = await uploadFile(file, app.useCase);
				pdfResults = [...pdfResults, { name: file.name, ok: true, msg: `${result.chunks ?? 0} Chunks` }];
			} catch (err) {
				const message = err instanceof Error ? err.message : 'Upload fehlgeschlagen';
				pdfResults = [...pdfResults, { name: file.name, ok: false, msg: message }];
			}
			pdfProgress = { done: i + 1, total: list.length };
		}
		pdfUploading = false;
		await load();
	}

	async function handleZipFile(files: FileList | null) {
		if (!files || files.length === 0) return;
		const file = files[0];
		zipUploading = true;
		zipResult = null;
		zipError = null;
		zipName = file.name;
		try {
			zipResult = await uploadFolder(file, app.useCase);
		} catch (err) {
			zipError = err instanceof Error ? err.message : 'Upload fehlgeschlagen';
		}
		zipUploading = false;
		await load();
	}

	function onPdfInput(e: Event) {
		const target = e.target as HTMLInputElement;
		handlePdfFiles(target.files);
		target.value = '';
	}

	function onZipInput(e: Event) {
		const target = e.target as HTMLInputElement;
		handleZipFile(target.files);
		target.value = '';
	}

	// Durable per-document image-description status (from disk), keyed by file_hash.
	let imageStatus = $state<Record<string, ImageStatus>>({});
	let regenerating = $state<Set<string>>(new Set());

	async function load() {
		loading = true;
		try {
			const data = await getDocuments(app.useCase);
			documents = data.documents ?? [];
			collections = data.collections ?? [];
		} catch {
			documents = [];
			collections = [];
		}
		loading = false;
		void loadImageStatuses();
	}

	async function loadImageStatuses() {
		for (const doc of documents) {
			if (!doc.file_hash || !hasGallery(doc.file_name)) continue;
			try {
				imageStatus[doc.file_hash] = await getImageStatus(doc.use_case, doc.file_hash);
			} catch {
				/* ignore — status is best-effort */
			}
		}
	}

	async function pollImageStatus(useCase: string, fileHash: string) {
		const deadline = Date.now() + 30 * 60 * 1000;
		while (Date.now() < deadline) {
			await new Promise((r) => setTimeout(r, 2500));
			let s: ImageStatus;
			try {
				s = await getImageStatus(useCase, fileHash);
			} catch {
				continue;
			}
			imageStatus[fileHash] = s;
			if (s.job_status !== 'running' && s.pending === 0) return;
			if (s.job_status === 'done' && !regenerating.has(fileHash)) return;
		}
	}

	async function regenerate(doc: { use_case: string; file_hash?: string }, force = false) {
		if (!doc.file_hash) return;
		const fh = doc.file_hash;
		regenerating.add(fh);
		regenerating = new Set(regenerating);
		try {
			await regenerateImages(doc.use_case, fh, force);
			await pollImageStatus(doc.use_case, fh);
		} finally {
			regenerating.delete(fh);
			regenerating = new Set(regenerating);
		}
	}

	let reindexingId = $state<string | null>(null);
	let reindexNotice = $state<string | null>(null);

	async function handleReindex(doc: {
		id: string;
		use_case: string;
		file_hash?: string;
		file_name: string;
		stored_path?: string;
		collection: string;
		chunk_count: number;
	}) {
		const msg =
			`„${doc.file_name}" neu transkribieren und einlesen?\n\n` +
			`Alle Bilder werden erneut vom Vision-Modell ausgelesen, das Dokument ` +
			`neu zerlegt und die ${doc.chunk_count.toLocaleString('de-AT')} bestehenden ` +
			`Chunks ersetzt.\n\n` +
			`Das dauert bei bildreichen PDFs mehrere Minuten — bitte die Seite ` +
			`währenddessen offen lassen.`;
		if (!confirm(msg)) return;

		reindexNotice = null;
		reindexingId = doc.id;
		try {
			const result = await reindexDocument(doc);
			if (result.error) {
				reindexNotice = `„${doc.file_name}": ${result.detail ?? result.error}`;
			} else if (result.images_complete === false) {
				// Indexed anyway, but some images stayed mute — say so rather than
				// reporting a success that quietly lost image content.
				reindexNotice =
					`„${doc.file_name}" neu eingelesen (${result.chunks ?? 0} Chunks), aber nur ` +
					`${result.images_described ?? 0} von ${result.image_count ?? 0} Bildern ` +
					`konnten beschrieben werden. Erneut ausführen ergänzt die fehlenden.`;
			} else {
				reindexNotice =
					`„${doc.file_name}": ${result.chunks ?? 0} Chunks neu indexiert, ` +
					`${result.images_described ?? 0} Bilder beschrieben.`;
			}
			await load();
			await loadImageStatuses();
		} catch (err) {
			reindexNotice = `„${doc.file_name}": ${(err as Error).message}`;
		} finally {
			reindexingId = null;
		}
	}

	function getExtension(name: string): string {
		const idx = name.lastIndexOf('.');
		return idx >= 0 ? name.slice(idx + 1).toLowerCase() : '';
	}

	function isTable(fileName: string): boolean {
		return TABLE_EXTS.has(getExtension(fileName));
	}

	function hasGallery(fileName: string): boolean {
		return GALLERY_EXTS.has(getExtension(fileName));
	}

	function openPreview(fileName: string, storedPath: string | undefined) {
		if (!storedPath) return;
		preview = { url: `/api/documents/${storedPath}`, fileName };
	}

	function openGallery(doc: { file_name: string; use_case: string; file_hash?: string }) {
		if (!doc.file_hash) return;
		gallery = { useCase: doc.use_case, fileHash: doc.file_hash, fileName: doc.file_name };
	}

	async function handleDelete(doc: { id: string; file_name: string; chunk_count: number }) {
		const msg =
			`„${doc.file_name}" und alle damit verbundenen Daten ` +
			`(Embeddings, extrahierte Bilder, Originaldatei, Log-Eintrag) ` +
			`endgültig löschen?\n\n` +
			`${doc.chunk_count.toLocaleString('de-AT')} Chunks aus Qdrant ` +
			`werden ebenfalls entfernt.`;
		if (!confirm(msg)) return;
		deleteError = null;
		deletingId = doc.id;
		try {
			const result = await deleteDocument(doc.id);
			if (result.status === 'partial' && result.errors?.length) {
				deleteError = `Teilweise erfolgreich: ${result.errors.join('; ')}`;
			}
			await load();
		} catch (e) {
			deleteError = e instanceof Error ? e.message : String(e);
		} finally {
			deletingId = null;
		}
	}

	$effect(() => {
		app.useCase;
		load();
	});
</script>

<main class="flex h-full flex-1 flex-col bg-gray-50 overflow-y-auto">
	<div class="mx-auto w-full max-w-4xl p-6 space-y-6">
		<div class="flex items-center justify-between">
			<h2 class="text-lg font-semibold text-gray-800">Dokumente & Datenbank</h2>
			<button
				onclick={load}
				class="rounded-lg border border-gray-300 bg-white px-3 py-1.5 text-xs text-gray-600 hover:bg-gray-50"
			>
				Aktualisieren
			</button>
		</div>

		{#if deleteError}
			<div class="rounded-lg border border-red-200 bg-red-50 px-4 py-2 text-xs text-red-700">
				{deleteError}
			</div>
		{/if}

		{#if reindexingId}
			<div class="rounded-lg border border-blue-200 bg-blue-50 px-4 py-2 text-xs text-blue-700">
				Dokument wird neu eingelesen. Jedes Bild wird dabei erneut vom
				Vision-Modell ausgelesen — das dauert bei bildreichen PDFs mehrere
				Minuten. Bitte die Seite offen lassen.
			</div>
		{/if}

		{#if reindexNotice}
			<div class="rounded-lg border border-gray-200 bg-gray-50 px-4 py-2 text-xs text-gray-700">
				{reindexNotice}
			</div>
		{/if}

		<!-- Upload -->
		<section>
			<h3 class="mb-3 text-sm font-semibold uppercase tracking-wider text-gray-400">Upload</h3>
			<div class="grid gap-3 sm:grid-cols-2">
				<!-- PDF / Dokument-Upload -->
				<div class="rounded-lg border border-gray-200 bg-white p-4 shadow-sm">
					<p class="mb-2 text-sm font-medium text-gray-700">Dokumente (PDF)</p>
					<label
						class="flex cursor-pointer flex-col items-center justify-center rounded-lg border-2 border-dashed border-gray-300 px-4 py-6 text-center transition-colors hover:border-blue-400 hover:bg-blue-50"
					>
						{#if pdfUploading}
							<span class="text-sm text-gray-500">Verarbeite {pdfProgress.done}/{pdfProgress.total} …</span>
						{:else}
							<span class="text-sm text-gray-500">PDFs hierher ziehen oder wählen</span>
							<span class="mt-1 text-xs text-blue-600">Mehrfachauswahl möglich</span>
						{/if}
						<input
							type="file"
							accept=".pdf,.docx,.pptx,.txt"
							multiple
							class="hidden"
							disabled={pdfUploading}
							onchange={onPdfInput}
						/>
					</label>
					{#if pdfResults.length > 0}
						<ul class="mt-2 space-y-1 text-xs">
							{#each pdfResults as r}
								<li class="flex items-center justify-between gap-2">
									<span class="truncate text-gray-600" title={r.name}>{r.name}</span>
									<span class={r.ok ? 'shrink-0 text-green-600' : 'shrink-0 text-red-600'} title={r.msg}>
										{r.ok ? `✓ ${r.msg}` : `✗ ${r.msg.slice(0, 50)}`}
									</span>
								</li>
							{/each}
						</ul>
					{/if}
				</div>

				<!-- ZIP / Ordner-Upload -->
				<div class="rounded-lg border border-gray-200 bg-white p-4 shadow-sm">
					<p class="mb-2 text-sm font-medium text-gray-700">Produkt-Ordner (ZIP)</p>
					<label
						class="flex cursor-pointer flex-col items-center justify-center rounded-lg border-2 border-dashed border-gray-300 px-4 py-6 text-center transition-colors hover:border-blue-400 hover:bg-blue-50"
					>
						{#if zipUploading}
							<span class="text-sm text-gray-500">Verarbeite {zipName} …</span>
						{:else}
							<span class="text-sm text-gray-500">ZIP des Produkt-Ordners wählen</span>
							<span class="mt-1 text-xs text-blue-600">CNC-Code, Stückliste, Einstellblätter</span>
						{/if}
						<input
							type="file"
							accept=".zip"
							class="hidden"
							disabled={zipUploading}
							onchange={onZipInput}
						/>
					</label>

					{#if zipError}
						<p class="mt-2 text-xs text-red-600" title={zipError}>✗ {zipError.slice(0, 80)}</p>
					{:else if zipResult}
						<div class="mt-2 space-y-1 text-xs">
							<p class="font-medium text-green-700">
								✓ {zipResult.chunks} Chunks aus {zipResult.products?.length ?? 0} Produkt(en)
							</p>
							{#each zipResult.products ?? [] as p}
								<div class="flex items-center justify-between gap-2 text-gray-600">
									<span class="truncate" title={p.product_id}>{p.product_id}</span>
									<span class="shrink-0 text-gray-400">
										{p.operations} Op. · {p.material_class ?? 'kein Material'}
									</span>
								</div>
							{/each}
							{#if zipResult.skipped_noncanonical?.length}
								<p class="text-gray-400">
									{zipResult.skipped_noncanonical.length} Datei(en) übersprungen (Vorlagen/CAM)
								</p>
							{/if}
						</div>
					{/if}
				</div>
			</div>
		</section>

		{#if loading}
			<p class="text-sm text-gray-400 animate-pulse">Laden...</p>
		{:else}
			<!-- Collections / Vector DB -->
			<section>
				<h3 class="mb-3 text-sm font-semibold uppercase tracking-wider text-gray-400">Vektordatenbank (Qdrant)</h3>
				{#if collections.length === 0}
					<p class="text-sm text-gray-400">Keine Collections vorhanden.</p>
				{:else}
					<div class="grid gap-3 sm:grid-cols-2">
						{#each collections as col}
							<div class="rounded-lg border border-gray-200 bg-white p-4 shadow-sm">
								<div class="flex items-center justify-between">
									<span class="font-medium text-gray-800 text-sm">{col.name}</span>
									<span class="rounded-full bg-blue-100 px-2 py-0.5 text-[10px] font-semibold text-blue-700">{col.count} Chunks</span>
								</div>
								<p class="mt-1 text-xs text-gray-400">{col.dimension}D Vektoren</p>
							</div>
						{/each}
					</div>
				{/if}
			</section>

			<!-- Uploaded Documents -->
			<section>
				<h3 class="mb-3 text-sm font-semibold uppercase tracking-wider text-gray-400">Hochgeladene Dokumente</h3>
				{#if documents.length === 0}
					<p class="text-sm text-gray-400">Noch keine Dokumente hochgeladen. Ingestion-Logging wird nach dem naechsten Upload aktiv.</p>
				{:else}
					<div class="space-y-2">
						{#each documents as doc}
							<div class="rounded-lg border border-gray-200 bg-white px-4 py-3 shadow-sm">
								<div class="flex items-center gap-3">
									<div class="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-gray-100 text-xs font-bold text-gray-500">
										{doc.file_name.split('.').pop()?.toUpperCase() ?? '?'}
									</div>
									<div class="min-w-0 flex-1">
										{#if doc.stored_path && isTable(doc.file_name)}
											<button
												type="button"
												onclick={() => openPreview(doc.file_name, doc.stored_path)}
												class="block truncate text-left text-sm font-medium text-blue-600 hover:underline"
												title="Tabellen-Vorschau öffnen"
											>
												{doc.file_name}
											</button>
										{:else if doc.stored_path}
											<a
												href="/api/documents/{doc.stored_path}"
												target="_blank"
												class="truncate text-sm font-medium text-blue-600 hover:underline block"
												title="Originaldokument oeffnen"
											>
												{doc.file_name}
												<svg class="inline h-3 w-3 -mt-0.5 ml-0.5" viewBox="0 0 20 20" fill="currentColor">
													<path d="M11 3a1 1 0 100 2h2.586l-6.293 6.293a1 1 0 101.414 1.414L15 6.414V9a1 1 0 102 0V4a1 1 0 00-1-1h-5z" />
													<path d="M5 5a2 2 0 00-2 2v8a2 2 0 002 2h8a2 2 0 002-2v-3a1 1 0 10-2 0v3H5V7h3a1 1 0 000-2H5z" />
												</svg>
											</a>
										{:else}
											<p class="truncate text-sm font-medium text-gray-800">{doc.file_name}</p>
										{/if}
										<p class="text-xs text-gray-400">
											{doc.collection} &middot; {doc.chunk_count} Chunks &middot;
											{new Date(doc.created_at).toLocaleString('de-AT')}
										</p>
										{#if doc.file_hash && imageStatus[doc.file_hash] && imageStatus[doc.file_hash].total > 0}
											{@const s = imageStatus[doc.file_hash]}
											<p class="mt-0.5 text-xs {s.described < s.total ? 'text-amber-600' : 'text-emerald-600'}">
												{#if s.job_status === 'running' || regenerating.has(doc.file_hash)}
													🖼 {s.described}/{s.total} Bilder interpretiert …
												{:else if s.pending > 0}
													🖼 {s.described}/{s.total} Bilder beschrieben · {s.pending} offen
												{:else}
													🖼 {s.total} Bilder beschrieben
												{/if}
											</p>
										{/if}
									</div>
									{#if doc.stored_path && isTable(doc.file_name)}
										<button
											type="button"
											onclick={() => openPreview(doc.file_name, doc.stored_path)}
											class="rounded-md border border-gray-200 bg-white px-2 py-1 text-[11px] font-medium text-gray-600 hover:border-blue-300 hover:bg-blue-50 hover:text-blue-700"
											title="Tabellen-Vorschau öffnen"
										>
											Tabelle anzeigen
										</button>
									{/if}
									{#if doc.file_hash && hasGallery(doc.file_name)}
										<button
											type="button"
											onclick={() => openGallery(doc)}
											class="rounded-md border border-gray-200 bg-white px-2 py-1 text-[11px] font-medium text-gray-600 hover:border-blue-300 hover:bg-blue-50 hover:text-blue-700"
											title="Extrahierte Bilder + Beschreibung anzeigen"
										>
											Bilder
										</button>
									{/if}
									{#if doc.file_hash && imageStatus[doc.file_hash] && imageStatus[doc.file_hash].pending > 0}
										<button
											type="button"
											onclick={() => regenerate(doc)}
											disabled={regenerating.has(doc.file_hash) || imageStatus[doc.file_hash].job_status === 'running'}
											class="rounded-md border border-amber-200 bg-white px-2 py-1 text-[11px] font-medium text-amber-700 hover:border-amber-400 hover:bg-amber-50 disabled:opacity-50"
											title="Fehlende Bildbeschreibungen im Hintergrund erzeugen"
										>
											{regenerating.has(doc.file_hash) ? 'Läuft…' : 'Beschreibungen erzeugen'}
										</button>
									{/if}
									{#if doc.file_hash && doc.stored_path}
										<button
											type="button"
											onclick={() => handleReindex(doc)}
											disabled={reindexingId !== null}
											class="rounded-md border border-blue-200 bg-white px-2 py-1 text-[11px] font-medium text-blue-700 hover:border-blue-400 hover:bg-blue-50 disabled:cursor-not-allowed disabled:opacity-50"
											title="Bilder erneut auslesen, Dokument neu zerlegen und die bestehenden Chunks ersetzen"
										>
											{reindexingId === doc.id ? 'Liest neu ein…' : 'Neu einlesen'}
										</button>
									{/if}
									<span class="rounded-full px-2 py-0.5 text-[10px] font-semibold
										{doc.status === 'ok' ? 'bg-green-100 text-green-700' : 'bg-red-100 text-red-700'}">
										{doc.status}
									</span>
									<button
										type="button"
										onclick={() => handleDelete(doc)}
										disabled={deletingId !== null}
										class="rounded-md border border-red-200 bg-white px-2 py-1 text-[11px] font-medium text-red-600 hover:border-red-400 hover:bg-red-50 disabled:cursor-not-allowed disabled:opacity-50"
										title="Dokument samt Embeddings, Bildern und Original entfernen"
									>
										{deletingId === doc.id ? 'Lösche…' : 'Löschen'}
									</button>
								</div>
							</div>
						{/each}
					</div>
				{/if}
			</section>
		{/if}
	</div>
</main>

{#if preview}
	<TablePreviewModal
		url={preview.url}
		fileName={preview.fileName}
		onClose={() => (preview = null)}
	/>
{/if}

{#if gallery}
	<ImageGalleryModal
		useCase={gallery.useCase}
		fileHash={gallery.fileHash}
		fileName={gallery.fileName}
		onClose={() => (gallery = null)}
	/>
{/if}
