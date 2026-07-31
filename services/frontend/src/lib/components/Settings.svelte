<script lang="ts">
	import { app } from '$lib/state.svelte';
	import {
		getMemory,
		updateMemory,
		getActions,
		updateAction,
		getConfig,
		updateConfig,
		listModels,
		listPromptKeys,
		updatePromptKey,
		listSkills,
		createSkill,
		updateSkill,
		deleteSkill,
		type UsecaseConfig,
		type PromptKeyEntry,
		type Skill
	} from '$lib/api';

	// ── Active tab ──────────────────────────────────────────
	type Tab = 'models' | 'prompts' | 'skills' | 'actions' | 'memory';
	let activeTab: Tab = $state('models');

	const tabs: { id: Tab; label: string }[] = [
		{ id: 'models', label: 'Modelle' },
		{ id: 'prompts', label: 'Prompts' },
		{ id: 'skills', label: 'Skills' },
		{ id: 'actions', label: 'Aktionen' },
		{ id: 'memory', label: 'Gedaechtnis' }
	];

	// ── Memory state ────────────────────────────────────────
	let memoryText = $state('');
	let memoryLoading = $state(false);
	let memorySaved = $state(false);

	// ── Actions state ───────────────────────────────────────
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

	// ── Provider/Model config state ─────────────────────────
	let cfg: UsecaseConfig | null = $state(null);
	let cryptoConfigured = $state(true);
	let cfgLoading = $state(false);
	let cfgSaved = $state(false);
	let cfgError = $state('');

	// User-input fields (separate from cfg so we can detect changes)
	let chatProvider = $state('ollama');
	let visionProvider = $state('ollama');
	let ollamaBaseUrl = $state('');
	let openaiBaseUrl = $state('');
	let ollamaApiKey = $state(''); // write-only; empty means "do not change"
	let openaiApiKey = $state('');
	let llmModel = $state('');
	let visionModel = $state('');
	let availableModels: string[] = $state([]);
	let modelsLoading = $state(false);
	let temperature = $state<number | ''>('');
	let maxTokens = $state<number | ''>('');
	let agentMaxSteps = $state<number | ''>('');

	// ── Prompts state ───────────────────────────────────────
	let promptEntries: (PromptKeyEntry & { dirty: boolean; saving: boolean; saved: boolean })[] =
		$state([]);
	let promptsLoading = $state(false);

	// ── Skills state ────────────────────────────────────────
	interface SkillRow extends Skill {
		expanded: boolean;
		dirty: boolean;
		saving: boolean;
		saved: boolean;
	}
	let skills: SkillRow[] = $state([]);
	let skillsLoading = $state(false);
	let newSkillName = $state('');
	let newSkillOverview = $state('');
	let newSkillTask = $state('');
	let creatingSkill = $state(false);
	let createSkillError = $state('');

	// ── Loaders ─────────────────────────────────────────────

	async function loadModels() {
		modelsLoading = true;
		try {
			const data = await listModels();
			availableModels = data.models ?? [];
		} catch {
			availableModels = [];
		}
		modelsLoading = false;
	}

	async function loadConfig() {
		cfgLoading = true;
		cfgError = '';
		try {
			const data = await getConfig(app.useCase);
			cfg = data.config;
			cryptoConfigured = data.crypto_configured;
			chatProvider = cfg.chat_provider;
			visionProvider = cfg.vision_provider;
			ollamaBaseUrl = cfg.ollama_base_url ?? '';
			openaiBaseUrl = cfg.openai_base_url ?? '';
			ollamaApiKey = '';
			openaiApiKey = '';
			llmModel = cfg.llm_model ?? '';
			visionModel = cfg.vision_model ?? '';
			temperature = cfg.temperature ?? '';
			maxTokens = cfg.max_tokens ?? '';
			agentMaxSteps = cfg.agent_max_steps ?? '';
		} catch (e) {
			cfgError = e instanceof Error ? e.message : String(e);
		}
		cfgLoading = false;
	}

	async function saveConfig() {
		cfgLoading = true;
		cfgError = '';
		const patch: Record<string, string | number | null> = {
			chat_provider: chatProvider,
			vision_provider: visionProvider,
			ollama_base_url: ollamaBaseUrl,
			openai_base_url: openaiBaseUrl,
			llm_model: llmModel || null,
			vision_model: visionModel || null,
			temperature: temperature === '' ? null : Number(temperature),
			max_tokens: maxTokens === '' ? null : Number(maxTokens),
			agent_max_steps: agentMaxSteps === '' ? null : Number(agentMaxSteps)
		};
		// Only include API-key fields if the user typed something. Empty string
		// is a meaningful "clear" signal — but blank by default is "no change".
		if (ollamaApiKey) patch.ollama_api_key = ollamaApiKey;
		if (openaiApiKey) patch.openai_api_key = openaiApiKey;

		try {
			const result = await updateConfig(app.useCase, patch);
			if (result.error) {
				cfgError = result.detail || result.error;
			} else {
				cfgSaved = true;
				ollamaApiKey = '';
				openaiApiKey = '';
				setTimeout(() => (cfgSaved = false), 2000);
				await loadConfig();
			}
		} catch (e) {
			cfgError = e instanceof Error ? e.message : String(e);
		}
		cfgLoading = false;
	}

	async function clearOllamaKey() {
		try {
			await updateConfig(app.useCase, { ollama_api_key: '' });
			await loadConfig();
		} catch {
			/* ignore */
		}
	}

	async function clearOpenaiKey() {
		try {
			await updateConfig(app.useCase, { openai_api_key: '' });
			await loadConfig();
		} catch {
			/* ignore */
		}
	}

	async function loadPrompts() {
		promptsLoading = true;
		try {
			const data = await listPromptKeys(app.useCase);
			promptEntries = data.prompts.map((p) => ({
				...p,
				dirty: false,
				saving: false,
				saved: false
			}));
		} catch {
			promptEntries = [];
		}
		promptsLoading = false;
	}

	async function savePrompt(idx: number) {
		const p = promptEntries[idx];
		if (!p) return;
		p.saving = true;
		try {
			await updatePromptKey(p.scope, p.key, p.value);
			p.dirty = false;
			p.saved = true;
			setTimeout(() => (p.saved = false), 2000);
		} catch {
			/* ignore */
		}
		p.saving = false;
	}

	async function loadSkills() {
		skillsLoading = true;
		try {
			const data = await listSkills(app.useCase);
			skills = (data.skills ?? []).map((s) => ({
				...s,
				expanded: false,
				dirty: false,
				saving: false,
				saved: false
			}));
		} catch {
			skills = [];
		}
		skillsLoading = false;
	}

	async function addSkill() {
		if (!newSkillName.trim() || !newSkillOverview.trim() || !newSkillTask.trim()) {
			createSkillError = 'Name, Übersicht und Beschreibung sind Pflichtfelder.';
			return;
		}
		creatingSkill = true;
		createSkillError = '';
		try {
			const result = await createSkill(app.useCase, {
				name: newSkillName.trim(),
				overview: newSkillOverview.trim(),
				detailed_task: newSkillTask,
				position: skills.length
			});
			if (result.error) {
				createSkillError = result.detail || result.error;
			} else {
				newSkillName = '';
				newSkillOverview = '';
				newSkillTask = '';
				await loadSkills();
			}
		} catch (e) {
			createSkillError = e instanceof Error ? e.message : String(e);
		}
		creatingSkill = false;
	}

	async function saveSkill(idx: number) {
		const s = skills[idx];
		if (!s) return;
		s.saving = true;
		try {
			await updateSkill(s.id, {
				name: s.name,
				overview: s.overview,
				detailed_task: s.detailed_task
			});
			s.dirty = false;
			s.saved = true;
			setTimeout(() => (s.saved = false), 2000);
		} catch {
			/* ignore */
		}
		s.saving = false;
	}

	async function toggleSkill(idx: number) {
		const s = skills[idx];
		if (!s) return;
		s.saving = true;
		const next = !s.enabled;
		try {
			await updateSkill(s.id, { enabled: next });
			s.enabled = next;
		} catch {
			/* ignore */
		}
		s.saving = false;
	}

	async function removeSkill(idx: number) {
		const s = skills[idx];
		if (!s) return;
		if (!confirm(`Skill "${s.name}" wirklich löschen?`)) return;
		try {
			await deleteSkill(s.id);
			await loadSkills();
		} catch {
			/* ignore */
		}
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
		loadConfig();
		loadModels();
		loadPrompts();
		loadSkills();
		loadMemory();
		loadActions();
	});
