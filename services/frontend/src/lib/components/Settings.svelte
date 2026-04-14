<script lang="ts">
	import { app } from '$lib/state.svelte';
	import {
		getPrompt,
		updatePrompt,
		getMemory,
		updateMemory,
		getActions,
		updateAction
	} from '$lib/api';

	// Prompt state
	let promptText = $state('');
	let promptLoading = $state(false);
	let promptSaved = $state(false);

	// Memory state
	let memoryText = $state('');
	let memoryLoading = $state(false);
	let memorySaved = $state(false);

	// Actions state
	interface ActionState {
		enabled: boolean;
		description: string;
		saving: boolean;
	}
	let actions: Record<string, ActionState> = $state({});
	let actionsLoading = $state(false);

	const actionLabels: Record<string, string> = {
		SEARCH: 'Suche',
		SEARCH_CNC: 'CNC-Suche',
		CLARIFY: 'Rueckfrage',
		RECALL_MEMORY: 'Gedaechtnis',
		LOOKUP_SOURCES: 'Quellen auflisten',
		FINAL_ANSWER: 'Antwort'
	};

	const actionIcons: Record<string, string> = {
		SEARCH: '&#128269;',
		SEARCH_CNC: '&#9881;',
		CLARIFY: '&#10067;',
		RECALL_MEMORY: '&#129504;',
		LOOKUP_SOURCES: '&#128218;',
		FINAL_ANSWER: '&#9989;'
	};

	// ── Loaders ─────────────────────────────────────────────

	async function loadPrompt() {
		promptLoading = true;
		promptSaved = false;
		try {
			const data = await getPrompt(app.useCase, app.role);
			promptText = data.prompt ?? '';
		} catch {
			promptText = '';
		}
		promptLoading = false;
	}

	async function savePrompt() {
		promptLoading = true;
		try {
			await updatePrompt(app.useCase, promptText, app.role);
			promptSaved = true;
			setTimeout(() => (promptSaved = false), 2000);
		} catch {
			/* ignore */
		}
		promptLoading = false;
	}

	async function loadMemory() {
		memoryLoading = true;
		memorySaved = false;
		try {
			const data = await getMemory(app.useCase);
			memoryText = data.memory_text ?? '';
		} catch {
			memoryText = '';
		}
		memoryLoading = false;
	}

	async function saveMemory() {
		memoryLoading = true;
		try {
			await updateMemory(app.useCase, memoryText);
			memorySaved = true;
			setTimeout(() => (memorySaved = false), 2000);
		} catch {
			/* ignore */
		}
		memoryLoading = false;
	}

	async function clearMemory() {
		memoryText = '';
		await saveMemory();
	}

	async function loadActions() {
		actionsLoading = true;
		try {
			const data = await getActions(app.useCase);
			const raw = data.actions ?? {};
			const result: Record<string, ActionState> = {};
			for (const [name, defn] of Object.entries(raw) as [
				string,
				{ enabled: boolean; description: string }
			][]) {
				result[name] = { enabled: defn.enabled, description: defn.description, saving: false };
			}
			actions = result;
		} catch {
			actions = {};
		}
		actionsLoading = false;
	}

	async function toggleAction(name: string) {
		const action = actions[name];
		if (!action) return;
		action.saving = true;
		const newEnabled = !action.enabled;
		try {
			await updateAction(app.useCase, name, { enabled: newEnabled });
			action.enabled = newEnabled;
		} catch {
			/* revert on error – state stays unchanged */
		}
		action.saving = false;
	}

	async function saveActionDescription(name: string) {
		const action = actions[name];
		if (!action) return;
		action.saving = true;
		try {
			await updateAction(app.useCase, name, { description: action.description });
		} catch {
			/* ignore */
		}
		action.saving = false;
	}

	// ── Reactive reload ─────────────────────────────────────

	$effect(() => {
		app.useCase;
		app.role;
		loadPrompt();
		loadMemory();
		loadActions();
	});
</script>

