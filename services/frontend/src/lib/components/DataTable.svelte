<script lang="ts">
	import type { CellValue } from '$lib/file-parser';

	interface Props {
		columns: string[];
		data: CellValue[][];
		loading?: boolean;
		error?: string | null;
		anonymize?: boolean;
		/**
		 * Optional renderer used when `anonymize` is true. Receives the original
		 * cell text and returns the string to display (e.g. with [NAME_1]
		 * placeholders substituted in). When omitted, falls back to a generic
		 * character mask.
		 */
		cellRenderer?: (rowIdx: number, colIdx: number, text: string) => string;
		maxRows?: number;
		caption?: string;
	}

	let {
		columns,
		data,
		loading = false,
		error = null,
		anonymize = false,
		cellRenderer,
		maxRows,
		caption
	}: Props = $props();

	const visibleRows = $derived(maxRows && maxRows > 0 ? data.slice(0, maxRows) : data);
	const truncated = $derived(maxRows && maxRows > 0 && data.length > maxRows);

	function formatCell(v: CellValue): string {
		if (v === null || v === undefined) return '';
		if (typeof v === 'boolean') return v ? 'true' : 'false';
		return String(v);
	}

	function genericMask(s: string): string {
		if (s.length === 0) return '';
		if (s.length <= 2) return '•'.repeat(s.length);
		return s[0] + '•'.repeat(Math.max(1, s.length - 2)) + s[s.length - 1];
	}

	function renderCell(rowIdx: number, colIdx: number, v: CellValue): string {
		const original = formatCell(v);
		if (!anonymize) return original;
		if (cellRenderer) return cellRenderer(rowIdx, colIdx, original);
		return genericMask(original);
	}
</script>

<div class="w-full">
	{#if caption}
		<div class="mb-2 text-xs uppercase tracking-wider text-gray-400">{caption}</div>
	{/if}

	{#if loading}
		<div class="rounded-lg border border-gray-200 bg-white p-6 text-sm text-gray-400 animate-pulse">
			Lade Tabelle…
		</div>
	{:else if error}
		<div class="rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-700">
			{error}
		</div>
	{:else if columns.length === 0}
		<div class="rounded-lg border border-gray-200 bg-white p-6 text-sm text-gray-400">
			Keine Daten vorhanden.
		</div>
	{:else}
		<div class="overflow-auto rounded-lg border border-gray-200 bg-white shadow-sm">
			<table class="min-w-full divide-y divide-gray-200 text-sm">
				<thead class="bg-gray-50">
					<tr>
						<th
							class="sticky left-0 z-10 bg-gray-50 px-3 py-2 text-left text-[10px] font-semibold uppercase tracking-wider text-gray-400"
						>
							#
						</th>
						{#each columns as col}
							<th
								class="px-3 py-2 text-left text-xs font-semibold text-gray-600 whitespace-nowrap"
							>
								{col}
							</th>
						{/each}
					</tr>
				</thead>
				<tbody class="divide-y divide-gray-100">
					{#each visibleRows as row, i}
						<tr class="hover:bg-gray-50">
							<td
								class="sticky left-0 z-10 bg-white px-3 py-1.5 text-[11px] text-gray-400 hover:bg-gray-50"
							>
								{i + 1}
							</td>
							{#each columns as _, ci}
								<td class="px-3 py-1.5 text-gray-700 whitespace-nowrap">
									{renderCell(i, ci, row[ci])}
								</td>
							{/each}
						</tr>
					{/each}
				</tbody>
			</table>
		</div>

		<div class="mt-2 flex items-center justify-between text-xs text-gray-400">
			<span>
				{data.length} Zeile{data.length === 1 ? '' : 'n'} · {columns.length} Spalte{columns.length === 1
					? ''
					: 'n'}
			</span>
			{#if truncated}
				<span>Anzeige begrenzt auf {maxRows} Zeilen</span>
			{/if}
		</div>
	{/if}
</div>