</script>

<main class="flex h-full flex-1 flex-col overflow-y-auto bg-gray-50">
	<div class="mx-auto w-full max-w-4xl space-y-6 p-6">
		<div class="flex items-baseline justify-between">
			<h2 class="text-lg font-semibold text-gray-800">Einstellungen</h2>
			<span class="text-xs text-gray-400">
				Use Case: <span class="font-medium text-gray-600">{app.useCase}</span>
				&middot; Rolle: <span class="font-medium text-gray-600">{app.role}</span>
			</span>
		</div>

		<!-- Tab nav -->
		<nav class="flex gap-1 border-b border-gray-200">
			{#each tabs as t}
				<button
					onclick={() => (activeTab = t.id)}
					class="-mb-px border-b-2 px-3 py-2 text-sm font-medium transition-colors
						{activeTab === t.id
							? 'border-blue-600 text-blue-600'
							: 'border-transparent text-gray-500 hover:text-gray-800'}"
				>
					{t.label}
				</button>
			{/each}
		</nav>

		{#if activeTab === 'models'}
		<!-- Provider / Modelle / API-Keys -->
		<section class="space-y-3">
			<div class="flex items-center justify-between">
				<div>
					<h3 class="text-sm font-semibold uppercase tracking-wider text-gray-400">
						Provider &amp; Modelle
					</h3>
					<p class="mt-0.5 text-xs text-gray-400">
						Leere Felder verwenden den Wert aus der globalen <code>.env</code>.
					</p>
				</div>
				<div class="flex items-center gap-2">
					{#if cfgSaved}
						<span class="text-xs text-green-600">Gespeichert!</span>
					{/if}
					<button
						onclick={saveConfig}
						disabled={cfgLoading}
						class="rounded-lg bg-blue-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-blue-700 disabled:opacity-40"
					>
						Speichern
					</button>
				</div>
			</div>

			{#if cfgError}
				<div class="rounded border border-red-200 bg-red-50 p-2 text-xs text-red-700">
					{cfgError}
				</div>
			{/if}

			{#if !cryptoConfigured}
				<div class="rounded border border-yellow-200 bg-yellow-50 p-2 text-xs text-yellow-800">
					<strong>Hinweis:</strong> <code>CONFIG_MASTER_KEY</code> ist nicht in
					<code>.env</code> gesetzt. API-Keys koennen nicht ueber das UI gespeichert werden,
					nur ueber <code>.env</code>.
				</div>
			{/if}

			<!-- Gruppe 1: Chat -->
			<fieldset class="rounded-lg border border-gray-200 bg-white p-3">
				<legend class="px-1 text-xs font-semibold uppercase text-gray-500">Chat</legend>
				<div class="grid grid-cols-1 gap-3 md:grid-cols-2">
					<label class="flex flex-col gap-1">
						<span class="text-xs font-medium text-gray-500">Provider</span>
						<select
							bind:value={chatProvider}
							class="rounded border border-gray-200 px-2.5 py-1.5 text-sm"
						>
							<option value="ollama">ollama</option>
							<option value="openai">openai</option>
						</select>
					</label>
					<label class="flex flex-col gap-1">
						<span class="text-xs font-medium text-gray-500">LLM-Modell</span>
						{#if availableModels.length > 0}
							<select
								bind:value={llmModel}
								class="rounded border border-gray-200 px-2.5 py-1.5 text-sm"
							>
								<option value="">– .env Default –</option>
								{#each availableModels as m}
									<option value={m}>{m}</option>
								{/each}
								{#if llmModel && !availableModels.includes(llmModel)}
									<option value={llmModel}>{llmModel} (aktuell)</option>
								{/if}
							</select>
						{:else}
							<input
								type="text"
								bind:value={llmModel}
								placeholder={modelsLoading ? 'Laden...' : 'qwen3:8b'}
								class="rounded border border-gray-200 px-2.5 py-1.5 text-sm"
								disabled={modelsLoading}
							/>
						{/if}
					</label>
					<label class="flex flex-col gap-1">
						<span class="text-xs font-medium text-gray-500">Temperature</span>
						<input
							type="number"
							step="0.1"
							min="0"
							max="2"
							bind:value={temperature}
							placeholder="0.2"
							class="rounded border border-gray-200 px-2.5 py-1.5 text-sm"
						/>
					</label>
					<label class="flex flex-col gap-1">
						<span class="text-xs font-medium text-gray-500">Max Tokens</span>
						<input
							type="number"
							min="1"
							bind:value={maxTokens}
							placeholder="(Provider-Default)"
							class="rounded border border-gray-200 px-2.5 py-1.5 text-sm"
						/>
					</label>
					<label class="flex flex-col gap-1 md:col-span-2">
						<span class="text-xs font-medium text-gray-500">Agent Max-Steps</span>
						<input
							type="number"
							min="1"
							bind:value={agentMaxSteps}
							placeholder="5"
							class="rounded border border-gray-200 px-2.5 py-1.5 text-sm"
						/>
					</label>
				</div>
			</fieldset>

			<!-- Gruppe 2: Vision -->
			<fieldset class="rounded-lg border border-gray-200 bg-white p-3">
				<legend class="px-1 text-xs font-semibold uppercase text-gray-500">Vision</legend>
				<div class="grid grid-cols-1 gap-3 md:grid-cols-2">
					<label class="flex flex-col gap-1">
						<span class="text-xs font-medium text-gray-500">Provider</span>
						<select
							bind:value={visionProvider}
							class="rounded border border-gray-200 px-2.5 py-1.5 text-sm"
						>
							<option value="ollama">ollama</option>
							<option value="openai">openai</option>
						</select>
					</label>
					<label class="flex flex-col gap-1">
						<span class="text-xs font-medium text-gray-500">Modell</span>
						<input
							type="text"
							bind:value={visionModel}
							placeholder="qwen3:8b"
							class="rounded border border-gray-200 px-2.5 py-1.5 text-sm"
						/>
					</label>
				</div>
			</fieldset>

			<!-- Gruppe 3: Endpunkte (nur relevante Provider zeigen) -->
			<fieldset class="rounded-lg border border-gray-200 bg-white p-3">
				<legend class="px-1 text-xs font-semibold uppercase text-gray-500">Endpunkte &amp; Keys</legend>
				<div class="grid grid-cols-1 gap-3 md:grid-cols-2">
					{#if chatProvider === 'ollama' || visionProvider === 'ollama'}
						<label class="flex flex-col gap-1">
							<span class="text-xs font-medium text-gray-500">Ollama Base URL</span>
							<input
								type="text"
								bind:value={ollamaBaseUrl}
								placeholder="http://ollama:11434"
								class="rounded border border-gray-200 px-2.5 py-1.5 text-sm"
							/>
						</label>
						<label class="flex flex-col gap-1">
							<span class="text-xs font-medium text-gray-500">
								Ollama API-Key
								{#if cfg?.ollama_api_key_set}
									<span class="text-green-600">&middot; gesetzt {cfg.ollama_api_key_preview}</span>
								{/if}
							</span>
							<div class="flex gap-1">
								<input
									type="password"
									bind:value={ollamaApiKey}
									placeholder={cfg?.ollama_api_key_set ? '(unveraendert)' : ''}
									disabled={!cryptoConfigured}
									class="flex-1 rounded border border-gray-200 px-2.5 py-1.5 text-sm disabled:bg-gray-50"
								/>
								{#if cfg?.ollama_api_key_set}
									<button
										type="button"
										onclick={clearOllamaKey}
										class="rounded border border-red-200 px-2 text-xs text-red-600 hover:bg-red-50"
									>
										Loeschen
									</button>
								{/if}
							</div>
						</label>
					{/if}
					{#if chatProvider === 'openai' || visionProvider === 'openai'}
						<label class="flex flex-col gap-1">
							<span class="text-xs font-medium text-gray-500">OpenAI Base URL</span>
							<input
								type="text"
								bind:value={openaiBaseUrl}
								placeholder="https://api.openai.com/v1"
								class="rounded border border-gray-200 px-2.5 py-1.5 text-sm"
							/>
						</label>
						<label class="flex flex-col gap-1">
							<span class="text-xs font-medium text-gray-500">
								OpenAI API-Key
								{#if cfg?.openai_api_key_set}
									<span class="text-green-600">&middot; gesetzt {cfg.openai_api_key_preview}</span>
								{/if}
							</span>
							<div class="flex gap-1">
								<input
									type="password"
									bind:value={openaiApiKey}
									placeholder={cfg?.openai_api_key_set ? '(unveraendert)' : ''}
									disabled={!cryptoConfigured}
									class="flex-1 rounded border border-gray-200 px-2.5 py-1.5 text-sm disabled:bg-gray-50"
								/>
								{#if cfg?.openai_api_key_set}
									<button
										type="button"
										onclick={clearOpenaiKey}
										class="rounded border border-red-200 px-2 text-xs text-red-600 hover:bg-red-50"
									>
										Loeschen
									</button>
								{/if}
							</div>
						</label>
					{/if}
				</div>
			</fieldset>

			<p class="text-xs text-gray-400">
				Embedding-Modell &amp; -Dimension werden global ueber <code>.env</code> gepflegt
				&mdash; ein Wechsel erfordert ein Re-Embedding aller bestehenden Vektoren.
			</p>
		</section>
		{:else if activeTab === 'prompts'}

		<!-- Prompts (multi-key editor) -->
		<section class="space-y-3">
			<div>
				<h3 class="text-sm font-semibold uppercase tracking-wider text-gray-400">Prompts</h3>
				<p class="mt-0.5 text-xs text-gray-400">
					Editierbare Prompts pro Use Case. Leeres Feld &rarr; Default aus dem Code wird
					verwendet.
				</p>
			</div>

			{#if promptsLoading}
				<p class="animate-pulse text-sm text-gray-400">Laden...</p>
			{:else if promptEntries.length === 0}
				<p class="text-sm text-gray-400">Keine Prompts gefunden.</p>
			{:else}
				<div class="space-y-3">
					{#each promptEntries as p, idx}
						<div class="rounded-lg border border-gray-200 bg-white p-3">
							<div class="mb-1.5 flex items-center justify-between">
								<div class="flex items-center gap-2">
									<span class="text-sm font-semibold text-gray-800">{p.label}</span>
									<code class="text-[10px] text-gray-400">{p.scope}/{p.key}</code>
									{#if p.is_override}
										<span class="rounded bg-blue-100 px-1.5 text-[10px] text-blue-700"
											>ueberschrieben</span
										>
									{:else}
										<span class="rounded bg-gray-100 px-1.5 text-[10px] text-gray-600">
											Default
										</span>
									{/if}
								</div>
								<div class="flex items-center gap-2">
									{#if p.saved}
										<span class="text-xs text-green-600">Gespeichert!</span>
									{/if}
									<button
										onclick={() => savePrompt(idx)}
										disabled={p.saving}
										class="rounded bg-blue-600 px-2 py-1 text-xs font-medium text-white hover:bg-blue-700 disabled:opacity-40"
									>
										Speichern
									</button>
								</div>
							</div>
							<textarea
								bind:value={p.value}
								oninput={() => (p.dirty = true)}
								rows={6}
								class="w-full rounded border border-gray-200 px-3 py-2 text-xs text-gray-700 focus:border-blue-400 focus:outline-none focus:ring-1 focus:ring-blue-400"
							></textarea>
						</div>
					{/each}
				</div>
			{/if}
		</section>

		{:else if activeTab === 'skills'}

		<!-- Skills / Regeln -->
		<section class="space-y-3">
			<div>
				<h3 class="text-sm font-semibold uppercase tracking-wider text-gray-400">
					Skills / Regeln
				</h3>
				<p class="mt-0.5 text-xs text-gray-400">
					Module aus Übersicht + Beschreibung, die an den Agent-System-Prompt
					angehängt werden. Beispiel: <em>quality_manager</em>, der prüft ob die
					Antwort schlüssig ist. Aktive Skills greifen sofort bei neuen Anfragen.
				</p>
			</div>

			<!-- New skill form -->
			<div class="rounded-lg border border-gray-200 bg-white p-3">
				<h4 class="mb-2 text-xs font-semibold text-gray-500">Neuen Skill anlegen</h4>
				<div class="grid grid-cols-1 gap-2 md:grid-cols-3">
					<input
						type="text"
						bind:value={newSkillName}
						placeholder="Name (z.B. quality_manager)"
						class="rounded border border-gray-200 px-2.5 py-1.5 text-sm"
					/>
					<input
						type="text"
						bind:value={newSkillOverview}
						placeholder="Übersicht (kurze Beschreibung)"
						class="rounded border border-gray-200 px-2.5 py-1.5 text-sm md:col-span-2"
					/>
				</div>
				<textarea
					bind:value={newSkillTask}
					rows={4}
					placeholder="Genaue Anweisung: Wann greift der Skill? Was soll der Agent tun? (Workflow erweitern, abbrechen, oder modifizieren)"
					class="mt-2 w-full rounded border border-gray-200 px-3 py-2 text-xs text-gray-700"
				></textarea>
				{#if createSkillError}
					<p class="mt-2 text-xs text-red-600">{createSkillError}</p>
				{/if}
				<div class="mt-2 flex justify-end">
					<button
						onclick={addSkill}
						disabled={creatingSkill}
						class="rounded bg-blue-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-blue-700 disabled:opacity-40"
					>
						Hinzufügen
					</button>
				</div>
			</div>

			{#if skillsLoading}
				<p class="animate-pulse text-sm text-gray-400">Laden...</p>
			{:else if skills.length === 0}
				<p class="text-sm text-gray-400">Noch keine Skills definiert.</p>
			{:else}
				<div class="space-y-2">
					{#each skills as s, idx}
						<div
							class="rounded-lg border bg-white shadow-sm transition-opacity
								{s.enabled ? 'border-gray-200' : 'border-gray-100 opacity-60'}"
						>
							<div class="flex items-start gap-3 px-4 py-3">
								<button
									onclick={() => toggleSkill(idx)}
									disabled={s.saving}
									class="mt-0.5 shrink-0"
									title={s.enabled ? 'Deaktivieren' : 'Aktivieren'}
								>
									<div
										class="relative h-5 w-9 rounded-full transition-colors
											{s.enabled ? 'bg-blue-600' : 'bg-gray-300'}"
									>
										<div
											class="absolute top-0.5 h-4 w-4 rounded-full bg-white shadow transition-transform
												{s.enabled ? 'translate-x-4' : 'translate-x-0.5'}"
										></div>
									</div>
								</button>

								<div class="min-w-0 flex-1">
									<div class="flex items-center gap-2">
										<input
											type="text"
											bind:value={s.name}
											oninput={() => (s.dirty = true)}
											class="rounded border border-transparent px-1 text-sm font-semibold text-gray-800 hover:border-gray-200 focus:border-blue-400 focus:outline-none"
										/>
										<button
											onclick={() => (s.expanded = !s.expanded)}
											class="text-xs text-blue-600 hover:underline"
										>
											{s.expanded ? 'Zuklappen' : 'Bearbeiten'}
										</button>
										{#if s.saved}
											<span class="text-xs text-green-600">Gespeichert!</span>
										{/if}
										{#if s.saving}
											<span class="animate-pulse text-[10px] text-blue-500">...</span>
										{/if}
										<button
											onclick={() => removeSkill(idx)}
											class="ml-auto text-xs text-red-500 hover:underline"
										>
											Löschen
										</button>
									</div>

									<input
										type="text"
										bind:value={s.overview}
										oninput={() => (s.dirty = true)}
										class="mt-1.5 w-full rounded border border-gray-200 px-2.5 py-1 text-xs text-gray-600 focus:border-blue-400 focus:outline-none focus:ring-1 focus:ring-blue-400"
										placeholder="Übersicht"
									/>

									{#if s.expanded}
										<textarea
											bind:value={s.detailed_task}
											oninput={() => (s.dirty = true)}
											rows={6}
											class="mt-2 w-full rounded border border-gray-200 px-3 py-2 font-mono text-xs text-gray-700 focus:border-blue-400 focus:outline-none focus:ring-1 focus:ring-blue-400"
											placeholder="Genaue Anweisung"
										></textarea>
										<div class="mt-2 flex justify-end">
											<button
												onclick={() => saveSkill(idx)}
												disabled={!s.dirty || s.saving}
												class="rounded bg-blue-600 px-2 py-1 text-xs font-medium text-white hover:bg-blue-700 disabled:opacity-40"
											>
												Speichern
											</button>
										</div>
									{/if}
								</div>
							</div>
						</div>
					{/each}
				</div>
			{/if}
		</section>

		{:else if activeTab === 'actions'}

		<!-- Agent Actions -->
		<section class="space-y-3">
			<div>
				<h3 class="text-sm font-semibold uppercase tracking-wider text-gray-400">
					Agent-Aktionen
				</h3>
				<p class="mt-0.5 text-xs text-gray-400">
					Aktionen einzeln aktivieren/deaktivieren und beschreiben.
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
								<button
									onclick={() => toggleAction(name)}
									disabled={action.saving}
									class="mt-0.5 shrink-0"
									title={action.enabled ? 'Deaktivieren' : 'Aktivieren'}
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

								<div class="min-w-0 flex-1">
									<div class="flex items-center gap-2">
										<span class="text-sm">{@html icon}</span>
										<span class="text-sm font-semibold text-gray-800">{name}</span>
										<span class="text-xs text-gray-400">{label}</span>
										{#if action.saving}
											<span class="animate-pulse text-[10px] text-blue-500">...</span>
										{/if}
									</div>

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

		{:else if activeTab === 'memory'}

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
		{/if}
	</div>
</main>
