import { json } from '@sveltejs/kit';
import { SERVICES } from '$lib/server/services';
import type { RequestHandler } from '@sveltejs/kit';

export const GET: RequestHandler = async ({ params }) => {
	const useCase = (params as Record<string, string>).useCase ?? 'neumann';
	const resp = await fetch(`${SERVICES.evaluation}/v1/admin/actions/${useCase}`);
	return json(await resp.json());
};
