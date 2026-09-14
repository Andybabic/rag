/** GET /api/v1/me — what the presented token is allowed to do. */
import { forwardToPipeline } from '$lib/server/public-api';
import type { RequestHandler } from './$types';

export const GET: RequestHandler = ({ request }) => forwardToPipeline(request, '/v1/public/me');
