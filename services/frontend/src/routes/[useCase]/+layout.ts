import { error } from '@sveltejs/kit';
import { getUseCaseBySlug } from '$lib/use-cases';

export const ssr = false;

export function load({ params }: { params: { useCase: string } }) {
	const uc = getUseCaseBySlug(params.useCase);
	if (!uc) {
		error(404, `Use Case '${params.useCase}' nicht gefunden.`);
	}
	return { useCase: uc };
}
