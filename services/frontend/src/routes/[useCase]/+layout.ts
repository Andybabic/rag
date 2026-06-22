import { error } from '@sveltejs/kit';
import type { UseCaseDef } from '$lib/use-cases';
import type { LayoutLoad } from './$types';

export const ssr = false;

export const load: LayoutLoad = async ({ params, parent }) => {
	const { useCases } = (await parent()) as { useCases: UseCaseDef[] };
	const uc = (useCases ?? []).find((u) => u.slug === params.useCase);
	if (!uc) {
		error(404, `Use Case '${params.useCase}' nicht gefunden.`);
	}
	return { useCase: uc };
};
