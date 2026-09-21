const BASE = '/api';

export async function sendQuery(
	query: string,
	useCase: string,
	sessionId: string,
	role: string,
	config: Record<string, unknown> = {},
	history: Array<{ role: string; content: string }> = [],
	images: string[] = []
) {
	const resp = await fetch(`${BASE}/query`, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({
			query,
			use_case: useCase,
			session_id: sessionId,
			role,
			config,
			history,
			images
		})
	});
	if (!resp.ok) throw new Error(await resp.text());
	return resp.json();
}

export async function streamQuery(
	query: string,
	useCase: string,
	sessionId: string,
	role: string,
	config: Record<string, unknown> = {},
	history: Array<{ role: string; content: string }> = [],
	onEvent?: (event: Record<string, unknown>) => void,
	images: string[] = []
) {
	const resp = await fetch(`${BASE}/query/stream`, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({
			query,
			use_case: useCase,
			session_id: sessionId,
			role,
			config,
			history,
			images
		})
	});
	if (!resp.ok || !resp.body) throw new Error(await resp.text());

	const reader = resp.body.getReader();
	const decoder = new TextDecoder();
	let buffer = '';
	let final: Record<string, unknown> | null = null;

	while (true) {
		const { done, value } = await reader.read();
		if (done) break;
		buffer += decoder.decode(value, { stream: true });
		let nl: number;
		while ((nl = buffer.indexOf('\n')) >= 0) {
			const line = buffer.slice(0, nl).trim();
			buffer = buffer.slice(nl + 1);
			if (!line) continue;
			try {
				const event = JSON.parse(line);
				if (event.type === 'final') final = event;
				// Diagnostic metrics arrive after 'final': the server sends the
				// answer first and embeds the chunks afterwards, so the user is
				// not kept waiting for numbers only the Eval page reads. Merge
				// them into the final payload rather than handling them in the
				// caller — the caller applies `final` wholesale once the stream
				// ends, and would overwrite anything patched in before that.
				if (event.type === 'chunk_metrics' && final) applyChunkMetrics(final, event);
				if (event.type === 'error') throw new Error(String(event.detail ?? 'stream error'));
				onEvent?.(event);
			} catch (e) {
				if (e instanceof SyntaxError) continue;
				throw e;
			}
		}
	}
	return final;
}

/**
 * Write the post-response similarity metrics onto every chunk of `final`.
 *
 * Chunks are matched on the first 200 characters of their text, the same key
 * the server deduplicates them by. Position would be the obvious alternative
 * and the wrong one: the chunks live in several nested places at once
 * (per sub-agent step and in the global pool), and nothing guarantees the two
 * sides enumerate them alike.
 *
 * Without this the chat export loses the metrics, because the export is built
 * from client state while the Eval page reads them from the database.
 */
function applyChunkMetrics(final: Record<string, unknown>, event: Record<string, unknown>) {
	const byPrefix = event.by_text_prefix as Record<string, Record<string, number>> | undefined;
	if (!byPrefix) return;

	const patch = (chunks: unknown) => {
		if (!Array.isArray(chunks)) return;
		for (const chunk of chunks) {
			if (!chunk || typeof chunk !== 'object') continue;
			const text = (chunk as { text?: string }).text;
			if (typeof text !== 'string') continue;
			const metrics = byPrefix[text.trim().slice(0, 200)];
			if (metrics) Object.assign(chunk, metrics);
		}
	};

	for (const sub of (final.subagents as { agent_steps?: unknown[] }[] | undefined) ?? []) {
		for (const step of sub.agent_steps ?? []) {
			patch((step as { chunks?: unknown }).chunks);
		}
	}
	for (const step of (final.agent_steps as { chunks?: unknown }[] | undefined) ?? []) {
		patch(step.chunks);
	}
	patch(final.global_chunks);

	const audit = final.audit as Record<string, unknown> | undefined;
	if (audit) audit.post_response_ms = event.post_response_ms;
}

export async function sendFeedback(queryId: string, rating: string, comment = '') {
	const resp = await fetch(`${BASE}/feedback`, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ query_id: queryId, feedback: rating, comment })
	});
	if (!resp.ok) throw new Error(await resp.text());
	return resp.json();
}

export interface UploadResult {
	status: string;
	chunks?: number;
	file_hash?: string;
	image_count?: number;
	[k: string]: unknown;
}

