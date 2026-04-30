<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import {
		parseFileToTable,
		MultipleSheetsError,
		UnsupportedFileError,
		EmptyFileError,
		type ParsedTable
	} from '$lib/file-parser';
	import DataTable from './DataTable.svelte';

	interface Props {
		url: string;
		fileName: string;
		onClose: () => void;
	}

	let { url, fileName, onClose }: Props = $props();

	let parsed = $state<ParsedTable | null>(null);
	let loading = $state(true);
	let error = $state<string | null>(null);

	function getExtension(name: string): string {
		const idx = name.lastIndexOf('.');
		return idx >= 0 ? name.slice(idx + 1).toLowerCase() : '';
	}

	async function load() {
		loading = true;
		error = null;
		parsed = null;
		try {
			const resp = await fetch(url);
			if (!resp.ok) {
				throw new Error(`Datei konnte nicht geladen werden (HTTP ${resp.status}).`);
			}
			const blob = await resp.blob();
			const ext = getExtension(fileName);
			const mime =
				ext === 'csv'
					? 'text/csv'
					: ext === 'xlsx'
					? 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
					: ext === 'xls'
					? 'application/vnd.ms-excel'
					: blob.type;
			const file = new File([blob], fileName, { type: mime });
			parsed = await parseFileToTable(file);
		} catch (err) {
			if (
				err instanceof MultipleSheetsError ||
				err instanceof UnsupportedFileError ||
				err instanceof EmptyFileError
			) {
				error = err.message;
			} else {
				error = err instanceof Error ? err.message : 'Vorschau fehlgeschlagen.';
			}
		} finally {
			loading = false;
		}
	}

	function onKey(e: KeyboardEvent) {
		if (e.key === 'Escape') onClose();
	}

	function onBackdropClick(e: MouseEvent) {
		if (e.target === e.currentTarget) onClose();
	}

	onMount(() => {
		window.addEventListener('keydown', onKey);
		load();
	});

	onDestroy(() => {
		window.removeEventListener('keydown', onKey);
	});
</script>

<div
	class="fixed inset-0 z-50 flex items-center justify-center bg-gray-900/60 p-4"
	role="dialog"
	aria-modal="true"
	aria-label="Tabellen-Vorschau"
	onclick={onBackdropClick}
	onkeydown={(e) => {
		if (e.key === 'Enter') onBackdropClick(e as unknown as MouseEvent);
	}}
	tabindex="-1"
>
	<div class="flex h-[85vh] w-full max-w-6xl flex-col overflow-hidden rounded-xl bg-white shadow-xl">
		<header class="flex items-center justify-between gap-3 border-b border-gray-200 px-5 py-3">
			<div class="min-w-0">
				<h2 class="truncate text-sm font-semibold text-gray-800" title={fileName}>
					{fileName}
				</h2>
				{#if parsed}
					<p class="text-[11px] text-gray-400">
						{parsed.sourceType.toUpperCase()}
						{#if parsed.sheetName}· Sheet <span class="font-mono">{parsed.sheetName}</span>{/if}
						· {parsed.rows.length} Zeilen · {parsed.headers.length} Spalten
					</p>
				{/if}
			</div>
			<div class="flex items-center gap-2">
				<a
					href={url}
					target="_blank"
					rel="noopener"
					class="rounded-lg border border-gray-300 bg-white px-3 py-1.5 text-xs text-gray-600 hover:bg-gray-50"
					title="Originaldatei in neuem Tab öffnen"
				>
					Original ↗
				</a>
				<button
					onclick={onClose}
					class="rounded-lg border border-gray-300 bg-white px-3 py-1.5 text-xs text-gray-600 hover:bg-gray-50"
					aria-label="Schließen"
				>
					Schließen
				</button>
			</div>
		</header>

		<div class="flex-1 overflow-auto p-4">
			{#if loading}
				<div class="rounded-lg border border-gray-200 bg-gray-50 p-6 text-sm text-gray-400 animate-pulse">
					Lade Datei und parse Tabelle …
				</div>
			{:else if error}
				<div class="rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-700">
					{error}
				</div>
			{:else if parsed}
				<DataTable
					columns={parsed.headers}
					data={parsed.rows}
					maxRows={500}
				/>
			{/if}
		</div>
	</div>
</div>
