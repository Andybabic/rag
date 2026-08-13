import { json } from '@sveltejs/kit';
import { SERVICES } from '$lib/server/services';
import type { RequestHandler } from './$types';

export const POST: RequestHandler = async ({ params, request }) => {
	const body = await request.text();
	const resp = await fetch(`${SERVICES.evaluation}/v1/admin/config/${params.useCase}/test`, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: body || '{}'
	});
	return json(await resp.json(), { status: resp.status });
};
