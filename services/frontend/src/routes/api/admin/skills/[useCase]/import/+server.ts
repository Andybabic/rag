import { json } from '@sveltejs/kit';
import { SERVICES } from '$lib/server/services';
import type { RequestHandler } from './$types';

/**
 * Import skills from an exported document.
 *
 * Admin-only via hooks.server.ts, and deliberately so: importing a skill means
 * importing code the platform will execute.
 */
export const POST: RequestHandler = async ({ params, request }) => {
	const body = await request.text();
	const resp = await fetch(`${SERVICES.evaluation}/v1/admin/skills/${params.useCase}/import`, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body
	});
	return json(await resp.json(), { status: resp.status });
};
