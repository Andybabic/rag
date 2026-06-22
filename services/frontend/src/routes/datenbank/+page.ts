import type { UseCaseDef } from '$lib/use-cases';
import type { PageLoad } from './$types';

export const ssr = false;

export const load: PageLoad = async ({ parent }) => {
	const { useCases } = (await parent()) as { useCases: UseCaseDef[] };
	return { useCases: useCases ?? [] };
};
