import { json } from '@sveltejs/kit';
import { SERVICES } from '$lib/server/services';
import type { RequestHandler } from './$types';

/**
 * Folder/ZIP ingest for the CNC use case (GW St. Pölten).
 *
 * A whole product folder is uploaded as a single ZIP. The data-structure
 * service unpacks it, groups files per product, parses every CNC program
 * into one chunk per tool operation and weaves in the material class from
 * the Stückliste. We then embed + upsert the returned chunks per
 * collection — the same back half as the single-file ingest.
 *
 * Einstellblatt PDFs are detected and reported by the structure service but
 * NOT chunked here (they need the cleaning service); ingest those through
 * the regular /api/ingest path with use_case=gw_stpoelten.
 */
export const POST: RequestHandler = async ({ request }) => {
	const formData = await request.formData();
	const file = formData.get('file') as File | null;
	const useCase = (formData.get('use_case') as string) || 'gw_stpoelten';

	if (!file) {
		return json({ error: 'no_file', detail: 'No ZIP file provided' }, { status: 400 });
	}

	// 1. Structure the whole folder (raw ZIP body → CNC + material chunks)
	const zipBytes = await file.arrayBuffer();
	const structureResp = await fetch(`${SERVICES.dataStructure}/v1/structure/folder`, {
		method: 'POST',
		headers: { 'Content-Type': 'application/zip' },
		body: zipBytes
	});

	if (!structureResp.ok) {
		return json(
			{ error: 'structure_failed', detail: await structureResp.text() },
			{ status: 502 }
		);
	}

	const structureData = await structureResp.json();
	const chunks = (structureData.chunks ?? []) as Array<{
		id?: string;
		text: string;
		metadata?: Record<string, unknown> & { collection?: string };
	}>;

	if (!chunks.length) {
		return json({
			status: 'ok',
			chunks: 0,
			products: structureData.products ?? [],
			message: 'No chunks produced'
		});
	}

	// 2. Group chunks by target collection
	const byCollection = new Map<string, typeof chunks>();
	for (const chunk of chunks) {
		const col = (chunk.metadata?.collection as string) || `${useCase}_default`;
		if (!byCollection.has(col)) byCollection.set(col, []);
		byCollection.get(col)!.push(chunk);
	}

	let totalUpserted = 0;
	const collections: string[] = [];

	for (const [collection, colChunks] of byCollection) {
		collections.push(collection);

		// 3. Embed
		const embedItems = colChunks.map((c) => ({ type: 'text', content: c.text, metadata: {} }));
		const embedResp = await fetch(`${SERVICES.embedding}/v1/embed/batch`, {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ chunks: embedItems, use_case: useCase })
		});
		if (!embedResp.ok) {
			return json({ error: 'embed_failed', detail: await embedResp.text() }, { status: 502 });
		}
		const embedData = await embedResp.json();
		const embeddings = embedData.embeddings as Array<{
			chunk_id?: string;
			vector: number[];
			model?: string;
		}>;
		const embedModel = embeddings[0]?.model ?? null;

		// 4. Upsert – keep the full operation metadata (product_id,
		//    operation_type, tool_type, material_class, …) in the payload.
		const upsertItems = colChunks.map((chunk, i) => ({
			chunk_id: chunk.id ?? embeddings[i]?.chunk_id ?? '',
			vector: embeddings[i].vector,
			metadata: {
				text: chunk.text.slice(0, 500),
				...(chunk.metadata ?? {})
			}
		}));

		const upsertResp = await fetch(`${SERVICES.vectordb}/v1/upsert`, {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ collection, embeddings: upsertItems, embed_model: embedModel })
		});
		if (!upsertResp.ok) {
			return json({ error: 'upsert_failed', detail: await upsertResp.text() }, { status: 502 });
		}
		const upsertData = await upsertResp.json();
		totalUpserted += upsertData.upserted ?? 0;
	}

	// 5. Log to ingestion_log (non-critical)
	try {
		await fetch(`${SERVICES.evaluation}/v1/ingest-log`, {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({
				file_name: file.name,
				file_hash: '',
				stored_path: '',
				collection: collections.join(', '),
				use_case: useCase,
				chunk_count: totalUpserted
			})
		});
	} catch {
		// ignore logging failures
	}

	return json({
		status: 'ok',
		file_name: file.name,
		use_case: useCase,
		products: structureData.products ?? [],
		skipped_noncanonical: structureData.skipped_noncanonical ?? [],
		collections,
		chunks: totalUpserted
	});
};
