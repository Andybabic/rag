const BASE = '/api';

export async function sendQuery(
	query: string,
	useCase: string,
	sessionId: string,
	role: string,
	config: Record<string, unknown> = {},
	history: Array<{ role: string; content: string }> = []
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
			history
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
	onEvent?: (event: Record<string, unknown>) => void
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
			history
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

export async function sendFeedback(queryId: string, rating: string) {
	const resp = await fetch(`${BASE}/feedback`, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ query_id: queryId, feedback: rating })
	});
	return resp.json();
}

export async function uploadFile(file: File, useCase: string) {
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
