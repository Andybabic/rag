import { json } from '@sveltejs/kit';
import { SERVICES } from '$lib/server/services';
import type { RequestHandler } from './$types';

export const POST: RequestHandler = async ({ params }) => {
	const resp = await fetch(
		`${SERVICES.evaluation}/v1/admin/api-tokens/${encodeURIComponent(params.token_id)}/revoke`,
		{ method: 'POST' }
	);
	return json(await resp.json().catch(() => ({})), { status: resp.status });
};
