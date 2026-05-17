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
	guidance?: string;
	phase?: 'started' | 'done' | 'error';
	detail?: string;
}

export interface Message {
	role: 'user' | 'assistant';
	text: string;
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
