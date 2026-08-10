<script lang="ts">
	import { page } from "$app/stores";
	import { getMetrics, type MetricsResponse } from "$lib/api";
	import { getUseCaseBySlug } from "$lib/use-cases";

	const useCase = $derived.by(() => {
		const slug = $page.params.useCase ?? "";
		return getUseCaseBySlug(slug)?.apiId ?? "neumann";
	});

	let data: MetricsResponse = $state({ queries: [], summary: { total: 0, avg_steps: 0, avg_chunks: 0, avg_answer_length: 0, sufficient_count: 0, sufficient_pct: 0, avg_processing_ms: null, avg_total_tokens: null, avg_retrieval_quality: null } });
	let loading = $state(false);
	let error = $state("");
	let expandedId: string | null = $state(null);
	let analyzingId: string | null = $state(null);
	let analysisData: Record<string, unknown> | null = $state(null);
	let analysisError: string = $state("");
	let analysisConcurrency: number = $state(8);
	let analysisModel1: string = $state("qwen3.5:9b");
	let analysisModel2: string = $state("");  // mapping model (empty = use same as extraction)
		let availableModels: string[] = $state(["qwen3.5:9b", "gemma4:12b"]);

	async function loadModels() {
		try {
			const r = await fetch("/api/admin/models");
			const data = await r.json() as Record<string, unknown>;
			const models = (data.models as string[]) || [];
			if (models.length > 0) availableModels = models;
		} catch { /* keep defaults */ }
	}
	let analysisStartTime = $state(0);
	let analysisElapsed = $state(0);
	let analysisTimer: ReturnType<typeof setInterval> | null = null;
	let pollProgressTimer: ReturnType<typeof setInterval> | null = null;
	let analysisProgress = $state("");
	let expandedClaimChunks: Record<string, boolean> = $state({});

	function toggle(id: string) {
		expandedId = expandedId === id ? null : id;
	}

	function toggleClaims(key: string) {
		expandedClaimChunks = { ...expandedClaimChunks, [key]: !expandedClaimChunks[key] };
	}

	async function load() {
		loading = true;
		error = "";
		try {
			data = await getMetrics(useCase);
		} catch (e) {
			error = e instanceof Error ? e.message : "Fehler beim Laden";
		}
		loading = false;
	}

	async function analyzeChunks(queryId: string) {
		analyzingId = queryId;
		analysisError = "";
		analysisData = null;
		analysisStartTime = Date.now();
		analysisElapsed = 0;
		if (analysisTimer) clearInterval(analysisTimer);
		analysisTimer = setInterval(() => {
			analysisElapsed = Math.round((Date.now() - analysisStartTime) / 1000);
		}, 1000);
		pollProgressTimer = setInterval(async () => {
			try {
				const r = await fetch(`/api/admin/analyze-chunks/${queryId}/progress`);
				const p = await r.json() as Record<string,number>;
				if (p.total > 0) analysisProgress = `${p.done}/${p.total}`;
			} catch { /* ignore */ }
		}, 500);
		expandedClaimChunks = {};

		const threshold = 0.75;
		try {
			// POST starts analysis, results via polling
			const startResp = await fetch(`/api/admin/analyze-chunks/${queryId}`, {
				method: "POST",
				headers: { "Content-Type": "application/json" },
				body: JSON.stringify({ threshold, max_concurrent: analysisConcurrency, models: [analysisModel1], mapping_model: analysisModel2 || null }),
			});
			if (!startResp.ok) {
				const err = await startResp.json().catch(() => ({}));
				analysisError = (err as Record<string,unknown>).detail as string || `HTTP ${startResp.status}`;
				return;
			}
			const startData = await startResp.json() as Record<string, unknown>;
			if (startData.status === "already_running") {
				analysisError = "Analyse luft bereits";
				return;
			}
			// Poll /result, 5 min timeout
			let polls = 0;
			while (polls < 3600) {
				polls++;
				await new Promise(r => setTimeout(r, 500));
				const resultResp = await fetch(`/api/admin/analyze-chunks/${queryId}/result`);
				if (!resultResp.ok) continue;
				const result = await resultResp.json() as Record<string, unknown>;
				if (result.status === "done") {
					analysisData = result.data as Record<string, unknown>;
					break;
				}
				if (result.status === "error") {
					analysisError = result.error as string || "Unknown error";
					break;
				}
			}
			if (polls >= 200) {
				analysisError = "Analyse hat zu lange gedauert (>30 min)";
			}
		} catch (e) {
			analysisError = e instanceof Error ? e.message : "Analyse fehlgeschlagen";
		} finally {
			if (analysisTimer) { clearInterval(analysisTimer); analysisTimer = null; }
			if (pollProgressTimer) { clearInterval(pollProgressTimer); pollProgressTimer = null; }
			analysisProgress = "";
			analyzingId = null;
		}
	}

	$effect(() => {
		useCase;
		load();
		loadModels();
	});

	const actionLabels: Record<string, string> = {
		SEARCH: "Suche",
		SEARCH_CNC: "CNC-Suche",
		FINAL_ANSWER: "Antwort",
		MANAGER_PLAN: "Manager-Plan",
		SYNTHESIZE: "Zusammenführung",
		REFINE_QUERY: "Verfeinerung",
		CLARIFY: "Rückfrage",
		COMPLIANCE_CHECK: "Compliance-Prüfung",
	};

	const actionColors: Record<string, string> = {
		SEARCH: "border-purple-300 bg-purple-50",
		SEARCH_CNC: "border-purple-300 bg-purple-50",
		FINAL_ANSWER: "border-green-300 bg-green-50",
		MANAGER_PLAN: "border-indigo-300 bg-indigo-50",
		SYNTHESIZE: "border-fuchsia-300 bg-fuchsia-50",
		REFINE_QUERY: "border-orange-300 bg-orange-50",
		CLARIFY: "border-yellow-300 bg-yellow-50",
		COMPLIANCE_CHECK: "border-rose-300 bg-rose-50",
	};

	const actionDots: Record<string, string> = {
		SEARCH: "bg-purple-500",
		SEARCH_CNC: "bg-purple-500",
		FINAL_ANSWER: "bg-green-500",
		MANAGER_PLAN: "bg-indigo-500",
		SYNTHESIZE: "bg-fuchsia-500",
		REFINE_QUERY: "bg-orange-500",
		CLARIFY: "bg-yellow-500",
		COMPLIANCE_CHECK: "bg-rose-500",
	};

	function formatTokens(t: number | null | undefined): string {
		if (t == null) return "&ndash;";
		if (t >= 1000) return (t / 1000).toFixed(1) + "K";
		return t.toString();
	}

	function computeStepTimes(steps: Record<string, unknown>[], queryCreatedAt: string | null): string[] {
		if (!queryCreatedAt) return steps.map(() => "&ndash;");
		const base = new Date(queryCreatedAt).getTime();
		const times: string[] = [];
		let acc = 0;
		for (const step of steps) {
			times.push(new Date(base + acc).toLocaleString("de-AT", { day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" }));
			acc += (step.duration_ms as number) || 0;
		}
		return times;
	}

	function countDuplicates(answerClaims: unknown[]): number {
		return answerClaims.filter(ac => ((ac as Record<string,unknown>).matched_by_chunks as unknown[])?.length > 1).length;
	}

	function countUnmatched(answerClaims: unknown[]): number {
		return answerClaims.filter(ac => ((ac as Record<string,unknown>).matched_by_chunks as unknown[])?.length === 0).length;
	}

	const CATEGORY_LABELS: Record<string, string> = {
		fact: 'Fakt',
		verified_fact: 'Verifizierter Fakt',
		recommendation: 'Empfehlung',
		conclusion: 'Fazit',
		no_category: 'Keine Kategorie'
	};

	const CATEGORY_CLASSES: Record<string, string> = {
		fact: 'bg-blue-100 text-blue-700',
		verified_fact: 'bg-green-100 text-green-700',
		recommendation: 'bg-purple-100 text-purple-700',
		conclusion: 'bg-amber-100 text-amber-700',
		no_category: 'bg-gray-100 text-gray-500'
	};

	function catLabel(cat: unknown): string {
		const c = (cat as string) || 'no_category';
		return CATEGORY_LABELS[c] || CATEGORY_LABELS.no_category;
	}

	function catClass(cat: unknown): string {
		const c = (cat as string) || 'no_category';
		return CATEGORY_CLASSES[c] || CATEGORY_CLASSES.no_category;
	}

	function catCounts(answerClaims: unknown[]): Record<string, number> {
		const counts: Record<string, number> = {};
		for (const ac of answerClaims) {
			const c = ((ac as Record<string, unknown>).category as string) || 'no_category';
			counts[c] = (counts[c] || 0) + 1;
		}
		return counts;
	}

	function catSummary(answerClaims: unknown[]): string {
		const counts = catCounts(answerClaims);
		const parts: string[] = [];
		for (const [c, n] of Object.entries(counts)) {
			if (n > 0 && c !== 'no_category') parts.push(`${CATEGORY_LABELS[c]}: ${n}`);
		}
		return parts.length ? ' · ' + parts.join(' · ') : '';
	}

	// Merge answer claims across steps: a claim is marked matched if ANY step found a match.
	// Uses union of matched_by_chunks and best best_similarity across all steps.
	function mergeAnswerClaims(adSteps: unknown[]): Record<string, unknown[]> {
		const modelClaims: Record<string, Record<string, Record<string, unknown>>> = {};
		for (const step of adSteps) {
			const so = step as Record<string, unknown>;
			const analyses = so.analyses as Record<string, Record<string, unknown>> | undefined;
			if (!analyses) continue;
			for (const [modelName, stepAnalysis] of Object.entries(analyses)) {
				if (modelName === '_judge') continue;
				const claims = stepAnalysis.answer_claims as unknown[] | undefined;
				if (!claims) continue;
				if (!modelClaims[modelName]) modelClaims[modelName] = {};
				for (const ac of claims) {
					const a = ac as Record<string, unknown>;
					const id = a.id as string;
					if (!id) continue;
					if (!modelClaims[modelName][id]) {
						modelClaims[modelName][id] = { ...a };
					} else {
						const existing = modelClaims[modelName][id];
						const existingMatched = (existing.matched_by_chunks as string[]) || [];
						const newMatched = (a.matched_by_chunks as string[]) || [];
						const mergedMatched = [...new Set([...existingMatched, ...newMatched])];
						(existing as Record<string, unknown>).matched_by_chunks = mergedMatched;
						if (mergedMatched.length === 0) {
							const existingSim = (existing.best_similarity as number) || 0;
							const newSim = (a.best_similarity as number) || 0;
							(existing as Record<string, unknown>).best_similarity = Math.max(existingSim, newSim);
						} else {
							delete (existing as Record<string, unknown>).best_similarity;
						}
					}
				}
			}
		}
		const result: Record<string, unknown[]> = {};
		for (const [modelName, claimsById] of Object.entries(modelClaims)) {
			result[modelName] = Object.values(claimsById);
		}
		return result;
	}

	function escapeHtml(text: string): string {
		return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
	}

	function highlightAnswerText(answerText: string, answerClaims: unknown[]): string {
		if (!answerText || !answerClaims || answerClaims.length === 0) return escapeHtml(answerText || '');
		interface ClaimMatch { start: number; end: number; matchedBy: string[]; id: string }
		const matches: ClaimMatch[] = [];
		const used: { start: number; end: number }[] = [];
		// Sort: matched first, then duplicates, then unmatched
		const sorted = [...answerClaims].sort((a, b) => {
			const am = ((a as Record<string,unknown>).matched_by_chunks as string[]) || [];
			const bm = ((b as Record<string,unknown>).matched_by_chunks as string[]) || [];
			if (am.length === 1 && bm.length !== 1) return -1;
			if (bm.length === 1 && am.length !== 1) return 1;
			if (am.length > 1 && bm.length === 0) return -1;
			if (bm.length > 1 && am.length === 0) return 1;
			return 0;
		});
		for (const ac of sorted) {
			const a = ac as Record<string, unknown>;
			const matchedBy = (a.matched_by_chunks as string[]) || [];
			const id = (a.id as string) || '';
			// Use LLM-mapped exact text if available, otherwise fall back to claim text
			const searchText = ((a.text_in_answer as string) || (a.text as string) || '').trim();
			if (!searchText || searchText.length < 3) continue;
			let idx = answerText.indexOf(searchText);
			if (idx === -1) idx = answerText.toLowerCase().indexOf(searchText.toLowerCase());
			if (idx === -1) continue;
			const found = { start: idx, end: idx + searchText.length };
			// Check overlap with existing matches
			let overlaps = false;
			let existingIdx = -1;
			for (let ri = 0; ri < used.length; ri++) {
				const r = used[ri];
				if (found.start === r.start && found.end === r.end) {
					existingIdx = ri;
					break;
				}
				if (found.start < r.end && found.end > r.start) { overlaps = true; break; }
			}
			if (existingIdx >= 0) {
				const existing = matches[existingIdx];
				existing.matchedBy = [...new Set([...existing.matchedBy, ...matchedBy])];
				existing.id = existing.id + ', ' + id;
			} else if (!overlaps) {
				used.push(found);
				matches.push({ start: found.start, end: found.end, matchedBy, id });
			}
		}
		if (matches.length === 0) return escapeHtml(answerText);
		matches.sort((a, b) => a.start - b.start);
		let html = '';
		let pos = 0;
		for (const m of matches) {
			html += escapeHtml(answerText.slice(pos, m.start));
			let cls: string;
			let title: string;
			if (m.matchedBy.length === 0) {
				cls = 'bg-red-100 border-b-2 border-red-300 rounded';
				title = m.id + ': Unmatched';
			} else if (m.matchedBy.length > 1) {
				cls = 'bg-amber-100 border-b-2 border-amber-300 rounded';
				title = m.id + ': Duplicate (' + m.matchedBy.slice(0, 3).join(', ') + (m.matchedBy.length > 3 ? '...' : '') + ')';
			} else {
				cls = 'bg-green-100 border-b-2 border-green-300 rounded';
				title = m.id + ': ' + m.matchedBy[0];
			}
			html += '<mark class="' + cls + '" title="' + escapeHtml(title) + '">' + escapeHtml(answerText.slice(m.start, m.end)) + '</mark>';
			pos = m.end;
		}
		html += escapeHtml(answerText.slice(pos));
		return html;
	}
</script>

<main class="flex h-full flex-1 flex-col overflow-y-auto bg-gray-50">
	<div class="mx-auto w-full max-w-6xl p-6 space-y-6">
		<div class="flex items-center justify-between">
			<h2 class="text-lg font-semibold text-gray-800">Evaluation Dashboard</h2>
			<button
				onclick={load}
				class="rounded-lg border border-gray-300 bg-white px-3 py-1.5 text-xs text-gray-600 hover:bg-gray-50"
			>
				Aktualisieren
			</button>
		</div>

		{#if loading}
			<p class="text-sm text-gray-400 animate-pulse">Laden...</p>
		{:else if error}
			<div class="rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-700">
				{error}
			</div>
		{:else}
			<!-- Summary Cards -->
			<div class="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-8">
				<div class="rounded-xl border border-gray-200 bg-white p-4 shadow-sm">
					<p class="text-[10px] font-semibold uppercase tracking-wider text-gray-400">Anfragen</p>
					<p class="mt-1 text-2xl font-bold text-gray-800">{data.summary.total}</p>
				</div>
				<div class="rounded-xl border border-gray-200 bg-white p-4 shadow-sm">
					<p class="text-[10px] font-semibold uppercase tracking-wider text-gray-400">&empty; Schritte</p>
					<p class="mt-1 text-2xl font-bold text-gray-800">{data.summary.avg_steps}</p>
				</div>
				<div class="rounded-xl border border-gray-200 bg-white p-4 shadow-sm">
					<p class="text-[10px] font-semibold uppercase tracking-wider text-gray-400">&empty; Chunks</p>
					<p class="mt-1 text-2xl font-bold text-gray-800">{data.summary.avg_chunks}</p>
				</div>
				<div class="rounded-xl border border-gray-200 bg-white p-4 shadow-sm">
					<p class="text-[10px] font-semibold uppercase tracking-wider text-gray-400">&empty; Antwort</p>
					<p class="mt-1 text-2xl font-bold text-gray-800">{data.summary.avg_answer_length}</p>
					<p class="text-[10px] text-gray-400">Zeichen</p>
				</div>
				<div class="rounded-xl border border-gray-200 bg-white p-4 shadow-sm">
					<p class="text-[10px] font-semibold uppercase tracking-wider text-gray-400">Ausreichend</p>
					<p class="mt-1 text-2xl font-bold text-gray-800">{data.summary.sufficient_pct}%</p>
					<p class="text-[10px] text-gray-400">{data.summary.sufficient_count}/{data.summary.total}</p>
				</div>
				{#if data.summary.avg_processing_ms}
					<div class="rounded-xl border border-gray-200 bg-white p-4 shadow-sm">
						<p class="text-[10px] font-semibold uppercase tracking-wider text-gray-400">&empty; Laufzeit</p>
						<p class="mt-1 text-2xl font-bold text-gray-800">{(data.summary.avg_processing_ms / 1000).toFixed(1)}s</p>
					</div>
				{/if}
				{#if data.summary.avg_total_tokens}
					<div class="rounded-xl border border-gray-200 bg-white p-4 shadow-sm">
						<p class="text-[10px] font-semibold uppercase tracking-wider text-gray-400">&empty; Tokens</p>
						<p class="mt-1 text-2xl font-bold text-gray-800">{@html formatTokens(data.summary.avg_total_tokens)}</p>
					</div>
				{/if}
				{#if data.summary.avg_retrieval_quality != null}
					<div class="rounded-xl border border-gray-200 bg-white p-4 shadow-sm">
						<p class="text-[10px] font-semibold uppercase tracking-wider text-gray-400">&empty; RQ</p>
						<p class="mt-1 text-2xl font-bold text-gray-800">{data.summary.avg_retrieval_quality.toFixed(2)}</p>
					</div>
				{/if}
				{#if data.summary.compliance_total != null}
					<div class="rounded-xl border border-gray-200 bg-white p-4 shadow-sm">
						<p class="text-[10px] font-semibold uppercase tracking-wider text-gray-400">Compliance</p>
						<p class="mt-1 text-2xl font-bold text-gray-800">{data.summary.compliance_total}</p>
						<p class="text-[10px] text-gray-400">geprüft</p>
					</div>
					<div class="rounded-xl border border-green-200 bg-green-50/50 p-4 shadow-sm">
						<p class="text-[10px] font-semibold uppercase tracking-wider text-green-600">OK</p>
						<p class="mt-1 text-2xl font-bold text-green-700">{data.summary.compliance_ok || 0}</p>
					</div>
					<div class="rounded-xl border border-amber-200 bg-amber-50/50 p-4 shadow-sm">
						<p class="text-[10px] font-semibold uppercase tracking-wider text-amber-600">Rewrite</p>
						<p class="mt-1 text-2xl font-bold text-amber-700">{data.summary.compliance_rewrite || 0}</p>
					</div>
					<div class="rounded-xl border border-red-200 bg-red-50/50 p-4 shadow-sm">
						<p class="text-[10px] font-semibold uppercase tracking-wider text-red-600">Refuse</p>
						<p class="mt-1 text-2xl font-bold text-red-700">{data.summary.compliance_refuse || 0}</p>
					</div>
					<div class="rounded-xl border border-gray-200 bg-white p-4 shadow-sm">
						<p class="text-[10px] font-semibold uppercase tracking-wider text-gray-400">Issues</p>
						<p class="mt-1 text-2xl font-bold text-gray-800">{data.summary.compliance_total_issues || 0}</p>
						<p class="text-[10px] text-gray-400">gesamt</p>
					</div>
				{/if}
			</div>

			{#if data.summary.compliance_category_totals}
			<div class="mt-2 grid grid-cols-2 gap-2 sm:grid-cols-5">
				<div class="rounded-lg border border-blue-200 bg-blue-50/50 px-3 py-2 text-center">
					<p class="text-[10px] font-semibold text-blue-600">Zitierabdeckung</p>
					<p class="text-lg font-bold text-blue-700">{data.summary.compliance_category_totals.citation_coverage || 0}</p>
				</div>
				<div class="rounded-lg border border-purple-200 bg-purple-50/50 px-3 py-2 text-center">
					<p class="text-[10px] font-semibold text-purple-600">Quellenechtheit</p>
					<p class="text-lg font-bold text-purple-700">{data.summary.compliance_category_totals.source_authenticity || 0}</p>
				</div>
				<div class="rounded-lg border border-red-200 bg-red-50/50 px-3 py-2 text-center">
					<p class="text-[10px] font-semibold text-red-600">Halluzination</p>
					<p class="text-lg font-bold text-red-700">{data.summary.compliance_category_totals.hallucination || 0}</p>
				</div>
				<div class="rounded-lg border border-amber-200 bg-amber-50/50 px-3 py-2 text-center">
					<p class="text-[10px] font-semibold text-amber-600">Use-Case</p>
					<p class="text-lg font-bold text-amber-700">{data.summary.compliance_category_totals.use_case_policy || 0}</p>
				</div>
				<div class="rounded-lg border border-gray-200 bg-gray-50/50 px-3 py-2 text-center">
					<p class="text-[10px] font-semibold text-gray-500">Unbekannt</p>
					<p class="text-lg font-bold text-gray-600">{data.summary.compliance_category_totals.unknown || 0}</p>
				</div>
			</div>
			{/if}

			<!-- Queries -->
			{#if data.queries.length === 0}
				<div class="flex h-40 items-center justify-center rounded-lg border border-gray-200 bg-white text-gray-400">
					<p class="text-sm">Noch keine Anfragen gespeichert. F&uuml;hre zuerst eine Query aus.</p>
				</div>
			{:else}
				<div class="space-y-2">
					{#each data.queries as q (q.id)}
						{@const isOpen = expandedId === q.id}
						{@const steps = (q.agent_steps || []) as Record<string, unknown>[]}
						<!-- Row header (clickable) -->
						<button
							onclick={() => toggle(q.id)}
							class="w-full rounded-xl border border-gray-200 bg-white p-4 text-left shadow-sm transition hover:border-gray-300 hover:shadow-md"
						>
							<div class="flex items-center gap-3">
								<span class="text-xs text-gray-400 transition-transform" class:rotate-90={isOpen}>&#9654;</span>
								<span class="whitespace-nowrap text-xs text-gray-500">
									{q.created_at ? new Date(q.created_at).toLocaleString("de-AT", { day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "&ndash;"}
								</span>
								<span class="flex-1 truncate text-sm font-medium text-gray-800">{q.query_text}</span>
								<span class="inline-flex items-center gap-1 rounded-full bg-indigo-50 px-2 py-0.5 text-[10px] font-semibold text-indigo-700">
									{q.step_count}
									{#if q.subtask_count > 1}
										<span class="text-indigo-400">({q.subtask_count}x)</span>
									{/if}
								</span>
								<span class="font-mono text-xs text-gray-500">{q.chunk_count} Chunks</span>
								<span class="font-mono text-xs text-gray-500">{q.answer_length} Z.</span>
								{#if q.total_tokens}
									<span class="font-mono text-xs text-gray-500" title="Total Tokens">{@html formatTokens(q.total_tokens)} Tok.</span>
								{/if}
								{#if q.retrieval_quality !== null}
									<span class="font-mono text-xs text-gray-500" title="Best Retrieval Quality">RQ: {q.retrieval_quality.toFixed(2)}</span>
								{/if}
								<span class="font-mono text-xs text-gray-500">
									{#if q.processing_ms}
										{(q.processing_ms / 1000).toFixed(1)}s
									{:else}
										&ndash;
									{/if}
								</span>
								{#if q.sufficient}
									<span class="inline-flex items-center gap-1 rounded-full bg-green-50 px-2 py-0.5 text-[10px] font-semibold text-green-700">OK</span>
								{:else}
									<span class="inline-flex items-center gap-1 rounded-full bg-amber-50 px-2 py-0.5 text-[10px] font-semibold text-amber-700">Unvollst.</span>
								{/if}
								{#if q.model}
									<span class="ml-auto text-[10px] text-gray-400" title="Modell">{q.model}</span>
								{/if}
							</div>
						</button>

						<!-- Expanded detail -->
						{#if isOpen}
							<div class="rounded-xl border border-gray-200 bg-white p-6 shadow-sm space-y-6">
								<!-- Answer -->
								{#if q.answer_text}
									<div>
										<h4 class="mb-2 text-xs font-semibold uppercase tracking-wider text-gray-400">Antwort</h4>
										<div class="prose prose-sm max-w-none rounded-lg bg-gray-50 p-4 text-sm leading-relaxed text-gray-700 whitespace-pre-wrap">{q.answer_text}</div>
									</div>
								{/if}

								<!-- Agent Trace -->
								{#if steps.length > 0}
									{@const stepTimes = computeStepTimes(steps, q.created_at)}
									<div>
										<h4 class="mb-3 text-xs font-semibold uppercase tracking-wider text-gray-400">Agent-Trace ({steps.length} Schritte)</h4>
										<div class="space-y-2">
											{#each steps as step, i}
												{@const action = (step.action as string) || "?"}
												{@const label = actionLabels[action] || action}
												{@const color = actionColors[action] || "border-gray-300 bg-gray-50"}
												{@const dot = actionDots[action] || "bg-gray-400"}
												{@const duration = (step.duration_ms as number) || 0}									{@const llmTiming = (step.llm_timing as { load_ms: number; pp_ms: number; tp_ms: number; total_ms: number; prompt_tokens: number; completion_tokens: number } | null) || null}
												{@const thought = (step.thought as string) || ""}
												{@const observation = (step.observation as string) || ""}
												{@const args = (step.args as Record<string,unknown> | null) || null}
												{@const chunks = (step.chunks as unknown[]) || []}
												{@const stepMaxRq = chunks.length > 0 ? chunks.reduce((max, c) => { const r = (c as Record<string,unknown>); const s = (r.rerank_score ?? r.score) as number; return s != null && s > max ? s : max; }, -Infinity) : -1}
												{@const stepRqScores = chunks.map(c => ((c as Record<string,unknown>).rerank_score ?? (c as Record<string,unknown>).score) as number).filter(s => s != null)}
												{@const stepAvgRq = stepRqScores.length > 0 ? stepRqScores.reduce((a, b) => a + b, 0) / stepRqScores.length : -1}
												{@const stepMinRq = stepRqScores.length > 0 ? Math.min(...stepRqScores) : -1}
												{@const subagent = (step.subagent_id as string) || ""}
												<div class="rounded-lg border-l-4 {color} p-4">
													<div class="mb-1 flex items-center gap-2">
														<span class="inline-block h-2 w-2 rounded-full {dot}"></span>
														<span class="text-xs font-semibold text-gray-700">Schritt {i + 1}: {label}</span>
													</div>
													<div class="flex items-center gap-2">
														{#if subagent}
															<span class="text-[10px] text-gray-400">{subagent}</span>
														{/if}
														{#if duration > 0}
															<span class="text-[10px] text-gray-400">&middot; {(duration / 1000).toFixed(2)}s</span>
															{#if q.created_at}
																<span class="text-[10px] text-gray-400">&middot; Time: {stepTimes[i]}</span>
															{/if}
															{#if llmTiming && llmTiming.total_ms > 0}										{#if typeof duration === 'number' && typeof llmTiming?.total_ms === 'number'}{@const otherMs = duration - llmTiming.total_ms}{#if otherMs > 100}<span class="text-[10px] text-amber-500">&middot; Embed: {(otherMs / 1000).toFixed(1)}s</span>{/if}{/if}
																			<span class="text-[10px] text-gray-400">&middot; LLM: {(llmTiming.total_ms / 1000).toFixed(1)}s</span>
																		<span class="text-[10px] text-gray-400">(L:{(llmTiming.load_ms / 1000).toFixed(1)} PP:{(llmTiming.pp_ms / 1000).toFixed(1)} TP:{(llmTiming.tp_ms / 1000).toFixed(1)}{#if llmTiming.prompt_tokens || llmTiming.completion_tokens} &middot; In: {llmTiming.prompt_tokens}, Out: {llmTiming.completion_tokens} Tok.{/if})</span>
															{/if}
														{/if}

													</div>
													{#if action === 'COMPLIANCE_CHECK' && args}
														{@const verdict = (args.verdict as string) || ''}
														{@const issues = (args.issues as string[]) || []}
														<div class="mt-1 mb-1 flex items-center gap-2">
															{#if verdict === 'OK'}
																<span class="inline-flex items-center gap-1 rounded-full bg-green-100 px-2 py-0.5 text-[10px] font-semibold text-green-700">&#10003; OK</span>
															{:else if verdict === 'REWRITE'}
																<span class="inline-flex items-center gap-1 rounded-full bg-amber-100 px-2 py-0.5 text-[10px] font-semibold text-amber-700">&#9888; REWRITE</span>
															{:else if verdict === 'REFUSE'}
																<span class="inline-flex items-center gap-1 rounded-full bg-red-100 px-2 py-0.5 text-[10px] font-semibold text-red-700">&#10007; REFUSE</span>
															{:else}
																<span class="inline-flex items-center gap-1 rounded-full bg-gray-100 px-2 py-0.5 text-[10px] font-semibold text-gray-600">{verdict || 'UNKNOWN'}</span>
															{/if}
															{#if issues.length > 0}
																<span class="text-[10px] text-gray-400">{issues.length} issue{issues.length !== 1 ? 's' : ''}</span>
															{/if}
														</div>
														{@const classified = (args.classified_issues as any[]) || []}
														{#if classified.length > 0}
															<details class="mt-1">
																<summary class="cursor-pointer text-[10px] font-semibold uppercase tracking-wider text-rose-400 hover:text-rose-600">Issues ({classified.length})</summary>
																<ul class="mt-1 space-y-1 pl-0">
																	{#each classified as ci}
																		<li class="flex items-start gap-1.5 text-[10px] leading-relaxed text-gray-600">
																			<span class="mt-0.5 shrink-0 rounded border px-1 py-0 text-[8px] font-bold uppercase {ci.category === 'citation_coverage' ? 'border-blue-200 bg-blue-50 text-blue-700' : ci.category === 'source_authenticity' ? 'border-purple-200 bg-purple-50 text-purple-700' : ci.category === 'hallucination' ? 'border-red-200 bg-red-50 text-red-700' : ci.category === 'use_case_policy' ? 'border-amber-200 bg-amber-50 text-amber-700' : 'border-gray-200 bg-gray-50 text-gray-600'}">
																				{ci.category_label}
																			</span>
																			<span class="pt-0.5">{ci.text}</span>
																		</li>
																	{/each}
																</ul>
															</details>
														{:else if issues.length > 0}
															<details class="mt-1">
																<summary class="cursor-pointer text-[10px] font-semibold uppercase tracking-wider text-rose-400 hover:text-rose-600">Issues ({issues.length})</summary>
																<ul class="mt-1 list-inside list-disc space-y-0.5 text-[10px] leading-relaxed text-gray-600">
																	{#each issues as issue}
																		<li>{issue}</li>
																	{/each}
																</ul>
															</details>
														{/if}
													{/if}
													{#if thought}
														<details class="mt-2">
															<summary class="cursor-pointer text-[10px] font-semibold uppercase tracking-wider text-gray-400 hover:text-gray-600">Thought</summary>
															<p class="mt-1 text-xs leading-relaxed text-gray-600 whitespace-pre-wrap">{thought}</p>
														</details>
													{/if}
												{#if observation}
													<details class="mt-2">
														<summary class="cursor-pointer text-[10px] font-semibold uppercase tracking-wider text-gray-400 hover:text-gray-600">Observation</summary>
														<p class="mt-1 text-xs leading-relaxed text-gray-600 whitespace-pre-wrap">{observation}</p>
													</details>
												{/if}
												{#if chunks.length > 0}
													<details class="mt-2">
														<summary class="cursor-pointer text-[10px] font-semibold uppercase tracking-wider text-gray-400 hover:text-gray-600">
															{chunks.length} Chunks{#if stepMaxRq !== -1} (max: {stepMaxRq.toFixed(3)}, avg: {stepAvgRq.toFixed(3)}, min: {stepMinRq.toFixed(3)}){/if}
														</summary>
														<div class="mt-2 max-h-80 overflow-y-auto">
															<table class="w-full text-[10px]">
																<thead>
																	<tr class="border-b border-gray-200 text-left text-gray-400">
																		<th class="py-1 pr-3 font-semibold w-8">#</th>
																		<th class="py-1 pr-3 font-semibold">RQ</th>
																		<th class="py-1 pr-3 font-semibold">&Delta;</th>
																		<th class="py-1 pr-3 font-semibold" title="Similarity to Rank 1">SimR1</th>
																		<th class="py-1 font-semibold" title="Answer Similarity">Ans</th>
																	</tr>
																</thead>
																<tbody>
																	{#each chunks as chunk, ci}
																		{@const c = chunk as Record<string,unknown>}
																		{@const cScore = (c.rerank_score ?? c.score) as number | undefined}
																		{@const cSimR1 = c.similarity_to_rank_1 as number | undefined}
																		{@const cAnsSim = c.answer_similarity as number | undefined}
																		<tr class="border-b border-gray-50 hover:bg-gray-50">
																			<td class="py-1 pr-3 font-semibold text-gray-700">{ci + 1}</td>
																			<td class="py-1 pr-3">
																				{#if cScore != null}
																					<span class="inline-flex items-center gap-1 rounded-full bg-blue-50 px-1.5 py-0.5 text-[9px] font-semibold text-blue-700">{cScore.toFixed(3)}</span>
																				{/if}
																			</td>
																			<td class="py-1 pr-3">
																				{#if cScore != null && stepMaxRq !== -1}
																					{#if ci === 0}
																						<span class="text-gray-400">(best)</span>
																					{:else if cScore < stepMaxRq}
																						<span class="text-red-400">{(cScore - stepMaxRq).toFixed(3)}</span>
																					{/if}
																				{/if}
																			</td>
																			<td class="py-1 pr-3">
																				{#if cSimR1 != null}
																					<span class="text-gray-400">{cSimR1.toFixed(3)}</span>
																				{/if}
																			</td>
																			<td class="py-1">
																				{#if cAnsSim != null}
																					<span class="text-gray-400">{cAnsSim.toFixed(3)}</span>
																				{/if}
																			</td>
																		</tr>
																	{/each}
																</tbody>
															</table>
														</div>
													</details>
												{/if}
											</div>
											{/each}
										</div>
									</div>
								{/if}

								<!-- Chunk Utilization Analysis (offline, per-query) -->
								<div class="border-t border-gray-100 pt-4">
									{#if analysisData && (analysisData as Record<string,unknown>).query_id === q.id}
										{@const ad = analysisData as Record<string,unknown>}
										{@const adSteps = (ad.steps as unknown[]) || []}							<h4 class="mb-2 text-xs font-semibold uppercase tracking-wider text-gray-400">Chunk-Nutzungsanalyse</h4>
											{@const adMode = (ad.mode as string) || "sentence"}
											{@const isClaim = adMode === "claim"}
											{@const unitName = isClaim ? "Claims" : "S&auml;tze"}
											<p class="mb-1 text-[10px] text-gray-400">
												Modus: {adMode} &middot; Cosine Similarity Threshold: {ad.threshold as number}
											</p>

											<!-- Highlighted Answer -->
											{#if (ad.answer_text as string)}
												{@const adModelsArr = (ad.models as string[] | undefined)}
												{@const mergedForHighlight = mergeAnswerClaims(adSteps)}
												{@const firstClaims = adModelsArr && adModelsArr.length > 0 && Object.keys(mergedForHighlight).length > 0 ? mergedForHighlight[adModelsArr[0]] : (Object.values(mergedForHighlight)[0] as unknown[] | undefined)}
												<details class="mb-3 rounded border border-gray-200 bg-gray-50/50 p-3" open>
													<summary class="cursor-pointer text-[10px] font-semibold text-gray-600 hover:text-gray-800">Antwort (mit Claim-Hervorhebung)</summary>
													<div class="mt-2 max-h-96 overflow-y-auto rounded bg-white p-3 text-sm leading-relaxed text-gray-700 whitespace-pre-wrap">
														{#if firstClaims && firstClaims.length > 0}
															{@html highlightAnswerText(ad.answer_text as string, firstClaims)}
														{:else}
															{ad.answer_text as string}
														{/if}
													</div>
												</details>
											{/if}

											<!-- Answer Claims Overview -->
											{@const adModels = ad.models as string[] | undefined}
											{#if adModels}
												{@const mergedClaimsByModel = mergeAnswerClaims(adSteps)}
												{#if Object.keys(mergedClaimsByModel).length > 0}
													<div class="grid grid-cols-2 gap-2 mb-3">
														{#each Object.entries(mergedClaimsByModel) as [modelName, answerClaims]}
															{#if answerClaims && answerClaims.length > 0}
																{@const dupes = countDuplicates(answerClaims)}
																{@const unmatched = countUnmatched(answerClaims)}
																{@const matched = answerClaims.length - unmatched - dupes}
																<details class="rounded border border-gray-200 bg-gray-50/50 p-2">
																	<summary class="cursor-pointer text-[9px] font-semibold text-gray-600 hover:text-gray-800">
																		<span class="text-indigo-600">{modelName}</span> &mdash; {answerClaims.length} Claims &mdash; M: {matched + dupes} ({(((matched + dupes)/answerClaims.length)*100).toFixed(0)}%) [{matched}&#10003; + {dupes}&#9888;] | U: {unmatched} ({((unmatched/answerClaims.length)*100).toFixed(0)}%)<span class="ml-1 font-normal text-gray-400">{catSummary(answerClaims)}</span>
																	</summary>
																	<div class="mt-1 max-h-40 overflow-y-auto space-y-0.5">
																		{#each answerClaims as ac}
																			{@const a = ac as Record<string,unknown>}
																			{@const matchedBy = (a.matched_by_chunks as string[]) || []}
																			<div class="rounded px-2 py-1 text-[9px] leading-relaxed {matchedBy.length > 1 ? 'bg-amber-50' : matchedBy.length === 0 ? 'bg-red-50' : 'bg-green-50'}">
																				<div class="flex items-start gap-1.5">
																					<span class="font-semibold text-gray-500 whitespace-nowrap mt-px">{a.id as string}</span>
																					{#if (a.category as string)}<span class="ml-1 rounded px-1 py-px text-[8px] font-semibold {catClass(a.category as string)}">{catLabel(a.category as string)}      {#if (a.category_confidence as number) != null} 
         <span class="font-normal opacity-60">{a.category_confidence as number}%</span>
       {/if}</span>{/if}
																					<span class="text-gray-600 break-words">{(a.text as string)?.substring(0, 200)}</span>
																				</div>
																				<div class="mt-0.5">
																					{#if matchedBy.length > 1}
																						<span class="text-amber-600 font-semibold">&#9888; Duplicate matches: {matchedBy.slice(0, 8).join(", ")}{#if matchedBy.length > 8} +{matchedBy.length - 8} more{/if}</span>
																					{:else if matchedBy.length === 0}
																						<span class="text-red-500">&#10060; Not matched{#if (a.best_similarity as number) != null} (cosine: {(a.best_similarity as number).toFixed(2)}{#if (a.best_nli as string)}, NLI: {a.best_nli as string}{/if}){/if}</span>
																					{:else}
																						<span class="text-green-500">&check; {matchedBy[0]}</span>
																					{/if}
																				</div>
																			</div>
																		{/each}
																	</div>
																</details>
															{/if}
														{/each}
													</div>
												{/if}
											{:else}
												{@const merged = mergeAnswerClaims(adSteps)}
												{@const answerClaims = Object.values(merged)[0] as unknown[] | undefined}
											{#if answerClaims && answerClaims.length > 0}
												{@const dupes = countDuplicates(answerClaims)}
												{@const unmatched = countUnmatched(answerClaims)}
												{@const matched = answerClaims.length - unmatched - dupes}
												<details class="mb-3 rounded border border-gray-200 bg-gray-50/50 p-3">
													<summary class="cursor-pointer text-[10px] font-semibold text-gray-600 hover:text-gray-800">
														Answer Claims ({answerClaims.length} total) &mdash; Matched: {matched + dupes} ({(((matched + dupes)/answerClaims.length)*100).toFixed(0)}%) [{matched}&#10003; + {dupes}&#9888;] | Unmatched: {unmatched} ({((unmatched/answerClaims.length)*100).toFixed(0)}%)<span class="ml-1 font-normal text-gray-400">{catSummary(answerClaims)}</span>
													</summary>
													<div class="mt-2 max-h-60 overflow-y-auto space-y-1">
														{#each answerClaims as ac}
															{@const a = ac as Record<string,unknown>}
															{@const matchedBy = (a.matched_by_chunks as string[]) || []}
															<div class="rounded px-2 py-1 text-[10px] leading-relaxed {matchedBy.length > 1 ? 'bg-amber-50' : matchedBy.length === 0 ? 'bg-red-50' : 'bg-green-50'}">
																<span class="font-semibold text-gray-500">{a.id as string}</span>
															{#if (a.category as string)}<span class="ml-1 rounded px-1 py-px text-[9px] font-semibold {catClass(a.category as string)}">{catLabel(a.category as string)}      {#if (a.category_confidence as number) != null} 
         <span class="font-normal opacity-60">{a.category_confidence as number}%</span>
       {/if}</span>{/if}
																<span class="ml-1 text-gray-600">{(a.text as string)?.substring(0, 150)}</span>
																{#if matchedBy.length > 1}
																	<span class="ml-1 text-amber-600 font-semibold">&#9888; Duplicate matches: {matchedBy.slice(0, 8).join(", ")}{#if matchedBy.length > 8} +{matchedBy.length - 8} more{/if}</span>
																{:else if matchedBy.length === 0}
																	<span class="ml-1 text-red-500">&#10060; Not matched{#if (a.best_similarity as number) != null} (cosine: {(a.best_similarity as number).toFixed(2)}{#if (a.best_nli as string)}, NLI: {a.best_nli as string}{/if}){/if}</span>
																{:else}
																	<span class="ml-1 text-green-500">&check; {matchedBy[0]}</span>
																{/if}
															</div>
														{/each}
													</div>
												</details>
											{/if}
										{/if}

											<!-- Per-step analysis -->
											<div class="space-y-3">
											{#each adSteps as stepObj}
												{@const so = stepObj as Record<string,unknown>}
												{@const stepAnalyses = so.analyses as Record<string, Record<string,unknown>> | undefined}
												{@const stepModels = stepAnalyses
													? Object.entries(stepAnalyses).filter(([name]) => name !== "_judge").map(([name, a]) => ({model: name, analysis: a}))
													: [{model: null, analysis: so.analysis as Record<string,unknown>}]
												}
												<div class={stepModels.length > 1 ? 'grid grid-cols-2 gap-3' : ''}>
												{#each stepModels as {model, analysis}}
													{@const summary = analysis.summary as Record<string,unknown>}
													{@const perChunk = (analysis.per_chunk as unknown[]) || []}
													{#if (so.chunk_count as number) > 0 && perChunk.length > 0}
														<details class="rounded border border-gray-200 bg-gray-50/50 p-3" open={model == null || model === Object.keys(stepAnalyses || {})[0]}>
															<summary class="cursor-pointer text-[10px] font-semibold text-gray-600 hover:text-gray-800">
																{#if model}<span class="text-indigo-600">{model}</span> &mdash; {/if}{so.step_label as string} ({so.chunk_count as number} Chunks)
																&mdash; &empty; Nutzung: {summary.avg_utilization_pct as number}% &middot; &empty; Abdeckung: {summary.avg_attribution_pct as number}%
															</summary>
														<div class="mt-2 max-h-60 overflow-y-auto">
															<table class="w-full text-[10px]">
																<thead>
																	<tr class="border-b border-gray-200 text-left text-gray-400">
																		<th class="py-1 pr-3 font-semibold w-8">#</th>
																		<th class="py-1 pr-3 font-semibold">Genutzt</th>
																		<th class="py-1 font-semibold">Antwort-Abdeckung</th>
																	</tr>
																</thead>
																<tbody>
																	{#each perChunk as pc, pci}
																					{@const p = pc as Record<string,unknown>}
																		{@const util = p.utilization_pct as number}
																		{@const attr = p.attribution_pct as number}
																		{@const totalUnits = (p.total_units as number) ?? (p.total_sentences as number) ?? 0}
																		{@const matchedUnits = (p.matched_units as number) ?? (p.matched_sentences as number) ?? 0}
																		{@const claims = (p.claims as unknown[]) || []}
																		{@const claimKey = `${(so.step_index ?? pci) as number}-${p.rank as number}`}
																		{@const claimsOpen = expandedClaimChunks[claimKey] === true}
																		<tr class="border-b border-gray-100 hover:bg-gray-50 cursor-pointer" onclick={() => toggleClaims(claimKey)}>
																			<td class="py-1 pr-3 font-semibold text-gray-700">
																				<span class="mr-1 text-gray-300">{claimsOpen ? '▼' : '▶'}</span>
																				{p.rank as number}
																			</td>
																			<td class="py-1 pr-3">
																				<span class="inline-flex items-center gap-1 rounded-full px-1.5 py-0.5 text-[9px] font-semibold {util >= 60 ? 'bg-green-50 text-green-700' : util >= 30 ? 'bg-amber-50 text-amber-700' : 'bg-red-50 text-red-700'}">{util}%</span>
																				<span class="text-gray-400">({matchedUnits}/{totalUnits} {@html unitName})</span>
																			</td>
																			<td class="py-1">
																				<span class="inline-flex items-center gap-1 rounded-full px-1.5 py-0.5 text-[9px] font-semibold {attr >= 40 ? 'bg-green-50 text-green-700' : attr >= 15 ? 'bg-amber-50 text-amber-700' : 'bg-red-50 text-red-700'}">{attr}%</span>
																			</td>
																		</tr>
																		{#if claimsOpen}
																			<tr>
																				<td colspan="3" class="py-1 pl-6">
																					<div class="space-y-0.5">
																							<!-- Full chunk text -->
																							{#if p.chunk_text as string}
																								<div class="text-[9px] font-semibold text-gray-400 mb-0.5">Chunk-Text:</div>
																								<div class="mb-1.5 max-h-96 overflow-y-auto rounded bg-blue-50/40 p-1.5 text-[9px] text-gray-600 leading-relaxed whitespace-pre-wrap break-words">{p.chunk_text as string}</div>
																								{#if claims.length > 0}<div class="border-t border-gray-100 pt-0.5 mb-0.5"></div>{/if}
																							{/if}
																							<div class="max-h-40 overflow-y-auto space-y-0.5">
																							{#each claims as claim}
																							{@const cl = claim as Record<string,unknown>}												{@const matchedTo = cl.matched_to as Array<{id: string, sim: number, entailment?: string}> | null}
												{@const bestMatch = cl.best_match as {id: string, sim: number, entailment?: string} | null}
												<div class="flex flex-col gap-0.5 text-[9px] leading-relaxed {matchedTo ? 'text-green-700' : (bestMatch ? 'text-amber-600' : 'text-red-500')}">
																								<div class="flex items-start gap-2">
																									<span class="font-semibold text-gray-400 whitespace-nowrap">{cl.id as string}</span>
																									<span class="break-words">{(cl.text as string)?.substring(0, 200)}</span>
																								</div>
																								{#if matchedTo && matchedTo.length > 0}
																									<div class="text-green-500 font-semibold break-words">&rarr; {matchedTo.slice(0, 8).map(m => m.id + " (" + Math.round(m.sim * 100) + "%" + (m.entailment ? ", " + m.entailment : "") + ")").join(", ")}{#if matchedTo.length > 8} +{matchedTo.length - 8} more{/if} &check;</div>												{:else if bestMatch}
													<div class="break-words">&rarr; {bestMatch.id} ({Math.round(bestMatch.sim * 100)}%{bestMatch.entailment && bestMatch.entailment !== '?' ? ', ' + bestMatch.entailment : ''}) &cross;</div>
												{:else}
													<div class="text-red-400">&rarr; &times;</div>
																								{/if}
																							</div>
																						{/each}
																					</div>
																				</div>
																				</td>
																			</tr>
																		{/if}
																	{/each}
																</tbody>
															</table>
														</div>
													</details>
												{/if}
											{/each}
											<!-- Judge Review Section -->
											{#if stepAnalyses?._judge}
												{@const judge = stepAnalyses._judge as {model: string; review_text: string; confidence: number}}
												{@const confPct = Math.round(judge.confidence * 100)}
												<details class="rounded border border-purple-200 bg-purple-50/30" open>
													<summary class="cursor-pointer px-2 py-1.5 text-[10px] font-semibold text-purple-700 hover:text-purple-900 select-none">
														Factual Summary
														<span class="ml-2 inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[9px] font-bold {confPct >= 70 ? 'bg-green-100 text-green-700' : confPct >= 40 ? 'bg-amber-100 text-amber-700' : 'bg-red-100 text-red-700'}">
															{confPct === 100 ? 'Computed from data' : 'Confidence: ' + confPct + '%'}
														</span>
													</summary>
													<div class="px-2 pb-2 pt-1">
														<p class="text-[10px] leading-relaxed text-purple-800 whitespace-pre-line">{judge.review_text}</p>
													</div>
												</details>
											{/if}
										</div>
									{/each}
								</div>
									{:else if analysisError && !analysisData}
										<p class="text-[10px] text-red-500">{analysisError}</p>
									{:else}
										<div class="flex items-center gap-2">
											<select
												bind:value={analysisConcurrency}
												class="rounded border border-gray-300 bg-white px-2 py-1 text-[10px] text-gray-500"
												title="Parallel requests"
											>
												<option value={1}>1 parallel</option>
												<option value={2}>2 parallel</option>
												<option value={4}>4 parallel</option>
												<option value={8}>8 parallel</option>
											</select>
											<select
												bind:value={analysisModel1}
												class="rounded border border-gray-300 bg-white px-2 py-1 text-[10px] text-gray-500"
												title="Extraction model (answer + chunk claims)"
											>
												{#each availableModels as m}
													<option value={m}>{m}</option>
												{/each}
											</select>
											<select
												bind:value={analysisModel2}
												class="rounded border border-gray-300 bg-white px-2 py-1 text-[10px] text-gray-500"
												title="Mapping model (claim-to-answer alignment; same as extraction if empty)"
											>
												<option value="">(same)</option>
												{#each availableModels as m}
													<option value={m}>{m}</option>
												{/each}
											</select>

											<button
												onclick={() => analyzeChunks(q.id)}
												disabled={analyzingId === q.id}
												class="rounded-lg border border-gray-300 bg-white px-3 py-1.5 text-[10px] font-semibold text-gray-500 hover:border-gray-400 hover:text-gray-700 disabled:opacity-50"
											>
												{#if analyzingId === q.id}
													<span class="spinner"></span> Analysiere... ({analysisProgress ? analysisProgress + ", " : ""}{analysisElapsed}s)
												{:else}
													Chunk-Nutzung analysieren
												{/if}
											</button>
										</div>
									{/if}
								</div>
							</div>
						{/if}
					{/each}
				</div>
			{/if}
		{/if}
	</div>
</main>


<style>
  .spinner {
    display: inline-block;
    width: 10px;
    height: 10px;
    border: 2px solid #d1d5db;
    border-top-color: #6b7280;
    border-radius: 50%;
    animation: spin 0.6s linear infinite;
    margin-right: 4px;
    vertical-align: middle;
  }
  @keyframes spin {
    to { transform: rotate(360deg); }
  }
</style>
