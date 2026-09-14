/**
 * GET /api/v1/openapi.json — machine-readable description of the public API.
 *
 * Exempt from the bearer gate in `hooks.server.ts`: it is a schema, not data,
 * and a client generating code against the API should not need a credential
 * just to read the contract.
 */
import { forwardToPipeline } from '$lib/server/public-api';
import type { RequestHandler } from './$types';

export const GET: RequestHandler = ({ request }) =>
	forwardToPipeline(request, '/v1/public/openapi.json');
