<script lang="ts">
	import { sendFeedback } from '$lib/api';
	import type { Message } from '$lib/state.svelte';
	import { marked } from 'marked';

	let { message }: { message: Message } = $props();

	let showSteps = $state(false);
	let openSteps = $state<Set<number>>(new Set());
	// Auto-expand the steps panel while the agent is still streaming so the
	// user sees progress live; user can still collapse manually after.
	$effect(() => {
		if (message.streaming) {
			showSteps = true;
		}
	});
	let openChunks = $state<Set<string>>(new Set());
	let showSystemPrompt = $state(false);
	let showLlmResponse = $state<Set<number>>(new Set());
	let feedbackSent: string | null = $state(null);

	// PDF Preview
	let previewUrl = $state('');
	let previewBaseUrl = $state('');
	let previewTitle = $state('');
	let previewPage = $state(0);

	function openPreview(storedPath: string, fileName: string, page?: number) {
		const base = `/api/documents/${storedPath}`;
		previewBaseUrl = base;
		previewPage = page ?? 0;
		// #page=N works in Chrome/Firefox/Edge native PDF viewer
		// #page=N&view=Fit also helps with zoom
		previewUrl = previewPage ? `${base}#page=${previewPage}&view=Fit` : base;
		previewTitle = fileName + (previewPage ? ` – Seite ${previewPage}` : '');
	}

	function closePreview() {
		previewUrl = '';
		previewBaseUrl = '';
		previewTitle = '';
		previewPage = 0;
	}

	// Configure marked for inline rendering (no wrapping <p> for short texts)
	marked.setOptions({ breaks: true, gfm: true });

	async function onFeedback(rating: string) {
		if (feedbackSent) return;
		try {
			await sendFeedback(message.requestId ?? '', rating);
			feedbackSent = rating;
		} catch {
			feedbackSent = null;
		}
	}

	function toggleStep(stepNum: number) {
		if (openSteps.has(stepNum)) {
			openSteps.delete(stepNum);
		} else {
			openSteps.add(stepNum);
		}
		openSteps = new Set(openSteps);
	}

	function toggleChunk(key: string) {
		if (openChunks.has(key)) {
			openChunks.delete(key);
		} else {
			openChunks.add(key);
		}
		openChunks = new Set(openChunks);
	}

	function toggleLlmResponse(stepNum: number) {
		if (showLlmResponse.has(stepNum)) {
			showLlmResponse.delete(stepNum);
		} else {
			showLlmResponse.add(stepNum);
		}
		showLlmResponse = new Set(showLlmResponse);
	}

	function formatScore(n: number | undefined): string {
		if (n === undefined || n === null) return '–';
		return Math.abs(n) >= 10 ? n.toFixed(2) : n.toFixed(3);
	}

	function chunkSummary(chunk: { text?: string; metadata?: Record<string, unknown> }): string {
		const raw = (chunk.text ?? (chunk.metadata?.text as string | undefined) ?? '').trim();
		return raw.length > 120 ? raw.slice(0, 120) + '…' : raw || '(leerer Chunk)';
	}

	function chunkFullText(chunk: { text?: string; metadata?: Record<string, unknown> }): string {
		return (chunk.text ?? (chunk.metadata?.text as string | undefined) ?? '').trim();
	}

	function metadataWithoutText(meta: Record<string, unknown> | undefined): string {
		if (!meta) return '';
		const { text: _text, ...rest } = meta;
		return Object.keys(rest).length ? JSON.stringify(rest, null, 2) : '';
	}

	function formatAnswer(text: string): string {
		// First render markdown to HTML
		let html = marked.parse(text) as string;
		// Then add citation badges
		html = html.replace(
			/\[(\d+)]/g,
			'<span class="citation-badge inline-flex items-center justify-center w-5 h-5 rounded-full bg-blue-100 text-blue-700 text-xs font-bold cursor-pointer hover:bg-blue-200" data-ref="$1" title="Quelle $1 anzeigen">$1</span>'
		);
		return html;
	}

	function formatArgs(args: Record<string, unknown> | undefined): string {
		if (!args || Object.keys(args).length === 0) return '';
		try {
			return JSON.stringify(args, null, 2);
		} catch {
			return String(args);
		}
	}

	function scrollToCitation(ref: number) {
		const el = document.getElementById(`citation-${ref}`);
		if (el) {
			el.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
			el.classList.add('ring-2', 'ring-blue-400');
			setTimeout(() => el.classList.remove('ring-2', 'ring-blue-400'), 2000);
		}
	}

	function handleAnswerClick(e: MouseEvent) {
		const target = e.target as HTMLElement;
		if (target.classList.contains('citation-badge')) {
			const ref = parseInt(target.dataset.ref ?? '0');
			if (ref) scrollToCitation(ref);
		}
	}

	const actionLabels: Record<string, string> = {
		SEARCH: 'Suche in Vektordatenbank',
		SEARCH_CNC: 'CNC-Suche',
		CLARIFY: 'Rückfrage',
		RECALL_MEMORY: 'Gedächtnis abrufen',
		LOOKUP_SOURCES: 'Quellen auflisten',
		FINAL_ANSWER: 'Antwort formuliert'
	};

	const actionColors: Record<string, string> = {
		SEARCH: 'bg-purple-100 text-purple-700 border-purple-200',
		SEARCH_CNC: 'bg-purple-100 text-purple-700 border-purple-200',
		CLARIFY: 'bg-yellow-100 text-yellow-700 border-yellow-200',
		RECALL_MEMORY: 'bg-cyan-100 text-cyan-700 border-cyan-200',
		LOOKUP_SOURCES: 'bg-teal-100 text-teal-700 border-teal-200',
		FINAL_ANSWER: 'bg-green-100 text-green-700 border-green-200'
	};
