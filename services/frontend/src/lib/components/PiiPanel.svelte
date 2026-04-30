<script lang="ts">
	import { app, freshPiiState } from '$lib/state.svelte';
	import { scanTable, getPiiModel, type PiiDetection, type PiiType } from '$lib/pii-scanner';
	import { detectionKey, approveAll } from '$lib/pii-anonymizer';

	const TYPE_LABEL: Record<PiiType, string> = {
		NAME: 'Personen',
		ORG: 'Organisationen',
		LOC: 'Orte',
		ADDRESS: 'Adressen',
		EMAIL: 'E-Mail',
		PHONE: 'Telefon',
		URL: 'URLs',
		DATE: 'Datum',
		ACCOUNT: 'Kontonummer',
		SECRET: 'Secrets',
		MISC: 'Sonstige'
	};

	const TYPE_COLOR: Record<PiiType, string> = {
		NAME: 'bg-rose-100 text-rose-700',
		ORG: 'bg-amber-100 text-amber-700',
		LOC: 'bg-sky-100 text-sky-700',
		ADDRESS: 'bg-cyan-100 text-cyan-700',
		EMAIL: 'bg-violet-100 text-violet-700',
		PHONE: 'bg-emerald-100 text-emerald-700',
		URL: 'bg-teal-100 text-teal-700',
		DATE: 'bg-orange-100 text-orange-700',
		ACCOUNT: 'bg-fuchsia-100 text-fuchsia-700',
		SECRET: 'bg-red-100 text-red-700',
		MISC: 'bg-gray-100 text-gray-700'
	};

	function progressPct(): number {
		const p = app.pii;
		if (p.total === 0) return 0;
		return Math.min(100, Math.round((p.progress / p.total) * 100));
	}

	async function runScan() {
		if (!app.parsedTable) return;
		app.pii = freshPiiState();
		app.pii.status = 'loading-model';
		app.audit.scanStartedAt = new Date().toISOString();
		app.audit.scanCompletedAt = null;
		try {
			const detections = await scanTable(app.parsedTable, (p) => {
				app.pii.status = p.stage === 'loading-model' ? 'loading-model' : 'scanning';
				app.pii.progress = p.processed;
				app.pii.total = p.total;
			});
			app.pii.detections = detections;
			app.pii.approvals = approveAll(detections);
			app.pii.status = 'done';
			app.audit.scanCompletedAt = new Date().toISOString();
			app.anonymizeMode = 'auto';
		} catch (err) {
			app.pii.status = 'error';
			app.pii.error =
				err instanceof Error ? err.message : 'Scan fehlgeschlagen.';
		}
	}

	function setMode(mode: 'off' | 'auto' | 'manual') {
		app.anonymizeMode = mode;
		if (mode === 'auto') {
			app.pii.approvals = approveAll(app.pii.detections);
		} else if (mode === 'off') {
			app.pii.approvals = new Set<string>();
		}
		// manual: leave approvals as-is (last user state, or all-approved from auto)
	}

	function toggle(d: PiiDetection) {
		const key = detectionKey(d);
		const next = new Set(app.pii.approvals);
		if (next.has(key)) next.delete(key);
		else next.add(key);
		app.pii.approvals = next;
	}

	function approveType(type: PiiType, approve: boolean) {
		const next = new Set(app.pii.approvals);
		for (const d of app.pii.detections) {
			if (d.type !== type) continue;
			const key = detectionKey(d);
			if (approve) next.add(key);
			else next.delete(key);
		}
		app.pii.approvals = next;
	}

	const grouped = $derived.by(() => {
		const out: Partial<Record<PiiType, PiiDetection[]>> = {};
		for (const d of app.pii.detections) {
			(out[d.type] ??= []).push(d);
		}
		return out;
	});

	const approvedCount = $derived(app.pii.approvals.size);
	const totalCount = $derived(app.pii.detections.length);

	function rowColLabel(d: PiiDetection): string {
		const headers = app.parsedTable?.headers ?? [];
		const colName = headers[d.colIdx] ?? `Spalte ${d.colIdx + 1}`;
		return `Z${d.rowIdx + 1} · ${colName}`;
	}
</script>

