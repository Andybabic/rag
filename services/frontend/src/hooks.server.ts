/**
 * Auth gate for the whole app.
 *
 * - Resolves the session cookie into `locals.user` on every request.
 * - Public paths (login page + login API + health) are always allowed.
 * - /api/v1/* is the external pipeline API: no session, bearer token instead.
 *   The token itself is verified downstream by the evaluation service, which
 *   owns the token store; the gate here only rejects requests that carry no
 *   credential at all, and adds CORS so browser clients can call it.
 * - Everything else requires a valid session; unauthenticated browser
 *   navigations are redirected to /login, API calls get 401.
 * - Admin-only paths (/admin and /api/admin) additionally require role=admin;
 *   non-admins get redirected to "/" (pages) or 403 (API).
 */
import { redirect, type Handle } from '@sveltejs/kit';
import { env } from '$env/dynamic/private';
import { Agent, setGlobalDispatcher } from 'undici';
import { SESSION_COOKIE, verifySession } from '$lib/server/auth';

// Node's global fetch (undici) aborts after ~300s waiting for response headers.
// Ingesting a large/scanned PDF (MinerU OCR + image analysis) legitimately
// takes longer, so the proxy fetch to the cleaning service failed with an
// opaque "TypeError: fetch failed". Raise the header/body timeouts so long
// ingestion calls complete instead of being killed mid-flight. Configurable
// via FETCH_TIMEOUT_MS (default 30 min); connect timeout stays short so an
// unreachable service still fails fast.
const FETCH_TIMEOUT_MS = Number(env.FETCH_TIMEOUT_MS ?? '') || 30 * 60 * 1000;
setGlobalDispatcher(
	new Agent({
		headersTimeout: FETCH_TIMEOUT_MS,
		bodyTimeout: FETCH_TIMEOUT_MS,
		connectTimeout: 10_000
	})
);

// Login is a form action on the public /login page; logout requires a session.
const PUBLIC_PATHS = new Set(['/login', '/health', '/api/health']);

// Externally documented API surface, proxied to the evaluation service.
const PUBLIC_API_PREFIX = '/api/v1/';

// The API's own description is a contract, not data. Requiring a credential to
// read it would stop a client from generating code against the API before it
// has one, and reveals nothing a caller could not learn by reading the docs.
const PUBLIC_API_OPEN_PATHS = new Set(['/api/v1/openapi.json']);

// Bearer tokens are never sent automatically by a browser, so a wide origin
// here cannot be abused the way it could with cookie auth. Narrow it with
// API_CORS_ORIGIN if a deployment wants to.
const CORS_HEADERS = {
	'Access-Control-Allow-Origin': env.API_CORS_ORIGIN ?? '*',
	'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
	'Access-Control-Allow-Headers': 'authorization, content-type',
	'Access-Control-Max-Age': '86400'
};

function jsonError(status: number, error: string, detail: string, extra: HeadersInit = {}) {
	return new Response(JSON.stringify({ error, detail }), {
		status,
		headers: { 'content-type': 'application/json', ...CORS_HEADERS, ...extra }
	});
}

function isPublic(pathname: string): boolean {
	if (PUBLIC_PATHS.has(pathname)) return true;
	// SvelteKit internal assets + favicon
	if (pathname.startsWith('/_app/') || pathname === '/favicon.svg') return true;
	return false;
}

function isAdminPath(pathname: string): boolean {
	return pathname === '/admin' || pathname.startsWith('/admin/') || pathname.startsWith('/api/admin');
}

export const handle: Handle = async ({ event, resolve }) => {
	const token = event.cookies.get(SESSION_COOKIE);
	event.locals.user = verifySession(token);

	const { pathname } = event.url;
	const isApi = pathname.startsWith('/api/');

	if (pathname.startsWith(PUBLIC_API_PREFIX)) {
		if (event.request.method === 'OPTIONS') {
			return new Response(null, { status: 204, headers: CORS_HEADERS });
		}
		const auth = event.request.headers.get('authorization') ?? '';
		const needsToken = !PUBLIC_API_OPEN_PATHS.has(pathname);
		if (needsToken && (!auth.toLowerCase().startsWith('bearer ') || !auth.slice(7).trim())) {
			return jsonError(401, 'unauthorized', 'Missing bearer token', {
				'WWW-Authenticate': 'Bearer'
			});
		}
		const response = await resolve(event);
		for (const [key, value] of Object.entries(CORS_HEADERS)) {
			response.headers.set(key, value);
		}
		return response;
	}

	if (!isPublic(pathname)) {
		if (!event.locals.user) {
			if (isApi) {
				return new Response(JSON.stringify({ error: 'Unauthorized' }), {
					status: 401,
					headers: { 'content-type': 'application/json' }
				});
			}
			redirect(303, `/login?next=${encodeURIComponent(pathname)}`);
		}

		if (isAdminPath(pathname) && event.locals.user.role !== 'admin') {
			if (isApi) {
				return new Response(JSON.stringify({ error: 'Forbidden' }), {
					status: 403,
					headers: { 'content-type': 'application/json' }
				});
			}
			redirect(303, '/');
		}
	}

	return resolve(event);
};
