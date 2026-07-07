<script lang="ts">
	import { onMount } from 'svelte';

	interface FeedbackRow {
		id: string;
		query_id: string | null;
		rating: 'positive' | 'negative';
		comment: string | null;
		created_at: string | null;
		use_case: string | null;
		session_id: string | null;
		role: string | null;
		query_text: string | null;
		answer_text: string | null;
	}

	interface ChatMessage {
		id: string;
		use_case: string | null;
		role: string | null;
		query_text: string | null;
		answer_text: string | null;
		created_at: string | null;
		rating: string | null;
		comment: string | null;
		feedback_at: string | null;
	}

	let feedback: FeedbackRow[] = $state([]);
	let loading = $state(true);
	let error = $state('');
	let ratingFilter: '' | 'positive' | 'negative' = $state('');
	let useCaseFilter = $state('');
	let exportingId: string | null = $state(null);

	// Distinct use cases present in the current result set (for the filter).
	let useCases = $derived(
		[...new Set(feedback.map((f) => f.use_case).filter((v): v is string => !!v))].sort()
	);

	let filtered = $derived(
		feedback.filter((f) => !useCaseFilter || f.use_case === useCaseFilter)
	);

	async function load() {
		loading = true;
		error = '';
		try {
			const params = new URLSearchParams();
			if (ratingFilter) params.set('rating', ratingFilter);
			const resp = await fetch(`/api/admin/feedback?${params.toString()}`);
			if (!resp.ok) throw new Error('Laden fehlgeschlagen');
			const data = await resp.json();
			feedback = data.feedback ?? [];
		} catch (e) {
			error = e instanceof Error ? e.message : 'Fehler';
		}
		loading = false;
	}

	function fmt(ts: string | null): string {
		if (!ts) return '–';
		try {
			return new Date(ts).toLocaleString('de-DE');
		} catch {
			return ts;
		}
	}

	function download(filename: string, content: string) {
		const blob = new Blob([content], { type: 'text/markdown;charset=utf-8' });
		const url = URL.createObjectURL(blob);
		const a = document.createElement('a');
		a.href = url;
		a.download = filename;
		a.click();
		URL.revokeObjectURL(url);
	}

	function ratingLabel(r: string | null): string {
		if (r === 'positive') return '👍 Positiv';
		if (r === 'negative') return '👎 Negativ';
		return '';
	}

	/** Build a human-readable Markdown transcript for one feedback entry. */
	function buildMarkdown(header: string, messages: ChatMessage[]): string {
		const lines: string[] = [header, ''];
		messages.forEach((m, i) => {
			lines.push(`## ${i + 1}. Frage${m.role ? ` (${m.role})` : ''} — ${fmt(m.created_at)}`);
			lines.push('');
			lines.push(m.query_text ?? '');
			lines.push('');
			lines.push('### Antwort');
			lines.push('');
			lines.push(m.answer_text ?? '');
			lines.push('');
			if (m.rating) {
				lines.push(`**Feedback:** ${ratingLabel(m.rating)}`);
				if (m.comment) lines.push(`**Kommentar:** ${m.comment}`);
				lines.push('');
			}
			lines.push('---');
			lines.push('');
		});
		return lines.join('\n');
	}

	async function exportChat(f: FeedbackRow) {
		exportingId = f.id;
		error = '';
		try {
			let messages: ChatMessage[];
			if (f.session_id) {
				const resp = await fetch(`/api/admin/chat/${encodeURIComponent(f.session_id)}`);
				if (!resp.ok) throw new Error('Chat-Export fehlgeschlagen');
				const data = await resp.json();
				messages = data.messages ?? [];
			} else {
				// No session linkage — export just the rated query/answer.
				messages = [
					{
						id: f.query_id ?? f.id,
						use_case: f.use_case,
						role: f.role,
						query_text: f.query_text,
						answer_text: f.answer_text,
						created_at: f.created_at,
						rating: f.rating,
						comment: f.comment,
						feedback_at: f.created_at
					}
				];
			}
			const header =
				`# Chat-Export – ${f.use_case ?? 'unbekannt'}\n\n` +
				`- Session: ${f.session_id ?? '(keine)'}\n` +
				`- Bewertung: ${ratingLabel(f.rating)}\n` +
				(f.comment ? `- Kommentar: ${f.comment}\n` : '') +
				`- Exportiert: ${new Date().toLocaleString('de-DE')}\n`;
			const md = buildMarkdown(header, messages);
			const stamp = (f.created_at ?? '').slice(0, 10) || 'chat';
			download(`feedback-${f.use_case ?? 'chat'}-${stamp}.md`, md);
		} catch (e) {
			error = e instanceof Error ? e.message : 'Export-Fehler';
		}
		exportingId = null;
	}

	onMount(load);
