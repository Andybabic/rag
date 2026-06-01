import { json } from '@sveltejs/kit';
import { SERVICES } from '$lib/server/services';
import type { RequestHandler } from '@sveltejs/kit';

interface DeletedDocument {
	id: string;
	file_name: string;
	file_hash: string;
	stored_path?: string;
	collection: string;
	use_case: string;
}

export const DELETE: RequestHandler = async ({ params }) => {
	const docId = (params as Record<string, string>).doc_id ?? '';
	if (!docId) {
		return json({ error: 'no_id' }, { status: 400 });
	}

	// 1. Take ownership of the DB row (delete + return payload). We do this
	//    first so a partial cleanup can't leave the row pointing at vanished
	//    files / vectors.
	const evalResp = await fetch(
		`${SERVICES.evaluation}/v1/admin/documents/${encodeURIComponent(docId)}`,
		{ method: 'DELETE' }
	);
	if (!evalResp.ok) {
		return json(
			{ error: 'evaluation_failed', detail: await evalResp.text() },
			{ status: 502 }
		);
	}
	const evalData = await evalResp.json();
	if (evalData.error === 'not_found') {
		return json({ error: 'not_found', detail: evalData.detail }, { status: 404 });
	}
	const doc = evalData.document as DeletedDocument | undefined;
	if (!doc) {
		return json({ error: 'no_document_payload' }, { status: 502 });
	}

	const errors: string[] = [];
	let pointsDeleted = 0;

	// 2. Qdrant: delete every point with this file_hash, across all
	//    collections the row was striped over (ingest joins them with ", ").
	const collections = (doc.collection || '')
		.split(',')
		.map((c) => c.trim())
		.filter(Boolean);
	for (const col of collections) {
		const resp = await fetch(`${SERVICES.vectordb}/v1/delete`, {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({
				collection: col,
				filters: { file_hash: doc.file_hash }
			})
		});
		if (!resp.ok) {
			errors.push(`vectordb(${col}): HTTP ${resp.status}`);
			continue;
		}
		const data = await resp.json();
		pointsDeleted += Number(data.deleted ?? 0);
	}

	// 3. Cleaning: remove original + all extracted images/sidecars.
	const url = new URL(
		`${SERVICES.cleaning}/v1/documents/${encodeURIComponent(doc.use_case)}/${encodeURIComponent(doc.file_hash)}`
	);
	if (doc.stored_path) {
		url.searchParams.set('stored_path', doc.stored_path);
	}
	const cleanResp = await fetch(url, { method: 'DELETE' });
	let filesRemoved: string[] = [];
	if (cleanResp.ok) {
		const data = await cleanResp.json();
		filesRemoved = data.removed ?? [];
	} else {
		errors.push(`cleaning: HTTP ${cleanResp.status}`);
	}

	return json({
		status: errors.length === 0 ? 'ok' : 'partial',
		document: doc,
		points_deleted: pointsDeleted,
		files_removed: filesRemoved,
		errors
	});
};
