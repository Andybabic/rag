import { SERVICES } from '$lib/server/services';
import type { UseCaseDef } from '$lib/use-cases';
import type { LayoutServerLoad } from './$types';

/**
 * Expose the authenticated user and the DB-backed use-case list to every page.
 * Use cases are fetched straight from the evaluation service (server-to-server,
 * no auth hook in between); only loaded once a user is logged in.
 *
 * Access control: admins see every use case; a regular user only sees the use
 * cases explicitly assigned to them (see the Benutzer dashboard). Filtering
 * here also gates route access, because `[useCase]/+layout.ts` 404s on any
 * slug that isn't in this list.
 */
export const load: LayoutServerLoad = async ({ locals, fetch }) => {
	let useCases: UseCaseDef[] = [];
	if (locals.user) {
		try {
			const resp = await fetch(`${SERVICES.evaluation}/v1/use-cases`);
			if (resp.ok) {
				const body = await resp.json();
				useCases = body.use_cases ?? [];
			}
		} catch {
			useCases = [];
		}

		if (locals.user.role !== 'admin') {
			try {
				const resp = await fetch(
					`${SERVICES.evaluation}/v1/auth/users/${encodeURIComponent(locals.user.username)}/use-cases`
				);
				if (resp.ok) {
					const body = await resp.json();
					const allowed = new Set<string>(body.use_cases ?? []);
					useCases = useCases.filter((uc) => allowed.has(uc.apiId));
				} else {
					// On failure, deny rather than leak access to everything.
					useCases = [];
				}
			} catch {
				useCases = [];
			}
		}
	}
	return { user: locals.user, useCases };
};
