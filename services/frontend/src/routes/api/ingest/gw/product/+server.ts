/**
 * POST /api/ingest/gw/product — (re)index one GW product on one machine.
 *
 * Body: { product_id, machine, category, files: [{ path, content_b64 }] }
 *       `files` holds the CNC programs and the Stückliste named by the upload
 *       plan; Einstellblatt PDFs and photos follow separately via /api/ingest.
 *
 * Replace, not append: everything of this product on this machine is removed
 * first (points, document-list rows, stored originals – see replaceProduct),
 * so uploading a folder of the same name again replaces what was there.
 *
 * `machine` / `category` in the body win over the file paths: a product folder
 * uploaded on its own gets them from the index or from the user.
 */
import { json } from '@sveltejs/kit';
import { SERVICES } from '$lib/server/services';
import {
	callStage,
	embedAndUpsert,
	failureBody,
	logIngestion,
	PipelineError,
	type IndexChunk
} from '$lib/server/ingest-pipeline';
import { GW_USE_CASE, productLogKey, replaceProduct } from '$lib/server/gw-products';
import type { RequestHandler } from './$types';

const PRODUCT_ID_RE = /^\d{5}\.\d{3,4}$/;

interface ProductUpload {
	product_id?: unknown;
	machine?: unknown;
	category?: unknown;
	files?: Array<{ path?: unknown; content_b64?: unknown }>;
}

const text = (v: unknown): string | null => (typeof v === 'string' && v ? v : null);

export const POST: RequestHandler = async ({ request }) => {
	const body = (await request.json().catch(() => null)) as ProductUpload | null;
	const productId = text(body?.product_id) ?? '';
	if (!PRODUCT_ID_RE.test(productId)) {
		return json(
			{ error: 'bad_product', detail: 'product_id muss die Form 11221.7199 haben' },
			{ status: 400 }
		);
	}
	const machine = text(body?.machine);
	const category = text(body?.category);
	const files = (body?.files ?? []).filter(
		(f): f is { path: string; content_b64: string } =>
			typeof f?.path === 'string' && typeof f?.content_b64 === 'string'
	);

	const fileHash = productLogKey(productId, machine);
	const label = `${productId}${machine ? ` (${machine})` : ''} – CNC-Programme`;

	try {
		const replaced = await replaceProduct(productId, machine);
		const base = {
			status: 'ok',
			product_id: productId,
			machine,
			replaced: replaced.points,
			documents_removed: replaced.documents
		};
		if (!files.length) return json({ ...base, chunks: 0, product: null });

		const structureData = await (
			await callStage(
				'Strukturierung/CNC',
				'data-structure-service',
				`${SERVICES.dataStructure}/v1/structure/product`,
				{
					method: 'POST',
					headers: { 'Content-Type': 'application/json' },
					body: JSON.stringify({ files, machine, category })
				}
			)
		).json();
		const chunks = (structureData.chunks ?? []) as IndexChunk[];
		const product = (structureData.products ?? [])[0] ?? null;

		let upserted = 0;
		if (chunks.length) {
			const result = await embedAndUpsert(chunks, GW_USE_CASE, {
				fallbackCollection: 'gw_cnc_steps',
				payload: { file_hash: fileHash }
			});
			upserted = result.upserted;
			await logIngestion({
				file_name: label,
				file_hash: fileHash,
				stored_path: '',
				collection: result.collections.join(', '),
				use_case: GW_USE_CASE,
				chunk_count: upserted
			});
		}
		return json({ ...base, chunks: upserted, product });
	} catch (err) {
		if (err instanceof PipelineError) {
			return json(failureBody(err), { status: err.failure.status });
		}
		throw err;
	}
};
