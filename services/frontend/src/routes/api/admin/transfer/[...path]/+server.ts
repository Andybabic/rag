/**
 * Proxy für den Use-Case-Transfer (Export/Import) zum Evaluation-Service.
 *
 * Admin-only über hooks.server.ts (/api/admin). Antworten werden gestreamt
 * durchgereicht — ein Export kann Gigabytes groß sein und darf nicht erst im
 * Speicher des Frontends landen. Uploads kommen in Teilen unter dem
 * BODY_SIZE_LIMIT an; jeder Teil wird unverändert weitergegeben.
 */
import { SERVICES } from '$lib/server/services';
import type { RequestHandler } from './$types';

const PASS_HEADERS = ['content-type', 'content-length', 'content-disposition'];

const forward: RequestHandler = async ({ params, request, url }) => {
	const target = new URL(`${SERVICES.evaluation}/v1/admin/transfer/${params.path}`);
	target.search = url.search;

	const hasBody = request.method !== 'GET' && request.method !== 'HEAD';
	let resp: Response;
	try {
		resp = await fetch(target, {
			method: request.method,
			headers: { 'content-type': request.headers.get('content-type') ?? 'application/json' },
			body: hasBody ? await request.arrayBuffer() : undefined
		});
	} catch {
		return new Response(JSON.stringify({ detail: 'Evaluation-Service nicht erreichbar.' }), {
			status: 502,
			headers: { 'content-type': 'application/json' }
		});
	}

	const headers = new Headers();
	for (const name of PASS_HEADERS) {
		const value = resp.headers.get(name);
		if (value) headers.set(name, value);
	}
	return new Response(resp.body, { status: resp.status, headers });
};

export const GET = forward;
export const POST = forward;
export const PUT = forward;
export const DELETE = forward;
