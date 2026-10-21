import { json } from '@sveltejs/kit';
import { failureBody, ingestDocument, PipelineError } from '$lib/server/ingest-pipeline';
import type { RequestHandler } from './$types';

/**
 * Ingest one document: clean → structure → embed → upsert → log.
 *
 * Optional form fields:
 *   meta          JSON object copied into every chunk's metadata – how an
 *                 Einstellblatt stays linked to its product and machine.
 *   display_name  Name for the document list (many products each have an
 *                 "Einstellblatt 1.pdf"; the bare name would be ambiguous).
 *   fast          "true": digital PDFs from their text layer, no image
 *                 descriptions – for bulk uploads (scripts/bulk_ingest.py).
 */
export const POST: RequestHandler = async ({ request }) => {
	const formData = await request.formData();
	const file = formData.get('file') as File | null;
	const useCase = (formData.get('use_case') as string) || 'neumann';

	if (!file) {
		return json({ error: 'no_file', detail: 'No file provided' }, { status: 400 });
	}

	let extra: Record<string, unknown> | undefined;
	const rawMeta = formData.get('meta');
	if (typeof rawMeta === 'string' && rawMeta.trim()) {
		try {
			extra = JSON.parse(rawMeta);
		} catch {
			extra = undefined;
		}
		if (!extra || typeof extra !== 'object' || Array.isArray(extra)) {
			return json({ error: 'bad_meta', detail: 'meta must be a JSON object' }, { status: 400 });
		}
	}
	const displayName = (formData.get('display_name') as string | null) ?? undefined;
	const fast = formData.get('fast') === 'true';

	try {
		return json(await ingestDocument(file, useCase, { extra, displayName, fast }));
	} catch (err) {
		if (err instanceof PipelineError) {
			return json(failureBody(err), { status: err.failure.status });
		}
		throw err;
	}
};
