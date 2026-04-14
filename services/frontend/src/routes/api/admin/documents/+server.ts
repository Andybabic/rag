import { json } from '@sveltejs/kit';
import { SERVICES } from '$lib/server/services';
import type { RequestHandler } from './$types';

export const GET: RequestHandler = async ({ url }) => {
	const useCase = url.searchParams.get('use_case') || '';

	// Fetch both documents (ingestion log) and collections in parallel
	const [docsResp, colsResp] = await Promise.all([
		fetch(`${SERVICES.evaluation}/v1/admin/documents?use_case=${useCase}`),
		fetch(`${SERVICES.vectordb}/v1/collections`)
	]);

	const docs = await docsResp.json();
	const cols = await colsResp.json();

	return json({
		documents: docs.documents ?? [],
		collections: cols.collections ?? []
	});
};
