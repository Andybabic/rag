/**
 * Gateway for the external pipeline API (`/api/v1/*`).
 *
 * The frontend is the only service exposed to the outside, so it is also the
 * door for API clients. It stays a pure pass-through on purpose: the bearer
 * token travels untouched to the evaluation service, which owns the token
 * store and therefore decides what a credential may see. Verifying here as
 * well would mean two places that must agree on scope rules — and a round trip
 * per request just to ask.
 */
import { SERVICES } from '$lib/server/services';

function jsonResponse(status: number, body: unknown): Response {
	return new Response(JSON.stringify(body), {
		status,
		headers: { 'content-type': 'application/json' }
	});
}

/** Forward a request to the evaluation service's public router. */
export async function forwardToPipeline(request: Request, upstreamPath: string): Promise<Response> {
	const headers: Record<string, string> = {
		authorization: request.headers.get('authorization') ?? ''
	};

	let body: string | undefined;
	if (request.method !== 'GET' && request.method !== 'HEAD') {
		body = await request.text();
		headers['content-type'] = 'application/json';
	}

	let upstream: Response;
	try {
		upstream = await fetch(`${SERVICES.evaluation}${upstreamPath}`, {
			method: request.method,
			headers,
			body
		});
	} catch (err) {
		return jsonResponse(502, {
			error: 'upstream_unavailable',
			detail: `Pipeline nicht erreichbar: ${err instanceof Error ? err.message : String(err)}`
		});
	}

	// 204 (token deleted) legitimately carries no body — constructing a
	// Response with one would throw.
	const text = upstream.status === 204 ? null : await upstream.text();
	return new Response(text, {
		status: upstream.status,
		headers: {
			'content-type': upstream.headers.get('content-type') ?? 'application/json',
			...(upstream.headers.has('www-authenticate')
				? { 'www-authenticate': upstream.headers.get('www-authenticate')! }
				: {})
		}
	});
}
