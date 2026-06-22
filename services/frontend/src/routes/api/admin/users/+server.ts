import { json } from '@sveltejs/kit';
import { SERVICES } from '$lib/server/services';
import type { RequestHandler } from './$types';

// Admin-only is enforced in hooks.server.ts (path starts with /api/admin).

export const GET: RequestHandler = async () => {
	const resp = await fetch(`${SERVICES.evaluation}/v1/auth/users`);
	return json(await resp.json(), { status: resp.status });
};

export const POST: RequestHandler = async ({ request }) => {
	const body = await request.text();
	const resp = await fetch(`${SERVICES.evaluation}/v1/auth/users`, {
		method: 'POST',
		headers: { 'content-type': 'application/json' },
		body
	});
	return json(await resp.json().catch(() => ({})), { status: resp.status });
};
