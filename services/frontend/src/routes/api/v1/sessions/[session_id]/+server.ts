/** GET /api/v1/sessions/{session_id} — the whole conversation as JSON. */
import { forwardToPipeline } from '$lib/server/public-api';
import type { RequestHandler } from './$types';

export const GET: RequestHandler = ({ request, params }) =>
	forwardToPipeline(request, `/v1/public/sessions/${encodeURIComponent(params.session_id)}`);
