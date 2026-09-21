import { json } from '@sveltejs/kit';
import { SERVICES } from '$lib/server/services';
import type { RequestHandler } from './$types';

/**
 * A skill's code file. Reachable only under /api/admin, which hooks.server.ts
 * restricts to role=admin — uploading a function file is uploading code that
 * the platform will execute.
 */
export const GET: RequestHandler = async ({ params }) => {
	const resp = await fetch(`${SERVICES.evaluation}/v1/admin/skills/item/${params.id}/code`);
	return json(await resp.json(), { status: resp.status });
};

export const PUT: RequestHandler = async ({ params, request }) => {
	const body = await request.text();
	const resp = await fetch(`${SERVICES.evaluation}/v1/admin/skills/item/${params.id}/code`, {
		method: 'PUT',
		headers: { 'Content-Type': 'application/json' },
		body
	});
	return json(await resp.json(), { status: resp.status });
};

export const DELETE: RequestHandler = async ({ params }) => {
	const resp = await fetch(`${SERVICES.evaluation}/v1/admin/skills/item/${params.id}/code`, {
		method: 'DELETE'
	});
	return json(await resp.json(), { status: resp.status });
};
