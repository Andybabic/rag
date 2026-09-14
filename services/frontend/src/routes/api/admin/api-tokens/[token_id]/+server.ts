import { json } from '@sveltejs/kit';
import { SERVICES } from '$lib/server/services';
import type { RequestHandler } from './$types';

export const DELETE: RequestHandler = async ({ params }) => {
	const resp = await fetch(
		`${SERVICES.evaluation}/v1/admin/api-tokens/${encodeURIComponent(params.token_id)}`,
		{ method: 'DELETE' }
	);
	if (resp.status === 204) return new Response(null, { status: 204 });
	return json(await resp.json().catch(() => ({})), { status: resp.status });
};
