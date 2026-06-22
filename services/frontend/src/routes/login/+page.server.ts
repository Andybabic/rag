import { fail, redirect } from '@sveltejs/kit';
import { SERVICES } from '$lib/server/services';
import { createSession, SESSION_COOKIE, SESSION_MAX_AGE } from '$lib/server/auth';
import type { Actions, PageServerLoad } from './$types';

/** Already logged in? Skip the login page. */
export const load: PageServerLoad = async ({ locals, url }) => {
	if (locals.user) {
		redirect(303, url.searchParams.get('next') || '/');
	}
	return {};
};

function safeNext(next: string | null): string {
	// Only allow same-site relative paths to avoid open-redirects.
	if (next && next.startsWith('/') && !next.startsWith('//')) return next;
	return '/';
}

export const actions: Actions = {
	default: async ({ request, cookies, fetch, url }) => {
		const data = await request.formData();
		const username = String(data.get('username') ?? '').trim();
		const password = String(data.get('password') ?? '');
		const next = safeNext(url.searchParams.get('next'));

		if (!username || !password) {
			return fail(400, { username, error: 'Bitte Benutzername und Passwort eingeben.' });
		}

		let resp: Response;
		try {
			resp = await fetch(`${SERVICES.evaluation}/v1/auth/login`, {
				method: 'POST',
				headers: { 'content-type': 'application/json' },
				body: JSON.stringify({ username, password })
			});
		} catch {
			return fail(502, { username, error: 'Authentifizierungsdienst nicht erreichbar.' });
		}

		if (resp.status === 401) {
			return fail(401, { username, error: 'Benutzername oder Passwort ist falsch.' });
		}
		if (!resp.ok) {
			return fail(502, { username, error: 'Anmeldung fehlgeschlagen. Bitte erneut versuchen.' });
		}

		const user = await resp.json();
		const token = createSession({ username: user.username, role: user.role });
		cookies.set(SESSION_COOKIE, token, {
			path: '/',
			httpOnly: true,
			sameSite: 'lax',
			secure: url.protocol === 'https:',
			maxAge: SESSION_MAX_AGE
		});

		redirect(303, next);
	}
};
