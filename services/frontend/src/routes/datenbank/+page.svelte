<script lang="ts">
	import { onMount } from 'svelte';
	import { getCollections, getUseCaseCollections } from '$lib/api';
	import { USE_CASES, type UseCaseDef } from '$lib/use-cases';

	interface CollectionInfo {
		name: string;
		count: number;
		dimension: number;
	}

	interface UseCaseSection {
		uc: UseCaseDef;
		collections: CollectionInfo[];
		totalChunks: number;
	}

	let sections: UseCaseSection[] = $state([]);
	let orphanCollections: CollectionInfo[] = $state([]);
	let loading = $state(true);
	let totalChunks = $state(0);
	let totalCollections = $state(0);

	async function loadData() {
		loading = true;

		// Fetch all collections globally
		let allCollections: CollectionInfo[] = [];
		try {
			const data = await getCollections();
			allCollections = data.collections ?? [];
		} catch {
			allCollections = [];
		}

		totalCollections = allCollections.length;
		totalChunks = allCollections.reduce((sum, c) => sum + c.count, 0);

		// Fetch per-use-case collections
		const assigned = new Set<string>();
		const results: UseCaseSection[] = [];

		for (const uc of USE_CASES) {
			try {
				const data = await getUseCaseCollections(uc.apiId);
				const cols: CollectionInfo[] = data.collections ?? [];
				cols.forEach((c) => assigned.add(c.name));
				results.push({
					uc,
					collections: cols,
					totalChunks: cols.reduce((sum, c) => sum + c.count, 0)
				});
			} catch {
				results.push({ uc, collections: [], totalChunks: 0 });
			}
		}

		sections = results;

		// Orphan collections (not assigned to any use case)
		orphanCollections = allCollections.filter((c) => !assigned.has(c.name));

		loading = false;
	}

	onMount(loadData);
</script>

<svelte:head>
	<title>Datenbank – RAG Platform</title>
</svelte:head>

