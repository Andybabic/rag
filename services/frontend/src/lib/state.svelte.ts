import { browser } from '$app/environment';
import type { ParsedTable } from './file-parser';
import type { PiiDetection } from './pii-scanner';
import { PlaceholderMap } from './pii-anonymizer';

export interface Citation {
	ref: string;
	file_name?: string;
	page?: number;
	excerpt?: string;
	score?: number;
	stored_path?: string;
}

export interface RetrievedChunk {
	text?: string;
	score?: number;
	chunk_id?: string;
	original_score?: number;
	rerank_score?: number;
	relevance_label?: string;
	metadata?: Record<string, unknown>;
}

export interface AgentStep {
	step: number;
	action: string;
	thought?: string;
	observation?: string;
	args?: Record<string, unknown>;
	llm_response?: string;
	chunks?: RetrievedChunk[];
	subagent_id?: string;
	subagent_role?: string;
	duration_ms?: number;
	llm_ms?: number;
	action_ms?: number;
	embed_ms?: number;
	search_ms?: number;
	rerank_ms?: number;
	llm_timing?: {
		load_ms?: number;
		pp_ms?: number;
		tp_ms?: number;
		total_ms?: number;
		wall_ms?: number;
		prompt_tokens?: number;
		completion_tokens?: number;
	};
}

export interface ManagerSubtask {
	role: string;
	sub_query: string;
	focus?: string;
}

export interface ManagerPlan {
	rationale?: string;
	merge_strategy?: 'complementary' | 'comparative' | 'fallback' | string;
	subtasks: ManagerSubtask[];
}

export interface SubAgentTrace {
	subagent_id: string;
	role: string;
	role_label: string;
	sub_query: string;
	focus?: string;
	answer: string;
	agent_steps: AgentStep[];
	chunks?: RetrievedChunk[];
	sufficient?: boolean;
	searched_collections?: string[];
	error?: string | null;
	status?: 'pending' | 'running' | 'done' | 'error';
	duration_ms?: number;
}

export interface SynthesizerTrace {
	phase: 'started' | 'done' | 'skipped';
	merge_strategy?: string;
	fragment_count?: number;
	global_chunk_count?: number;
	answer_length?: number;
	reason?: string;
}

export interface ComplianceTrace {
	verdict?: 'OK' | 'REWRITE' | 'REFUSE';
	issues?: string[];
	classified_issues?: Array<{
		text: string;
		category: string;
		category_label: string;
		confidence: number;
		nli_label: string;
	}>;
	guidance?: string;
	phase?: 'started' | 'done' | 'error';
	detail?: string;
}

export interface TimingPhase {
	id: string;
	label: string;
	ms: number;
	llm_ms?: number | null;
	children?: TimingPhase[];
}

export interface TimingReport {
	total_ms: number;
	phases: TimingPhase[];
	bottleneck?: { id: string; label: string; ms: number; share_pct: number } | null;
}

export interface AuditInfo {
	model?: string | null;
	llm_provider?: string;
	temperature?: number | null;
	max_tokens?: number | null;
	generated_at?: string;
	processing_ms?: number;
	timing?: TimingReport;
	step_count?: number;
	subtask_count?: number;
	chunk_count?: number;
	citation_count?: number;
	answer_length?: number;
	compliance_verdict?: string;
	merge_strategy?: string;
	sufficient?: boolean;
	searched_collections_count?: number;
}

export interface ImageRef {
	id: string;
	page?: number | null;
	alt_text?: string;
	url?: string;
}

export interface Message {
	role: 'user' | 'assistant';
	text: string;
	/** Data-URI images attached to a user message (sent to the vision model). */
	images?: string[];
	/** ISO timestamp set client-side when the message is created. */
	createdAt?: string;
	/** Client-perceived round-trip duration for the assistant reply, ms. */
	durationMs?: number;
	/** Server-reported model/provider/timing audit block. */
	audit?: AuditInfo;
	timing?: TimingReport;
	citations?: Citation[];
	agentSteps?: AgentStep[];
	managerPlan?: ManagerPlan;
	subAgents?: SubAgentTrace[];
	synthesizer?: SynthesizerTrace;
	compliance?: ComplianceTrace;
	searchedCollections?: string[];
	systemPrompt?: string;
	enrichedQuery?: string;
	requestId?: string;
	sufficient?: boolean;
	error?: boolean;
	streaming?: boolean;
	currentPhase?: string;
	/** Images the answer explicitly cited via [BILD: <id>] markers — only
	 * those that match a persisted image are forwarded by the server, so
	 * everything here is renderable. */
	imagesUsed?: ImageRef[];
}

