/**
 * GW product bookkeeping shared by the folder plan, the product upload and the
 * ZIP ingest: where a product already lives, and replacing it completely.
 */
import { SERVICES } from '$lib/server/services';
import { callStage } from '$lib/server/ingest-pipeline';
import type { Location } from '$lib/gw-location';

export const GW_USE_CASE = 'gw_stpoelten';
export const GW_COLLECTIONS = ['gw_cnc_steps', 'gw_material_info', 'gw_ruest_data'];

/** One document-list row per product and machine; re-uploading updates it. */
export function productLogKey(productId: string, machine: string | null): string {
	return `gw:${productId}:${machine ?? '-'}`;
}

async function scrollPayloads(
	collection: string,
	filters: Record<string, unknown>,
	limit = 1000
): Promise<Array<Record<string, unknown>>> {
	const resp = await callStage('Bestand lesen', 'vectordb-service', `${SERVICES.vectordb}/v1/scroll`, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ collection, filters, limit })
	});
	return (await resp.json()).points ?? [];
}

/** The machines a product is already indexed on. */
export async function knownLocations(productId: string): Promise<Location[]> {
	const out: Location[] = [];
	for (const collection of ['gw_cnc_steps', 'gw_material_info']) {
		for (const point of await scrollPayloads(collection, { 'extra.product_id': productId })) {
			const extra = (point.extra ?? {}) as Record<string, unknown>;
			if (typeof extra.machine === 'string') {
				out.push({
					machine: extra.machine,
					category: typeof extra.category === 'string' ? extra.category : null
				});
			}
		}
	}
	return out;
}

/**
 * Every machine the index knows.
 *
 * Reads up to 1000 CNC operations – plenty to list the handful of machines,
 * but a machine whose operations all lie beyond that window would be missed
 * and its folder then treated as unknown (the user is asked, nothing breaks).
 */
export async function knownMachines(): Promise<Location[]> {
	const out = new Map<string, Location>();
	for (const point of await scrollPayloads('gw_cnc_steps', { 'extra.is_cnc_block': true })) {
		const extra = (point.extra ?? {}) as Record<string, unknown>;
		if (typeof extra.machine === 'string' && !out.has(extra.machine)) {
			out.set(extra.machine, {
				machine: extra.machine,
				category: typeof extra.category === 'string' ? extra.category : null
			});
		}
	}
	return [...out.values()];
}

/**
 * Remove a product on a machine completely: its points in every GW
 * collection, and the document-list rows and stored originals of its PDFs and
 * photos.
 *
 * Originals are stored by content hash, so a byte-identical Einstellblatt or
 * photo in another product shares the very same file and row. Those are only
 * removed once no other point refers to them any more – deleting them outright
 * would strip the other product of its images.
 */
export async function replaceProduct(
	productId: string,
	machine: string | null
): Promise<{ points: number; documents: number }> {
	// Points from before chunks carried a machine are superseded as well; a
	// null filter value addresses "field absent" (vectordb _build_filter).
	const filters: Array<Record<string, string | null>> = [
		{ 'extra.product_id': productId, 'extra.machine': machine }
	];
	if (machine !== null) filters.push({ 'extra.product_id': productId, 'extra.machine': null });

	const hashes = new Set<string>();
	for (const collection of GW_COLLECTIONS) {
		for (const f of filters) {
			for (const point of await scrollPayloads(collection, f)) {
				if (typeof point.file_hash === 'string' && point.file_hash) hashes.add(point.file_hash);
			}
		}
	}

	let points = 0;
	for (const collection of GW_COLLECTIONS) {
		for (const f of filters) {
			const resp = await callStage('Alte Daten entfernen', 'vectordb-service', `${SERVICES.vectordb}/v1/delete`, {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ collection, filters: f })
			});
			points += Number((await resp.json()).deleted ?? 0);
		}
	}

	const orphaned: string[] = [];
	for (const hash of hashes) {
		let stillUsed = false;
		for (const collection of GW_COLLECTIONS) {
			if ((await scrollPayloads(collection, { file_hash: hash }, 1)).length) {
				stillUsed = true;
				break;
			}
		}
		if (!stillUsed) orphaned.push(hash);
	}
	if (!orphaned.length) return { points, documents: 0 };

	const logResp = await callStage(
		'Dokumentliste bereinigen',
		'evaluation-service',
		`${SERVICES.evaluation}/v1/ingest-log/delete`,
		{
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ file_hashes: orphaned, use_case: GW_USE_CASE })
		}
	);
	const removed = ((await logResp.json()).documents ?? []) as Array<{
		file_hash: string;
		stored_path: string;
		use_case: string;
	}>;
	for (const doc of removed) {
		if (!doc.stored_path || doc.file_hash.startsWith('gw:')) continue;
		const url = new URL(
			`${SERVICES.cleaning}/v1/documents/${encodeURIComponent(doc.use_case)}/${encodeURIComponent(doc.file_hash)}`
		);
		url.searchParams.set('stored_path', doc.stored_path);
		try {
			await fetch(url, { method: 'DELETE' });
		} catch {
			// A leftover file on disk is harmless; the index is already clean.
		}
	}
	return { points, documents: removed.length };
}
