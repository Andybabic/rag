import { json } from '@sveltejs/kit';
import { SERVICES } from '$lib/server/services';
import type { RequestHandler } from './$types';

export const GET: RequestHandler = async () => {
	const resp = await fetch(`${SERVICES.vectordb}/v1/collections`);
	const data = await resp.json();
	return json(data, { status: resp.status });
};