export async function uploadFile(
	file: File,
	useCase: string,
	opts: { meta?: Record<string, unknown>; displayName?: string } = {}
): Promise<UploadResult> {
	const form = new FormData();
	form.append('file', file);
	form.append('use_case', useCase);
	// Document-level metadata copied into every chunk (e.g. an Einstellblatt's
	// product and machine) and the name shown in the document list.
	if (opts.meta) form.append('meta', JSON.stringify(opts.meta));
	if (opts.displayName) form.append('display_name', opts.displayName);
	const resp = await fetch(`${BASE}/ingest`, {
		method: 'POST',
		body: form
	});
	if (!resp.ok) throw new Error(await resp.text());
	return resp.json();
}

export interface ImageProgress {
	file_hash: string;
	total: number;
	done: number;
	failed: number;
	status: 'running' | 'done' | 'unknown';
}

/** Poll background alt-text (image interpretation) progress for a document. */
export async function getImageProgress(fileHash: string): Promise<ImageProgress> {
	const resp = await fetch(`${BASE}/cleaning/image-progress/${encodeURIComponent(fileHash)}`);
	return resp.json();
}

export interface ImageStatus {
	file_hash: string;
	total: number;
	described: number;
	pending: number;
	job_status: 'running' | 'done' | 'unknown';
}

/** Durable image-description status (from disk) for the documents overview. */
export async function getImageStatus(useCase: string, fileHash: string): Promise<ImageStatus> {
	const resp = await fetch(
		`${BASE}/cleaning/image-status/${encodeURIComponent(useCase)}/${encodeURIComponent(fileHash)}`
	);
	return resp.json();
}

/**
 * (Re-)generate alt-text for a document's images.
 *
 * Default: only images that have no description yet. With `force`, every image
 * is described again — what you want after the vision prompt changed, since the
 * existing texts are outdated rather than missing.
 *
 * Note this refreshes the gallery only. The descriptions the retrieval layer
 * uses live in the chunk payload, so making them searchable needs
 * `reindexDocument`.
 */
export async function regenerateImages(
	useCase: string,
	fileHash: string,
	force = false
): Promise<{ status: string; pending?: number; total?: number }> {
	const query = force ? '?force=true' : '';
	const resp = await fetch(
		`${BASE}/cleaning/image-regenerate/${encodeURIComponent(useCase)}/${encodeURIComponent(fileHash)}${query}`,
		{ method: 'POST' }
	);
	return resp.json();
}

export interface ReindexResult {
	status?: string;
	error?: string;
	detail?: string;
	chunks?: number;
	points_deleted?: number;
	image_count?: number;
	images_described?: number;
	images_complete?: boolean;
}

/**
 * Re-run the whole ingest for a stored document: re-extract, transcribe the
 * images again, re-chunk and replace the vectors.
 *
 * This is the action that makes improved image descriptions actually reachable
 * — chunk payloads are written once at ingest time and never updated in place.
 * Long-running: it waits for every image to be described before chunking.
 */
export async function reindexDocument(doc: {
	id: string;
	use_case: string;
	file_hash?: string;
	file_name: string;
	stored_path?: string;
	collection: string;
}): Promise<ReindexResult> {
	const resp = await fetch(
		`${BASE}/admin/documents/${encodeURIComponent(doc.id)}/reindex`,
		{
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({
				use_case: doc.use_case,
				file_hash: doc.file_hash,
				file_name: doc.file_name,
				stored_path: doc.stored_path,
				collection: doc.collection
			})
		}
	);
	return resp.json();
}

export interface FolderUploadResult {
	status: string;
	chunks: number;
	products?: Array<{
		product_id: string;
		cnc_files: number;
		operations: number;
		material_class: string | null;
		einstellblaetter: string[];
		images: number;
	}>;
	skipped_noncanonical?: string[];
}

/** Upload a whole product folder as a single ZIP (CNC use case). */
export async function uploadFolder(file: File, useCase: string): Promise<FolderUploadResult> {
	const form = new FormData();
	form.append('file', file);
	form.append('use_case', useCase);
	const resp = await fetch(`${BASE}/ingest/folder`, {
		method: 'POST',
		body: form
	});
	if (!resp.ok) throw new Error(await resp.text());
	return resp.json();
}

