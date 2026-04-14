import { json } from '@sveltejs/kit';
import { SERVICES } from '$lib/server/services';
import type { RequestHandler } from '@sveltejs/kit';

export const GET: RequestHandler = async ({ params }) => {
	const resp = await fetch(
		`${SERVICES.evaluation}/v1/use-case/${params.id}/collections`
	);
	const data = await resp.json();
	return json(data, { status: resp.status });
};
