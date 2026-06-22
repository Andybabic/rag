import { SERVICES } from '$lib/server/services';
import type { UseCaseDef } from '$lib/use-cases';
import type { LayoutServerLoad } from './$types';

/**
 * Expose the authenticated user and the DB-backed use-case list to every page.
 * Use cases are fetched straight from the evaluation service (server-to-server,
 * no auth hook in between); only loaded once a user is logged in.
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
	}
	return { user: locals.user, useCases };
};
