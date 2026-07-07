import { json } from '@sveltejs/kit';
import { SERVICES } from '$lib/server/services';
import type { RequestHandler } from './$types';

export const GET: RequestHandler = async ({ url }) => {
	const useCase = url.searchParams.get('use_case') || '';
	const rating = url.searchParams.get('rating') || '';
	const limit = url.searchParams.get('limit') || '100';
	const resp = await fetch(
		`${SERVICES.evaluation}/v1/admin/feedback?use_case=${encodeURIComponent(useCase)}` +
			`&rating=${encodeURIComponent(rating)}&limit=${encodeURIComponent(limit)}`
	);
	return json(await resp.json(), { status: resp.status });
};
