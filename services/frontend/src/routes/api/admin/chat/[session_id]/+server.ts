import { json } from '@sveltejs/kit';
import { SERVICES } from '$lib/server/services';
import type { RequestHandler } from './$types';

export const GET: RequestHandler = async ({ params }) => {
	const resp = await fetch(
		`${SERVICES.evaluation}/v1/admin/chat/${encodeURIComponent(params.session_id)}`
	);
	return json(await resp.json(), { status: resp.status });
};
