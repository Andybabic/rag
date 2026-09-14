import { json } from '@sveltejs/kit';
import { SERVICES } from '$lib/server/services';
import type { RequestHandler } from './$types';

export const GET: RequestHandler = async ({ url }) => {
	const useCase = url.searchParams.get('use_case') ?? '';
	const limit = url.searchParams.get('limit') ?? '50';
	const query = new URLSearchParams({ limit });
	if (useCase) query.set('use_case', useCase);
	const resp = await fetch(`${SERVICES.evaluation}/v1/admin/api-jobs?${query}`);
	return json(await resp.json(), { status: resp.status });
};
