import { json } from '@sveltejs/kit';
import { SERVICES } from '$lib/server/services';
import type { RequestHandler } from '@sveltejs/kit';

export const PUT: RequestHandler = async ({ params, request }) => {
	const p = params as Record<string, string>;
	const body = await request.json();
	const resp = await fetch(
		`${SERVICES.evaluation}/v1/admin/actions/${p.useCase}/${p.actionName}`,
		{
			method: 'PUT',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify(body)
		}
	);
	return json(await resp.json());
};
