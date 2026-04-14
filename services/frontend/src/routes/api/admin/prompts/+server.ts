import { json } from '@sveltejs/kit';
import { SERVICES } from '$lib/server/services';
import type { RequestHandler } from './$types';

export const GET: RequestHandler = async ({ url }) => {
	const useCase = url.searchParams.get('use_case') || 'neumann';
	const role = url.searchParams.get('role') || 'default';
	const resp = await fetch(
		`${SERVICES.evaluation}/v1/admin/prompts/${useCase}?role=${role}`
	);
	return json(await resp.json());
};

export const PUT: RequestHandler = async ({ request }) => {
	const body = await request.json();
	const useCase = body.use_case || 'neumann';
	const resp = await fetch(`${SERVICES.evaluation}/v1/admin/prompts/${useCase}`, {
		method: 'PUT',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ role: body.role || 'default', prompt: body.prompt })
	});
	return json(await resp.json());
};
