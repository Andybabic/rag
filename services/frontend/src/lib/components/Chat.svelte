<script lang="ts">
	import { page } from '$app/stores';
	import { app, type Message, type AgentStep } from '$lib/state.svelte';
	import { streamQuery } from '$lib/api';
	import { getUseCaseBySlug } from '$lib/use-cases';
	import ChatMessage from './ChatMessage.svelte';

	let input = $state('');
	let chatContainer: HTMLDivElement | undefined = $state();

	// Use case comes from the URL — single source of truth. app.useCase can
	// be stale (e.g. when the layout's $effect hasn't run yet, or after HMR
	// resets state to the 'neumann' default), which led to queries being
	// sent under the wrong use case.
	const useCase = $derived.by(() => {
		const slug = $page.params.useCase ?? '';
		return getUseCaseBySlug(slug)?.apiId ?? app.useCase;
	});

	const actionLabels: Record<string, string> = {
		SEARCH: 'Suche in Vektordatenbank',
		SEARCH_CNC: 'CNC-Suche',
		CLARIFY: 'Stelle Rückfrage',
		RECALL_MEMORY: 'Lese Gedächtnis',
		LOOKUP_SOURCES: 'Liste Quellen auf',
		FINAL_ANSWER: 'Formuliere Antwort'
	};

	function scrollToBottom() {
		if (chatContainer) {
			setTimeout(() => {
				chatContainer!.scrollTop = chatContainer!.scrollHeight;
			}, 50);
		}
	}

	async function send() {
		const text = input.trim();
		if (!text || app.isLoading) return;

		// Build conversation history from previous messages (only text, no metadata)
		const history = app.messages
			.filter((m) => !m.error)
			.map((m) => ({
				role: m.role,
				content: m.text
			}));

		input = '';
		app.messages.push({ role: 'user', text });
		app.isLoading = true;

		// Push an empty assistant message immediately and fill it in as events
		// arrive. This keeps every step visible permanently instead of flashing
		// in a transient progress bubble that disappears at the end.
		const assistantIdx = app.messages.length;
		app.messages.push({
			role: 'assistant',
			text: '',
			agentSteps: [],
			streaming: true,
			currentPhase: 'Verbinde …'
		});
		scrollToBottom();

		const updateMsg = (patch: Partial<Message>) => {
			const current = app.messages[assistantIdx];
			app.messages[assistantIdx] = { ...current, ...patch };
		};

		try {
			const result = await streamQuery(
				text,
				useCase,
				app.sessionId,
				app.role,
				{},
				history,
				(event) => {
					const type = event.type as string;
					const msg = app.messages[assistantIdx];
					const steps = [...(msg.agentSteps ?? [])];

					if (type === 'started') {
						updateMsg({ currentPhase: 'Agent gestartet …' });
					} else if (type === 'thinking') {
						updateMsg({ currentPhase: `Schritt ${event.step}: LLM denkt nach …` });
					} else if (type === 'action') {
						// Push a partial step so it shows up in the UI immediately,
						// even before the action result has come back.
						steps.push({
							step: event.step as number,
							action: event.action as string,
							thought: event.thought as string,
							args: (event.args as Record<string, unknown>) ?? {},
							observation: '',
							chunks: []
						});
						const label = actionLabels[event.action as string] ?? (event.action as string);
						updateMsg({
							agentSteps: steps,
							currentPhase: `Schritt ${event.step}: ${label} …`
						});
					} else if (type === 'step') {
						// Replace the partial step with the full one (incl. observation + chunks).
						const idx = steps.findIndex((s) => s.step === event.step);
						const full = {
							step: event.step as number,
							action: event.action as string,
							thought: event.thought as string,
							args: (event.args as Record<string, unknown>) ?? {},
							observation: (event.observation as string) ?? '',
							llm_response: event.llm_response as string,
							chunks: (event.chunks as AgentStep['chunks']) ?? []
						};
						if (idx >= 0) steps[idx] = full;
						else steps.push(full);
						const label = actionLabels[event.action as string] ?? (event.action as string);
						updateMsg({
							agentSteps: steps,
							currentPhase: `Schritt ${event.step}: ${label} – fertig`
						});
					}
					scrollToBottom();
				}
			);
			if (!result) throw new Error('Kein Ergebnis vom Stream');
			updateMsg({
				text: (result.answer as string) ?? '',
				citations: (result.citations as Message['citations']) ?? [],
				agentSteps: (result.agent_steps as Message['agentSteps']) ?? app.messages[assistantIdx].agentSteps,
				searchedCollections: (result.searched_collections as string[]) ?? [],
				systemPrompt: (result.system_prompt as string) ?? '',
				enrichedQuery: (result.enriched_query as string) ?? '',
				requestId: (result.request_id as string) ?? '',
				sufficient: result.sufficient as boolean | undefined,
				streaming: false,
				currentPhase: undefined
			});
		} catch (err) {
			const message = err instanceof Error ? err.message : 'Unbekannter Fehler';
			updateMsg({ text: `Fehler: ${message}`, error: true, streaming: false, currentPhase: undefined });
		} finally {
			app.isLoading = false;
			scrollToBottom();
		}
	}


	function onKeyDown(e: KeyboardEvent) {
		if (e.key === 'Enter' && !e.shiftKey) {
			e.preventDefault();
			send();
		}
	}

	function exportChat() {
		const exportData = {
			use_case: useCase,
			session_id: app.sessionId,
			exported_at: new Date().toISOString(),
			messages: app.messages.map((m) => ({
				role: m.role,
				text: m.text,
				...(m.citations?.length ? { citations: m.citations } : {}),
				...(m.agentSteps?.length ? { agent_steps: m.agentSteps } : {}),
				...(m.searchedCollections?.length
					? { searched_collections: m.searchedCollections }
					: {}),
				...(m.requestId ? { request_id: m.requestId } : {}),
				...(m.sufficient !== undefined ? { sufficient: m.sufficient } : {}),
				...(m.error ? { error: true } : {})
			}))
		};

		const blob = new Blob([JSON.stringify(exportData, null, 2)], {
			type: 'application/json'
		});
		const url = URL.createObjectURL(blob);
		const a = document.createElement('a');
		a.href = url;
		a.download = `chat_${useCase}_${new Date().toISOString().slice(0, 19).replace(/:/g, '-')}.json`;
		a.click();
		URL.revokeObjectURL(url);
	}
