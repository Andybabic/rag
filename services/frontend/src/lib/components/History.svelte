<script lang="ts">
	import { app, type Message, type AgentStep, type Citation } from '$lib/state.svelte';
	import { goto } from '$app/navigation';
	import { getQueries } from '$lib/api';
	import { getUseCaseByApiId } from '$lib/use-cases';

	interface QueryRecord {
		id: string;
		use_case: string;
		session_id: string | null;
		role: string;
		query_text: string;
		answer_text: string;
		sufficient: boolean;
		created_at: string;
		agent_steps: AgentStep[];
		citations: Citation[];
		images: string[];
	}

	let queries: QueryRecord[] = $state([]);
	let loading = $state(false);
	let expandedId: string | null = $state(null);

	async function load() {
		loading = true;
		try {
			const data = await getQueries(app.useCase);
			queries = data.queries ?? [];
		} catch {
			queries = [];
		}
		loading = false;
	}

	function toggle(id: string) {
		expandedId = expandedId === id ? null : id;
	}

	function reuse(queryText: string) {
		const uc = getUseCaseByApiId(app.useCase);
		const slug = uc?.slug ?? 'neumann';
		goto(`/${slug}`).then(() => {
			setTimeout(() => {
				const textarea = document.querySelector('textarea') as HTMLTextAreaElement | null;
				if (textarea) {
					textarea.value = queryText;
					textarea.dispatchEvent(new Event('input', { bubbles: true }));
				}
			}, 100);
		});
	}

	/**
	 * Load the full conversation into the chat: every Q&A round of the same
	 * session (chronological), each with its persisted intermediate steps.
	 * Falls back to just this entry if it has no session id.
	 */
	function loadConversation(q: QueryRecord) {
		const convo = q.session_id
			? queries.filter((x) => x.session_id === q.session_id)
			: [q];
		convo.sort(
			(a, b) => new Date(a.created_at).getTime() - new Date(b.created_at).getTime()
		);

		const msgs: Message[] = [];
		for (const item of convo) {
			msgs.push({
				role: 'user',
				text: item.query_text,
				images: item.images?.length ? item.images : undefined,
				createdAt: item.created_at
			});
			msgs.push({
				role: 'assistant',
				text: item.answer_text ?? '',
				createdAt: item.created_at,
				agentSteps: item.agent_steps ?? [],
				citations: item.citations ?? [],
				sufficient: item.sufficient
			});
		}

		app.messages = msgs;
		const uc = getUseCaseByApiId(app.useCase);
		goto(`/${uc?.slug ?? 'neumann'}`);
	}

	$effect(() => {
		app.useCase;
		load();
	});
</script>

<main class="flex h-full flex-1 flex-col bg-gray-50 overflow-y-auto">
	<div class="mx-auto w-full max-w-4xl p-6 space-y-4">
		<div class="flex items-center justify-between">
			<h2 class="text-lg font-semibold text-gray-800">Anfrage-Verlauf</h2>
			<button
				onclick={load}
				class="rounded-lg border border-gray-300 bg-white px-3 py-1.5 text-xs text-gray-600 hover:bg-gray-50"
			>
				Aktualisieren
			</button>
		</div>

		{#if loading}
			<p class="text-sm text-gray-400 animate-pulse">Laden...</p>
		{:else if queries.length === 0}
			<div class="flex h-40 items-center justify-center text-gray-400">
				<p class="text-sm">Noch keine Anfragen gespeichert.</p>
			</div>
		{:else}
			<div class="space-y-2">
				{#each queries as q}
					{@const isOpen = expandedId === q.id}
					<div class="rounded-lg border border-gray-200 bg-white shadow-sm">
						<!-- svelte-ignore a11y_click_events_have_key_events -->
						<!-- svelte-ignore a11y_no_static_element_interactions -->
						<div
							class="flex w-full items-start gap-3 px-4 py-3 text-left hover:bg-gray-50 cursor-pointer"
							onclick={() => toggle(q.id)}
						>
							<span class="mt-0.5 transform transition-transform text-gray-400 {isOpen ? 'rotate-90' : ''}">&#9654;</span>
							<div class="min-w-0 flex-1">
								<p class="text-sm font-medium text-gray-800 line-clamp-1">{q.query_text}</p>
								<p class="mt-0.5 text-xs text-gray-400">
									{new Date(q.created_at).toLocaleString('de-AT')}
									&middot; {q.agent_steps?.length ?? 0} Schritte
									{#if q.sufficient}
										<span class="text-green-600">&middot; beantwortet</span>
									{:else}
										<span class="text-amber-600">&middot; unvollstaendig</span>
									{/if}
								</p>
							</div>
							<div class="flex shrink-0 gap-1.5">
								<button
									class="rounded border border-blue-200 bg-blue-50 px-2 py-1 text-[10px] font-medium text-blue-700 hover:bg-blue-100"
									onclick={(e: MouseEvent) => { e.stopPropagation(); loadConversation(q); }}
									title="Gesamte Unterhaltung inkl. Zwischenschritte in den Chat laden"
								>
									Laden
								</button>
								<button
									class="rounded border border-gray-200 px-2 py-1 text-[10px] text-gray-500 hover:bg-gray-100"
									onclick={(e: MouseEvent) => { e.stopPropagation(); reuse(q.query_text); }}
									title="Nur den Prompt erneut verwenden"
								>
									Wiederholen
								</button>
							</div>
						</div>

						{#if isOpen}
							<div class="border-t border-gray-100 px-4 py-3 space-y-2">
								<div>
									<p class="text-[10px] font-semibold uppercase tracking-wider text-gray-400 mb-1">Antwort</p>
									<p class="text-xs text-gray-600 whitespace-pre-wrap line-clamp-10">{q.answer_text}</p>
								</div>
								{#if q.agent_steps && q.agent_steps.length > 0}
									<div>
										<p class="text-[10px] font-semibold uppercase tracking-wider text-gray-400 mb-1">Schritte</p>
										<div class="flex flex-wrap gap-1">
											{#each q.agent_steps as step}
												<span class="rounded bg-gray-100 px-1.5 py-0.5 text-[10px] text-gray-600">
													#{step.step} {step.action}
												</span>
											{/each}
										</div>
									</div>
								{/if}
							</div>
						{/if}
					</div>
				{/each}
			</div>
		{/if}
	</div>
</main>
