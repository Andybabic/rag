import { json } from '@sveltejs/kit';
import { SERVICES } from '$lib/server/services';
import type { RequestHandler } from './$types';

/**
 * Skills of a use case as one portable document, or a single skill via
 * ?skill_id=. Never includes variable values.
 */
export const GET: RequestHandler = async ({ params, url }) => {
	const skillId = url.searchParams.get('skill_id');
	const query = skillId ? `?skill_id=${encodeURIComponent(skillId)}` : '';
	const resp = await fetch(
		`${SERVICES.evaluation}/v1/admin/skills/${params.useCase}/export${query}`
	);
	return json(await resp.json(), { status: resp.status });
};