<main class="flex h-full flex-1 flex-col overflow-y-auto bg-gray-50">
	<div class="mx-auto w-full max-w-4xl space-y-8 p-6">
		<h2 class="text-lg font-semibold text-gray-800">Einstellungen</h2>

		<!-- Agent Actions -->
		<section class="space-y-3">
			<div>
				<h3 class="text-sm font-semibold uppercase tracking-wider text-gray-400">
					Agent-Aktionen
				</h3>
				<p class="mt-0.5 text-xs text-gray-400">
					Aktionen einzeln aktivieren/deaktivieren und beschreiben.
					Use Case: <span class="font-medium text-gray-600">{app.useCase}</span>
				</p>
			</div>

			{#if actionsLoading}
				<p class="animate-pulse text-sm text-gray-400">Laden...</p>
			{:else if Object.keys(actions).length === 0}
				<p class="text-sm text-gray-400">Keine Aktionen konfiguriert.</p>
			{:else}
				<div class="space-y-2">
					{#each Object.entries(actions) as [name, action]}
						{@const label = actionLabels[name] ?? name}
						{@const icon = actionIcons[name] ?? '&#9654;'}
						<div
							class="rounded-lg border bg-white shadow-sm transition-opacity
								{action.enabled ? 'border-gray-200' : 'border-gray-100 opacity-60'}"
						>
							<div class="flex items-start gap-3 px-4 py-3">
								<!-- Toggle -->
								<button
									onclick={() => toggleAction(name)}
									disabled={action.saving}
									class="mt-0.5 shrink-0"
									title="{action.enabled ? 'Deaktivieren' : 'Aktivieren'}"
								>
									<div
										class="relative h-5 w-9 rounded-full transition-colors
											{action.enabled ? 'bg-blue-600' : 'bg-gray-300'}"
									>
										<div
											class="absolute top-0.5 h-4 w-4 rounded-full bg-white shadow transition-transform
												{action.enabled ? 'translate-x-4' : 'translate-x-0.5'}"
										></div>
									</div>
								</button>

								<!-- Icon + Name -->
								<div class="min-w-0 flex-1">
									<div class="flex items-center gap-2">
										<span class="text-sm">{@html icon}</span>
										<span class="text-sm font-semibold text-gray-800">{name}</span>
										<span class="text-xs text-gray-400">{label}</span>
										{#if action.saving}
											<span class="animate-pulse text-[10px] text-blue-500">...</span>
										{/if}
									</div>

									<!-- Editable description -->
									<div class="mt-1.5 flex gap-2">
										<input
											type="text"
											bind:value={action.description}
											onblur={() => saveActionDescription(name)}
											class="flex-1 rounded border border-gray-200 px-2.5 py-1.5 text-xs text-gray-600
												focus:border-blue-400 focus:outline-none focus:ring-1 focus:ring-blue-400
												disabled:opacity-50"
											disabled={action.saving}
											placeholder="Beschreibung der Aktion..."
										/>
									</div>
								</div>
							</div>
						</div>
					{/each}
				</div>
			{/if}
		</section>

		<!-- System Prompt Editor -->
		<section class="space-y-3">
			<div class="flex items-center justify-between">
				<div>
					<h3 class="text-sm font-semibold uppercase tracking-wider text-gray-400">
						System-Prompt
					</h3>
					<p class="mt-0.5 text-xs text-gray-400">
						Use Case: <span class="font-medium text-gray-600">{app.useCase}</span>
						&middot; Rolle: <span class="font-medium text-gray-600">{app.role}</span>
					</p>
				</div>
				<div class="flex items-center gap-2">
					{#if promptSaved}
						<span class="text-xs text-green-600">Gespeichert!</span>
					{/if}
					<button
						onclick={savePrompt}
						disabled={promptLoading}
						class="rounded-lg bg-blue-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-blue-700 disabled:opacity-40"
					>
						Speichern
					</button>
				</div>
			</div>

			<textarea
				bind:value={promptText}
				rows={8}
				disabled={promptLoading}
				class="w-full rounded-lg border border-gray-300 bg-white px-4 py-3 text-sm text-gray-700 focus:border-blue-400 focus:outline-none focus:ring-1 focus:ring-blue-400 disabled:opacity-50"
				placeholder="System-Prompt laden..."
			></textarea>

			<p class="text-xs text-gray-400">
				Der System-Prompt definiert das Verhalten des Assistenten. Aenderungen gelten sofort
				fuer neue Anfragen.
			</p>
		</section>

		<!-- Agent Memory -->
		<section class="space-y-3">
			<div class="flex items-center justify-between">
				<div>
					<h3 class="text-sm font-semibold uppercase tracking-wider text-gray-400">
						Agent-Gedaechtnis
					</h3>
					<p class="mt-0.5 text-xs text-gray-400">
						Vom Assistenten automatisch befuellt. Hilft bei zukuenftigen Anfragen.
					</p>
				</div>
				<div class="flex items-center gap-2">
					{#if memorySaved}
						<span class="text-xs text-green-600">Gespeichert!</span>
					{/if}
					<button
						onclick={clearMemory}
						disabled={memoryLoading}
						class="rounded-lg border border-red-200 bg-white px-3 py-1.5 text-xs font-medium text-red-600 hover:bg-red-50 disabled:opacity-40"
					>
						Leeren
					</button>
					<button
						onclick={saveMemory}
						disabled={memoryLoading}
						class="rounded-lg bg-blue-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-blue-700 disabled:opacity-40"
					>
						Speichern
					</button>
				</div>
			</div>

			<textarea
				bind:value={memoryText}
				rows={10}
				disabled={memoryLoading}
				class="w-full rounded-lg border border-gray-300 bg-white px-4 py-3 font-mono text-xs text-gray-600 focus:border-blue-400 focus:outline-none focus:ring-1 focus:ring-blue-400 disabled:opacity-50"
				placeholder="Noch kein Gedaechtnis vorhanden. Das Gedaechtnis wird automatisch nach Anfragen befuellt."
			></textarea>

			<p class="text-xs text-gray-400">
				Das Gedaechtnis speichert Erkenntnisse aus vergangenen Anfragen pro Use Case. Sie
				koennen es manuell bearbeiten oder leeren.
			</p>
		</section>
	</div>
</main>
