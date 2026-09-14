<script lang="ts">
	/**
	 * Upload GW St. Pölten data folders (Kategorie / Maschine / Produkt).
	 *
	 * 1. The user picks or drops one or more folders – the whole data set, a
	 *    machine, or single product folders – and can keep adding more. Only the
	 *    paths go to the server, which answers with a plan: products per machine
	 *    and the files each needs. .doc versions, templates and CAM folders never
	 *    leave the machine.
	 * 2. Phase 1 uploads the CNC programs + Stückliste of every product. Each
	 *    upload replaces that product's earlier data completely, so a folder of
	 *    the same name can simply be uploaded again. Takes seconds and makes the
	 *    whole set searchable right away.
	 * 3. Phase 2 sends the Einstellblatt PDFs and photos, renamed to
	 *    "<Produkt> (<Maschine>) – <Datei>" so every chunk names its product –
	 *    "Einstellblatt 1.pdf" exists in nearly every folder. This is the slow
	 *    part (OCR, image analysis).
	 */
	import { planGwFolder, uploadFile, uploadGwProduct, type GwPlan, type GwPlanProduct } from '$lib/api';
	import {
		filesFromDrop,
		filesFromInput,
		renamed,
		shrinkImage,
		toBase64,
		type PickedFile
	} from '$lib/folder-files';
	import { unionByFile } from '$lib/gw-location';

	let { onDone }: { onDone?: () => void } = $props();

	type RowStatus = 'wartet' | 'läuft' | 'ok' | 'fehler';
	interface Row {
		product: GwPlanProduct;
		machine: string | null;
		category: string | null;
		status: RowStatus;
		cncDone: boolean;
		ops: number;
		/** Paths of PDFs/photos already sent – a resumed upload skips them. */
		docsSent: string[];
		docsFailed: number;
		msg: string;
		notes: string[];
	}

	const SKIP_LABELS: Record<string, string> = {
		vorlage: 'Vorlagen',
		cam_arbeitsordner: 'CAM-Dateien (Hypermill)',
		alte_version: 'aus „Alte Version"',
		systemdatei: 'Systemdateien'
	};

	let dragover = $state(false);
	let planning = $state(false);
	let uploading = $state(false);
	let stopRequested = $state(false);
	let error = $state('');
	let includeDocs = $state(true);

	let picked = new Map<string, File>();
	let plan = $state<GwPlan | null>(null);
	let rows = $state<Row[]>([]);

	const totals = $derived.by(() => {
		const products = plan?.products ?? [];
		return {
			products: products.length,
			machines: new Set(products.map((p) => p.machine ?? '?')).size,
			cnc: products.reduce((n, p) => n + p.cnc_candidates.length, 0),
			pdfs: products.reduce((n, p) => n + p.einstellblaetter.length, 0),
			photos: products.reduce((n, p) => n + p.bilder.length, 0),
			docs: products.reduce((n, p) => n + p.ignored_documents, 0)
		};
	});
	const remaining = $derived(rows.filter((r) => r.status !== 'ok').length);
	const finished = $derived(rows.filter((r) => r.status === 'ok' || r.status === 'fehler').length);

	const rowKey = (p: GwPlanProduct) => `${p.product_id}|${p.machine ?? ''}`;
	const needsChoice = (r: Row) => !!r.product.machine_choices?.length && !r.machine;
	const docPaths = (r: Row) => [...r.product.einstellblaetter, ...r.product.bilder];

	function newRow(product: GwPlanProduct): Row {
		return {
			product,
			machine: product.machine,
			category: product.category,
			status: 'wartet',
			cncDone: false,
			ops: 0,
			docsSent: [],
			docsFailed: 0,
			msg: '',
			notes: []
		};
	}

	async function addFiles(files: PickedFile[]) {
		if (!files.length || uploading) return;
		for (const f of files) picked.set(f.path, f.file);
		error = '';
		planning = true;
		// Finished products keep their state when more folders are added.
		const previous = new Map(rows.map((r) => [rowKey(r.product), r]));
		try {
			plan = await planGwFolder([...picked.keys()]);
			rows = plan.products.map((product) => {
				const prev = previous.get(rowKey(product));
				return prev && prev.status === 'ok' ? { ...prev, product } : newRow(product);
			});
			if (!plan.products.length) {
				error = 'Keine Produktordner gefunden (erwartet: …/11221.xxxx/…).';
			}
		} catch (e) {
			error = e instanceof Error ? e.message : 'Planung fehlgeschlagen';
		}
		planning = false;
	}

	function reset() {
		picked = new Map();
		plan = null;
		rows = [];
		error = '';
	}

	function onInput(e: Event) {
		const input = e.target as HTMLInputElement;
		void addFiles(filesFromInput(input.files));
		input.value = '';
	}

	async function onDrop(e: DragEvent) {
		e.preventDefault();
		dragover = false;
		void addFiles(await filesFromDrop(e.dataTransfer));
	}

	function chooseMachine(r: Row, machine: string) {
		const choice = r.product.machine_choices?.find((c) => c.machine === machine);
		const pid = r.product.product_id;
		const twin = rows.find((o) => o !== r && o.product.product_id === pid && o.machine === machine);
		if (!twin) {
			r.machine = choice?.machine ?? null;
			r.category = choice?.category ?? r.category;
			return;
		}
		// That product is already a row for this machine. Two rows would upload
		// it twice: the CNC data replaces itself, but the documents would arrive
		// twice. One row with the files of both.
		const merged = {
			...twin.product,
			cnc_candidates: unionByFile(pid, twin.product.cnc_candidates, r.product.cnc_candidates),
			einstellblaetter: unionByFile(pid, twin.product.einstellblaetter, r.product.einstellblaetter),
			bilder: unionByFile(pid, twin.product.bilder, r.product.bilder),
			stueckliste: twin.product.stueckliste ?? r.product.stueckliste,
			warnings: [...twin.product.warnings, 'Ordner mehrfach gewählt – zusammengeführt']
		};
		merged.upload_paths = [...merged.cnc_candidates, ...(merged.stueckliste ? [merged.stueckliste] : [])];
		twin.product = merged;
		// Already uploaded: send again with the merged files – replacing is safe.
		if (twin.status === 'ok') Object.assign(twin, { status: 'wartet', cncDone: false, docsSent: [], msg: '' });
		rows = rows.filter((o) => o !== r);
	}

	const label = (r: Row) => (r.machine ? `${r.product.product_id} (${r.machine})` : r.product.product_id);

	function summary(r: Row): string {
		const docs = docPaths(r).length;
		const parts = [`${r.ops} Operationen`];
		if (includeDocs && docs) parts.push(`${r.docsSent.length - r.docsFailed}/${docs} Dokumente & Fotos`);
		if (r.notes.length) parts.push(`⚠ ${r.notes.join(' · ')}`);
		return parts.join(' · ');
	}

	async function uploadCnc(r: Row) {
		const files = [];
		for (const path of r.product.upload_paths) {
			const file = picked.get(path);
			if (file) files.push({ path, content_b64: await toBase64(file) });
		}
		const result = await uploadGwProduct({
			product_id: r.product.product_id,
			machine: r.machine,
			category: r.category,
			files
		});
		// The product was replaced as a whole – its documents must be sent again.
		r.cncDone = true;
		r.docsSent = [];
		r.docsFailed = 0;
		r.ops = result.product?.operations ?? 0;
		r.notes = [...(result.product?.warnings ?? [])];
	}

	/** Returns false when stopped before all documents were sent. */
	async function uploadDocs(r: Row): Promise<boolean> {
		const docs = docPaths(r);
		for (const [i, path] of docs.entries()) {
			if (stopRequested) return false;
			if (r.docsSent.includes(path)) continue;
			const original = picked.get(path);
			const isPhoto = r.product.bilder.includes(path);
			if (!original) continue;
			r.msg = `${r.ops} Operationen · ${isPhoto ? 'Foto' : 'Einstellblatt'} ${i + 1}/${docs.length} …`;
			try {
				const file = isPhoto ? await shrinkImage(original) : original;
				const name = `${label(r)} – ${file.name}`;
				await uploadFile(renamed(file, name), 'gw_stpoelten', {
					meta: {
						doc_kind: isPhoto ? 'foto' : 'einstellblatt',
						product_id: r.product.product_id,
						machine: r.machine,
						category: r.category
					},
					displayName: name
				});
			} catch (e) {
				r.docsFailed++;
				r.notes.push(`${original.name}: ${e instanceof Error ? e.message.slice(0, 80) : 'fehlgeschlagen'}`);
			}
			r.docsSent.push(path);
		}
		return true;
	}

	async function uploadAll() {
		uploading = true;
		stopRequested = false;
		error = '';
		const todo = rows.filter((r) => r.status !== 'ok' && !needsChoice(r));

		// Phase 1: CNC programs of every product – seconds, and the whole set is
		// searchable right away.
		for (const r of todo) {
			if (stopRequested) break;
			if (r.cncDone) continue;
			r.status = 'läuft';
			r.msg = 'CNC-Programme …';
			try {
				await uploadCnc(r);
				const pending = includeDocs && docPaths(r).length > 0;
				r.status = pending ? 'wartet' : 'ok';
				r.msg = pending ? `${r.ops} Operationen · Dokumente & Fotos folgen` : summary(r);
			} catch (e) {
				r.status = 'fehler';
				r.msg = e instanceof Error ? e.message : 'Upload fehlgeschlagen';
			}
		}

		// Phase 2: Einstellblätter and photos – OCR and image analysis, the slow
		// part. Sequential: parallel uploads would only queue up at the shared
		// vision backend.
		if (includeDocs) {
			for (const r of todo) {
				if (stopRequested) break;
				if (!r.cncDone || r.status === 'ok' || r.status === 'fehler') continue;
				r.status = 'läuft';
				const complete = await uploadDocs(r);
				r.status = complete ? 'ok' : 'wartet';
				r.msg = complete ? summary(r) : `${summary(r)} · angehalten`;
			}
		}
		uploading = false;
		onDone?.();
	}