<section class="space-y-4 rounded-lg border border-gray-200 bg-white p-4 shadow-sm">
	<div class="flex flex-wrap items-center justify-between gap-3">
		<div>
			<h3 class="text-sm font-semibold text-gray-800">Lokale PII-Anonymisierung</h3>
			<p class="text-[11px] text-gray-400">
				Modell <span class="font-mono">{getPiiModel()}</span> · läuft im Browser, keine Daten verlassen das Gerät.
			</p>
		</div>

		{#if app.pii.status === 'idle' || app.pii.status === 'error'}
			<button
				onclick={runScan}
				class="rounded-lg bg-blue-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-blue-700"
			>
				Scan starten
			</button>
		{:else if app.pii.status === 'done'}
			<button
				onclick={runScan}
				class="rounded-lg border border-gray-300 bg-white px-3 py-1.5 text-xs text-gray-600 hover:bg-gray-50"
			>
				Erneut scannen
			</button>
		{/if}
	</div>

	{#if app.pii.status === 'loading-model'}
		<div class="text-xs text-gray-500">Lade Modell… (kann beim ersten Mal etwas dauern)</div>
	{:else if app.pii.status === 'scanning'}
		<div>
			<div class="mb-1 text-xs text-gray-500">
				Scanne Zellen … {app.pii.progress}/{app.pii.total}
			</div>
			<div class="h-1.5 w-full rounded-full bg-gray-100">
				<div
					class="h-1.5 rounded-full bg-blue-500 transition-all"
					style="width: {progressPct()}%"
				></div>
			</div>
		</div>
	{:else if app.pii.status === 'error'}
		<div class="rounded-lg border border-red-200 bg-red-50 p-3 text-xs text-red-700">
			Scan fehlgeschlagen: {app.pii.error}
		</div>
	{:else if app.pii.status === 'done'}
		<div class="space-y-3">
			<div class="flex flex-wrap items-center gap-2">
				<span class="text-xs text-gray-500">Modus:</span>
				{#each [{ id: 'off', label: 'Original' }, { id: 'auto', label: 'Vollständig anonymisieren' }, { id: 'manual', label: 'Selektiv' }] as opt}
					<button
						class="rounded-full px-3 py-1 text-xs font-medium transition-colors
							{app.anonymizeMode === opt.id
								? 'bg-blue-600 text-white'
								: 'border border-gray-300 bg-white text-gray-600 hover:bg-gray-50'}"
						onclick={() => setMode(opt.id as 'off' | 'auto' | 'manual')}
					>
						{opt.label}
					</button>
				{/each}
				<span class="ml-auto text-[11px] text-gray-400">
					{approvedCount}/{totalCount} Treffer maskiert
				</span>
			</div>

			{#if totalCount === 0}
				<p class="text-xs text-gray-400">Keine PII-Treffer gefunden.</p>
			{:else}
				<div class="space-y-3">
					{#each Object.entries(grouped) as [type, list]}
						{@const t = type as PiiType}
						<div>
							<div class="mb-1.5 flex items-center justify-between">
								<div class="flex items-center gap-2">
									<span class="rounded px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider {TYPE_COLOR[t]}">
										{TYPE_LABEL[t]}
									</span>
									<span class="text-[11px] text-gray-400">{list.length}</span>
								</div>
								{#if app.anonymizeMode === 'manual'}
									<div class="flex gap-1">
										<button
											class="text-[11px] text-gray-500 hover:text-gray-800"
											onclick={() => approveType(t, true)}
										>Alle</button>
										<span class="text-[11px] text-gray-300">·</span>
										<button
											class="text-[11px] text-gray-500 hover:text-gray-800"
											onclick={() => approveType(t, false)}
										>Keine</button>
									</div>
								{/if}
							</div>
							<ul class="space-y-1">
								{#each list as d}
									{@const key = detectionKey(d)}
									{@const checked = app.pii.approvals.has(key)}
									<li class="flex items-center gap-2 rounded border border-gray-100 bg-gray-50 px-2 py-1 text-xs">
										<input
											type="checkbox"
											{checked}
											disabled={app.anonymizeMode !== 'manual'}
											onchange={() => toggle(d)}
											class="h-3.5 w-3.5 rounded border-gray-300"
										/>
										<span class="font-mono text-gray-800 truncate" title={d.text}>{d.text}</span>
										<span class="ml-auto shrink-0 text-[10px] text-gray-400">{rowColLabel(d)}</span>
									</li>
								{/each}
							</ul>
						</div>
					{/each}
				</div>
			{/if}
		</div>
	{/if}
</section>
