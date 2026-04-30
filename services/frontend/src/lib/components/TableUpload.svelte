<script lang="ts">
	import { app, freshPiiState, freshReleaseState, freshAuditTrail } from '$lib/state.svelte';
	import {
		parseFileToTable,
		MultipleSheetsError,
		UnsupportedFileError,
		EmptyFileError
	} from '$lib/file-parser';
	import { buildCellTransform } from '$lib/pii-anonymizer';
	import DataTable from './DataTable.svelte';
	import PiiPanel from './PiiPanel.svelte';
	import ReleasePanel from './ReleasePanel.svelte';
	import StepIndicator, { type Step, type StepStatus } from './StepIndicator.svelte';

	let dragover = $state(false);

	function resetSession() {
		app.parsedTable = null;
		app.tableError = null;
		app.pii = freshPiiState();
		app.anonymizeMode = 'off';
		app.release = freshReleaseState();
		app.audit = freshAuditTrail();
	}

	async function handleFiles(files: FileList | null) {
		if (!files || files.length === 0) return;
		const file = files[0];

		app.tableLoading = true;
		resetSession();

		try {
			const parsed = await parseFileToTable(file);
			app.parsedTable = parsed;
			app.audit.fileLoadedAt = new Date().toISOString();
		} catch (err) {
			if (
				err instanceof MultipleSheetsError ||
				err instanceof UnsupportedFileError ||
				err instanceof EmptyFileError
			) {
				app.tableError = err.message;
			} else {
				app.tableError =
					err instanceof Error ? err.message : 'Datei konnte nicht eingelesen werden.';
			}
		} finally {
			app.tableLoading = false;
		}
	}

	function onDrop(e: DragEvent) {
		e.preventDefault();
		dragover = false;
		handleFiles(e.dataTransfer?.files ?? null);
	}

	function onDragOver(e: DragEvent) {
		e.preventDefault();
		dragover = true;
	}

	function onFileInput(e: Event) {
		const target = e.target as HTMLInputElement;
		handleFiles(target.files);
		target.value = '';
	}

	const cellRenderer = $derived.by(() => {
		if (app.anonymizeMode === 'off') return undefined;
		return buildCellTransform({
			detections: app.pii.detections,
			approvals: app.pii.approvals,
			placeholders: app.pii.placeholders
		});
	});

	const anonymize = $derived(app.anonymizeMode !== 'off');

	function uploadStep(): StepStatus {
		if (app.tableError) return 'error';
		if (app.parsedTable) return 'done';
		if (app.tableLoading) return 'current';
		return 'current';
	}

	function scanStep(): StepStatus {
		if (!app.parsedTable) return 'pending';
		if (app.pii.status === 'done') return 'done';
		if (app.pii.status === 'error') return 'error';
		if (app.pii.status === 'loading-model' || app.pii.status === 'scanning')
			return 'current';
		return 'current';
	}

	function anonStep(): StepStatus {
		if (!app.parsedTable || app.pii.status !== 'done') return 'pending';
		// Once the user picked any explicit mode (or scan finished), this step is "done"
		// in the sense that it's a deliberate choice. We treat 'done' here generously.
		if (
			app.release.status === 'released' ||
			app.release.status === 'importing' ||
			app.release.status === 'imported'
		)
			return 'done';
		return 'current';
	}

	function releaseStep(): StepStatus {
		if (app.release.status === 'imported') return 'done';
		if (app.release.status === 'error') return 'error';
		if (app.release.status === 'importing' || app.release.status === 'released')
			return 'current';
		if (app.parsedTable && app.pii.status === 'done') return 'current';
		return 'pending';
	}

	const steps = $derived<Step[]>([
		{ id: 'upload', label: 'Upload', status: uploadStep() },
		{ id: 'scan', label: 'Scan', status: scanStep() },
		{ id: 'anon', label: 'Anonymisieren', status: anonStep() },
		{ id: 'release', label: 'Freigabe & Import', status: releaseStep() }
	]);
</script>

<section class="space-y-5">
	<div class="flex items-center justify-between">
		<h2 class="text-lg font-semibold text-gray-800">Tabellen-Import</h2>
		{#if app.parsedTable}
			<button
				onclick={resetSession}
				class="rounded-lg border border-gray-300 bg-white px-3 py-1.5 text-xs text-gray-600 hover:bg-gray-50"
			>
				Session zurücksetzen
			</button>
		{/if}
	</div>

	<StepIndicator {steps} />

	<div
		class="cursor-pointer rounded-lg border-2 border-dashed p-6 text-center transition-colors
			{dragover ? 'border-blue-400 bg-blue-50' : 'border-gray-300 bg-white hover:border-gray-400'}"
		ondrop={onDrop}
		ondragover={onDragOver}
		ondragleave={() => (dragover = false)}
		role="button"
		tabindex="0"
	>
		<p class="text-sm text-gray-600">CSV oder Excel-Datei hierher ziehen</p>
		<p class="mt-1 text-xs text-gray-400">Erlaubt: .csv, .xlsx, .xls (Excel: genau 1 Sheet)</p>
		<label class="mt-3 inline-block cursor-pointer text-xs font-medium text-blue-600 hover:text-blue-700">
			Datei auswählen
			<input
				type="file"
				accept=".csv,.xlsx,.xls"
				class="hidden"
				onchange={onFileInput}
			/>
		</label>
	</div>

	{#if app.tableError}
		<div class="rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-700">
			{app.tableError}
		</div>
	{/if}

	{#if app.parsedTable}
		<div class="rounded-lg border border-gray-200 bg-white p-3 text-xs text-gray-500">
			<span class="font-medium text-gray-700">{app.parsedTable.fileName}</span>
			{#if app.parsedTable.sheetName}
				· Sheet <span class="font-mono text-gray-600">{app.parsedTable.sheetName}</span>
			{/if}
			· Format <span class="uppercase">{app.parsedTable.sourceType}</span>
			· {app.parsedTable.rows.length} Zeilen · {app.parsedTable.headers.length} Spalten
		</div>

		<PiiPanel />

		<DataTable
			columns={app.parsedTable.headers}
			data={app.parsedTable.rows}
			loading={app.tableLoading}
			{anonymize}
			{cellRenderer}
			caption={anonymize ? 'Vorschau (anonymisiert)' : 'Vorschau (Original)'}
			maxRows={200}
		/>

		<ReleasePanel />
	{:else if app.tableLoading}
		<DataTable columns={[]} data={[]} loading={true} />
	{/if}
</section>
