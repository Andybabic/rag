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

export async function uploadFile(file: File, useCase: string): Promise<UploadResult> {
	const form = new FormData();
	form.append('file', file);
	form.append('use_case', useCase);
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

/** (Re-)generate alt-text for a document's images that have none yet. */
export async function regenerateImages(
	useCase: string,
	fileHash: string
): Promise<{ status: string; pending?: number; total?: number }> {
	const resp = await fetch(
		`${BASE}/cleaning/image-regenerate/${encodeURIComponent(useCase)}/${encodeURIComponent(fileHash)}`,
		{ method: 'POST' }
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

export async function getConfig(
	useCase: string
): Promise<{ use_case: string; crypto_configured: boolean; config: UsecaseConfig }> {
	const resp = await fetch(`${BASE}/admin/config/${useCase}`);
	if (!resp.ok) throw new Error(await resp.text());
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
	if (!resp.ok) throw new Error(await resp.text());
	return resp.json();
}

export async function listModels(): Promise<{ models: string[]; error?: string }> {
	const resp = await fetch(`${BASE}/admin/models`);
	if (!resp.ok) throw new Error(await resp.text());
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
