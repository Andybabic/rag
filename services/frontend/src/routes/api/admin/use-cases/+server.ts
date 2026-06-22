import { json } from '@sveltejs/kit';
import { SERVICES } from '$lib/server/services';
import type { RequestHandler } from './$types';

// Admin-only is enforced in hooks.server.ts (path starts with /api/admin).

export const GET: RequestHandler = async () => {
	const resp = await fetch(`${SERVICES.evaluation}/v1/admin/use-cases`);
	return json(await resp.json(), { status: resp.status });
};