// ── GW St. Pölten: Datenbestand-Ordner ─────────────────────────

export interface GwPlanProduct {
	product_id: string;
	machine: string | null;
	category: string | null;
	cnc_candidates: string[];
	stueckliste: string | null;
	einstellblaetter: string[];
	/** Photos – ingested one by one like the Einstellblätter. */
	bilder: string[];
	/** CNC programs + Stückliste – the files for the product upload. */
	upload_paths: string[];
	images: number;
	ignored_documents: number;
	/** .doc/.xls and similar – listed so they can be followed up separately. */
	ignored_paths?: string[];
	variants: Array<{ path: string; role: string }>;
	warnings: string[];
	/** Set when the product is known on several machines: the user picks one. */
	machine_choices?: Array<{ machine: string | null; category: string | null; isNew?: boolean }>;
}

export interface GwPlan {
	products: GwPlanProduct[];
	skipped: Record<string, number>;
	unassigned: number;
}

/** Ask the server which products a picked folder holds and what to upload. */
export async function planGwFolder(paths: string[]): Promise<GwPlan> {
	const resp = await fetch(`${BASE}/ingest/gw/plan`, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ paths })
	});
	if (!resp.ok) throw new Error(await readErrorMessage(resp, 'Ordner konnte nicht geplant werden'));
	return resp.json();
}

export interface GwProductResult {
	status: string;
	product_id: string;
	machine: string | null;
	/** Points of the previous upload that were removed. */
	replaced: number;
	/** Old PDFs/photos removed from the document list with them. */
	documents_removed: number;
	chunks: number;
	product: {
		operations: number;
		material_class: string | null;
		warnings: string[];
		programs: Array<{ file: string; operations: number; dialect: string; role: string }>;
	} | null;
}

/** (Re)index one product on one machine – replaces what was there. */
export async function uploadGwProduct(body: {
	product_id: string;
	machine: string | null;
	category: string | null;
	files: Array<{ path: string; content_b64: string }>;
}): Promise<GwProductResult> {
	const resp = await fetch(`${BASE}/ingest/gw/product`, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(body)
	});
	if (!resp.ok) throw new Error(await readErrorMessage(resp, 'Produkt-Upload fehlgeschlagen'));
	return resp.json();
}

export async function getCollections() {
	const resp = await fetch(`${BASE}/collections`);
	return resp.json();
}

export async function getUseCaseCollections(useCase: string) {
	const resp = await fetch(`${BASE}/use-case/${useCase}/collections`);
	return resp.json();
}

// ── Admin API ────────────────────────────────────────────────

export async function getQueries(useCase: string, limit = 50) {
	const resp = await fetch(`${BASE}/admin/queries?use_case=${useCase}&limit=${limit}`);
	return resp.json();
}

export async function getDocuments(useCase: string) {
	const resp = await fetch(`${BASE}/admin/documents?use_case=${useCase}`);
	return resp.json();
}

export async function getDocumentImages(useCase: string, fileHash: string) {
	const resp = await fetch(
		`/api/documents/${encodeURIComponent(useCase)}/${encodeURIComponent(fileHash)}/images`
	);
	if (!resp.ok) return { images: [] };
	return resp.json();
}

export async function deleteDocument(docId: string) {
	const resp = await fetch(`/api/admin/documents/${encodeURIComponent(docId)}`, {
		method: 'DELETE'
	});
	if (!resp.ok) {
		let detail = `HTTP ${resp.status}`;
		try {
			const body = await resp.json();
			detail = body.detail ?? body.error ?? detail;
		} catch {
			/* keep default */
		}
		throw new Error(detail);
	}
	return resp.json();
}

export async function getMemory(useCase: string) {
	const resp = await fetch(`${BASE}/admin/memory?use_case=${useCase}`);
	return resp.json();
}

export async function updateMemory(useCase: string, memoryText: string) {
	const resp = await fetch(`${BASE}/admin/memory`, {
		method: 'PUT',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ use_case: useCase, memory_text: memoryText })
	});
	return resp.json();
}

export async function getPrompt(useCase: string, role = 'default') {
	const resp = await fetch(`${BASE}/admin/prompts?use_case=${useCase}&role=${role}`);
	return resp.json();
}

