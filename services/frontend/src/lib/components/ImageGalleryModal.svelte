<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import { getDocumentImages } from '$lib/api';

	interface ImageEntry {
		image_id: string;
		page: number;
		alt_text: string;
		text_before: string;
		text_after: string;
		url: string;
	}

	interface Props {
		useCase: string;
		fileHash: string;
		fileName: string;
		onClose: () => void;
	}

	let { useCase, fileHash, fileName, onClose }: Props = $props();

	let images = $state<ImageEntry[]>([]);
	let loading = $state(true);
	let error = $state<string | null>(null);
	let showContext = $state(false);

	async function load() {
		loading = true;
		error = null;
		try {
			const data = await getDocumentImages(useCase, fileHash);
			images = (data.images ?? []) as ImageEntry[];
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
			images = [];
		}
		loading = false;
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
	aria-label="Bilder-Vorschau"
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
					Bilder · {fileName}
				</h2>
				<p class="text-[11px] text-gray-400">
					{#if loading}Lade…{:else}{images.length} Bild{images.length === 1 ? '' : 'er'}{/if}
				</p>
			</div>
			<div class="flex items-center gap-2">
				<label class="flex items-center gap-1.5 text-xs text-gray-600">
					<input type="checkbox" bind:checked={showContext} class="h-3.5 w-3.5" />
					Vor/Nach-Kontext zeigen
				</label>
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
					Lade Bilder …
				</div>
			{:else if error}
				<div class="rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-700">
					{error}
				</div>
			{:else if images.length === 0}
				<div class="rounded-lg border border-gray-200 bg-gray-50 p-6 text-sm text-gray-500">
					Zu diesem Dokument sind keine Bilder gespeichert. Falls die PDF Bilder enthält,
					wurden sie evtl. vor dieser Version eingespielt — bitte erneut hochladen.
				</div>
			{:else}
				<div class="grid gap-4 sm:grid-cols-2">
					{#each images as img}
						<article class="flex flex-col rounded-lg border border-gray-200 bg-white p-3 shadow-sm">
							<div class="flex items-center justify-between text-[11px] text-gray-500">
								<span class="font-mono">{img.image_id}</span>
								<span class="rounded-full bg-gray-100 px-2 py-0.5 font-semibold text-gray-600">
									S. {img.page}
								</span>
							</div>
							<a
								href={img.url}
								target="_blank"
								rel="noopener"
								class="mt-2 block overflow-hidden rounded-md border border-gray-100 bg-gray-50"
								title="Bild in neuem Tab öffnen"
							>
								<img
									src={img.url}
									alt={img.alt_text || img.image_id}
									class="max-h-72 w-full object-contain"
									loading="lazy"
								/>
							</a>
							<p class="mt-3 whitespace-pre-wrap text-sm text-gray-800">
								{img.alt_text || '(keine Beschreibung generiert)'}
							</p>
							{#if showContext && (img.text_before || img.text_after)}
								<div class="mt-3 space-y-2 border-t border-gray-100 pt-3 text-[11px] text-gray-500">
									{#if img.text_before}
										<div>
											<div class="mb-1 font-semibold uppercase tracking-wider text-gray-400">
												Text davor
											</div>
											<p class="whitespace-pre-wrap">{img.text_before}</p>
										</div>
									{/if}
									{#if img.text_after}
										<div>
											<div class="mb-1 font-semibold uppercase tracking-wider text-gray-400">
												Text danach
											</div>
											<p class="whitespace-pre-wrap">{img.text_after}</p>
										</div>
									{/if}
								</div>
							{/if}
						</article>
					{/each}
				</div>
			{/if}
		</div>
	</div>
</div>
