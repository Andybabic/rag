import { SERVICES } from '$lib/server/services';
import type { RequestHandler } from '@sveltejs/kit';

export const GET: RequestHandler = async ({ params }) => {
	const docPath = (params as Record<string, string>).path ?? '';
	const resp = await fetch(`${SERVICES.cleaning}/v1/documents/${docPath}`);

	if (!resp.ok) {
		return new Response('Document not found', { status: 404 });
	}

	const contentType = resp.headers.get('content-type') ?? 'application/octet-stream';
	const disposition = resp.headers.get('content-disposition') ?? '';

	return new Response(resp.body, {
		status: 200,
		headers: {
			'content-type': contentType,
			...(disposition ? { 'content-disposition': disposition } : {})
		}
	});
};
