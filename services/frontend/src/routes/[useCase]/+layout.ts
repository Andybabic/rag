import { redirect } from '@sveltejs/kit';
import type { UseCaseDef } from '$lib/use-cases';
import type { LayoutLoad } from './$types';

export const ssr = false;

export const load: LayoutLoad = async ({ params, parent }) => {
	const { useCases } = (await parent()) as { useCases: UseCaseDef[] };
	const list = useCases ?? [];
	const uc = list.find((u) => u.slug === params.useCase);
	if (!uc) {
		// The requested use case doesn't exist (deleted or not accessible to this
		// user). Bounce to the first available one instead of a dead-end 404; if
		// none are available, go to "/" which shows a friendly hint.
		const first = list[0];
		redirect(307, first ? `/${first.slug}` : '/');
	}
	return { useCase: uc };
};
