/**
 * The shared back half of every ingest path.
 *
 *   clean → (wait for image descriptions) → structure → embed → upsert → log
 *
 * `/api/ingest`, the folder/ZIP ingest and the GW product upload all need the
 * embed/upsert/log tail; documents additionally need the clean/structure head.
 * Having it once means a fix to a stage (timeouts, payload shape, error
 * wording) reaches every entry point instead of whichever one was edited.
 *
 * Failures are thrown as `PipelineError` carrying the user-facing stage error
 * from `ingest-errors`; routes turn it into a JSON response.
 */
import { SERVICES } from '$lib/server/services';
import { fetchError, responseError } from '$lib/server/ingest-errors';
import { waitForImageDescriptions } from '$lib/server/image-descriptions';

export interface StageFailure {
	error: string;
	detail: string;
	stage?: string;
	status: number;
}

export class PipelineError extends Error {
	constructor(readonly failure: StageFailure) {
		super(failure.detail);
	}
}

export interface IndexChunk {
	id?: string;
	text: string;
	metadata?: Record<string, unknown>;
}

/** fetch() that turns network failures and non-2xx answers into PipelineError. */
export async function callStage(
	stage: string,
	service: string,
	url: string | URL,
	init: RequestInit
): Promise<Response> {
	let resp: Response;
	try {
		resp = await fetch(url, init);
	} catch (err) {
		throw new PipelineError(fetchError(stage, service, err));
	}
	if (!resp.ok) throw new PipelineError(await responseError(stage, service, resp));
	return resp;
}

/** Turn a caught PipelineError into the JSON error body the UI expects. */
export function failureBody(err: PipelineError) {
	const { error, detail, stage } = err.failure;
	return { error, detail, stage };
}

/**
 * Embed chunks and upsert them, grouped by their target collection.
 *
 * `payload` is merged into every point *before* the chunk's own metadata, so
 * the chunk cannot be overridden by it. It is how a point gets the flat
 * `file_hash` the document list deletes by.
 */
export async function embedAndUpsert(
	chunks: IndexChunk[],
	useCase: string,
	{
		fallbackCollection,
		payload = {}
	}: { fallbackCollection: string; payload?: Record<string, unknown> }
): Promise<{ upserted: number; collections: string[] }> {
	const byCollection = new Map<string, IndexChunk[]>();
	for (const chunk of chunks) {
		const col = (chunk.metadata?.collection as string) || fallbackCollection;
		if (!byCollection.has(col)) byCollection.set(col, []);
		byCollection.get(col)!.push(chunk);
	}

	let upserted = 0;
	const collections: string[] = [];
	for (const [collection, colChunks] of byCollection) {
		collections.push(collection);

		const embedResp = await callStage(
			'Embedding',
			'embedding-service',
			`${SERVICES.embedding}/v1/embed/batch`,
			{
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({
					chunks: colChunks.map((c) => ({ type: 'text', content: c.text, metadata: {} })),
					use_case: useCase
				})
			}
		);
		const embeddings = (await embedResp.json()).embeddings as Array<{
			chunk_id?: string;
			vector: number[];
			model?: string;
		}>;
		// Record which model produced these vectors so the index can later
		// reject queries embedded with a different model.
		const embedModel = embeddings[0]?.model ?? null;

		const upsertResp = await callStage(
			'Speichern',
			'vectordb-service',
			`${SERVICES.vectordb}/v1/upsert`,
			{
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({
					collection,
					embed_model: embedModel,
					embeddings: colChunks.map((chunk, i) => ({
						chunk_id: chunk.id ?? embeddings[i]?.chunk_id ?? '',
						vector: embeddings[i].vector,
						metadata: {
							text: chunk.text.slice(0, 500),
							...payload,
							...(chunk.metadata ?? {})
						}
					}))
				})
			}
		);
		upserted += Number((await upsertResp.json()).upserted ?? 0);
	}
	return { upserted, collections };
}

/** Document-level keys that are set at ingest time and must survive a re-ingest. */
const DOCUMENT_META_KEYS = [
	'doc_kind',
	'product_id',
	'machine',
	'category',
	// Neumann bulk upload (scripts/bulk_ingest.py): collection and folder.
	'machine_id',
	'source_path'
] as const;

/**
 * Read the document-level metadata of an ingested file back from the index.
 *
 * An Einstellblatt's link to its product and machine exists only in its chunk
 * payloads. A re-ingest deletes those first, so this has to run before – or
 * the document comes back unlinked and the GW plugin's heuristic may even
 * route it into the CNC collection. `null` when nothing was recorded.
 */
export async function readDocumentMeta(
	collections: string[],
	fileHash: string
): Promise<Record<string, unknown> | null> {
	for (const collection of collections) {
		try {
			const resp = await fetch(`${SERVICES.vectordb}/v1/scroll`, {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ collection, filters: { file_hash: fileHash }, limit: 1 })
			});
			if (!resp.ok) continue;
			const extra = (await resp.json()).points?.[0]?.extra as Record<string, unknown> | undefined;
			if (!extra) continue;
			const meta = Object.fromEntries(
				DOCUMENT_META_KEYS.filter((k) => extra[k] != null).map((k) => [k, extra[k]])
			);
			return Object.keys(meta).length ? meta : null;
		} catch {
			// Unreachable index: the delete that follows fails and stops the reindex.
		}
	}
	return null;
}