</script>

<svelte:head><title>Feedback – Dashboard</title></svelte:head>

<div class="mx-auto max-w-4xl space-y-6 p-6">
	<div>
		<h2 class="text-xl font-bold text-gray-800">Feedback</h2>
		<p class="mt-1 text-sm text-gray-500">
			Nutzerbewertungen aus dem Chat. Exportiere den vollständigen Verlauf zur Nachvollziehbarkeit.
		</p>
	</div>

	{#if error}
		<p class="rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700">{error}</p>
	{/if}

	<!-- Filters -->
	<div class="flex flex-wrap items-end gap-3">
		<div>
			<label for="rf" class="mb-1 block text-xs text-gray-500">Bewertung</label>
			<select
				id="rf"
				bind:value={ratingFilter}
				onchange={load}
				class="rounded-lg border border-gray-300 px-3 py-2 text-sm"
			>
				<option value="">Alle</option>
				<option value="positive">👍 Positiv</option>
				<option value="negative">👎 Negativ</option>
			</select>
		</div>
		<div>
			<label for="uf" class="mb-1 block text-xs text-gray-500">Use Case</label>
			<select
				id="uf"
				bind:value={useCaseFilter}
				class="rounded-lg border border-gray-300 px-3 py-2 text-sm"
			>
				<option value="">Alle</option>
				{#each useCases as uc}
					<option value={uc}>{uc}</option>
				{/each}
			</select>
		</div>
		<button
			onclick={load}
			class="rounded-lg border border-gray-300 px-3 py-2 text-sm text-gray-600 hover:bg-gray-100"
		>
			Aktualisieren
		</button>
		<span class="ml-auto text-xs text-gray-400">{filtered.length} Einträge</span>
	</div>

	<!-- List -->
	{#if loading}
		<p class="animate-pulse text-sm text-gray-400">Lade…</p>
	{:else if filtered.length === 0}
		<div class="rounded-xl border border-gray-200 bg-white p-8 text-center text-sm text-gray-400">
			Kein Feedback vorhanden.
		</div>
	{:else}
		<div class="space-y-3">
			{#each filtered as f (f.id)}
				<div class="rounded-xl border border-gray-200 bg-white p-4 shadow-sm">
					<div class="flex items-start justify-between gap-3">
						<div class="flex items-center gap-2">
							<span
								class="rounded-full px-2.5 py-0.5 text-xs font-semibold
								{f.rating === 'positive'
									? 'bg-green-100 text-green-700'
									: 'bg-red-100 text-red-700'}"
							>
								{ratingLabel(f.rating)}
							</span>
							{#if f.use_case}
								<span class="rounded-full bg-gray-100 px-2.5 py-0.5 text-xs text-gray-600">
									{f.use_case}
								</span>
							{/if}
							<span class="text-xs text-gray-400">{fmt(f.created_at)}</span>
						</div>
						<div class="flex shrink-0 gap-2">
							{#if f.session_id}
								<a
									href="/admin/feedback/{f.session_id}"
									class="rounded-lg bg-gray-800 px-3 py-1.5 text-xs font-medium text-white hover:bg-gray-900"
								>
									Verlauf öffnen
								</a>
							{/if}
							<button
								onclick={() => exportChat(f)}
								disabled={exportingId === f.id}
								class="rounded-lg bg-blue-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-blue-700 disabled:opacity-60"
							>
								{exportingId === f.id ? 'Export…' : 'Exportieren'}
							</button>
						</div>
					</div>

					{#if f.comment}
						<p class="mt-3 rounded-lg bg-amber-50 px-3 py-2 text-sm text-gray-700">
							„{f.comment}“
						</p>
					{/if}

					{#if f.query_text}
						<div class="mt-3 text-sm">
							<p class="text-xs font-semibold uppercase tracking-wider text-gray-400">Frage</p>
							<p class="mt-0.5 text-gray-700">{f.query_text}</p>
						</div>
					{/if}
					{#if f.answer_text}
						<div class="mt-2 text-sm">
							<p class="text-xs font-semibold uppercase tracking-wider text-gray-400">Antwort</p>
							<p class="mt-0.5 line-clamp-4 whitespace-pre-wrap text-gray-600">{f.answer_text}</p>
						</div>
					{/if}
				</div>
			{/each}
		</div>
	{/if}
</div>
