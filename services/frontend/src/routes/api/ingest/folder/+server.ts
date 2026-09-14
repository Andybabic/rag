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
import { knownLocations, knownMachines, productLogKey, replaceProduct } from '$lib/server/gw-products';
import { planLocation } from '$lib/gw-location';
import type { RequestHandler } from './$types';

/**
 * Folder/ZIP ingest for the CNC use case (GW St. Pölten).
 *
 * A ZIP of one or more product folders. The data-structure service groups the
 * files per product and parses every CNC program into one chunk per tool
 * operation; Einstellblatt PDFs are only reported here. The folder upload
 * (/api/ingest/gw/*) additionally ingests PDFs and photos.
 *
 * Every product in the ZIP replaces its earlier upload, exactly like the
 * folder upload does: a ZIP of the same product must not double it.
 */

interface StructuredProduct {
	product_id: string;
	machine: string | null;
	category: string | null;
	warnings?: string[];
	[k: string]: unknown;
}

const extraOf = (c: IndexChunk) => ((c.metadata?.extra ?? {}) as Record<string, unknown>);

/**
 * Re-stamp chunks whose machine was settled differently from their path: taken
 * over from the index, or dropped because the folder was no machine at all.
 */
function applyLocation(chunks: IndexChunk[], machine: string | null, category: string | null) {
	for (const chunk of chunks) {
		const extra = extraOf(chunk);
		extra.machine = machine;
		extra.category = category;
		if (chunk.metadata) chunk.metadata.extra = extra;
		chunk.text = chunk.text.replace(/\nMaschine: [^\n]*\./, '');
		if (machine) chunk.text += `\nMaschine: ${machine}.`;
	}
}

export const POST: RequestHandler = async ({ request }) => {
	const formData = await request.formData();
	const file = formData.get('file') as File | null;
	const useCase = (formData.get('use_case') as string) || 'gw_stpoelten';

	if (!file) {
		return json({ error: 'no_file', detail: 'No ZIP file provided' }, { status: 400 });
	}
	// The structuring behind this route writes into the GW CNC collections.
	if (useCase !== 'gw_stpoelten') {
		return json(
			{
				error: 'wrong_use_case',
				detail: 'Der Ordner-/ZIP-Upload gibt es nur für GW St. Pölten (CNC-Programme). ' +
					'Dokumente anderer Use Cases bitte über den PDF-Upload hochladen.'
			},
			{ status: 400 }
		);
	}

	try {
		const structureData = await (
			await callStage(
				'Strukturierung/CNC',
				'data-structure-service',
				`${SERVICES.dataStructure}/v1/structure/folder`,
				{
					method: 'POST',
					headers: { 'Content-Type': 'application/zip' },
					body: await file.arrayBuffer()
				}
			)
		).json();

		const chunks = (structureData.chunks ?? []) as IndexChunk[];
		const products = (structureData.products ?? []) as StructuredProduct[];
		const allMachines = await knownMachines();
		let upserted = 0;
		let replaced = 0;
		const collections = new Set<string>();

		for (const product of products) {
			const own = chunks.filter(
				(c) =>
					extraOf(c).product_id === product.product_id &&
					(extraOf(c).machine ?? null) === (product.machine ?? null)
			);
			// Same rules as the folder upload; a ZIP cannot ask, so an open choice
			// leaves the machine empty and says so.
			const { location, choices, notes } = planLocation({
				pathMachine: product.machine ?? null,
				pathCategory: product.category ?? null,
				known: await knownLocations(product.product_id),
				allMachines
			});
			const machine = location?.machine ?? null;
			const category = location?.category ?? null;
			if (machine !== (product.machine ?? null)) applyLocation(own, machine, category);
			product.warnings = [
				...(product.warnings ?? []).filter((w) => !w.startsWith('Produkt liegt auch auf')),
				...notes,
				...(choices.length ? ['Maschine offen – bitte über den Ordner-Upload hochladen und wählen'] : [])
			];
			product.machine = machine;
			product.category = category;

			replaced += (await replaceProduct(product.product_id, machine)).points;
			if (!own.length) continue;
			const fileHash = productLogKey(product.product_id, machine);
			const result = await embedAndUpsert(own, useCase, {
				fallbackCollection: 'gw_cnc_steps',
				payload: { file_hash: fileHash }
			});
			upserted += result.upserted;
			result.collections.forEach((c) => collections.add(c));
			await logIngestion({
				file_name: `${product.product_id}${machine ? ` (${machine})` : ''} – CNC-Programme`,
				file_hash: fileHash,
				stored_path: '',
				collection: result.collections.join(', '),
				use_case: useCase,
				chunk_count: result.upserted
			});
		}

		return json({
			status: 'ok',
			file_name: file.name,
			use_case: useCase,
			products,
			skipped_noncanonical: structureData.skipped_noncanonical ?? [],
			collections: [...collections],
			replaced,
			chunks: upserted
		});
	} catch (err) {
		if (err instanceof PipelineError) {
			return json(failureBody(err), { status: err.failure.status });
		}
		throw err;
	}
};
