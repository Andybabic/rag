import { json } from '@sveltejs/kit';
import { SERVICES } from '$lib/server/services';
import type { RequestHandler } from './$types';

export const GET: RequestHandler = async ({ params }) => {
	try {
		const resp = await fetch(
			`${SERVICES.cleaning}/v1/images/progress/${encodeURIComponent(params.file_hash)}`
		);
		return json(await resp.json(), { status: resp.status });
	} catch {
		return json(
			{ file_hash: params.file_hash, total: 0, done: 0, failed: 0, status: 'unknown' },
			{ status: 200 }
		);
	}
};
