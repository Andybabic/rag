import { json } from '@sveltejs/kit';
import { SERVICES } from '$lib/server/services';
import type { RequestHandler } from './$types';

export const PUT: RequestHandler = async ({ params, request }) => {
	const body = await request.text();
	const resp = await fetch(`${SERVICES.evaluation}/v1/admin/skills/${params.id}`, {
		method: 'PUT',
		headers: { 'Content-Type': 'application/json' },
		body
	});
	return json(await resp.json());
};

export const DELETE: RequestHandler = async ({ params }) => {
	const resp = await fetch(`${SERVICES.evaluation}/v1/admin/skills/${params.id}`, {
		method: 'DELETE'
	});
	return json(await resp.json());
};
