import { json } from '@sveltejs/kit';
import { SERVICES } from '$lib/server/services';
import type { RequestHandler } from './$types';

/** Turn the consent prompt for one function on or off. Admin-only. */
export const PUT: RequestHandler = async ({ params, request }) => {
	const body = await request.text();
	const resp = await fetch(
		`${SERVICES.evaluation}/v1/admin/skills/item/${params.id}/functions/${encodeURIComponent(params.fn)}/permission`,
		{ method: 'PUT', headers: { 'Content-Type': 'application/json' }, body }
	);
	return json(await resp.json(), { status: resp.status });
};
