import { json } from '@sveltejs/kit';
import { SERVICES } from '$lib/server/services';
import { fetchError, responseError } from '$lib/server/ingest-errors';
import type { RequestHandler } from './$types';

export const POST: RequestHandler = async ({ request }) => {
	const formData = await request.formData();
	const file = formData.get('file') as File | null;
	const useCase = (formData.get('use_case') as string) || 'neumann';

	if (!file) {
		return json({ error: 'no_file', detail: 'No file provided' }, { status: 400 });
	}

	// 1. Clean (pass use_case so original file gets stored in the right folder)
	const cleanForm = new FormData();
	cleanForm.append('file', file, file.name);
	cleanForm.append('config', JSON.stringify({ use_case: useCase }));

	let cleanResp: Response;
	try {
		cleanResp = await fetch(`${SERVICES.cleaning}/v1/clean`, {
			method: 'POST',
			body: cleanForm
		});
	} catch (err) {
		const e = fetchError('Bereinigung/PDF-Extraktion', 'cleaning-service', err);
		return json({ error: e.error, detail: e.detail, stage: e.stage }, { status: e.status });
	}

	if (!cleanResp.ok) {
		const e = await responseError('Bereinigung/PDF-Extraktion', 'cleaning-service', cleanResp);
		return json({ error: e.error, detail: e.detail, stage: e.stage }, { status: e.status });
	}

	const cleanData = await cleanResp.json();
	const fileHash = cleanData.metadata?.file_hash ?? '';
	const storedPath = cleanData.metadata?.stored_path ?? '';

	// Strip the heavy base64 payload before passing images through — the
	// structuring service only needs the per-image metadata (id, page,
	// alt_text, url) to bind chunks to images.
	const rawImages = (cleanData.images ?? []) as Array<Record<string, unknown>>;
	const images = rawImages
		.filter((img) => typeof img.image_id === 'string')
		.map((img) => ({
			image_id: img.image_id,
			page: img.page,
			alt_text: img.alt_text ?? '',
			url: img.url ?? '',
			stored_path: img.stored_path ?? ''
		}));

	// 2. Structure
	let structureResp: Response;
	try {
		structureResp = await fetch(`${SERVICES.dataStructure}/v1/structure`, {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({
				markdown: cleanData.markdown,
				metadata: cleanData.metadata ?? {},
				use_case: useCase,
				images
			})
		});
	} catch (err) {
		const e = fetchError('Strukturierung/Chunking', 'data-structure-service', err);
		return json({ error: e.error, detail: e.detail, stage: e.stage }, { status: e.status });
	}

	if (!structureResp.ok) {
		const e = await responseError('Strukturierung/Chunking', 'data-structure-service', structureResp);
		return json({ error: e.error, detail: e.detail, stage: e.stage }, { status: e.status });
	}

	const structureData = await structureResp.json();
	const chunks = structureData.chunks as Array<{
		id?: string;
		text: string;
		metadata?: Record<string, unknown>;
	}>;

	if (!chunks.length) {
		return json({ status: 'ok', chunks: 0, message: 'No chunks produced' });
	}

	// Group chunks by target collection
	const fallbackCollection =
		structureData.routing?.collection ?? `${useCase}_default`;

	const byCollection = new Map<string, typeof chunks>();
	for (const chunk of chunks) {
		const col = (chunk.metadata?.collection as string) || fallbackCollection;
		if (!byCollection.has(col)) byCollection.set(col, []);
		byCollection.get(col)!.push(chunk);
	}

	let totalUpserted = 0;
	const collections: string[] = [];

	for (const [collection, colChunks] of byCollection) {
		collections.push(collection);

		// 3. Embed
		const embedItems = colChunks.map((c) => ({
			type: 'text',
			content: c.text,
			metadata: {}
		}));

		let embedResp: Response;
		try {
			embedResp = await fetch(`${SERVICES.embedding}/v1/embed/batch`, {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ chunks: embedItems, use_case: useCase })
			});
		} catch (err) {
			const e = fetchError('Embedding', 'embedding-service', err);
			return json({ error: e.error, detail: e.detail, stage: e.stage }, { status: e.status });
		}

		if (!embedResp.ok) {
			const e = await responseError('Embedding', 'embedding-service', embedResp);
			return json({ error: e.error, detail: e.detail, stage: e.stage }, { status: e.status });
		}

		const embedData = await embedResp.json();
		const embeddings = embedData.embeddings as Array<{
			chunk_id?: string;
			vector: number[];
			model?: string;
		}>;
		// Record which model produced these vectors so the index can later
		// reject queries embedded with a different model.
		const embedModel = embeddings[0]?.model ?? null;

		// 4. Upsert – include file_hash + stored_path in metadata for document linking
		const upsertItems = colChunks.map((chunk, i) => ({
			chunk_id: chunk.id ?? embeddings[i]?.chunk_id ?? '',
			vector: embeddings[i].vector,
			metadata: {
				text: chunk.text.slice(0, 500),
				file_hash: fileHash,
				stored_path: storedPath,
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
		totalUpserted += upsertData.upserted ?? 0;
	}

	// 5. Log to ingestion_log via evaluation service
	try {
		await fetch(`${SERVICES.evaluation}/v1/ingest-log`, {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({
				file_name: file.name,
				file_hash: fileHash,
				stored_path: storedPath,
				collection: collections.join(', '),
				use_case: useCase,
				chunk_count: totalUpserted
			})
		});
	} catch {
		// Non-critical – don't fail the ingest if logging fails
	}

	return json({
		status: 'ok',
		file_name: file.name,
		use_case: useCase,
		chunks: totalUpserted,
		stored_path: storedPath
	});
};
