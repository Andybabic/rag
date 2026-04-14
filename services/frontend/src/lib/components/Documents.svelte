<script lang="ts">
	import { app } from '$lib/state.svelte';
	import { getDocuments } from '$lib/api';

	let documents: Array<{
		id: string;
		file_name: string;
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
										{#if doc.stored_path}
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
									</div>
									<span class="rounded-full px-2 py-0.5 text-[10px] font-semibold
										{doc.status === 'ok' ? 'bg-green-100 text-green-700' : 'bg-red-100 text-red-700'}">
										{doc.status}
									</span>
								</div>
							</div>
						{/each}
					</div>
				{/if}
			</section>
		{/if}
	</div>
</main>
