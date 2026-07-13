<script lang="ts">
	import { app } from '$lib/state.svelte';
	import { uploadFile, getImageProgress } from '$lib/api';

	let dragover = $state(false);
	let uploading = $state(false);
	let totalCount = $state(0);
	let doneCount = $state(0);

	// Poll background alt-text ("Bild-Interpretation") progress and reflect it
	// in the file's status line, e.g. "12 Chunks · 14/44 Bilder interpretiert".
	async function pollImageProgress(
		index: number,
		fileName: string,
		fileHash: string,
		chunkMsg: string,
		total: number
	) {
		const deadline = Date.now() + 30 * 60 * 1000;
		while (Date.now() < deadline) {
			await new Promise((r) => setTimeout(r, 2000));
			// Bail if the list was reset or a different file now sits at this slot.
			if (app.uploadResults[index]?.name !== fileName) return;
			let p;
			try {
				p = await getImageProgress(fileHash);
			} catch {
				continue;
			}
			const t = p.total || total;
			if (p.status === 'done' || (t > 0 && p.done >= t)) {
				app.uploadResults[index] = {
					...app.uploadResults[index],
					msg: `${chunkMsg} · ${t} Bilder interpretiert`
				};
				return;
			}
			// Job may not be registered yet (brief race) — keep the initial count.
			const done = p.status === 'unknown' ? 0 : p.done;
			app.uploadResults[index] = {
				...app.uploadResults[index],
				msg: `${chunkMsg} · ${done}/${t} Bilder interpretiert`
			};
		}
	}

	async function handleFiles(files: FileList | null) {
		if (!files || files.length === 0) return;
		const list = Array.from(files);
		uploading = true;
		app.uploadStatus = null;
		app.uploadResults = list.map((f) => ({ name: f.name, ok: false, msg: 'Wartet …', pending: true }));
		totalCount = list.length;
		doneCount = 0;

		// Upload sequentially – the cleaning + ingest pipeline is heavy and
		// running them in parallel can overwhelm a single Ollama instance.
		for (let i = 0; i < list.length; i++) {
			const file = list[i];
			try {
				const result = await uploadFile(file, app.useCase);
				const chunkMsg = `${result.chunks ?? 0} Chunks`;
				const imageCount = result.image_count ?? 0;
				app.uploadResults[i] = {
					name: file.name,
					ok: true,
					msg: imageCount > 0 ? `${chunkMsg} · 0/${imageCount} Bilder interpretiert` : chunkMsg
				};
				// Alt-text runs in the background — poll and show live progress
				// without blocking the next file's upload.
				if (imageCount > 0 && result.file_hash) {
					pollImageProgress(i, file.name, result.file_hash, chunkMsg, imageCount);
				}
			} catch (err) {
				const message = err instanceof Error ? err.message : 'Upload fehlgeschlagen';
				app.uploadResults[i] = { name: file.name, ok: false, msg: message };
			}
			doneCount = i + 1;
		}

		const okCount = app.uploadResults.filter((r) => r.ok).length;
		app.uploadStatus = {
			ok: okCount === list.length,
			msg: `${okCount}/${list.length} Dateien verarbeitet`
		};
		uploading = false;
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
		// Reset so re-selecting the same file fires onchange again
		target.value = '';
	}
</script>

<div>
	<p class="mb-2 text-xs uppercase tracking-wider text-gray-400">Datei-Upload</p>

	<div
		class="cursor-pointer rounded-lg border-2 border-dashed p-4 text-center transition-colors
		{dragover ? 'border-blue-400 bg-gray-800' : 'border-gray-600 hover:border-gray-500'}"
		ondrop={onDrop}
		ondragover={onDragOver}
		ondragleave={() => (dragover = false)}
		role="button"
		tabindex="0"
	>
		{#if uploading}
			<div class="text-sm text-gray-400">
				Verarbeite {doneCount}/{totalCount} …
			</div>
		{:else}
			<div class="mb-1 text-sm text-gray-400">PDFs hierher ziehen (mehrere möglich)</div>
			<label class="cursor-pointer text-xs text-blue-400 hover:text-blue-300">
				oder Dateien wählen
				<input
					type="file"
					accept=".pdf,.docx,.pptx,.txt"
					multiple
					class="hidden"
					onchange={onFileInput}
				/>
			</label>
		{/if}
	</div>

	{#if app.uploadResults.length > 0}
		<ul class="mt-2 space-y-1 text-xs">
			{#each app.uploadResults as r}
				<li class="flex items-center justify-between gap-2">
					<span class="truncate text-gray-300" title={r.name}>{r.name}</span>
					<span
						class={r.pending
							? 'text-gray-500'
							: r.ok
							? 'shrink-0 text-green-400'
							: 'shrink-0 text-red-400'}
						title={r.msg}
					>
						{r.pending ? '…' : r.ok ? `✓ ${r.msg}` : `✗ ${r.msg.slice(0, 60)}`}
					</span>
				</li>
			{/each}
		</ul>
	{/if}

	{#if app.uploadStatus && !uploading}
		<div class="mt-2 text-xs {app.uploadStatus.ok ? 'text-green-400' : 'text-yellow-400'}">
			{app.uploadStatus.msg}
		</div>
	{/if}
</div>
