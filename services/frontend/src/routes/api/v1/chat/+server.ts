/**
 * POST /api/v1/chat — start a pipeline request.
 *
 * Body: { use_case, query, images?, session_id?, role?, history?, config? }
 * Returns 202 with the request_id used to poll for the answer.
 */
import { forwardToPipeline } from '$lib/server/public-api';
import type { RequestHandler } from './$types';

export const POST: RequestHandler = ({ request }) =>
	forwardToPipeline(request, '/v1/public/chat');
