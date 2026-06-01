import { SERVICES } from '$lib/server/services';
import type { RequestHandler } from '@sveltejs/kit';

export const GET: RequestHandler = async ({ params }) => {
	const useCase = (params as Record<string, string>).use_case ?? '';
	const imageId = (params as Record<string, string>).image_id ?? '';
	const resp = await fetch(
		`${SERVICES.cleaning}/v1/images/${encodeURIComponent(useCase)}/${encodeURIComponent(imageId)}`
	);

	if (!resp.ok) {
		return new Response('Image not found', { status: 404 });
	}

	const contentType = resp.headers.get('content-type') ?? 'image/jpeg';
	return new Response(resp.body, {
		status: 200,
		headers: { 'content-type': contentType, 'cache-control': 'public, max-age=3600' }
	});
};
