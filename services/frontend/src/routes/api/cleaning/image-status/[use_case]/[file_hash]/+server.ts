import { json } from '@sveltejs/kit';
import { SERVICES } from '$lib/server/services';
import type { RequestHandler } from './$types';

export const GET: RequestHandler = async ({ params }) => {
	try {
		const resp = await fetch(
			`${SERVICES.cleaning}/v1/images/status/${encodeURIComponent(params.use_case)}/${encodeURIComponent(params.file_hash)}`
		);
		return json(await resp.json(), { status: resp.status });
	} catch {
		return json(
			{ file_hash: params.file_hash, total: 0, described: 0, pending: 0, job_status: 'unknown' },
			{ status: 200 }
		);
	}
};
