/** GET /api/v1/chat/{request_id}/result — the answer, or 409 while unfinished. */
import { forwardToPipeline } from '$lib/server/public-api';
import type { RequestHandler } from './$types';

export const GET: RequestHandler = ({ request, params }) =>
	forwardToPipeline(request, `/v1/public/chat/${encodeURIComponent(params.request_id)}/result`);
