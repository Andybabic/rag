import { json } from '@sveltejs/kit';
import { SERVICES } from '$lib/server/services';
import type { RequestHandler } from './$types';

/**
 * The user's answer to a "Skill X möchte Y ausführen — erlauben?" prompt.
 *
 * The chat stream that asked is still open and blocked on this call, so the
 * round trip has to be short: the evaluation service resolves the pending
 * request in-process and the stream continues.
 */
export const POST: RequestHandler = async ({ params, request, locals }) => {
	const body = await request.json();

	const resp = await fetch(`${SERVICES.evaluation}/v1/agent/permission/${params.call_id}`, {
		method: 'POST',
		headers: {
			'Content-Type': 'application/json',
			...(locals.user?.username ? { 'X-User': locals.user.username } : {})
		},
		body: JSON.stringify(body)
	});

	return json(await resp.json(), { status: resp.status });
};