export interface SubCollection {
	name: string;
	count: number;
	dimension: number;
}

export type Tab = 'chat' | 'tabelle' | 'documents' | 'history' | 'settings';

export type AnonymizeMode = 'off' | 'auto' | 'manual';

export type PiiScanStatus = 'idle' | 'loading-model' | 'scanning' | 'done' | 'error';

export type ReleaseStatus = 'idle' | 'released' | 'importing' | 'imported' | 'error';

export type ChunkingStrategy = 'per-row' | 'whole-file';

export interface ReleaseSnapshot {
	fileName: string;
	sourceType: 'csv' | 'xlsx' | 'xls';
	headers: string[];
	rows: string[][];
	rowCount: number;
	columnCount: number;
	anonymized: boolean;
	anonymizeMode: AnonymizeMode;
	detectionTotal: number;
	detectionApproved: number;
	chunkingStrategy: ChunkingStrategy;
	releasedAt: string;
}

export interface ReleaseState {
	status: ReleaseStatus;
	snapshot: ReleaseSnapshot | null;
	importedAt: string | null;
	importResult: { chunks?: number; storedPath?: string } | null;
	error: string | null;
}

export interface AuditTrail {
	fileLoadedAt: string | null;
	scanStartedAt: string | null;
	scanCompletedAt: string | null;
}

export function freshReleaseState(): ReleaseState {
	return {
		status: 'idle',
		snapshot: null,
		importedAt: null,
		importResult: null,
		error: null
	};
}

export function freshAuditTrail(): AuditTrail {
	return { fileLoadedAt: null, scanStartedAt: null, scanCompletedAt: null };
}

export interface PiiScanState {
	status: PiiScanStatus;
	progress: number;
	total: number;
	error: string | null;
	detections: PiiDetection[];
	approvals: Set<string>;
	placeholders: PlaceholderMap;
}

export function freshPiiState(): PiiScanState {
	return {
		status: 'idle',
		progress: 0,
		total: 0,
		error: null,
		detections: [],
		approvals: new Set<string>(),
		placeholders: new PlaceholderMap()
	};
}

function getOrCreateSessionId(): string {
	if (!browser) return '';
	let id = sessionStorage.getItem('rag_session_id');
	if (!id) {
		id = crypto.randomUUID();
		sessionStorage.setItem('rag_session_id', id);
	}
	return id;
}

export const app = $state({
	useCase: 'neumann' as string,
	role: 'default' as string,
	sessionId: '' as string,
	messages: [] as Message[],
	isLoading: false,
	uploadStatus: null as { ok: boolean; msg: string } | null,
	uploadResults: [] as Array<{ name: string; ok: boolean; msg: string; pending?: boolean }>,
	activeTab: 'chat' as Tab,
	availableCollections: [] as SubCollection[],
	collectionsLoading: false,
	parsedTable: null as ParsedTable | null,
	tableLoading: false,
	tableError: null as string | null,
	pii: freshPiiState() as PiiScanState,
	anonymizeMode: 'off' as AnonymizeMode,
	chunkingStrategy: 'per-row' as ChunkingStrategy,
	release: freshReleaseState() as ReleaseState,
	audit: freshAuditTrail() as AuditTrail
});

export function initSession() {
	if (!app.sessionId) {
		app.sessionId = getOrCreateSessionId();
	}
}

/** Mint a brand-new session id (also persisted for the browser tab). */
function freshSessionId(): string {
	if (!browser) return '';
	const id = crypto.randomUUID();
	sessionStorage.setItem('rag_session_id', id);
	return id;
}

/**
 * Start a fresh conversation: clear the transcript and begin a new session so
 * the next messages are logged under their own session id. Called when the
 * user switches use case — the chat must not carry over from another use case.
 */
export function resetChat() {
	app.messages = [];
	app.isLoading = false;
	app.sessionId = freshSessionId();
}
