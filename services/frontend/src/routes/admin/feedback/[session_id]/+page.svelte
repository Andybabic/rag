<script lang="ts">
	import { onMount } from 'svelte';
	import { page } from '$app/stores';
	import ChatMessage from '$lib/components/ChatMessage.svelte';
	import type { Message } from '$lib/state.svelte';

	interface ChatRow {
		id: string;
		use_case: string | null;
		role: string | null;
		query_text: string | null;
		answer_text: string | null;
		agent_steps?: Message['agentSteps'];
		citations?: Message['citations'];
		images?: string[];
		sufficient?: boolean | null;
		created_at: string | null;
		rating: string | null;
		comment: string | null;
		feedback_at: string | null;
	}

	const sessionId = $page.params.session_id ?? '';

	let rows: ChatRow[] = $state([]);
	let useCase = $state('');
	let loading = $state(true);
	let error = $state('');

	async function load() {
		loading = true;
		error = '';
		try {
			const resp = await fetch(`/api/admin/chat/${encodeURIComponent(sessionId)}`);
			if (!resp.ok) throw new Error('Verlauf konnte nicht geladen werden');
			const data = await resp.json();
			rows = data.messages ?? [];
			useCase = rows.find((r) => r.use_case)?.use_case ?? '';
		} catch (e) {
			error = e instanceof Error ? e.message : 'Fehler';
		}
		loading = false;
	}

	// Each DB row is one question/answer exchange → two chat bubbles, exactly
	// like the live chat renders them.
	function userMsg(r: ChatRow): Message {
		return {
			role: 'user',
			text: r.query_text ?? '',
			images: r.images ?? [],
			createdAt: r.created_at ?? undefined
		};
	}
	function assistantMsg(r: ChatRow): Message {
		return {
			role: 'assistant',
			text: r.answer_text ?? '',
			citations: r.citations ?? [],
			agentSteps: r.agent_steps ?? [],
			sufficient: r.sufficient ?? undefined,
			requestId: r.id,
			createdAt: r.created_at ?? undefined
		};
	}

	function ratingLabel(rt: string | null): string {
		if (rt === 'positive') return '👍 Positiv';
		if (rt === 'negative') return '👎 Negativ';
		return '';
	}

	onMount(load);
</script>

<svelte:head><title>Verlauf – Feedback</title></svelte:head>

<div class="flex h-full flex-col">
	<!-- Header -->
	<div class="flex items-center justify-between border-b border-gray-200 bg-white px-6 py-3">
		<div>
			<a href="/admin/feedback" class="text-xs text-blue-600 hover:underline">← Zurück zum Feedback</a>
			<h2 class="mt-0.5 text-lg font-bold text-gray-800">
				Chat-Verlauf{useCase ? ` – ${useCase}` : ''}
			</h2>
		</div>
		<span class="font-mono text-[10px] text-gray-400">Session {sessionId.slice(0, 8)}…</span>
	</div>

	<!-- Transcript – same rendering as the live chat -->
	<div class="flex-1 space-y-4 overflow-y-auto bg-gray-50 p-6">
		{#if loading}
			<p class="animate-pulse text-sm text-gray-400">Lade Verlauf…</p>
		{:else if error}
			<p class="rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700">{error}</p>
		{:else if rows.length === 0}
			<div class="flex h-full items-center justify-center text-gray-400">
				<p class="text-sm">Kein Verlauf für diese Session gefunden.</p>
			</div>
		{:else}
			{#each rows as r (r.id)}
				<ChatMessage message={userMsg(r)} readonly />
				<ChatMessage message={assistantMsg(r)} readonly />
				{#if r.rating}
					<div class="flex justify-start">
						<div
							class="max-w-2xl rounded-xl border px-3 py-2 text-xs
							{r.rating === 'positive'
								? 'border-green-200 bg-green-50 text-green-800'
								: 'border-red-200 bg-red-50 text-red-800'}"
						>
							<span class="font-semibold">Feedback: {ratingLabel(r.rating)}</span>
							{#if r.comment}
								<p class="mt-1 text-gray-700">„{r.comment}“</p>
							{/if}
						</div>
					</div>
				{/if}
			{/each}
		{/if}
	</div>
</div>