<div class="flex h-full">
	<!-- Minimal sidebar for navigation back -->
	<aside class="flex h-full w-80 shrink-0 flex-col bg-gray-900 text-white">
		<div class="border-b border-gray-700 p-5">
			<h1 class="text-lg font-bold tracking-tight">RAG Platform</h1>
			<p class="mt-1 text-xs text-gray-400">Prototyp v0.1</p>
		</div>

		<!-- Use Case Navigation -->
		<div class="space-y-2 p-4">
			<p class="mb-2 text-xs uppercase tracking-wider text-gray-400">Use Case</p>
			{#each USE_CASES as item}
				<a
					href="/{item.slug}"
					class="block w-full rounded-lg bg-gray-800 p-3 text-left text-gray-300 transition-colors hover:bg-gray-700"
				>
					<div class="text-sm font-medium">{item.label}</div>
					<div class="text-xs opacity-75">{item.desc}</div>
				</a>
			{/each}
		</div>

		<!-- Active: Datenbank -->
		<div class="px-4 pb-4">
			<div
				class="flex w-full items-center gap-2 rounded-lg border border-gray-600 bg-gray-700 p-3 text-sm font-medium text-white shadow-lg"
			>
				<svg class="h-4 w-4 text-blue-400" viewBox="0 0 20 20" fill="currentColor">
					<path
						d="M3 12v3c0 1.657 3.134 3 7 3s7-1.343 7-3v-3c0 1.657-3.134 3-7 3s-7-1.343-7-3z"
					/>
					<path
						d="M3 7v3c0 1.657 3.134 3 7 3s7-1.343 7-3V7c0 1.657-3.134 3-7 3S3 8.657 3 7z"
					/>
					<path d="M17 5c0 1.657-3.134 3-7 3S3 6.657 3 5s3.134-3 7-3 7 1.343 7 3z" />
				</svg>
				Datenbank
			</div>
		</div>

		<div class="mt-auto"></div>
	</aside>

	<!-- Main content -->
	<main class="flex-1 overflow-y-auto bg-gray-50">
		<div class="mx-auto max-w-5xl p-6 space-y-8">
			<!-- Header -->
			<div>
				<h2 class="text-xl font-bold text-gray-800">Datenbank-Uebersicht</h2>
				<p class="mt-1 text-sm text-gray-500">
					Gliederung aller Vektordatenbanken nach Use Case
				</p>
			</div>

			{#if loading}
				<p class="animate-pulse text-sm text-gray-400">Lade Datenbank-Informationen...</p>
			{:else}
				<!-- Global Stats -->
				<div class="grid gap-4 sm:grid-cols-3">
					<div class="rounded-xl border border-gray-200 bg-white p-5 shadow-sm">
						<div class="text-2xl font-bold text-gray-800">{totalCollections}</div>
						<div class="text-xs text-gray-500">Collections gesamt</div>
					</div>
					<div class="rounded-xl border border-gray-200 bg-white p-5 shadow-sm">
						<div class="text-2xl font-bold text-gray-800">
							{totalChunks.toLocaleString('de-AT')}
						</div>
						<div class="text-xs text-gray-500">Chunks gesamt</div>
					</div>
					<div class="rounded-xl border border-gray-200 bg-white p-5 shadow-sm">
						<div class="text-2xl font-bold text-gray-800">{USE_CASES.length}</div>
						<div class="text-xs text-gray-500">Use Cases</div>
					</div>
				</div>

				<!-- Per Use Case -->
				{#each sections as section}
					{@const uc = section.uc}
					<section class="rounded-xl border border-gray-200 bg-white shadow-sm overflow-hidden">
						<!-- Use case header -->
						<div class="flex items-center gap-3 border-b border-gray-100 px-5 py-4">
							<div
								class="h-3 w-3 rounded-full"
								style="background-color: {uc.accent}"
							></div>
							<div class="flex-1">
								<h3 class="text-sm font-bold text-gray-800">{uc.label}</h3>
								<p class="text-xs text-gray-400">{uc.desc}</p>
							</div>
							<div class="text-right">
								<div class="text-sm font-semibold text-gray-700">
									{section.totalChunks.toLocaleString('de-AT')} Chunks
								</div>
								<div class="text-[10px] text-gray-400">
									{section.collections.length} Collection{section.collections.length !== 1 ? 's' : ''}
								</div>
							</div>
							<a
								href="/{uc.slug}"
								class="rounded-lg border border-gray-200 px-3 py-1.5 text-xs text-gray-600 hover:bg-gray-50"
							>
								Oeffnen
							</a>
						</div>

						<!-- Collections tree -->
						{#if section.collections.length > 0}
							<div class="px-5 py-3 space-y-0">
								{#each section.collections as col, i}
									<div
										class="flex items-center gap-3 py-2
											{i < section.collections.length - 1 ? 'border-b border-gray-50' : ''}"
									>
										<!-- Tree connector -->
										<div class="flex items-center text-gray-300">
											{#if i < section.collections.length - 1}
												<span class="font-mono text-xs">&#9500;&#9472;</span>
											{:else}
												<span class="font-mono text-xs">&#9492;&#9472;</span>
											{/if}
										</div>

										<!-- Collection icon -->
										<svg
											class="h-4 w-4 shrink-0 text-gray-400"
											viewBox="0 0 20 20"
											fill="currentColor"
										>
											<path
												d="M3 12v3c0 1.657 3.134 3 7 3s7-1.343 7-3v-3c0 1.657-3.134 3-7 3s-7-1.343-7-3z"
											/>
											<path
												d="M3 7v3c0 1.657 3.134 3 7 3s7-1.343 7-3V7c0 1.657-3.134 3-7 3S3 8.657 3 7z"
											/>
											<path
												d="M17 5c0 1.657-3.134 3-7 3S3 6.657 3 5s3.134-3 7-3 7 1.343 7 3z"
											/>
										</svg>

										<!-- Name + stats -->
										<div class="flex-1">
											<span class="text-sm font-medium text-gray-700">{col.name}</span>
										</div>
										<span
											class="rounded-full bg-gray-100 px-2 py-0.5 text-[10px] font-semibold text-gray-600"
										>
											{col.count.toLocaleString('de-AT')} Chunks
										</span>
										<span class="text-[10px] text-gray-400">{col.dimension}D</span>
									</div>
								{/each}
							</div>
						{:else}
							<div class="px-5 py-4">
								<p class="text-xs text-gray-400">
									Keine Collections. Laden Sie Dokumente unter
									<a href="/{uc.slug}" class="text-blue-500 hover:underline">{uc.label}</a> hoch.
								</p>
							</div>
						{/if}
					</section>
				{/each}

				<!-- Orphan collections -->
				{#if orphanCollections.length > 0}
					<section class="rounded-xl border border-amber-200 bg-amber-50 shadow-sm overflow-hidden">
						<div class="flex items-center gap-3 border-b border-amber-100 px-5 py-4">
							<svg
								class="h-4 w-4 text-amber-500"
								viewBox="0 0 20 20"
								fill="currentColor"
							>
								<path
									fill-rule="evenodd"
									d="M8.257 3.099c.765-1.36 2.722-1.36 3.486 0l5.58 9.92c.75 1.334-.213 2.98-1.742 2.98H4.42c-1.53 0-2.493-1.646-1.743-2.98l5.58-9.92zM11 13a1 1 0 11-2 0 1 1 0 012 0zm-1-8a1 1 0 00-1 1v3a1 1 0 002 0V6a1 1 0 00-1-1z"
									clip-rule="evenodd"
								/>
							</svg>
							<div class="flex-1">
								<h3 class="text-sm font-bold text-amber-800">Nicht zugeordnete Collections</h3>
								<p class="text-xs text-amber-600">
									Diese Collections gehoeren keinem Use Case
								</p>
							</div>
						</div>
						<div class="px-5 py-3 space-y-2">
							{#each orphanCollections as col}
								<div class="flex items-center gap-3 py-1">
									<span class="text-sm text-amber-800">{col.name}</span>
									<span class="rounded-full bg-amber-100 px-2 py-0.5 text-[10px] text-amber-700">
										{col.count} Chunks
									</span>
								</div>
							{/each}
						</div>
					</section>
				{/if}
			{/if}
		</div>
	</main>
</div>
