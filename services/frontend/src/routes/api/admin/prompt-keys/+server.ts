import { json } from '@sveltejs/kit';
import { SERVICES } from '$lib/server/services';
import type { RequestHandler } from './$types';

export const PUT: RequestHandler = async ({ request }) => {
	const body = await request.text();
	const resp = await fetch(`${SERVICES.evaluation}/v1/admin/prompt-keys`, {
		method: 'PUT',
		headers: { 'Content-Type': 'application/json' },
		body
	});
	return json(await resp.json());
};
