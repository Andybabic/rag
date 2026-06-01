import { json } from '@sveltejs/kit';
import { SERVICES } from '$lib/server/services';
import type { RequestHandler } from '@sveltejs/kit';

export const GET: RequestHandler = async ({ params }) => {
	const useCase = (params as Record<string, string>).use_case ?? '';
	const fileHash = (params as Record<string, string>).file_hash ?? '';
	const resp = await fetch(
		`${SERVICES.cleaning}/v1/documents/${encodeURIComponent(useCase)}/${encodeURIComponent(fileHash)}/images`
	);
	if (!resp.ok) {
		return json({ images: [] }, { status: resp.status });
	}
	const data = await resp.json();
	return json({ images: data.images ?? [] });
};