export async function updatePrompt(useCase: string, prompt: string, role = 'default') {
	const resp = await fetch(`${BASE}/admin/prompts`, {
		method: 'PUT',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ use_case: useCase, role, prompt })
	});
	return resp.json();
}

// ── Per-usecase Config + Multi-Prompt API ──────────────────

export interface UsecaseConfig {
	chat_provider: string;
	embedding_provider: string;
	vision_provider: string;
	ollama_base_url: string;
	openai_base_url: string;
	ollama_api_key_set: boolean;
	ollama_api_key_preview: string;
	openai_api_key_set: boolean;
	openai_api_key_preview: string;
	llm_model: string | null;
	embedding_model: string | null;
	vision_model: string | null;
	embedding_dimension: number | null;
	temperature: number | null;
	max_tokens: number | null;
	embed_batch_size: number | null;
	agent_max_steps: number | null;
	memory_max_chars: number | null;
}

async function readErrorMessage(resp: Response, fallback = 'Anfrage fehlgeschlagen'): Promise<string> {
	try {
		const body = await resp.json();
		const detail = body.message ?? body.detail ?? body.error;
		if (typeof detail === 'string' && detail.trim()) return detail;
		if (Array.isArray(detail)) {
			return detail
				.map((d) => (typeof d === 'string' ? d : d.msg ?? JSON.stringify(d)))
				.join('; ');
		}
	} catch {
		/* not JSON */
	}
	return `${fallback} (HTTP ${resp.status})`;
}

export async function getConfig(
	useCase: string
): Promise<{ use_case: string; crypto_configured: boolean; config: UsecaseConfig }> {
	const resp = await fetch(`${BASE}/admin/config/${useCase}`);
	if (!resp.ok) throw new Error(await readErrorMessage(resp, 'Konfiguration konnte nicht geladen werden'));
	return resp.json();
}

export async function updateConfig(
	useCase: string,
	patch: Partial<Record<string, string | number | null>>
) {
	const resp = await fetch(`${BASE}/admin/config/${useCase}`, {
		method: 'PUT',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(patch)
	});
	if (!resp.ok) throw new Error(await readErrorMessage(resp, 'Konfiguration konnte nicht gespeichert werden'));
	return resp.json();
}

export interface ProviderCheck {
	role: 'chat' | 'vision';
	ok: boolean;
	skipped: boolean;
	provider: string;
	base_url: string;
	model: string | null;
	models: string[];
	latency_ms: number;
	code: string;
	message: string;
	hint: string;
	detail: string;
}

export interface ConfigTestResult {
	ok: boolean;
	use_case?: string;
	checks: ProviderCheck[];
	error?: string;
	detail?: string;
	message?: string;
}

export async function testConfig(
	useCase: string,
	patch: Partial<Record<string, string | null>> = {}
): Promise<ConfigTestResult> {
	const resp = await fetch(`${BASE}/admin/config/${useCase}/test`, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(patch)
	});
	if (!resp.ok) throw new Error(await readErrorMessage(resp, 'Verbindungstest fehlgeschlagen'));
	return resp.json();
}

export async function listModels(
	useCase = ''
): Promise<{ models: string[]; error?: string; message?: string; hint?: string }> {
	const qs = useCase ? `?use_case=${encodeURIComponent(useCase)}` : '';
	const resp = await fetch(`${BASE}/admin/models${qs}`);
	if (!resp.ok) throw new Error(await readErrorMessage(resp, 'Modellliste konnte nicht geladen werden'));
	return resp.json();
}

export interface PromptKeyEntry {
	key: string;
	scope: string;
	label: string;
	value: string;
	is_override: boolean;
}

export async function listPromptKeys(
	useCase: string
): Promise<{ use_case: string; prompts: PromptKeyEntry[] }> {
	const resp = await fetch(`${BASE}/admin/prompt-keys/${useCase}`);
	if (!resp.ok) throw new Error(await resp.text());
	return resp.json();
}

export async function updatePromptKey(scope: string, key: string, content: string) {
	const resp = await fetch(`${BASE}/admin/prompt-keys`, {
		method: 'PUT',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ scope, key, content })
	});
	if (!resp.ok) throw new Error(await resp.text());
	return resp.json();
}

// ── Skills / Regeln API ────────────────────────────────────

