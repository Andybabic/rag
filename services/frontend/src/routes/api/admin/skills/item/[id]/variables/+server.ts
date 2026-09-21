import { json } from '@sveltejs/kit';
import { SERVICES } from '$lib/server/services';
import type { RequestHandler } from './$types';

/**
 * Skill variables — admin-only, and read-masked.
 *
 * GET returns names and a masked preview, never the values: a key stored here
 * is used by the sandbox, not read back through the browser. PUT replaces the
 * whole object.
 */
export const GET: RequestHandler = async ({ params }) => {
	const resp = await fetch(`${SERVICES.evaluation}/v1/admin/skills/item/${params.id}/variables`);
	return json(await resp.json(), { status: resp.status });
};

export const PUT: RequestHandler = async ({ params, request }) => {
	const body = await request.text();
	const resp = await fetch(`${SERVICES.evaluation}/v1/admin/skills/item/${params.id}/variables`, {
		method: 'PUT',
		headers: { 'Content-Type': 'application/json' },
		body
	});
	return json(await resp.json(), { status: resp.status });
};