</script>

<div
	class="rounded-lg border bg-white p-4 shadow-sm transition-colors
		{dragover ? 'border-emerald-400 bg-emerald-50' : 'border-emerald-200'}"
	role="region"
	aria-label="Datenbestand-Ordner hochladen"
	ondragover={(e) => {
		e.preventDefault();
		if (!uploading) dragover = true;
	}}
	ondragleave={() => (dragover = false)}
	ondrop={onDrop}
>
	<p class="text-sm font-medium text-gray-700">Datenbestand-Ordner (GW St. Pölten)</p>
	<p class="mb-3 mt-0.5 text-xs text-gray-500">
		Einen oder mehrere Ordner wählen oder hineinziehen – den ganzen Bestand, eine Maschine oder
		einzelne Produktordner. Hochgeladen werden CNC-Programme, Stücklisten, Einstellblätter (PDF)
		und Fotos. Ein Ordner mit demselben Namen ersetzt beim erneuten Hochladen die alten Daten.
	</p>

	{#if !plan || !rows.length}
		<label
			class="flex cursor-pointer flex-col items-center justify-center rounded-lg border-2 border-dashed border-gray-300 px-4 py-6 text-center transition-colors hover:border-emerald-400 hover:bg-emerald-50"
		>
			{#if planning}
				<span class="text-sm text-gray-500">Ordner wird analysiert …</span>
			{:else}
				<span class="text-sm text-gray-500">Ordner hierher ziehen oder wählen</span>
				<span class="mt-1 text-xs text-emerald-700">mehrere Ordner gleichzeitig ziehen ist möglich</span>
			{/if}
			<input
				type="file"
				class="hidden"
				multiple
				disabled={planning}
				onchange={onInput}
				{...{ webkitdirectory: true }}
			/>
		</label>
	{/if}

	{#if error}
		<p class="mt-2 rounded-lg bg-red-50 px-3 py-2 text-xs text-red-700">{error}</p>
	{/if}

	{#if plan && rows.length}
		<div class="mb-3 grid grid-cols-2 gap-2 text-xs sm:grid-cols-4">
			<div class="rounded bg-gray-50 px-3 py-2">
				<p class="text-lg font-semibold text-gray-800">{totals.products}</p>
				<p class="text-gray-500">Produkte auf {totals.machines} Maschinen</p>
			</div>
			<div class="rounded bg-gray-50 px-3 py-2">
				<p class="text-lg font-semibold text-gray-800">{totals.cnc}</p>
				<p class="text-gray-500">CNC-Programme</p>
			</div>
			<div class="rounded bg-gray-50 px-3 py-2">
				<p class="text-lg font-semibold text-gray-800">{totals.pdfs}</p>
				<p class="text-gray-500">Einstellblätter (PDF)</p>
			</div>
			<div class="rounded bg-gray-50 px-3 py-2">
				<p class="text-lg font-semibold text-gray-800">{totals.photos}</p>
				<p class="text-gray-500">Fotos</p>
			</div>
		</div>

		<p class="mb-3 text-xs text-gray-500">
			{#if totals.docs}{totals.docs} .doc-Fassungen bleiben lokal (nur PDFs zählen).{/if}
			{#if Object.keys(plan.skipped).length || plan.unassigned}
				Übersprungen:
				{Object.entries(plan.skipped)
					.map(([k, n]) => `${n} ${SKIP_LABELS[k] ?? k}`)
					.join(', ')}{plan.unassigned ? `, ${plan.unassigned} ohne Produktordner` : ''}.
			{/if}
		</p>

		<div class="max-h-96 overflow-y-auto rounded border border-gray-100">
			<table class="w-full text-xs">
				<thead class="sticky top-0 bg-gray-50 text-left uppercase text-gray-400">
					<tr>
						<th class="px-3 py-2 font-medium">Maschine</th>
						<th class="px-3 py-2 font-medium">Produkt</th>
						<th class="px-3 py-2 font-medium">Dateien</th>
						<th class="px-3 py-2 font-medium">Status</th>
					</tr>
				</thead>
				<tbody class="divide-y divide-gray-100">
					{#each rows as row (rowKey(row.product))}
						{@const p = row.product}
						<tr>
							<td class="px-3 py-2 text-gray-600">
								{#if p.machine_choices?.length}
									<select
										value={row.machine ?? ''}
										disabled={uploading}
										onchange={(e) => chooseMachine(row, e.currentTarget.value)}
										class="rounded border border-amber-300 px-1.5 py-1 text-xs"
									>
										<option value="" disabled>Maschine wählen …</option>
										{#each p.machine_choices as c}
											<option value={c.machine}>{c.machine}{c.isNew ? ' (neu)' : ''}</option>
										{/each}
									</select>
								{:else}
									{row.machine ?? '–'}
								{/if}
								{#if row.category}<span class="block text-[10px] text-gray-400">{row.category}</span>{/if}
							</td>
							<td class="px-3 py-2 font-mono text-gray-800">{p.product_id}</td>
							<td class="px-3 py-2 text-gray-500">
								{p.cnc_candidates.length} CNC{p.stueckliste ? ' · Stückliste' : ''} ·
								{p.einstellblaetter.length} PDF · {p.bilder.length} Fotos
								{#if p.variants.length}
									<span class="block text-[10px] text-gray-400">
										{p.variants.length} Vorrichtung/Nacharbeit (markiert)
									</span>
								{/if}
							</td>
							<td class="px-3 py-2">
								{#if row.status === 'läuft'}
									<span class="animate-pulse text-blue-600">{row.msg}</span>
								{:else if row.status === 'ok'}
									<span class="text-green-700" title={row.msg}>✓ {row.msg}</span>
								{:else if row.status === 'fehler'}
									<span class="text-red-600" title={row.msg}>✗ {row.msg.slice(0, 90)}</span>
								{:else if needsChoice(row)}
									<span class="text-amber-600">Maschine wählen</span>
								{:else}
									<span class="text-gray-400">{row.msg || 'wartet'}</span>
								{/if}
								{#if row.status !== 'ok'}
									{#each p.warnings as w}
										<span class="block text-[10px] text-amber-600">⚠ {w}</span>
									{/each}
								{/if}
							</td>
						</tr>
					{/each}
				</tbody>
			</table>
		</div>

		<div class="mt-3 flex flex-wrap items-center gap-3">
			{#if uploading}
				<span class="text-xs text-gray-500">{finished}/{rows.length} Produkte</span>
				<button
					onclick={() => (stopRequested = true)}
					disabled={stopRequested}
					class="rounded-lg border border-gray-300 px-3 py-1.5 text-xs text-gray-600 hover:bg-gray-50 disabled:opacity-60"
				>
					{stopRequested ? 'Stoppt nach der aktuellen Datei …' : 'Anhalten'}
				</button>
			{:else if remaining === 0}
				<span class="text-sm font-medium text-green-700">✓ Alle {rows.length} Produkte hochgeladen</span>
			{:else}
				<button
					onclick={uploadAll}
					disabled={planning}
					class="rounded-lg bg-emerald-600 px-4 py-2 text-sm font-medium text-white hover:bg-emerald-700 disabled:opacity-60"
				>
					{finished ? `${remaining} verbleibende hochladen` : `${rows.length} Produkte hochladen`}
				</button>
				<label class="flex items-center gap-2 text-xs text-gray-600">
					<input type="checkbox" bind:checked={includeDocs} class="h-4 w-4 rounded border-gray-300" />
					Einstellblätter & Fotos mit einlesen
				</label>
			{/if}
			{#if !uploading}
				<label class="cursor-pointer text-xs text-emerald-700 hover:underline">
					+ Weitere Ordner
					<input
						type="file"
						class="hidden"
						multiple
						disabled={planning}
						onchange={onInput}
						{...{ webkitdirectory: true }}
					/>
				</label>
				<button onclick={reset} class="text-xs text-gray-500 hover:underline">Auswahl leeren</button>
			{/if}
		</div>
		{#if includeDocs && (totals.pdfs || totals.photos) && !finished}
			<p class="mt-2 text-xs text-gray-400">
				Zuerst werden die CNC-Programme aller Produkte eingelesen (Sekunden), danach Einstellblätter
				und Fotos – jedes durchläuft Texterkennung bzw. Bildanalyse und dauert deutlich länger. Die
				Seite muss dafür offen bleiben; „Anhalten" und später fortsetzen ist jederzeit möglich.
			</p>
		{/if}
	{/if}
</div>
