import { json } from '@sveltejs/kit';
import { SERVICES } from '$lib/server/services';
import type { RequestHandler } from './$types';

/** Audit trail of skill-function executions for one use case. */
export const GET: RequestHandler = async ({ params, url }) => {
	const limit = url.searchParams.get('limit') ?? '50';
	const resp = await fetch(
		`${SERVICES.evaluation}/v1/admin/skills/${params.useCase}/executions?limit=${limit}`
	);
	return json(await resp.json(), { status: resp.status });
};
