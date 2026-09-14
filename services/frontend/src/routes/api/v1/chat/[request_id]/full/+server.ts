/** GET /api/v1/chat/{request_id}/full — untrimmed pipeline result incl. trace. */
import { forwardToPipeline } from '$lib/server/public-api';
import type { RequestHandler } from './$types';

export const GET: RequestHandler = ({ request, params }) =>
	forwardToPipeline(request, `/v1/public/chat/${encodeURIComponent(params.request_id)}/full`);
