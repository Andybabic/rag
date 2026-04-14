<script lang="ts">
	import { app } from '$lib/state.svelte';
	import { getUseCaseCollections } from '$lib/api';
	import FileUpload from './FileUpload.svelte';

	const useCases = [
		{ id: 'neumann', label: 'Firma Neumann', desc: 'Maschinenwartung', color: 'bg-blue-600' },
		{
			id: 'gw_stpoelten',
			label: 'GW St. Poelten',
			desc: 'CNC-Ruestung',
			color: 'bg-emerald-600'
		},
		{
			id: 'wiener_linien',
			label: 'Wiener Linien',
			desc: 'Wissensassistent',
			color: 'bg-purple-600'
		}
	];

	function select(id: string) {
		app.useCase = id;
		if (id !== 'wiener_linien') {
			app.role = 'default';
		}
	}

	// Reactive: fetch collections whenever use case changes
	$effect(() => {
		const uc = app.useCase;
		app.collectionsLoading = true;
		getUseCaseCollections(uc)
			.then((data) => {
				app.availableCollections = data.collections ?? [];
			})
			.catch(() => {
				app.availableCollections = [];
			})
			.finally(() => {
				app.collectionsLoading = false;
			});
	});

	const activeUseCase = $derived(useCases.find((u) => u.id === app.useCase));
</script>

<aside class="flex h-full w-80 shrink-0 flex-col bg-gray-900 text-white">
	<!-- Header -->
	<div class="border-b border-gray-700 p-5">
		<h1 class="text-lg font-bold tracking-tight">RAG Platform</h1>
		<p class="mt-1 text-xs text-gray-400">Prototyp v0.1</p>
	</div>

	<!-- Use Case Cards -->
	<div class="space-y-2 p-4">
		<p class="mb-2 text-xs uppercase tracking-wider text-gray-400">Use Case</p>
		{#each useCases as uc}
			<button
				class="w-full rounded-lg p-3 text-left transition-colors {app.useCase === uc.id
					? uc.color + ' text-white shadow-lg'
					: 'bg-gray-800 text-gray-300 hover:bg-gray-700'}"
				onclick={() => select(uc.id)}
			>
				<div class="text-sm font-medium">{uc.label}</div>
				<div class="text-xs opacity-75">{uc.desc}</div>
			</button>
		{/each}
	</div>

	<!-- Locked Use Case Badge -->
	{#if activeUseCase}
		<div class="px-4 pb-2">
			<div class="flex items-center gap-2 rounded-lg border border-gray-700 bg-gray-800/50 px-3 py-2.5">
				<svg class="h-4 w-4 shrink-0 text-red-400" viewBox="0 0 20 20" fill="currentColor">
					<path fill-rule="evenodd" d="M5 9V7a5 5 0 0110 0v2a2 2 0 012 2v5a2 2 0 01-2 2H5a2 2 0 01-2-2v-5a2 2 0 012-2zm8-2v2H7V7a3 3 0 016 0z" clip-rule="evenodd" />
				</svg>
				<div class="min-w-0 flex-1">
					<div class="truncate text-xs font-semibold text-white">{activeUseCase.label}</div>
					<div class="text-[10px] text-gray-400">Use Case fixiert</div>
				</div>
				<span class="shrink-0 rounded bg-red-600/80 px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-wider text-white">
					locked
				</span>
			</div>
		</div>
	{/if}

	<!-- Available Sub-Collections -->
	<div class="px-4 pb-4">
		<p class="mb-2 text-xs uppercase tracking-wider text-gray-400">
			Verfuegbare Quellen
			{#if app.collectionsLoading}
				<span class="ml-1 animate-pulse text-gray-500">...</span>
			{/if}
		</p>
		{#if app.availableCollections.length > 0}
			<div class="flex flex-wrap gap-1.5">
				{#each app.availableCollections as col}
					<span
						class="inline-flex items-center gap-1 rounded-full border border-gray-700 bg-gray-800 px-2.5 py-1 text-[11px] text-gray-300"
						title="{col.name}: {col.count} Chunks, {col.dimension}D"
					>
						<span class="h-1.5 w-1.5 rounded-full bg-emerald-400"></span>
						{col.name}
						<span class="text-gray-500">({col.count})</span>
					</span>
				{/each}
			</div>
			<p class="mt-2 text-[10px] text-gray-500">
				Der Agent sucht frei in diesen Quellen.
			</p>
		{:else if !app.collectionsLoading}
			<p class="text-xs text-gray-500">Keine Collections vorhanden.</p>
		{/if}
	</div>

	<!-- Role selector (Wiener Linien only) -->
	{#if app.useCase === 'wiener_linien'}
		<div class="px-4 pb-4">
			<p class="mb-2 text-xs uppercase tracking-wider text-gray-400">Rolle</p>
			<select
				bind:value={app.role}
				class="w-full rounded-lg border border-gray-600 bg-gray-800 px-3 py-2 text-sm text-gray-200 focus:border-purple-400 focus:outline-none"
			>
				<option value="default">Fachpersonal</option>
				<option value="trainee">Auszubildender</option>
			</select>
		</div>
	{/if}

	<!-- File Upload -->
	<div class="px-4 pb-4">
		<FileUpload />
	</div>

	<!-- Session Info -->
	<div class="mt-auto border-t border-gray-700 p-4">
		<p class="truncate text-xs text-gray-500">Session: {app.sessionId.slice(0, 8)}...</p>
	</div>
</aside>
