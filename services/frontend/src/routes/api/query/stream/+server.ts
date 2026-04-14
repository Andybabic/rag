import { SERVICES } from '$lib/server/services';
import type { RequestHandler } from './$types';

export const POST: RequestHandler = async ({ request }) => {
	const body = await request.json();

	const upstream = await fetch(`${SERVICES.evaluation}/v1/agent/query/stream`, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(body)
	});

	if (!upstream.ok || !upstream.body) {
		const text = await upstream.text();
		return new Response(text, { status: upstream.status });
	}

	// Forward the upstream stream untouched (NDJSON).
	return new Response(upstream.body, {
		status: 200,
		headers: {
			'Content-Type': 'application/x-ndjson',
			'Cache-Control': 'no-cache',
			'X-Accel-Buffering': 'no'
		}
	});
};
