import { json } from '@sveltejs/kit';
import { SERVICES } from '$lib/server/services';
import type { RequestHandler } from './$types';

// Admin-only is enforced in hooks.server.ts (path starts with /api/admin).

export const GET: RequestHandler = async () => {
	const resp = await fetch(`${SERVICES.evaluation}/v1/admin/api-tokens`);
	return json(await resp.json(), { status: resp.status });
};

export const POST: RequestHandler = async ({ request, locals }) => {
	const body = await request.text();
	// Stamp the issuing admin server-side — a client-supplied author would be
	// worthless as an audit trail.
	const query = locals.user
		? `?created_by=${encodeURIComponent(locals.user.username)}`
		: '';
	const resp = await fetch(`${SERVICES.evaluation}/v1/admin/api-tokens${query}`, {
		method: 'POST',
		headers: { 'content-type': 'application/json' },
		body
	});
	return json(await resp.json().catch(() => ({})), { status: resp.status });
};
