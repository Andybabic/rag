import { redirect } from '@sveltejs/kit';
import type { UseCaseDef } from '$lib/use-cases';
import type { PageLoad } from './$types';

export const ssr = false;

/**
 * Land on the first available use case instead of a hard-coded one — the
 * default use cases can be deleted, and a user may only be assigned a subset.
 * If none are available, fall through to +page.svelte which shows a hint.
 */
export const load: PageLoad = async ({ parent }) => {
	const { useCases } = (await parent()) as { useCases: UseCaseDef[] };
	const first = (useCases ?? [])[0];
	if (first) {
		redirect(307, `/${first.slug}`);
	}
	return {};
};
