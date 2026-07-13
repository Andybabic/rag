import { json } from '@sveltejs/kit';
import { SERVICES } from '$lib/server/services';
import type { RequestHandler } from './$types';

export const POST: RequestHandler = async ({ params }) => {
	const resp = await fetch(
		`${SERVICES.cleaning}/v1/images/regenerate/${encodeURIComponent(params.use_case)}/${encodeURIComponent(params.file_hash)}`,
		{ method: 'POST' }
	);
	return json(await resp.json().catch(() => ({})), { status: resp.status });
};
