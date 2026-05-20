<script lang="ts">
	import { page } from '$app/stores';
	import {
		app,
		type Message,
		type AgentStep,
		type SubAgentTrace,
		type ManagerPlan,
		type SynthesizerTrace,
		type ComplianceTrace,
		type AuditInfo
	} from '$lib/state.svelte';
	import { streamQuery } from '$lib/api';
	import { getUseCaseBySlug } from '$lib/use-cases';
	import ChatMessage from './ChatMessage.svelte';

	const APP_VERSION = __APP_VERSION__;
	const EXPORT_SCHEMA_VERSION = '2.0';

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
		FINAL_ANSWER: 'Formuliere Antwort',
		MANAGER_PLAN: 'Manager-Plan',
		SYNTHESIZE: 'Antworten zusammenführen'
	};

	const roleLabels: Record<string, string> = {
		facts: 'Fakten',
		procedure: 'Prozedur',
		context: 'Kontext'
	};

	function upsertSubAgent(
		list: SubAgentTrace[],
		subId: string,
		patch: Partial<SubAgentTrace>
	): SubAgentTrace[] {
		const idx = list.findIndex((s) => s.subagent_id === subId);
		if (idx < 0) {
			list.push({
				subagent_id: subId,
				role: patch.role ?? 'facts',
				role_label: patch.role_label ?? roleLabels[patch.role ?? 'facts'] ?? 'Spezialist',
				sub_query: patch.sub_query ?? '',
				focus: patch.focus,
				answer: patch.answer ?? '',
				agent_steps: patch.agent_steps ?? [],
				chunks: patch.chunks ?? [],
				sufficient: patch.sufficient,
				searched_collections: patch.searched_collections,
				error: patch.error ?? null,
				status: patch.status ?? 'running'
			});
		} else {
			list[idx] = { ...list[idx], ...patch };
		}
		return list;
	}

	function pushInnerStep(sub: SubAgentTrace, innerType: string, payload: Record<string, unknown>) {
		const steps = [...(sub.agent_steps ?? [])];
		const stepNum = (payload.step as number) ?? steps.length + 1;
		if (innerType === 'action') {
			steps.push({
				step: stepNum,
				action: (payload.action as string) ?? '',
				thought: (payload.thought as string) ?? '',
				args: (payload.args as Record<string, unknown>) ?? {},
				observation: '',
				chunks: [],
				subagent_id: sub.subagent_id,
				subagent_role: sub.role
			});
		} else if (innerType === 'step') {
			const idx = steps.findIndex((s) => s.step === stepNum);
			const full: AgentStep = {
				step: stepNum,
				action: (payload.action as string) ?? '',
				thought: (payload.thought as string) ?? '',
				args: (payload.args as Record<string, unknown>) ?? {},
				observation: (payload.observation as string) ?? '',
				llm_response: payload.llm_response as string,
				chunks: (payload.chunks as AgentStep['chunks']) ?? [],
				subagent_id: sub.subagent_id,
				subagent_role: sub.role
			};
			if (idx >= 0) steps[idx] = full;
			else steps.push(full);
		}
		sub.agent_steps = steps;
	}

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
		app.messages.push({ role: 'user', text, createdAt: new Date().toISOString() });
		app.isLoading = true;
		const startedAt = performance.now();

		// Push an empty assistant message immediately and fill it in as events
		// arrive. This keeps every step visible permanently instead of flashing
		// in a transient progress bubble that disappears at the end.
		const assistantIdx = app.messages.length;
		app.messages.push({
			role: 'assistant',
			text: '',
			createdAt: new Date().toISOString(),
			agentSteps: [],
			subAgents: [],
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

					if (type === 'started') {
						updateMsg({ currentPhase: 'Manager startet …' });
					} else if (type === 'manager_plan') {
						const plan: ManagerPlan = {
							rationale: event.rationale as string,
							merge_strategy: event.merge_strategy as ManagerPlan['merge_strategy'],
							subtasks: (event.subtasks as ManagerPlan['subtasks']) ?? []
						};
						// Pre-seed sub-agent placeholders so the UI shows the plan
						// immediately, even before the first sub-agent emits.
						const seeded: SubAgentTrace[] = plan.subtasks.map((st, i) => ({
							subagent_id: `sub-${i + 1}-${st.role}`,
							role: st.role,
							role_label: roleLabels[st.role] ?? st.role,
							sub_query: st.sub_query,
							focus: st.focus,
							answer: '',
							agent_steps: [],
							status: 'pending'
						}));
						updateMsg({
							managerPlan: plan,
							subAgents: seeded,
							currentPhase: `Manager: ${plan.subtasks.length} Sub-Task(s) verteilt`
						});
					} else if (type === 'subagent_started') {
						const list = upsertSubAgent([...(msg.subAgents ?? [])], event.subagent_id as string, {
							role: event.subagent_role as string,
							role_label: (event.role_label as string) ??
								roleLabels[event.subagent_role as string] ?? 'Spezialist',
							sub_query: event.sub_query as string,
							focus: event.focus as string,
							status: 'running'
						});
						updateMsg({
							subAgents: list,
							currentPhase: `${event.role_label ?? event.subagent_role} sucht …`
						});
					} else if (type === 'subagent_step') {
						const list = [...(msg.subAgents ?? [])];
						const sub = list.find((s) => s.subagent_id === event.subagent_id);
						if (sub) {
							pushInnerStep(sub, event.inner_type as string,
								(event.payload as Record<string, unknown>) ?? {});
							const payload = (event.payload as Record<string, unknown>) ?? {};
							const innerAction = payload.action as string | undefined;
							if (innerAction) {
								const lbl = actionLabels[innerAction] ?? innerAction;
								updateMsg({
									subAgents: list,
									currentPhase: `${sub.role_label}: ${lbl} …`
								});
							} else {
								updateMsg({ subAgents: list });
							}
						}
					} else if (type === 'subagent_done') {
						const list = upsertSubAgent([...(msg.subAgents ?? [])], event.subagent_id as string, {
							answer: (event.answer_preview as string) ?? '',
							sufficient: event.sufficient as boolean,
							error: (event.error as string | null) ?? null,
							status: event.error ? 'error' : 'done'
						});
						updateMsg({
							subAgents: list,
							currentPhase: `${event.subagent_role}: fertig`
						});
					} else if (type === 'compliance') {
						const prev = msg.compliance ?? {};
						const comp: ComplianceTrace = {
							...prev,
							phase: event.phase as ComplianceTrace['phase'],
							verdict: (event.verdict as ComplianceTrace['verdict']) ?? prev.verdict,
							issues: (event.issues as string[]) ?? prev.issues,
							guidance: (event.guidance as string) ?? prev.guidance,
							detail: (event.detail as string) ?? prev.detail
						};
						const phaseLabel =
							comp.phase === 'started'
								? 'Compliance prüft …'
								: comp.verdict === 'REWRITE'
									? 'Compliance: Korrektur'
									: comp.verdict === 'REFUSE'
										? 'Compliance: abgelehnt'
										: 'Compliance: OK';
						updateMsg({ compliance: comp, currentPhase: phaseLabel });
					} else if (type === 'synthesizer') {
						const synth: SynthesizerTrace = {
							phase: event.phase as SynthesizerTrace['phase'],
							merge_strategy: event.merge_strategy as string,
							fragment_count: event.fragment_count as number,
							global_chunk_count: event.global_chunk_count as number,
							answer_length: event.answer_length as number,
							reason: event.reason as string
						};
						updateMsg({
							synthesizer: synth,
							currentPhase:
								synth.phase === 'started'
									? 'Synthesizer führt Antworten zusammen …'
									: synth.phase === 'skipped'
										? 'Synthesizer übersprungen'
										: 'Synthesizer fertig'
						});
					}
					scrollToBottom();
				}
			);
			if (!result) throw new Error('Kein Ergebnis vom Stream');
			// Prefer the rich sub-agent traces from the server (with full
			// step bodies) over the live-built partials.
			const finalSubAgents = (result.subagents as SubAgentTrace[] | undefined)?.map((s) => ({
				...s,
				role_label: s.role_label ?? roleLabels[s.role] ?? s.role,
				status: s.error ? 'error' : 'done'
			})) as SubAgentTrace[] | undefined;
			updateMsg({
				text: (result.answer as string) ?? '',
				citations: (result.citations as Message['citations']) ?? [],
				agentSteps: (result.agent_steps as Message['agentSteps']) ?? app.messages[assistantIdx].agentSteps,
				managerPlan: (result.manager_plan as ManagerPlan | undefined) ?? app.messages[assistantIdx].managerPlan,
				subAgents: finalSubAgents ?? app.messages[assistantIdx].subAgents,
				compliance: (result.compliance as ComplianceTrace | undefined) ?? app.messages[assistantIdx].compliance,
				searchedCollections: (result.searched_collections as string[]) ?? [],
				systemPrompt: (result.system_prompt as string) ?? '',
				enrichedQuery: (result.enriched_query as string) ?? '',
				requestId: (result.request_id as string) ?? '',
				sufficient: result.sufficient as boolean | undefined,
				audit: (result.audit as AuditInfo | undefined) ?? undefined,
				durationMs: Math.round(performance.now() - startedAt),
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

	// Recursively drop null/undefined and empty arrays/objects so the
	// protocol carries only meaningful data (e.g. no `subagent_id: null`,
	// no `error: null`, no empty `args: {}`).
	function clean<T>(value: T): T {
		if (Array.isArray(value)) {
			return value.map(clean).filter((v) => v !== undefined) as T;
		}
		if (value && typeof value === 'object') {
			const out: Record<string, unknown> = {};
			for (const [k, v] of Object.entries(value)) {
				const cv = clean(v);
				if (cv === undefined || cv === null) continue;
				if (Array.isArray(cv) && cv.length === 0) continue;
				if (typeof cv === 'object' && !Array.isArray(cv) && Object.keys(cv).length === 0)
					continue;
				out[k] = cv;
			}
			return out as T;
		}
		return value;
	}

	// One ReAct step, trimmed for the protocol: the parsed structured fields
	// fully represent the step, so the raw `llm_response` (a verbatim repeat
	// of thought/action/args) is dropped. Inside a sub-agent the
	// subagent_id/role are implied by the parent and removed as noise.
	function exportStep(s: AgentStep, nested: boolean) {
		return clean({
			step: s.step,
			thought: s.thought,
			action: s.action,
			args: s.args,
			observation: s.observation,
			chunks: s.chunks,
			...(nested ? {} : { subagent_id: s.subagent_id, subagent_role: s.subagent_role })
		});
	}

	function exportChat() {
		// Traceable protocol of the entire conversation. Deduplicated: the
		// flattened top-level `agent_steps` mirror is omitted whenever
		// sub-agents are present (it only duplicates their steps plus the
		// manager/synthesizer events, which are already in `manager_plan`
		// and `synthesizer`). Retrieved chunks are kept once, at the step
		// where retrieval happened, not repeated at the sub-agent level.
		const exportData = clean({
			schema_version: EXPORT_SCHEMA_VERSION,
			app_version: APP_VERSION,
			use_case: useCase,
			session_id: app.sessionId,
			role: app.role,
			exported_at: new Date().toISOString(),
			message_count: app.messages.length,
			messages: app.messages.map((m, i) => {
				const hasSubAgents = (m.subAgents?.length ?? 0) > 0;
				return clean({
					index: i,
					role: m.role,
					created_at: m.createdAt,
					text: m.text,
					...(m.role === 'assistant' && m.durationMs !== undefined
						? { duration_ms: m.durationMs }
						: {}),
					audit: m.audit,
					enriched_query:
						m.enrichedQuery && m.enrichedQuery !== m.text ? m.enrichedQuery : undefined,
					citations: m.citations,
					manager_plan: m.managerPlan,
					sub_agents: hasSubAgents
						? m.subAgents!.map((s) => ({
								subagent_id: s.subagent_id,
								role: s.role,
								role_label: s.role_label,
								sub_query: s.sub_query,
								focus: s.focus,
								status: s.status,
								sufficient: s.sufficient,
								searched_collections: s.searched_collections,
								error: s.error,
								answer: s.answer,
								agent_steps: (s.agent_steps ?? []).map((st) => exportStep(st, true))
							}))
						: undefined,
					// Single-agent fallback: only when there are no sub-agents,
					// otherwise this is a redundant flattened mirror.
					agent_steps:
						!hasSubAgents && m.agentSteps?.length
							? m.agentSteps.map((st) => exportStep(st, false))
							: undefined,
					compliance: m.compliance,
					synthesizer: m.synthesizer,
					searched_collections: m.searchedCollections,
					system_prompt: m.systemPrompt,
					request_id: m.requestId,
					sufficient: m.sufficient,
					error: m.error ? true : undefined
				});
			})
		});

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

<main class="relative flex h-full flex-1 flex-col bg-gray-50">
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

	<!-- Export button, bottom right of the chat area -->
	{#if app.messages.length > 0}
		<button
			onclick={exportChat}
			class="absolute bottom-24 right-6 z-10 flex items-center gap-2 rounded-full bg-gray-800 px-4 py-2.5 text-sm font-medium text-white shadow-lg transition-colors hover:bg-gray-900"
			title="Gesamten Chat inkl. aller Schritte als JSON-Protokoll exportieren"
		>
			<svg class="h-4 w-4" viewBox="0 0 20 20" fill="currentColor">
				<path
					fill-rule="evenodd"
					d="M3 17a1 1 0 011-1h12a1 1 0 110 2H4a1 1 0 01-1-1zm3.293-7.707a1 1 0 011.414 0L9 10.586V3a1 1 0 112 0v7.586l1.293-1.293a1 1 0 111.414 1.414l-3 3a1 1 0 01-1.414 0l-3-3a1 1 0 010-1.414z"
					clip-rule="evenodd"
				/>
			</svg>
			Chat als JSON exportieren
		</button>
	{/if}

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
