import { browser } from '$app/environment';

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
}

export interface Message {
	role: 'user' | 'assistant';
	text: string;
	citations?: Citation[];
	agentSteps?: AgentStep[];
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

export type Tab = 'chat' | 'documents' | 'history' | 'settings';

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
	collectionsLoading: false
});

export function initSession() {
	if (!app.sessionId) {
		app.sessionId = getOrCreateSessionId();
	}
}
