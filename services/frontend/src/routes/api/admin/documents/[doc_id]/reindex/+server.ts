import { json } from '@sveltejs/kit';
import { SERVICES } from '$lib/server/services';
import type { RequestHandler } from './$types';

/**
 * Re-run the full ingest for an already-stored document.
 *
 * Needed whenever a stage that runs *before* indexing changes — the vision
 * prompt, the chunker, the image binding. None of those touch the vectors
 * already in Qdrant: chunk payloads are a snapshot taken at ingest time, so an
 * image description improved afterwards never reaches retrieval. Re-describing
 * alone is not enough for the same reason, which is why this re-chunks rather
 * than only refreshing the gallery.
 *
 * Re-cleaning regenerates the image sidecars from scratch (the cleaning service
 * rewrites them with an empty alt-text and re-runs vision), so this doubles as
 * the "transcribe again" action. Deterministic image ids keep the gallery URLs
 * stable across the round trip.
 *
 * The original upload is replayed through /api/ingest rather than reimplementing
 * clean → structure → embed → upsert here, so both paths cannot drift apart.
 */
export const POST: RequestHandler = async ({ params, request, fetch }) => {
	const docId = (params as Record<string, string>).doc_id ?? '';
	const body = (await request.json().catch(() => ({}))) as {
		use_case?: string;
		file_hash?: string;
		file_name?: string;
		stored_path?: string;
		collection?: string;
	};

	const useCase = body.use_case ?? '';
	const fileHash = body.file_hash ?? '';
	const fileName = body.file_name ?? '';
	const storedPath = body.stored_path ?? '';

	if (!docId || !useCase || !fileHash || !storedPath) {
		return json(
			{
				error: 'incomplete_document',
				detail:
					'use_case, file_hash und stored_path werden benötigt. Dokumente, ' +
					'die vor der Speicherung des Originals ingestiert wurden, lassen ' +
					'sich nicht neu einlesen — hier hilft nur ein erneuter Upload.'
			},
			{ status: 400 }
		);
	}

	// 1. Drop the existing vectors first. The upsert below reuses chunk ids
	//    derived from fresh uuid4s, so without this the old points would linger
	//    alongside the new ones and the same passage would be retrieved twice —
	//    once with the stale (empty) image descriptions.
	const collections = (body.collection || '')
		.split(',')
		.map((c) => c.trim())
		.filter(Boolean);

	let pointsDeleted = 0;
	const errors: string[] = [];
	for (const collection of collections) {
		try {
			const resp = await fetch(`${SERVICES.vectordb}/v1/delete`, {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ collection, filters: { file_hash: fileHash } })
			});
			if (!resp.ok) {
				errors.push(`vectordb(${collection}): HTTP ${resp.status}`);
				continue;
			}
			pointsDeleted += Number((await resp.json()).deleted ?? 0);
		} catch (err) {
			errors.push(`vectordb(${collection}): ${(err as Error).message}`);
		}
	}

	// A failed delete means the re-ingest would duplicate rather than replace.
	// Stopping here leaves the document exactly as it was.
	if (errors.length) {
		return json(
			{
				error: 'vector_cleanup_failed',
				detail: `Alte Vektoren konnten nicht entfernt werden: ${errors.join('; ')}. ` +
					'Es wurde nichts neu eingelesen.'
			},
			{ status: 502 }
		);
	}

	// 2. Fetch the stored original.
	let fileBlob: Blob;
	try {
		const fileResp = await fetch(
			`${SERVICES.cleaning}/v1/documents/${storedPath.split('/').map(encodeURIComponent).join('/')}`
		);
		if (!fileResp.ok) {
			return json(
				{
					error: 'original_missing',
					detail: `Originaldatei nicht abrufbar (HTTP ${fileResp.status}). ` +
						'Die Vektoren wurden bereits entfernt — bitte die Datei neu hochladen.'
				},
				{ status: 502 }
			);
		}
		fileBlob = await fileResp.blob();
	} catch (err) {
		return json(
			{ error: 'original_fetch_failed', detail: (err as Error).message },
			{ status: 502 }
		);
	}

	// 3. Replay it through the normal ingest path — including the wait for the
	//    freshly generated image descriptions.
	const form = new FormData();
	form.append('file', fileBlob, fileName || 'document');
	form.append('use_case', useCase);

	const ingestResp = await fetch('/api/ingest', { method: 'POST', body: form });
	const ingestData = await ingestResp.json();
	if (!ingestResp.ok) {
		return json(ingestData, { status: ingestResp.status });
	}

	return json({
		status: 'ok',
		document_id: docId,
		points_deleted: pointsDeleted,
		...ingestData
	});
};