export interface Skill {
	id: string;
	use_case: string;
	name: string;
	overview: string;
	detailed_task: string;
	enabled: boolean;
	position: number;
	/** Plugin layer. The source itself is fetched separately via getSkillCode();
	 *  variable *values* are never part of a skill listing. */
	code_language?: 'python' | 'javascript' | null;
	code_file_name?: string | null;
	has_code?: boolean;
	/** How many variables are stored — never which, never their values. */
	variable_count?: number;
	/** Function table, delivered with the listing so the overview needs no
	 *  extra request per skill. */
	functions?: SkillFunctionDef[];
}

export async function listSkills(useCase: string): Promise<{ use_case: string; skills: Skill[] }> {
	const resp = await fetch(`${BASE}/admin/skills/${useCase}`);
	if (!resp.ok) throw new Error(await resp.text());
	return resp.json();
}

export async function createSkill(
	useCase: string,
	skill: { name: string; overview: string; detailed_task: string; enabled?: boolean; position?: number }
) {
	const resp = await fetch(`${BASE}/admin/skills/${useCase}`, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(skill)
	});
	if (!resp.ok) throw new Error(await resp.text());
	return resp.json();
}

export async function updateSkill(
	id: string,
	patch: Partial<Pick<Skill, 'name' | 'overview' | 'detailed_task' | 'enabled' | 'position'>>
) {
	const resp = await fetch(`${BASE}/admin/skills/item/${id}`, {
		method: 'PUT',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(patch)
	});
	if (!resp.ok) throw new Error(await resp.text());
	return resp.json();
}

export async function deleteSkill(id: string) {
	const resp = await fetch(`${BASE}/admin/skills/item/${id}`, { method: 'DELETE' });
	if (!resp.ok) throw new Error(await resp.text());
	return resp.json();
}

// ── Skill-Funktionen (Plugin-Layer) ─────────────────────────

export interface SkillFunctionDef {
	id: string;
	name: string;
	signature: string;
	description: string;
	params: string[];
	/** Effective flag: the admin's choice if set, otherwise the source file's. */
	ask_for_permission: boolean;
	/** What the `ask for permission` marker in the code says. */
	permission_from_code?: boolean;
	/** The admin's explicit choice, or null when the code decides. */
	permission_override?: boolean | null;
	allowed_domains: string[];
	timeout_ms: number;
	memory_mb: number;
}

export interface SkillCode {
	language: 'python' | 'javascript' | null;
	file_name: string | null;
	source: string;
	functions: SkillFunctionDef[];
}

/** Read a skill's code file and the function table derived from it. */
export async function getSkillCode(id: string): Promise<SkillCode> {
	const resp = await fetch(`${BASE}/admin/skills/item/${id}/code`);
	if (!resp.ok) throw new Error(await resp.text());
	return resp.json();
}

/**
 * Save a skill's code file. The server parses it and rewrites the function
 * table, so the response carries the functions it actually found — including
 * which of them will ask for permission.
 */
export async function saveSkillCode(
	id: string,
	body: { file_name: string; source: string }
) {
	const resp = await fetch(`${BASE}/admin/skills/item/${id}/code`, {
		method: 'PUT',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(body)
	});
	return resp.json();
}

export async function deleteSkillCode(id: string) {
	const resp = await fetch(`${BASE}/admin/skills/item/${id}/code`, { method: 'DELETE' });
	if (!resp.ok) throw new Error(await resp.text());
	return resp.json();
}

/**
 * Skill variables. Values are never sent to the browser — the listing returns
 * names and a masked preview only, which is what keeps an API key out of the
 * skill UI. Saving replaces the whole object.
 */
export async function getSkillVariables(
	id: string
): Promise<{ variables: Array<{ name: string; preview: string }> }> {
	const resp = await fetch(`${BASE}/admin/skills/item/${id}/variables`);
	if (!resp.ok) throw new Error(await resp.text());
	return resp.json();
}

export async function saveSkillVariables(
	id: string,
	variables: Record<string, string | null>
) {
	const resp = await fetch(`${BASE}/admin/skills/item/${id}/variables`, {
		method: 'PUT',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ variables })
	});
	return resp.json();
}

export interface SkillImportEntry {
	name: string;
	language?: string | null;
	functions: string[];
	needs_permission: string[];
	reaches_hosts: string[];
	missing_variables: string[];
}

