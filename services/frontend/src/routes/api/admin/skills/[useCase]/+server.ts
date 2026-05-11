import { json } from '@sveltejs/kit';
import { SERVICES } from '$lib/server/services';
import type { RequestHandler } from './$types';

export const GET: RequestHandler = async ({ params }) => {
	const resp = await fetch(`${SERVICES.evaluation}/v1/admin/skills/${params.useCase}`);
	return json(await resp.json());
};

export const POST: RequestHandler = async ({ params, request }) => {
	const body = await request.text();
	const resp = await fetch(`${SERVICES.evaluation}/v1/admin/skills/${params.useCase}`, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body
	});
	return json(await resp.json());
};