</script>

<main class="flex h-full flex-1 flex-col bg-gray-50">
	<!-- Messages -->
	<div class="flex-1 space-y-4 overflow-y-auto p-6" bind:this={chatContainer}>
		{#if app.messages.length === 0}
			<div class="flex h-full items-center justify-center text-gray-400">
				<div class="text-center">
					<p class="text-lg font-medium">Willkommen</p>
					<p class="mt-1 text-sm">
						Stellen Sie eine Frage oder laden Sie ein Dokument hoch.
					</p>
				</div>
			</div>
		{/if}

		{#each app.messages as msg}
			<ChatMessage message={msg} />
		{/each}

	</div>

	<!-- Input -->
	<div class="border-t border-gray-200 bg-white p-4">
		<div class="mx-auto flex max-w-3xl gap-3">
			<textarea
				bind:value={input}
				onkeydown={onKeyDown}
				placeholder="Frage eingeben..."
				rows={1}
				class="flex-1 resize-none rounded-xl border border-gray-300 px-4 py-3 text-sm focus:border-blue-400 focus:outline-none focus:ring-1 focus:ring-blue-400"
				disabled={app.isLoading}
			></textarea>
			{#if app.messages.length > 0}
				<button
					onclick={exportChat}
					class="rounded-xl border border-gray-300 px-3 py-3 text-gray-400 transition-colors hover:border-gray-400 hover:text-gray-600"
					title="Chat als JSON exportieren"
				>
					<svg class="h-5 w-5" viewBox="0 0 20 20" fill="currentColor">
						<path
							fill-rule="evenodd"
							d="M3 17a1 1 0 011-1h12a1 1 0 110 2H4a1 1 0 01-1-1zm3.293-7.707a1 1 0 011.414 0L9 10.586V3a1 1 0 112 0v7.586l1.293-1.293a1 1 0 111.414 1.414l-3 3a1 1 0 01-1.414 0l-3-3a1 1 0 010-1.414z"
							clip-rule="evenodd"
						/>
					</svg>
				</button>
			{/if}
			<button
				onclick={send}
				disabled={app.isLoading || !input.trim()}
				class="rounded-xl bg-blue-600 px-5 py-3 text-sm font-medium text-white transition-colors hover:bg-blue-700 disabled:cursor-not-allowed disabled:opacity-40"
			>
				Senden
			</button>
		</div>
	</div>
</main>