export interface SkillImportResult {
	status?: string;
	error?: string;
	detail?: string;
	created: SkillImportEntry[];
	updated: SkillImportEntry[];
	skipped: string[];
	failed: Array<{ name: string; error: string }>;
}

/**
 * Export skills. Pass `skillId` for a single skill — same document shape, so
 * both import through the same path. Variable values are never included.
 */
export async function exportSkills(useCase: string, skillId?: string) {
	const query = skillId ? `?skill_id=${encodeURIComponent(skillId)}` : '';
	const resp = await fetch(`${BASE}/admin/skills/${useCase}/export${query}`);
	if (!resp.ok) throw new Error(await resp.text());
	return resp.json();
}

/**
 * Import skills from an exported document.
 *
 * The result reports what actually arrived — which functions exist now, which
 * ask before running and which hosts they may reach. Importing a skill means
 * importing executable code, so that is worth reading.
 */
export async function importSkills(
	useCase: string,
	doc: { version?: number; skills: unknown[] },
	overwrite: boolean
): Promise<SkillImportResult> {
	const resp = await fetch(`${BASE}/admin/skills/${useCase}/import`, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ version: doc.version ?? 1, skills: doc.skills, overwrite })
	});
	return resp.json();
}

/**
 * Turn the consent prompt for one function on or off.
 *
 * `null` hands the decision back to the `ask for permission` marker in the
 * source. Stored separately from the parsed flag, so saving the code file
 * again does not undo it.
 */
export async function setFunctionPermission(
	skillId: string,
	functionName: string,
	ask: boolean | null
) {
	const resp = await fetch(
		`${BASE}/admin/skills/item/${skillId}/functions/${encodeURIComponent(functionName)}/permission`,
		{
			method: 'PUT',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ ask })
		}
	);
	return resp.json();
}

export async function getSkillExecutions(useCase: string, limit = 50) {
	const resp = await fetch(`${BASE}/admin/skills/${useCase}/executions?limit=${limit}`);
	if (!resp.ok) throw new Error(await resp.text());
	return resp.json();
}

/**
 * Answer a pending "may this function run?" prompt.
 *
 * The stream that asked is still open and waiting on this, so it is sent as
 * its own short request rather than being folded into the stream body.
 * A 404 means the request already expired — the function did not run.
 */
export async function respondToPermission(
	callId: string,
	approved: boolean,
	sessionId: string
): Promise<{ status?: string; error?: string; detail?: string }> {
	const resp = await fetch(`${BASE}/permission/${callId}`, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ approved, session_id: sessionId })
	});
	return resp.json();
}

// ── Agent Actions API ───────────────────────────────────────

export async function getActions(useCase: string) {
	const resp = await fetch(`${BASE}/admin/actions/${useCase}`);
	return resp.json();
}

export async function updateAction(
	useCase: string,
	actionName: string,
	update: { enabled?: boolean; description?: string }
) {
	const resp = await fetch(`${BASE}/admin/actions/${useCase}/${actionName}`, {
		method: 'PUT',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(update)
	});
	return resp.json();
}

// ── Metrics Dashboard ──────────────────────────────────────

export interface MetricsQuery {
	id: string;
	use_case: string;
	session_id: string | null;
	role: string;
	query_text: string;
	answer_text: string;
	answer_length: number;
	step_count: number;
	subtask_count: number;
	chunk_count: number;
	citation_count: number;
	sufficient: boolean;
	agent_steps: Record<string, unknown>[];
	processing_ms: number | null;
	compliance_verdict: string | null;
	merge_strategy: string | null;
	model: string | null;
	llm_provider: string | null;
	total_tokens: number | null;
	retrieval_quality: number | null;
	created_at: string | null;
}

export interface MetricsSummary {
	total: number;
	avg_steps: number;
	avg_chunks: number;
	avg_answer_length: number;
	sufficient_count: number;
	sufficient_pct: number;
	avg_processing_ms: number | null;
	avg_total_tokens: number | null;
	avg_retrieval_quality: number | null;
}

export interface MetricsResponse {
	queries: MetricsQuery[];
	summary: MetricsSummary;
}

export async function getMetrics(useCase: string): Promise<MetricsResponse> {
	const resp = await fetch(`${BASE}/admin/metrics?use_case=${useCase}`);
	if (!resp.ok) throw new Error(await resp.text());
	return resp.json();
}
