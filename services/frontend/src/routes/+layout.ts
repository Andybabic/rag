import { setUseCases } from '$lib/use-cases';
import type { LayoutLoad } from './$types';

/**
 * Hydrate the in-memory use-case registry from the server load output so the
 * synchronous helpers (getUseCaseBySlug/ByApiId) work app-wide. Runs before
 * child loads and components.
 */
export const load: LayoutLoad = async ({ data }) => {
	setUseCases(data?.useCases);
	return data;
};
