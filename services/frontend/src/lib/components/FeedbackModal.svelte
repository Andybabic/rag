<script lang="ts">
	interface ClosestChunk {
		text: string;
		sim: number;
	}

	interface ClaimView {
		id: string;
		text: string;
		bestSimilarity: number | null;
		bestNli: string | null;
		closestChunk: ClosestChunk | null;
	}

	let { queryId, answerText, onclose }: { queryId: string; answerText: string; onclose: () => void } = $props();

	let phase = $state<'loading' | 'no-analysis' | 'ready' | 'done' | 'error'>('loading');
	let claims: ClaimView[] = $state([]);
	let idx = $state(0);
	let comment = $state('');
	let error = $state('');
	let submitting = $state(false);
	let savedCount = $state(0);
	let skippedCount = $state(0);

	const VERDICT_LABELS: Record<string, string> = {
		correct: 'Korrekt',
		partially_correct: 'Teilweise korrekt',
		incorrect: 'Falsch',
		cannot_judge: 'Nicht beurteilbar'
	};

	const VERDICT_CLASSES: Record<string, string> = {
		correct: 'border-green-300 bg-green-50 text-green-700 hover:bg-green-100',
		partially_correct: 'border-amber-300 bg-amber-50 text-amber-700 hover:bg-amber-100',
		incorrect: 'border-red-300 bg-red-50 text-red-700 hover:bg-red-100',
		cannot_judge: 'border-gray-300 bg-gray-50 text-gray-600 hover:bg-gray-100'
	};

	async function loadSavedAnalysis() {
		try {
			const r = await fetch(`/api/admin/analyze-chunks/${queryId}/result`);
			if (!r.ok) {
				phase = 'error';
				error = `HTTP ${r.status}`;
				return;
			}
			const result = (await r.json()) as Record<string, unknown>;
			if (result.status === 'done') {
				applyAnalysis(result.data as Record<string, unknown>);
			} else if (result.status === 'error') {
				phase = 'error';
				error = (result.error as string) || 'Analyse fehlgeschlagen';
			} else {
				phase = 'no-analysis';
			}
		} catch (e) {
			phase = 'error';
			error = e instanceof Error ? e.message : 'Fehler beim Laden';
		}
	}

	function applyAnalysis(data: Record<string, unknown>) {
		claims = buildClaims(data);
		idx = 0;
		phase = claims.length > 0 ? 'ready' : 'done';
	}

	function buildClaims(data: Record<string, unknown>): ClaimView[] {
		const steps = (data.steps as unknown[]) || [];
		const byId: Record<string, Record<string, unknown>> = {};
		const chunkSources: { claimId: string; text: string; sim: number }[] = [];

		for (const step of steps) {
			const analyses = (step as Record<string, unknown>).analyses as
				| Record<string, Record<string, unknown>>
				| undefined;
			if (!analyses) continue;
			for (const [modelName, sa] of Object.entries(analyses)) {
				if (modelName === '_judge') continue;
				const answerClaims = (sa.answer_claims as unknown[]) || [];
				for (const ac of answerClaims) {
					const a = ac as Record<string, unknown>;
					const id = a.id as string;
					if (!id) continue;
					if (!byId[id]) {
						byId[id] = { ...a };
					} else {
						const ex = byId[id];
						const existingMatched = (ex.matched_by_chunks as string[]) || [];
						const newMatched = (a.matched_by_chunks as string[]) || [];
						ex.matched_by_chunks = [...new Set([...existingMatched, ...newMatched])];
						if (((ex.matched_by_chunks as string[]).length === 0)) {
							const es = (ex.best_similarity as number) || 0;
							const ns = (a.best_similarity as number) || 0;
							if (ns > es) {
								ex.best_similarity = ns;
								ex.best_source_text = a.best_source_text;
								ex.best_nli = a.best_nli;
							}
						} else {
							delete ex.best_similarity;
						}
					}
				}
				const perChunk = (sa.per_chunk as unknown[]) || [];
				for (const pc of perChunk) {
					const cl = ((pc as Record<string, unknown>).claims as unknown[]) || [];
					for (const c of cl) {
						const cc = c as Record<string, unknown>;
						const bm = (cc.best_match as Record<string, unknown>) || null;
						if (bm && bm.id) {
							chunkSources.push({
								claimId: bm.id as string,
								text: (cc.text as string) || '',
								sim: (bm.sim as number) || 0
							});
						}
					}
				}
			}
		}

		const views: ClaimView[] = [];
		for (const [id, a] of Object.entries(byId)) {
			const matchedBy = (a.matched_by_chunks as string[]) || [];
			if (matchedBy.length > 0) continue;
			const bestSimilarity = (a.best_similarity as number) ?? null;
			const bestNli = (a.best_nli as string) ?? null;
			let best: ClosestChunk | null = null;
			// Newer analyses store the source chunk-claim text directly on the answer claim.
			const srcText = (a.best_source_text as string) || '';
			if (srcText) {
				best = { text: srcText, sim: bestSimilarity ?? 0 };
			} else {
				// Older analyses: recover the closest chunk claim via its best_match id.
				let bestSim = 0;
				for (const src of chunkSources) {
					if (src.claimId === id && src.sim > bestSim) {
						bestSim = src.sim;
						best = { text: src.text, sim: src.sim };
					}
				}
			}
			views.push({
				id,
				text: ((a.text_in_answer as string) || (a.text as string) || '').trim(),
				bestSimilarity,
				bestNli,
				closestChunk: best
			});
		}
		views.sort((x, y) => (x.bestSimilarity ?? -1) - (y.bestSimilarity ?? -1));
		return views;
	}

	async function submitVerdict(verdict: string) {
		if (submitting) return;
		const claim = claims[idx];
		submitting = true;
		error = '';
		try {
			const r = await fetch(`/api/admin/feedback/${queryId}`, {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({
					claim_id: claim.id,
					verdict,
					comment,
					claim_text: claim.text
				})
			});
			if (!r.ok) {
				error = `Speichern fehlgeschlagen (HTTP ${r.status})`;
				return;
			}
			savedCount++;
			comment = '';
			advance();
		} catch (e) {
			error = e instanceof Error ? e.message : 'Speichern fehlgeschlagen';
		} finally {
			submitting = false;
		}
	}

	function skip() {
		skippedCount++;
		comment = '';
		advance();
	}

	function advance() {
		if (idx + 1 >= claims.length) {
			phase = 'done';
		} else {
			idx++;
		}
	}

	function escapeHtml(text: string): string {
		return text
			.replace(/&/g, '&amp;')
			.replace(/</g, '&lt;')
			.replace(/>/g, '&gt;')
			.replace(/"/g, '&quot;');
	}

	function highlightAnswer(answerText: string, claimList: ClaimView[]): string {
		if (!answerText) return '';
		const spans: { start: number; end: number; id: string }[] = [];
		for (const c of claimList) {
			const t = (c.text || '').trim();
			if (t.length < 3) continue;
			let idx = answerText.indexOf(t);
			if (idx === -1) idx = answerText.toLowerCase().indexOf(t.toLowerCase());
			if (idx === -1) continue;
			spans.push({ start: idx, end: idx + t.length, id: c.id });
		}
		spans.sort((a, b) => a.start - b.start);
		let html = '';
		let pos = 0;
		for (const s of spans) {
			if (s.start < pos) continue;
			html += escapeHtml(answerText.slice(pos, s.start));
			html += `<mark class="bg-red-100 border-b-2 border-red-300 rounded" title="${escapeHtml(s.id + ': unbelegt')}">${escapeHtml(answerText.slice(s.start, s.end))}</mark>`;
			pos = s.end;
		}
		html += escapeHtml(answerText.slice(pos));
		return html;
	}

	$effect(() => {
		loadSavedAnalysis();
	});
</script>

<div
	class="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
	role="dialog"
	aria-modal="true"
	onclick={(e: MouseEvent) => { if (e.target === e.currentTarget) onclose(); }}
>
	<div class="flex max-h-[85vh] w-full max-w-2xl flex-col overflow-hidden rounded-xl bg-white shadow-xl">
		<div class="flex items-center justify-between border-b border-gray-100 px-5 py-3">
			<h3 class="text-sm font-semibold text-gray-800">Check this answer</h3>
			<button
				class="rounded px-2 py-1 text-xs text-gray-400 hover:bg-gray-100 hover:text-gray-600"
				onclick={onclose}
			>
				&times;
			</button>
		</div>

		<div class="flex-1 overflow-y-auto px-5 py-4">
			{#if phase === 'loading'}
				<p class="text-sm text-gray-400 animate-pulse">Lade Analyse...</p>

			{:else if phase === 'error'}
				<p class="text-sm text-red-600">{error}</p>
				<button
					class="mt-3 rounded-lg border border-gray-300 bg-white px-3 py-1.5 text-xs text-gray-600 hover:bg-gray-50"
					onclick={() => { phase = 'loading'; loadSavedAnalysis(); }}
				>
					Erneut versuchen
				</button>

			{:else if phase === 'no-analysis'}
				<p class="text-sm text-gray-600">
					Für diese Anfrage liegt noch keine Aussagen-Analyse vor.
				</p>

			{:else if phase === 'done'}
				<p class="text-sm text-gray-700">
					{savedCount > 0
						? `Danke! ${savedCount} Aussage${savedCount > 1 ? 'n' : ''} bewertet.`
						: 'Keine Aussagen bewertet.'}
					{#if skippedCount > 0}
						{' '}{skippedCount} übersprungen.
					{/if}
				</p>
				{#if claims.length === 0}
					<p class="mt-1 text-xs text-gray-400">
						Keine unbelegten Aussagen gefunden — alle Aussagen der Antwort sind durch Quellen belegt.
					</p>
				{/if}
				<button
					class="mt-4 rounded-lg bg-gray-800 px-4 py-2 text-xs font-medium text-white hover:bg-gray-700"
					onclick={onclose}
				>
					Schließen
				</button>

			{:else if phase === 'ready'}
				{@const claim = claims[idx]}
				<p class="text-[10px] font-semibold uppercase tracking-wider text-gray-400">
					Aussage {idx + 1} von {claims.length}
				</p>

				<div class="mt-2 max-h-40 overflow-y-auto rounded-lg border border-gray-200 bg-gray-50 p-3">
					<p class="text-[10px] font-semibold uppercase tracking-wider text-gray-400 mb-1">
						Antwort (unbelegte Aussagen rot markiert)
					</p>
					<div class="text-xs text-gray-700 whitespace-pre-wrap">{@html highlightAnswer(answerText, claims)}</div>
				</div>

				<div class="mt-4 rounded-lg border border-gray-200 p-4">
					<p class="text-[10px] font-semibold uppercase tracking-wider text-gray-400 mb-1">Aussage</p>
					<p class="text-sm font-medium text-gray-800">{claim.text || '(kein Text)'}</p>
				</div>

				<div class="mt-2 rounded-lg border border-gray-200 bg-gray-50 p-4">
					<p class="text-[10px] font-semibold uppercase tracking-wider text-gray-400 mb-1">Quelle</p>
					{#if claim.closestChunk}
						<blockquote class="text-xs text-gray-600 italic">
							&ldquo;{claim.closestChunk.text}&rdquo;
						</blockquote>
						<span class="mt-1 inline-block rounded bg-gray-200 px-1.5 py-0.5 text-[10px] text-gray-600">
							nächste Quelle, cosine {claim.closestChunk.sim.toFixed(2)}
							{#if claim.bestSimilarity != null && claim.closestChunk.sim < 0.75}
								(unter Schwelle)
							{/if}
							{#if claim.bestNli}
								(NLI: {claim.bestNli})
							{/if}
						</span>
					{:else}
						<p class="text-xs text-gray-500">
							Keine Quelle im Kontext gefunden
							{#if claim.bestSimilarity != null}
								(beste Ähnlichkeit {claim.bestSimilarity.toFixed(2)}
								{#if claim.bestNli}, NLI: {claim.bestNli}{/if})
							{/if}
						</p>
					{/if}
				</div>

				<div class="mt-4 grid grid-cols-2 gap-2">
					{#each Object.keys(VERDICT_LABELS) as v}
						<button
							class="rounded-lg border px-3 py-2 text-xs font-medium disabled:opacity-50 {VERDICT_CLASSES[v]}"
							disabled={submitting}
							onclick={() => submitVerdict(v)}
						>
							{VERDICT_LABELS[v]}
						</button>
					{/each}
				</div>

				<textarea
					class="mt-3 w-full rounded-lg border border-gray-300 p-2 text-xs text-gray-700 focus:outline-none focus:ring-1 focus:ring-blue-400"
					rows="2"
					placeholder="Was ist falsch oder fehlt? (optional)"
					bind:value={comment}
				></textarea>

				{#if error}
					<p class="mt-2 text-xs text-red-600">{error}</p>
				{/if}

				<div class="mt-3 flex items-center justify-between">
					<button
						class="text-xs text-gray-400 hover:text-gray-600"
						onclick={skip}
						disabled={submitting}
					>
						Skip
					</button>
					<span class="text-[10px] text-gray-300">{claim.id}</span>
				</div>
			{/if}
		</div>
	</div>
</div>