</script>

{#if message.role === 'user'}
	<!-- User message -->
	<div class="flex justify-end">
		<div
			class="max-w-2xl rounded-2xl rounded-tr-sm bg-blue-600 px-4 py-3 text-white shadow-sm"
		>
			<p class="whitespace-pre-wrap text-sm">{message.text}</p>
		</div>
	</div>
{:else}
	<!-- Assistant message -->
	<div class="flex gap-3">
		<div
			class="max-w-2xl rounded-2xl rounded-tl-sm border bg-white px-4 py-3 shadow-sm
			{message.error ? 'border-red-200' : 'border-gray-200'}"
		>
			<!-- Live status while streaming -->
			{#if message.streaming}
				<div class="mb-2 flex items-center gap-2 text-sm text-gray-600">
					<svg class="h-4 w-4 animate-spin text-blue-500" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24">
						<circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
						<path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z"></path>
					</svg>
					<span>{message.currentPhase ?? 'Arbeite …'}</span>
				</div>
			{/if}

			<!-- Answer text (rendered as markdown) -->
			{#if message.text}
				<!-- svelte-ignore a11y_click_events_have_key_events -->
				<!-- svelte-ignore a11y_no_static_element_interactions -->
				<div
					class="prose prose-sm max-w-none text-sm prose-headings:text-sm prose-headings:font-semibold prose-p:my-1 prose-ul:my-1 prose-ol:my-1 prose-li:my-0"
					onclick={handleAnswerClick}
				>
					{@html formatAnswer(message.text)}
				</div>
			{/if}

			<!-- Searched Collections -->
			{#if message.searchedCollections && message.searchedCollections.length > 0}
				<div class="mt-2 flex flex-wrap items-center gap-1.5">
					<span class="text-[10px] font-semibold uppercase tracking-wider text-gray-400">
						Durchsucht:
					</span>
					{#each message.searchedCollections as col}
						<span
							class="inline-flex items-center gap-1 rounded-full border border-indigo-100 bg-indigo-50 px-2 py-0.5 text-[10px] font-medium text-indigo-600"
						>
							<span class="h-1 w-1 rounded-full bg-indigo-400"></span>
							{col}
						</span>
					{/each}
				</div>
			{/if}

			<!-- Citations / Sources -->
			{#if message.citations && message.citations.length > 0}
				<div class="mt-3 border-t border-gray-100 pt-3">
					<p class="mb-2 text-xs font-semibold uppercase tracking-wider text-gray-400">Quellen</p>
					<div class="space-y-2">
						{#each message.citations as cite, i}
							<div
								id="citation-{i + 1}"
								class="rounded-lg border border-gray-200 bg-gray-50 px-3 py-2 text-xs transition-all"
							>
								<div class="flex items-start gap-2">
									<span class="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-blue-100 text-[10px] font-bold text-blue-700">{cite.ref?.replace(/[\[\]]/g, '')}</span>
									<div class="min-w-0 flex-1">
										<div class="flex items-center gap-2">
											{#if cite.file_name}
												{#if cite.stored_path}
													<button
														onclick={() => openPreview(cite.stored_path ?? '', cite.file_name ?? '', cite.page ?? undefined)}
														class="font-semibold text-blue-600 hover:underline text-left"
														title="Vorschau oeffnen"
													>
														{cite.file_name}
														<svg class="inline h-3 w-3 -mt-0.5 ml-0.5" viewBox="0 0 20 20" fill="currentColor">
															<path d="M10 12a2 2 0 100-4 2 2 0 000 4z" />
															<path fill-rule="evenodd" d="M.458 10C1.732 5.943 5.522 3 10 3s8.268 2.943 9.542 7c-1.274 4.057-5.064 7-9.542 7S1.732 14.057.458 10zM14 10a4 4 0 11-8 0 4 4 0 018 0z" clip-rule="evenodd" />
														</svg>
													</button>
												{:else}
													<span class="font-semibold text-gray-700">{cite.file_name}</span>
												{/if}
											{/if}
											{#if cite.page}
												<span class="rounded bg-blue-50 px-1.5 py-0.5 text-[10px] font-medium text-blue-600">Seite {cite.page}</span>
											{/if}
											{#if cite.score}
												<span class="text-[10px] text-gray-400">Score: {(cite.score * 100).toFixed(0)}%</span>
											{/if}
										</div>
										{#if cite.excerpt}
											<p class="mt-1 whitespace-pre-wrap text-gray-500">{cite.excerpt}</p>
										{/if}
									</div>
								</div>
							</div>
						{/each}
					</div>
				</div>
			{/if}

			<!-- Agent Steps (Pipeline Tracing) -->
			{#if message.agentSteps && message.agentSteps.length > 0}
				<div class="mt-3 border-t border-gray-100 pt-3">
					<button
						class="flex items-center gap-1 text-xs text-gray-400 hover:text-gray-600"
						onclick={() => (showSteps = !showSteps)}
					>
						<span class="transform transition-transform {showSteps ? 'rotate-90' : ''}"
							>&#9654;</span
						>
						{message.agentSteps.length} Schritte anzeigen
					</button>

					{#if showSteps}
						<div class="mt-2 space-y-0">
							{#each message.agentSteps as step, i}
								{@const colors = actionColors[step.action] ?? 'bg-gray-100 text-gray-700 border-gray-200'}
								{@const label = actionLabels[step.action] ?? step.action}
								{@const isOpen = openSteps.has(step.step)}
								{@const args = formatArgs(step.args)}

								<!-- Connector line -->
								{#if i > 0}
									<div class="ml-4 h-3 border-l-2 border-gray-200"></div>
								{/if}

								<div class="rounded-lg border {isOpen ? 'border-gray-300 bg-white shadow-sm' : 'border-gray-200 bg-gray-50'}">
									<!-- Step header (always visible) -->
									<button
										class="flex w-full items-center gap-2 px-3 py-2 text-left text-xs hover:bg-gray-50"
										onclick={() => toggleStep(step.step)}
									>
										<span class="transform transition-transform {isOpen ? 'rotate-90' : ''} text-gray-400">&#9654;</span>
										<span class="flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-gray-200 text-[10px] font-bold text-gray-600">{step.step}</span>
										<span class="rounded-md border px-1.5 py-0.5 text-[10px] font-semibold {colors}">{step.action}</span>
										<span class="truncate text-gray-500">{label}</span>
									</button>

									<!-- Step details (expandable) -->
									{#if isOpen}
										<div class="space-y-2 border-t border-gray-100 px-3 py-2">
											<!-- Thought / Reasoning -->
											{#if step.thought}
												<div>
													<p class="mb-1 text-[10px] font-semibold uppercase tracking-wider text-gray-400">Reasoning</p>
													<p class="whitespace-pre-wrap text-xs text-gray-600">{step.thought}</p>
												</div>
											{/if}

											<!-- Args (what was sent) -->
											{#if args}
												<div>
													<p class="mb-1 text-[10px] font-semibold uppercase tracking-wider text-gray-400">Gesendet</p>
													<pre class="overflow-x-auto rounded-md bg-gray-50 p-2 font-mono text-[11px] text-gray-600">{args}</pre>
												</div>
											{/if}

											<!-- Observation (what came back) -->
											{#if step.observation}
												<div>
													<p class="mb-1 text-[10px] font-semibold uppercase tracking-wider text-gray-400">Ergebnis (an LLM)</p>
													<pre class="max-h-60 overflow-auto whitespace-pre-wrap rounded-md bg-gray-50 p-2 font-mono text-[11px] text-gray-600">{step.observation}</pre>
												</div>
											{/if}

											<!-- Retrieved chunks (Vector-DB Rohdaten) -->
											{#if step.chunks && step.chunks.length > 0}
												<div>
													<p class="mb-1 text-[10px] font-semibold uppercase tracking-wider text-gray-400">
														Chunks aus Vektordatenbank ({step.chunks.length})
													</p>
													<div class="space-y-1">
														{#each step.chunks as chunk, ci}
															{@const key = `${step.step}-${ci}`}
															{@const isChunkOpen = openChunks.has(key)}
															{@const meta = chunk.metadata ?? {}}
															{@const collection = (meta._collection as string) ?? ''}
															{@const fileName = (meta.file_name as string) ?? ''}
															{@const page = meta.page as number | undefined}
															{@const label = (chunk as { relevance_label?: string }).relevance_label}
															<div class="rounded-md border border-gray-200 bg-white text-[11px]">
																<button
																	class="flex w-full items-start gap-2 px-2 py-1.5 text-left hover:bg-gray-50"
																	onclick={() => toggleChunk(key)}
																>
																	<span class="mt-0.5 transform transition-transform {isChunkOpen ? 'rotate-90' : ''} text-gray-400">&#9654;</span>
																	<span class="flex h-4 w-4 shrink-0 items-center justify-center rounded bg-purple-100 text-[9px] font-bold text-purple-700">{ci + 1}</span>
																	<div class="min-w-0 flex-1">
																		<div class="flex flex-wrap items-center gap-1.5">
																			{#if fileName}
																				<span class="font-semibold text-gray-700">{fileName}</span>
																			{/if}
																			{#if page}
																				<span class="rounded bg-blue-50 px-1 py-0.5 text-[9px] font-medium text-blue-600">S. {page}</span>
																			{/if}
																			{#if collection}
																				<span class="rounded bg-indigo-50 px-1 py-0.5 text-[9px] font-medium text-indigo-600">{collection}</span>
																			{/if}
																			{#if label}
																				<span class="rounded bg-gray-100 px-1 py-0.5 text-[9px] font-medium text-gray-600">{label}</span>
																			{/if}
																			<span class="ml-auto text-[9px] text-gray-400">
																				rerank {formatScore(chunk.rerank_score)}
																				{#if chunk.original_score !== undefined}
																					· orig {formatScore(chunk.original_score)}
																				{/if}
																			</span>
																		</div>
																		{#if !isChunkOpen}
																			<p class="mt-0.5 truncate text-gray-500">{chunkSummary(chunk)}</p>
																		{/if}
																	</div>
																</button>
																{#if isChunkOpen}
																	<div class="space-y-2 border-t border-gray-100 px-2 py-2">
																		{#if chunk.chunk_id}
																			<p class="font-mono text-[10px] text-gray-400">chunk_id: {chunk.chunk_id}</p>
																		{/if}
																		<div>
																			<p class="mb-1 text-[9px] font-semibold uppercase tracking-wider text-gray-400">Text</p>
																			<pre class="max-h-80 overflow-auto whitespace-pre-wrap rounded bg-gray-50 p-2 font-mono text-[11px] text-gray-700">{chunkFullText(chunk)}</pre>
																		</div>
																		{#if metadataWithoutText(meta)}
																			<div>
																				<p class="mb-1 text-[9px] font-semibold uppercase tracking-wider text-gray-400">Metadaten</p>
																				<pre class="max-h-40 overflow-auto rounded bg-gray-50 p-2 font-mono text-[10px] text-gray-600">{metadataWithoutText(meta)}</pre>
																			</div>
																		{/if}
																	</div>
																{/if}
															</div>
														{/each}
													</div>
												</div>
											{/if}

											<!-- Raw LLM response (exactly what the model returned this step) -->
											{#if step.llm_response}
												<div>
													<button
														class="flex items-center gap-1 text-[10px] font-semibold uppercase tracking-wider text-gray-400 hover:text-gray-600"
														onclick={() => toggleLlmResponse(step.step)}
													>
														<span class="transform transition-transform {showLlmResponse.has(step.step) ? 'rotate-90' : ''}">&#9654;</span>
														Rohantwort des LLM
													</button>
													{#if showLlmResponse.has(step.step)}
														<pre class="mt-1 max-h-60 overflow-auto whitespace-pre-wrap rounded-md bg-gray-900 p-2 font-mono text-[11px] text-gray-100">{step.llm_response}</pre>
													{/if}
												</div>
											{/if}
										</div>
									{/if}
								</div>
							{/each}
						</div>
					{/if}
				</div>
			{/if}

			<!-- System-Prompt + enriched Query (Eingang an das LLM) -->
			{#if message.systemPrompt || message.enrichedQuery}
				<div class="mt-3 border-t border-gray-100 pt-3">
					<button
						class="flex items-center gap-1 text-xs text-gray-400 hover:text-gray-600"
						onclick={() => (showSystemPrompt = !showSystemPrompt)}
					>
						<span class="transform transition-transform {showSystemPrompt ? 'rotate-90' : ''}">&#9654;</span>
						System-Prompt &amp; Query anzeigen
					</button>
					{#if showSystemPrompt}
						<div class="mt-2 space-y-2">
							{#if message.enrichedQuery}
								<div>
									<p class="mb-1 text-[10px] font-semibold uppercase tracking-wider text-gray-400">Angereicherte Query</p>
									<pre class="max-h-40 overflow-auto whitespace-pre-wrap rounded-md bg-gray-50 p-2 font-mono text-[11px] text-gray-600">{message.enrichedQuery}</pre>
								</div>
							{/if}
							{#if message.systemPrompt}
								<div>
									<p class="mb-1 text-[10px] font-semibold uppercase tracking-wider text-gray-400">System-Prompt</p>
									<pre class="max-h-80 overflow-auto whitespace-pre-wrap rounded-md bg-gray-50 p-2 font-mono text-[11px] text-gray-600">{message.systemPrompt}</pre>
								</div>
							{/if}
						</div>
					{/if}
				</div>
			{/if}

			<!-- Feedback Buttons -->
			{#if !message.error && !message.streaming}
				<div class="mt-3 flex gap-2 border-t border-gray-100 pt-2">
					<button
						class="text-lg transition-transform hover:scale-110
						{feedbackSent === 'positive' ? 'opacity-100' : 'opacity-40 hover:opacity-70'}"
						onclick={() => onFeedback('positive')}
						title="Hilfreich"
						disabled={feedbackSent !== null}
					>
						&#128077;
					</button>
					<button
						class="text-lg transition-transform hover:scale-110
						{feedbackSent === 'negative' ? 'opacity-100' : 'opacity-40 hover:opacity-70'}"
						onclick={() => onFeedback('negative')}
						title="Nicht hilfreich"
						disabled={feedbackSent !== null}
					>
						&#128078;
					</button>
				</div>
			{/if}
		</div>
	</div>

	<!-- PDF Preview Overlay -->
	{#if previewUrl}
		<!-- svelte-ignore a11y_click_events_have_key_events -->
		<!-- svelte-ignore a11y_no_static_element_interactions -->
		<div
			class="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
			onclick={(e) => { if (e.target === e.currentTarget) closePreview(); }}
		>
			<div class="flex h-[90vh] w-full max-w-5xl flex-col overflow-hidden rounded-xl bg-white shadow-2xl">
				<!-- Header -->
				<div class="flex items-center justify-between border-b border-gray-200 px-4 py-3">
					<div class="flex min-w-0 items-center gap-2">
						<svg class="h-5 w-5 shrink-0 text-red-500" viewBox="0 0 20 20" fill="currentColor">
							<path fill-rule="evenodd" d="M4 4a2 2 0 012-2h4.586A2 2 0 0112 2.586L15.414 6A2 2 0 0116 7.414V16a2 2 0 01-2 2H6a2 2 0 01-2-2V4z" clip-rule="evenodd" />
						</svg>
						<span class="truncate text-sm font-semibold text-gray-800">{previewTitle}</span>
					</div>
					<div class="flex items-center gap-2">
						{#if previewPage}
							<span class="rounded-full bg-blue-100 px-2.5 py-0.5 text-[10px] font-bold text-blue-700">
								Seite {previewPage}
							</span>
						{/if}
						<a
							href={previewUrl}
							target="_blank"
							class="rounded-lg border border-gray-200 px-3 py-1.5 text-xs text-gray-600 hover:bg-gray-50"
							title="Im neuen Tab oeffnen (springt zur Seite)"
						>
							Neuer Tab
						</a>
						<a
							href={previewBaseUrl}
							download
							class="rounded-lg border border-gray-200 px-3 py-1.5 text-xs text-gray-600 hover:bg-gray-50"
						>
							Download
						</a>
						<button
							onclick={closePreview}
							class="rounded-lg border border-gray-200 p-1.5 text-gray-400 hover:bg-gray-50 hover:text-gray-600"
							title="Schliessen"
						>
							<svg class="h-5 w-5" viewBox="0 0 20 20" fill="currentColor">
								<path fill-rule="evenodd" d="M4.293 4.293a1 1 0 011.414 0L10 8.586l4.293-4.293a1 1 0 111.414 1.414L11.414 10l4.293 4.293a1 1 0 01-1.414 1.414L10 11.414l-4.293 4.293a1 1 0 01-1.414-1.414L8.586 10 4.293 5.707a1 1 0 010-1.414z" clip-rule="evenodd" />
							</svg>
						</button>
					</div>
				</div>
				<!-- PDF viewer: <object> handles #page=N better than <iframe> across browsers -->
				<object
					data={previewUrl}
					type="application/pdf"
					class="w-full flex-1"
					title="Dokumentvorschau"
				>
					<!-- Fallback if browser can't render PDF inline -->
					<div class="flex h-full flex-col items-center justify-center gap-4 p-8 text-gray-500">
						<p class="text-sm">PDF-Vorschau wird von diesem Browser nicht unterstuetzt.</p>
						<a
							href={previewUrl}
							target="_blank"
							class="rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700"
						>
							PDF im neuen Tab oeffnen
						</a>
					</div>
				</object>
			</div>
		</div>
	{/if}
{/if}