export interface LogEntry {
	file_name: string;
	file_hash: string;
	stored_path: string;
	collection: string;
	use_case: string;
	chunk_count: number;
}

/** Record an ingest in ingestion_log. Best-effort: never fails the ingest. */
export async function logIngestion(entry: LogEntry): Promise<void> {
	try {
		await fetch(`${SERVICES.evaluation}/v1/ingest-log`, {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify(entry)
		});
	} catch {
		// Non-critical – the vectors are already written.
	}
}

export interface IngestOptions {
	/** Document-level metadata copied into every chunk (e.g. product link). */
	extra?: Record<string, unknown>;
	/**
	 * Name shown in the document list instead of the bare file name. It is
	 * also the chunks' file_name – what the agent and the citations show –
	 * so a folder path given here ("Ofen 5/Schaltplan/…pdf") stays attached
	 * to every hit.
	 */
	displayName?: string;
}

/**
 * Markdown for a photo: its name, its description, and the image anchor the
 * chunker binds the picture by. The name carries product and machine once the
 * upload has renamed the file ("11221.7199 (Chiron 243) – 4BEAR_1").
 */
function photoMarkdown(
	fileName: string,
	images: Array<{ alt_text?: unknown }>,
	original: string
): string {
	const stem = fileName.replace(/\.[^.]+$/, '');
	const description =
		images.map((img) => (typeof img.alt_text === 'string' ? img.alt_text.trim() : '')).find(Boolean) ??
		'Keine Bildbeschreibung verfügbar.';
	const anchors = original.match(/<!--\s*image id:[^>]*-->/g) ?? [];
	return ['<!-- Page 1 -->', `Foto: ${stem}`, description, ...anchors].join('\n\n');
}

/** Full ingest of one document file. */
export async function ingestDocument(file: File, useCase: string, opts: IngestOptions = {}) {
	const fileName = opts.displayName || file.name;
	// 1. Clean (use_case decides where the original is stored)
	const cleanForm = new FormData();
	cleanForm.append('file', file, file.name);
	cleanForm.append('config', JSON.stringify({ use_case: useCase }));
	const cleanData = await (
		await callStage(
			'Bereinigung/PDF-Extraktion',
			'cleaning-service',
			`${SERVICES.cleaning}/v1/clean`,
			{ method: 'POST', body: cleanForm }
		)
	).json();
	const fileHash = cleanData.metadata?.file_hash ?? '';
	const storedPath = cleanData.metadata?.stored_path ?? '';

	// Strip the heavy base64 payload before passing images through — the
	// structuring service only needs the per-image metadata (id, page,
	// alt_text, url) to bind chunks to images.
	const rawImages = (cleanData.images ?? []) as Array<Record<string, unknown>>;
	const baseImages = rawImages
		.filter((img) => typeof img.image_id === 'string')
		.map((img) => ({
			image_id: img.image_id as string,
			page: img.page,
			alt_text: (img.alt_text as string) ?? '',
			url: (img.url as string) ?? '',
			stored_path: (img.stored_path as string) ?? ''
		}));

	// The alt-texts are generated in the background and are still empty here.
	// Chunking now would freeze empty descriptions into the vector payload for
	// good, leaving every image searchable-but-mute. Wait for them first.
	const descriptions = await waitForImageDescriptions(
		SERVICES.cleaning,
		useCase,
		fileHash,
		baseImages
	);

	// A photo has no text of its own – its description is the content. Without
	// it the chunker finds nothing but the image anchor and indexes nothing.
	const markdown =
		cleanData.metadata?.doc_type === 'image'
			? photoMarkdown(fileName, descriptions.images, cleanData.markdown ?? '')
			: cleanData.markdown;

	// 2. Structure
	const structureData = await (
		await callStage(
			'Strukturierung/Chunking',
			'data-structure-service',
			`${SERVICES.dataStructure}/v1/structure`,
			{
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({
					markdown,
					metadata: { ...(cleanData.metadata ?? {}), file_name: fileName },
					use_case: useCase,
					images: descriptions.images,
					...(opts.extra ? { config: { extra: opts.extra } } : {})
				})
			}
		)
	).json();
	const chunks = (structureData.chunks ?? []) as IndexChunk[];
	if (!chunks.length) {
		return { status: 'ok', chunks: 0, message: 'No chunks produced' };
	}

	// 3 + 4. Embed + upsert – file_hash + stored_path link points to the document
	const { upserted, collections } = await embedAndUpsert(chunks, useCase, {
		fallbackCollection: structureData.routing?.collection ?? `${useCase}_default`,
		payload: { file_hash: fileHash, stored_path: storedPath }
	});

	// 5. Log
	await logIngestion({
		file_name: fileName,
		file_hash: fileHash,
		stored_path: storedPath,
		collection: collections.join(', '),
		use_case: useCase,
		chunk_count: upserted
	});

	return {
		status: 'ok',
		file_name: file.name,
		use_case: useCase,
		chunks: upserted,
		stored_path: storedPath,
		// For the upload UI to poll background alt-text progress.
		file_hash: fileHash,
		image_count: (cleanData.image_count as number) ?? 0,
		// Chunking waits for the descriptions; when the budget ran out first,
		// the affected images are indexed mute. Surfacing it beats a silent
		// half-ingest — /v1/images/regenerate plus a re-ingest repairs it.
		images_described: descriptions.described,
		images_complete: descriptions.complete
	};
}
