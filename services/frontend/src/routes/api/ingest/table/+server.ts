import { json } from '@sveltejs/kit';
import { SERVICES } from '$lib/server/services';
import type { RequestHandler } from './$types';

interface InboundChunk {
	text: string;
	metadata?: Record<string, unknown>;
}

/**
 * Per-row table ingest. Skips the structure service (we already ship the
 * chunking the user requested) and pipes directly through:
 *   clean (file storage) -> embed -> upsert -> log
 */
export const POST: RequestHandler = async ({ request }) => {
	const formData = await request.formData();
	const file = formData.get('file') as File | null;
	const useCase = (formData.get('use_case') as string) || 'neumann';
	const anonymized = formData.get('anonymized') === '1';
	const rawChunks = formData.get('chunks');

	if (!file) {
		return json({ error: 'no_file', detail: 'No file provided' }, { status: 400 });
	}
	if (typeof rawChunks !== 'string') {
		return json(
			{ error: 'no_chunks', detail: 'chunks JSON missing' },
			{ status: 400 }
		);
	}

	let chunks: InboundChunk[];
	try {
		const parsed = JSON.parse(rawChunks);
		if (!Array.isArray(parsed)) throw new Error('chunks must be an array');
		chunks = parsed.filter(
			(c): c is InboundChunk =>
				c && typeof c.text === 'string' && c.text.length > 0
		);
	} catch (err) {
		return json(
			{ error: 'bad_chunks', detail: err instanceof Error ? err.message : 'invalid chunks' },
			{ status: 400 }
		);
	}

	if (chunks.length === 0) {
		return json({ status: 'ok', chunks: 0, message: 'No non-empty chunks' });
	}

	// 1. Store the file via cleaning service so it shows up in /documents and
	//    can be opened in the table preview later.
	const cleanForm = new FormData();
	cleanForm.append('file', file, file.name);
	cleanForm.append('config', JSON.stringify({ use_case: useCase }));

	const cleanResp = await fetch(`${SERVICES.cleaning}/v1/clean`, {
		method: 'POST',
		body: cleanForm
	});
	if (!cleanResp.ok) {
		return json(
			{ error: 'clean_failed', detail: await cleanResp.text() },
			{ status: 502 }
		);
	}
	const cleanData = await cleanResp.json();
	const fileHash = cleanData.metadata?.file_hash ?? '';
	const storedPath = cleanData.metadata?.stored_path ?? '';

	// 2. Embed the user-supplied chunks directly.
	const embedItems = chunks.map((c) => ({
		type: 'text',
		content: c.text,
		metadata: {}
	}));
	const embedResp = await fetch(`${SERVICES.embedding}/v1/embed/batch`, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ chunks: embedItems })
	});
	if (!embedResp.ok) {
		return json(
			{ error: 'embed_failed', detail: await embedResp.text() },
			{ status: 502 }
		);
	}
	const embedData = await embedResp.json();
	const embeddings = embedData.embeddings as Array<{
		chunk_id?: string;
		vector: number[];
		model?: string;
	}>;
	const embedModel = embeddings[0]?.model ?? null;

	// 3. Upsert into a single per-use-case collection. Use the same fallback
	//    naming as the standard pipeline.
	const collection = `${useCase}_default`;
	const upsertItems = chunks.map((chunk, i) => ({
		chunk_id: embeddings[i]?.chunk_id ?? '',
		vector: embeddings[i].vector,
		metadata: {
			text: chunk.text.slice(0, 500),
			file_hash: fileHash,
			stored_path: storedPath,
			anonymized,
			ingest_strategy: 'per-row',
			...(chunk.metadata ?? {})
		}
	}));

	const upsertResp = await fetch(`${SERVICES.vectordb}/v1/upsert`, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ collection, embeddings: upsertItems, embed_model: embedModel })
	});
	if (!upsertResp.ok) {
		return json(
			{ error: 'upsert_failed', detail: await upsertResp.text() },
			{ status: 502 }
		);
	}
	const upsertData = await upsertResp.json();
	const upserted = (upsertData.upserted ?? chunks.length) as number;

	// 4. Log
	try {
		await fetch(`${SERVICES.evaluation}/v1/ingest-log`, {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({
				file_name: file.name,
				file_hash: fileHash,
				stored_path: storedPath,
				collection,
				use_case: useCase,
				chunk_count: upserted
			})
		});
	} catch {
		// non-critical
	}

	return json({
		status: 'ok',
		file_name: file.name,
		use_case: useCase,
		strategy: 'per-row',
		chunks: upserted,
		collection,
		stored_path: storedPath
	});
};
