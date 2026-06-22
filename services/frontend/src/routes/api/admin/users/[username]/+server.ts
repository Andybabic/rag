import { json } from '@sveltejs/kit';
import { SERVICES } from '$lib/server/services';
import type { RequestHandler } from './$types';

// Admin-only is enforced in hooks.server.ts (path starts with /api/admin).

export const PATCH: RequestHandler = async ({ params, request }) => {
	const body = await request.text();
	const resp = await fetch(
		`${SERVICES.evaluation}/v1/auth/users/${encodeURIComponent(params.username)}`,
		{
			method: 'PATCH',
			headers: { 'content-type': 'application/json' },
			body
		}
	);
	return json(await resp.json().catch(() => ({})), { status: resp.status });
};

export const DELETE: RequestHandler = async ({ params }) => {
	const resp = await fetch(
		`${SERVICES.evaluation}/v1/auth/users/${encodeURIComponent(params.username)}`,
		{ method: 'DELETE' }
	);
	if (resp.status === 204) return new Response(null, { status: 204 });
	return json(await resp.json().catch(() => ({})), { status: resp.status });
};
