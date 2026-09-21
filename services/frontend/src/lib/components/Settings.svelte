<script lang="ts">
	import { app } from '$lib/state.svelte';
	import {
		getMemory,
		updateMemory,
		getActions,
		updateAction,
		getConfig,
		updateConfig,
		testConfig,
		listModels,
		listPromptKeys,
		updatePromptKey,
		listSkills,
		createSkill,
		updateSkill,
		deleteSkill,
		getSkillCode,
		saveSkillCode,
		deleteSkillCode,
		getSkillVariables,
		saveSkillVariables,
		exportSkills,
		importSkills,
		setFunctionPermission,
		type UsecaseConfig,
		type PromptKeyEntry,
		type Skill,
		type SkillFunctionDef,
		type SkillImportResult,
		type ConfigTestResult
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
	let modelsError = $state('');
	let modelsHint = $state('');
	let testLoading = $state(false);
	let testResult: ConfigTestResult | null = $state(null);
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

	// ── Plugin layer: code file + variables per skill ────────
	// Loaded lazily, only for the skill whose panel is open: a code file can
	// be a quarter megabyte and the variables endpoint is a separate,
	// admin-only read that should not fire for every row in the list.
	interface SkillPluginState {
		loading: boolean;
		fileName: string;
		source: string;
		functions: SkillFunctionDef[];
		saving: boolean;
		error: string;
		saved: boolean;
		/** Name/value rows. Values are write-only — see loadPlugin(). */
		variables: Array<{ name: string; value: string; preview: string; isNew: boolean }>;
		variablesSaving: boolean;
		variablesError: string;
		variablesSaved: boolean;
	}
	let pluginState: Record<string, SkillPluginState> = $state({});

	function emptyPlugin(): SkillPluginState {
		return {
			loading: false,
			fileName: '',
			source: '',
			functions: [],
			saving: false,
			error: '',
			saved: false,
			variables: [],
			variablesSaving: false,
			variablesError: '',
			variablesSaved: false
		};
	}

	// ── Export / Import ─────────────────────────────────────

	let importInput: HTMLInputElement | undefined = $state();
	let importOverwrite = $state(false);
	let importResult: SkillImportResult | null = $state(null);
	let importError = $state('');
	let busyTransfer = $state(false);

	async function doExportSkills(skill?: SkillRow) {
		busyTransfer = true;
		importError = '';
		try {
			const doc = await exportSkills(app.useCase, skill?.id);
			const blob = new Blob([JSON.stringify(doc, null, 2) + '\n'], {
				type: 'application/json'
			});
			const url = URL.createObjectURL(blob);
			const a = document.createElement('a');
			a.href = url;
			a.download = skill
				? `skill_${app.useCase}_${skill.name}.json`
				: `skills_${app.useCase}.json`;
			a.click();
			URL.revokeObjectURL(url);
		} catch (e) {
			importError = e instanceof Error ? e.message : String(e);
		}
		busyTransfer = false;
	}

	async function onImportFile(event: Event) {
		const input = event.currentTarget as HTMLInputElement;
		const file = input.files?.[0];
		input.value = ''; // so the same file can be picked again after a fix
		if (!file) return;

		busyTransfer = true;
		importError = '';
		importResult = null;
		try {
			const doc = JSON.parse(await file.text());
			if (!Array.isArray(doc.skills)) {
				importError = 'Die Datei enthält kein "skills"-Array.';
			} else {
				const result = await importSkills(app.useCase, doc, importOverwrite);
				if (result.error) {
					importError = result.detail ?? result.error;
				} else {
					importResult = result;
					await loadSkills();
				}
			}
		} catch (e) {
			importError = e instanceof Error ? e.message : String(e);
		}
		busyTransfer = false;
	}

	async function toggleFunctionPermission(
		skill: SkillRow,
		fn: SkillFunctionDef,
		ask: boolean
	) {
		const entry = pluginState[skill.id];
		if (entry) entry.error = '';
		try {
			const res = await setFunctionPermission(skill.id, fn.name, ask);
			if (res.error) {
				if (entry) entry.error = res.detail ?? res.error;
				return;
			}
			// Update both views in place: the panel the admin is looking at and
			// the badge row of the collapsed list, which reads skill.functions.
			fn.ask_for_permission = ask;
			fn.permission_override = ask;
			const listed = (skill.functions ?? []).find((f) => f.name === fn.name);
			if (listed) {
				listed.ask_for_permission = ask;
				listed.permission_override = ask;
			}
		} catch (e) {
			if (entry) entry.error = e instanceof Error ? e.message : String(e);
		}
	}

	// ── Risikoprofil eines Skills ───────────────────────────
	// Was ein Skill tun *kann*, ist bei ausführbarem Code die wichtigste
	// Information der Seite. Sie kommt mit dem Listing mit, also braucht die
	// Übersicht keinen Request pro Zeile.

	function skillFunctions(s: SkillRow): SkillFunctionDef[] {
		return s.functions ?? [];
	}

	function gatedFunctions(s: SkillRow): SkillFunctionDef[] {
		return skillFunctions(s).filter((f) => f.ask_for_permission);
	}

	function skillHosts(s: SkillRow): string[] {
		return [...new Set(skillFunctions(s).flatMap((f) => f.allowed_domains ?? []))];
	}

	function toggleSkillPanel(idx: number) {
		const skill = skills[idx];
		skill.expanded = !skill.expanded;
		// Fetch on open, once. The code file and the (admin-only) variables are
		// two extra requests per skill, so they stay off the list view — but
		// making the user click a second time to see whether a skill even has
		// functions is a worse trade.
		if (skill.expanded && !pluginState[skill.id]) loadPlugin(skill.id);
	}

	async function loadPlugin(skillId: string) {
		if (pluginState[skillId]?.loading) return;
		const entry = pluginState[skillId] ?? emptyPlugin();
		entry.loading = true;
		pluginState[skillId] = entry;
		try {
			const [code, vars] = await Promise.all([
				getSkillCode(skillId),
				getSkillVariables(skillId)
			]);
			entry.fileName = code.file_name ?? '';
			entry.source = code.source ?? '';
			entry.functions = code.functions ?? [];
			// The server sends names and a mask, never the stored value. An
			// empty `value` therefore means "unchanged", not "empty" — saving
			// sends the mask back only for rows the admin did not touch.
			entry.variables = (vars.variables ?? []).map((v) => ({
				name: v.name,
				value: '',
				preview: v.preview,
				isNew: false
			}));
		} catch (e) {
			entry.error = e instanceof Error ? e.message : String(e);
		}
		entry.loading = false;
		pluginState[skillId] = entry;
	}

	async function savePluginCode(skillId: string) {
		const entry = pluginState[skillId];
		if (!entry) return;
		entry.saving = true;
		entry.error = '';
		entry.saved = false;
		try {
			const result = await saveSkillCode(skillId, {
				file_name: entry.fileName.trim(),
				source: entry.source
			});
			if (result.error) {
				entry.error = result.detail ?? result.error;
			} else {
				entry.functions = result.functions ?? [];
				entry.saved = true;
				setTimeout(() => {
					if (pluginState[skillId]) pluginState[skillId].saved = false;
				}, 2500);
				await loadSkills();
			}
		} catch (e) {
			entry.error = e instanceof Error ? e.message : String(e);
		}
		entry.saving = false;
	}

	async function removePluginCode(skillId: string) {
		if (!confirm('Code-Datei und alle daraus abgeleiteten Funktionen löschen?')) return;
		const entry = pluginState[skillId] ?? emptyPlugin();
		try {
			await deleteSkillCode(skillId);
			entry.fileName = '';
			entry.source = '';
			entry.functions = [];
			pluginState[skillId] = entry;
			await loadSkills();
		} catch (e) {
			entry.error = e instanceof Error ? e.message : String(e);
			pluginState[skillId] = entry;
		}
	}

	function addVariableRow(skillId: string) {
		const entry = pluginState[skillId] ?? emptyPlugin();
		entry.variables = [...entry.variables, { name: '', value: '', preview: '', isNew: true }];
		pluginState[skillId] = entry;
	}

	function removeVariableRow(skillId: string, index: number) {
		const entry = pluginState[skillId];
		if (!entry) return;
		entry.variables = entry.variables.filter((_, i) => i !== index);
	}

	async function savePluginVariables(skillId: string) {
		const entry = pluginState[skillId];
		if (!entry) return;
		// The read side is masked, so this form never holds the real value of
		// an existing variable. An untouched row is therefore sent as null,
		// which the server reads as "keep what is stored" — sending the mask
		// back would overwrite a live API key with the string '****abcd'.
		const payload: Record<string, string | null> = {};
		for (const row of entry.variables) {
			const name = row.name.trim();
			if (!name) continue;
			payload[name] = row.isNew || row.value ? row.value : null;
		}
		entry.variablesSaving = true;
		entry.variablesError = '';
		entry.variablesSaved = false;
		try {
			const result = await saveSkillVariables(skillId, payload);
			if (result.error) {
				entry.variablesError = result.detail ?? result.error;
			} else {
				entry.variablesSaved = true;
				entry.variables = entry.variables.map((v) => ({
					...v,
					value: '',
					isNew: false,
					preview: v.value ? `****${v.value.slice(-4)}` : v.preview
				}));
				setTimeout(() => {
					if (pluginState[skillId]) pluginState[skillId].variablesSaved = false;
				}, 2500);
			}
		} catch (e) {
			entry.variablesError = e instanceof Error ? e.message : String(e);
		}
		entry.variablesSaving = false;
	}

	// ── Loaders ─────────────────────────────────────────────

	async function loadModels() {
		modelsLoading = true;
		modelsError = '';
		modelsHint = '';
		try {
			const data = await listModels(app.useCase);
			availableModels = data.models ?? [];
			if (data.error) {
				modelsError = data.message || data.error;
				modelsHint = data.hint ?? '';
			}
		} catch (e) {
			availableModels = [];
			modelsError = e instanceof Error ? e.message : String(e);
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

	async function testConnection() {
		testLoading = true;
		testResult = null;
		cfgError = '';
		const patch: Record<string, string | null> = {
			chat_provider: chatProvider,
			vision_provider: visionProvider,
			ollama_base_url: ollamaBaseUrl || null,
			openai_base_url: openaiBaseUrl || null,
			llm_model: llmModel || null,
			vision_model: visionModel || null
		};
		if (ollamaApiKey) patch.ollama_api_key = ollamaApiKey;
		if (openaiApiKey) patch.openai_api_key = openaiApiKey;
		try {
			const result = await testConfig(app.useCase, patch);
			testResult = result;
			const chat = result.checks.find((c) => c.role === 'chat');
			if (chat?.ok && chat.models.length > 0 && chat.models.length <= 50) {
				availableModels = chat.models;
				modelsError = '';
				modelsHint = '';
			}
		} catch (e) {
			cfgError = e instanceof Error ? e.message : String(e);
		}
		testLoading = false;
	}

	async function saveConfig() {
		cfgLoading = true;
		cfgError = '';
		testResult = null;
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
				if (result.error === 'crypto_unavailable') {
					cfgError =
						'API-Keys können nicht gespeichert werden: CONFIG_MASTER_KEY ist nicht gesetzt.';
				} else if (result.error === 'db_unavailable') {
					cfgError =
						'Die Datenbank ist nicht erreichbar. Die Konfiguration wurde nicht gespeichert.';
				} else {
					cfgError = result.detail || result.error;
				}
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
						„Verbindung testen“ prüft die aktuell eingetragenen Werte (auch ungespeicherte Keys/URLs).
					</p>
				</div>
				<div class="flex items-center gap-2">
					{#if cfgSaved}
						<span class="text-xs text-green-600">Gespeichert!</span>
					{/if}
					<button
						type="button"
						onclick={testConnection}
						disabled={cfgLoading || testLoading}
						class="rounded-lg border border-blue-200 bg-white px-3 py-1.5 text-xs font-medium text-blue-700 hover:bg-blue-50 disabled:opacity-40"
					>
						{testLoading ? 'Teste…' : 'Verbindung testen'}
					</button>
					<button
						onclick={saveConfig}
						disabled={cfgLoading || testLoading}
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

			{#if testResult}
				<div class="space-y-2">
					{#each testResult.checks as check}
						<div
							class="rounded border p-3 text-xs
								{check.ok
								? 'border-green-200 bg-green-50 text-green-900'
								: 'border-red-200 bg-red-50 text-red-800'}"
						>
							<div class="flex items-start justify-between gap-2">
								<p class="font-semibold">
									{check.ok ? '✓' : '✗'}
									{check.role === 'chat' ? 'Chat' : 'Vision'}
									<span class="font-normal text-gray-600">
										· {check.provider}{#if check.model} · {check.model}{/if}
									</span>
								</p>
								<span class="shrink-0 text-[10px] text-gray-500">{check.latency_ms} ms</span>
							</div>
							<p class="mt-1">{check.message}</p>
							{#if check.hint}
								<p class="mt-1 text-gray-700">{check.hint}</p>
							{/if}
							{#if check.base_url}
								<p class="mt-1 font-mono text-[10px] text-gray-500">{check.base_url}</p>
							{/if}
							{#if !check.ok && !check.skipped && check.detail && check.detail !== check.message}
								<details class="mt-1.5">
									<summary class="cursor-pointer text-[10px] text-gray-500 hover:text-gray-700">
										Technische Details
									</summary>
									<pre class="mt-1 max-h-32 overflow-auto whitespace-pre-wrap break-all font-mono text-[10px] text-gray-600">{check.detail}</pre>
								</details>
							{/if}
						</div>
					{/each}
				</div>
			{/if}

			{#if modelsError}
				<div class="rounded border border-amber-200 bg-amber-50 p-2 text-xs text-amber-900">
					<p class="font-medium">Modellliste: {modelsError}</p>
					{#if modelsHint}
						<p class="mt-0.5 text-amber-800">{modelsHint}</p>
					{/if}
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

			<!-- Export / Import -->
			<div class="flex flex-wrap items-center gap-2 rounded-lg border border-gray-200 bg-white px-3 py-2">
				<button
					onclick={() => doExportSkills()}
					disabled={busyTransfer || skills.length === 0}
					class="rounded border border-gray-300 px-2.5 py-1 text-xs font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-40"
				>
					Exportieren
				</button>
				<button
					onclick={() => importInput?.click()}
					disabled={busyTransfer}
					class="rounded border border-gray-300 px-2.5 py-1 text-xs font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-40"
				>
					Importieren …
				</button>
				<input
					bind:this={importInput}
					type="file"
					accept="application/json,.json"
					onchange={onImportFile}
					class="hidden"
				/>
				<label class="flex items-center gap-1.5 text-xs text-gray-500">
					<input type="checkbox" bind:checked={importOverwrite} class="rounded border-gray-300" />
					Bestehende überschreiben
				</label>
				<span class="ml-auto text-[11px] text-gray-400">
					Der Export enthält den Code, aber keine Variablenwerte.
				</span>
			</div>

			{#if importError}
				<p class="rounded border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
					{importError}
				</p>
			{/if}

			{#if importResult}
				<!-- Was tatsächlich angekommen ist. Ein Import bringt Code ins
				     System, der ausgeführt wird — danach soll niemand suchen
				     müssen, was er sich geholt hat. -->
				<div class="rounded-lg border border-gray-200 bg-white p-3 text-xs">
					<div class="mb-1.5 flex items-center justify-between">
						<span class="font-semibold text-gray-700">Import-Ergebnis</span>
						<button
							onclick={() => (importResult = null)}
							class="text-gray-400 hover:text-gray-600"
							aria-label="Ergebnis schließen">&times;</button
						>
					</div>
					{#each [...importResult.created.map((e) => ({ e, tag: 'neu' })), ...importResult.updated.map((e) => ({ e, tag: 'aktualisiert' }))] as row}
						<div class="border-t border-gray-100 py-1.5">
							<div class="flex flex-wrap items-center gap-1.5">
								<span class="font-medium text-gray-800">{row.e.name}</span>
								<span class="rounded bg-emerald-50 px-1.5 py-0.5 text-[10px] text-emerald-700">
									{row.tag}
								</span>
								{#if row.e.functions.length > 0}
									<span class="text-gray-500">
										{row.e.functions.length}
										{row.e.functions.length === 1 ? 'Funktion' : 'Funktionen'}
									</span>
								{/if}
							</div>
							{#if row.e.needs_permission.length > 0}
								<p class="mt-0.5 text-amber-800">
									Fragt vor Ausführung nach: {row.e.needs_permission.join(', ')}
								</p>
							{/if}
							{#if row.e.reaches_hosts.length > 0}
								<p class="mt-0.5 text-sky-800">
									Darf erreichen: {row.e.reaches_hosts.join(', ')}
								</p>
							{/if}
							{#if row.e.missing_variables.length > 0}
								<p class="mt-0.5 text-gray-600">
									Variablen ohne Wert: {row.e.missing_variables.join(', ')} — im Skill
									hinterlegen, sonst laufen die Funktionen ins Leere.
								</p>
							{/if}
						</div>
					{/each}
					{#if importResult.skipped.length > 0}
						<p class="border-t border-gray-100 pt-1.5 text-gray-500">
							Übersprungen (Name existiert bereits): {importResult.skipped.join(', ')} —
							„Bestehende überschreiben" aktivieren, um sie zu ersetzen.
						</p>
					{/if}
					{#each importResult.failed as f}
						<p class="border-t border-gray-100 pt-1.5 text-red-700">
							{f.name}: {f.error}
						</p>
					{/each}
				</div>
			{/if}

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
											onclick={() => toggleSkillPanel(idx)}
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
											onclick={() => doExportSkills(s)}
											disabled={busyTransfer}
											class="ml-auto text-xs text-gray-500 hover:underline disabled:opacity-40"
											title="Nur diesen Skill als JSON exportieren"
										>
											Exportieren
										</button>
										<button
											onclick={() => removeSkill(idx)}
											class="text-xs text-red-500 hover:underline"
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

									<!-- Risikoprofil: was dieser Skill tun kann, ohne Aufklappen.
									     Bei ausführbarem Code ist das die wichtigste Information
									     der ganzen Seite, deshalb steht sie in der Zeile selbst. -->
									<div class="mt-2 flex flex-wrap items-center gap-1.5">
										{#if skillFunctions(s).length === 0}
											<span class="rounded bg-gray-100 px-1.5 py-0.5 text-[11px] text-gray-500">
												Nur Regeltext
											</span>
										{:else}
											<span class="rounded bg-gray-100 px-1.5 py-0.5 font-mono text-[11px] text-gray-600">
												{s.code_language === 'javascript' ? 'JS' : 'PY'}
											</span>
											<span class="rounded bg-gray-100 px-1.5 py-0.5 text-[11px] text-gray-600">
												{skillFunctions(s).length}
												{skillFunctions(s).length === 1 ? 'Funktion' : 'Funktionen'}
											</span>

											{#if gatedFunctions(s).length > 0}
												<span
													class="rounded bg-amber-100 px-1.5 py-0.5 text-[11px] font-medium text-amber-800"
													title="Fragt vor der Ausführung im Chat nach: {gatedFunctions(s).map((f) => f.name).join(', ')}"
												>
													{gatedFunctions(s).length === skillFunctions(s).length
														? 'Freigabe nötig'
														: `${gatedFunctions(s).length}× Freigabe nötig`}
												</span>
											{:else}
												<span
													class="rounded bg-emerald-50 px-1.5 py-0.5 text-[11px] text-emerald-700"
													title="Läuft ohne Rückfrage"
												>
													läuft automatisch
												</span>
											{/if}

											{#if skillHosts(s).length > 0}
												<span
													class="rounded bg-sky-100 px-1.5 py-0.5 text-[11px] text-sky-800"
													title="Erreichbare Hosts: {skillHosts(s).join(', ')}"
												>
													Netz: {skillHosts(s).length}
													{skillHosts(s).length === 1 ? 'Host' : 'Hosts'}
												</span>
											{:else}
												<span class="rounded bg-gray-100 px-1.5 py-0.5 text-[11px] text-gray-500">
													kein Netz
												</span>
											{/if}
										{/if}

										{#if (s.variable_count ?? 0) > 0}
											<span
												class="rounded bg-gray-100 px-1.5 py-0.5 text-[11px] text-gray-600"
												title="Werte sind nur schreibbar, nie lesbar"
											>
												{s.variable_count}
												{s.variable_count === 1 ? 'Variable' : 'Variablen'}
											</span>
										{/if}
									</div>

									{#if skillFunctions(s).length > 0 && !s.expanded}
										<!-- Die Aufrufe selbst. Sie sind das, was das Modell in eine
										     Antwort schreiben darf — konkreter geht "was kann
										     passieren" nicht. -->
										<ul class="mt-1.5 space-y-0.5">
											{#each skillFunctions(s) as fn}
												<li class="flex items-baseline gap-1.5 text-[11.5px]">
													<code class="font-mono text-gray-700">
														{s.name}.{fn.signature || fn.name + '()'}
													</code>
													{#if fn.ask_for_permission}
														<span class="text-[10px] text-amber-700">fragt nach</span>
													{/if}
													{#if fn.description}
														<span class="truncate text-gray-400">— {fn.description}</span>
													{/if}
												</li>
											{/each}
										</ul>
									{/if}

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

										<!-- ── Plugin layer: code file + variables ── -->
										{@const plugin = pluginState[s.id]}
										<div class="mt-3 rounded-lg border border-gray-200 bg-gray-50/70 p-3">
											<div class="flex items-center justify-between">
												<h5 class="text-xs font-semibold text-gray-600">
													Ausführbare Funktionen
												</h5>
												{#if !plugin}
													<button
														onclick={() => loadPlugin(s.id)}
														class="text-xs text-blue-600 hover:underline"
													>
														Erneut laden
													</button>
												{:else if s.has_code}
													<span class="text-[11px] text-gray-400">
														{plugin.functions.length}
														{plugin.functions.length === 1 ? 'Funktion' : 'Funktionen'}
														· {s.code_language === 'javascript' ? 'JavaScript' : 'Python'}
													</span>
												{/if}
											</div>
											<p class="mt-0.5 text-[11px] text-gray-400">
												Eine Datei pro Skill (<code>.py</code> oder <code>.js</code>). Jede
												Funktion wird als <code>{s.name}.funktion(...)</code> aufrufbar.
												Markiere eine Funktion mit
												<code>#&nbsp;ask&nbsp;for&nbsp;permission</code> (bzw.
												<code>//&nbsp;ask&nbsp;for&nbsp;permission</code>), wenn vor der
												Ausführung im Chat nachgefragt werden soll.
											</p>

											{#if plugin?.loading}
												<p class="mt-2 animate-pulse text-xs text-gray-400">Laden …</p>
											{:else if plugin}
												<input
													type="text"
													bind:value={plugin.fileName}
													placeholder="Dateiname, z.B. wetter.py"
													class="mt-2 w-full rounded border border-gray-200 px-2.5 py-1 font-mono text-xs"
												/>
												<textarea
													bind:value={plugin.source}
													rows={10}
													spellcheck="false"
													placeholder={'# allow-network: api.open-meteo.com\ndef aktuell(ort):\n    ...'}
													class="mt-2 w-full rounded border border-gray-200 px-3 py-2 font-mono text-[11.5px] leading-relaxed text-gray-700 focus:border-blue-400 focus:outline-none focus:ring-1 focus:ring-blue-400"
												></textarea>

												{#if plugin.error}
													<p class="mt-1.5 whitespace-pre-wrap text-xs text-red-600">
														{plugin.error}
													</p>
												{/if}

												<div class="mt-2 flex items-center gap-2">
													<button
														onclick={() => savePluginCode(s.id)}
														disabled={plugin.saving || !plugin.fileName.trim() || !plugin.source.trim()}
														class="rounded bg-blue-600 px-2 py-1 text-xs font-medium text-white hover:bg-blue-700 disabled:opacity-40"
													>
														Code speichern
													</button>
													{#if s.has_code}
														<button
															onclick={() => removePluginCode(s.id)}
															class="text-xs text-red-500 hover:underline"
														>
															Code entfernen
														</button>
													{/if}
													{#if plugin.saved}
														<span class="text-xs text-green-600">Gespeichert!</span>
													{/if}
												</div>

												{#if plugin.functions.length > 0}
													<ul class="mt-2.5 space-y-2">
														{#each plugin.functions as fn}
															<li class="rounded border border-gray-200 bg-white px-2.5 py-2">
																<div class="flex flex-wrap items-center gap-1.5 text-[11.5px]">
																	<code class="font-mono text-gray-800">
																		{s.name}.{fn.signature}
																	</code>
																	{#if fn.allowed_domains.length > 0}
																		<span class="rounded bg-sky-100 px-1.5 py-0.5 text-sky-800">
																			Netz: {fn.allowed_domains.join(', ')}
																		</span>
																	{:else}
																		<span class="rounded bg-gray-100 px-1.5 py-0.5 text-gray-500">
																			kein Netz
																		</span>
																	{/if}
																	{#if fn.description}
																		<span class="text-gray-500">{fn.description}</span>
																	{/if}
																</div>

																<label class="mt-1.5 flex items-center gap-1.5 text-[11.5px] text-gray-700">
																	<input
																		type="checkbox"
																		checked={fn.ask_for_permission}
																		onchange={(e) =>
																			toggleFunctionPermission(
																				s,
																				fn,
																				(e.currentTarget as HTMLInputElement).checked
																			)}
																		class="rounded border-gray-300"
																	/>
																	Vor dem Ausführen im Chat nachfragen
																	{#if fn.permission_override !== null && fn.permission_override !== undefined && fn.permission_override !== fn.permission_from_code}
																		<span
																			class="rounded bg-amber-50 px-1.5 py-0.5 text-[10px] text-amber-800"
																			title="Im Quelltext steht {fn.permission_from_code
																				? '# ask for permission'
																				: 'keine Markierung'}"
																		>
																			weicht vom Code ab
																		</span>
																	{/if}
																</label>
															</li>
														{/each}
													</ul>
												{/if}

												<!-- Variables. Values are write-only by design. -->
												<div class="mt-3 border-t border-gray-200 pt-2.5">
													<div class="flex items-center justify-between">
														<h5 class="text-xs font-semibold text-gray-600">
															Skill-Variablen
														</h5>
														<button
															onclick={() => addVariableRow(s.id)}
															class="text-xs text-blue-600 hover:underline"
														>
															+ Variable
														</button>
													</div>
													<p class="mt-0.5 text-[11px] text-gray-400">
														Stehen der Funktion als <code>SKILL_VARS["name"]</code> und als
														Umgebungsvariable zur Verfügung. Werte werden nie zurück an den
														Browser gesendet — ein leeres Feld lässt den gespeicherten Wert
														unverändert.
													</p>

													{#each plugin.variables as variable, vIdx}
														<div class="mt-1.5 flex items-center gap-1.5">
															<input
																type="text"
																bind:value={variable.name}
																placeholder="name"
																class="w-40 rounded border border-gray-200 px-2 py-1 font-mono text-[11.5px]"
															/>
															<input
																type="password"
																bind:value={variable.value}
																placeholder={variable.isNew ? 'Wert' : `unverändert (${variable.preview})`}
																class="min-w-0 flex-1 rounded border border-gray-200 px-2 py-1 font-mono text-[11.5px]"
															/>
															<button
																onclick={() => removeVariableRow(s.id, vIdx)}
																class="text-xs text-red-500 hover:underline"
																aria-label="Variable entfernen"
															>
																&times;
															</button>
														</div>
													{/each}

													{#if plugin.variablesError}
														<p class="mt-1.5 text-xs text-red-600">{plugin.variablesError}</p>
													{/if}
													<div class="mt-2 flex items-center gap-2">
														<button
															onclick={() => savePluginVariables(s.id)}
															disabled={plugin.variablesSaving}
															class="rounded border border-gray-300 bg-white px-2 py-1 text-xs font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-40"
														>
															Variablen speichern
														</button>
														{#if plugin.variablesSaved}
															<span class="text-xs text-green-600">Gespeichert!</span>
														{/if}
													</div>
												</div>
											{/if}
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
