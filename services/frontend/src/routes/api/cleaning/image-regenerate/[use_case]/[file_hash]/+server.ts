import { json } from '@sveltejs/kit';
import { SERVICES } from '$lib/server/services';
import type { RequestHandler } from './$types';

export const POST: RequestHandler = async ({ params, url }) => {
	// force=true also re-describes images that already have a description —
	// used after the vision prompt changes, where the existing texts are not
	// missing but outdated.
	const force = url.searchParams.get('force') === 'true';
	const target = new URL(
		`${SERVICES.cleaning}/v1/images/regenerate/${encodeURIComponent(params.use_case)}/${encodeURIComponent(params.file_hash)}`
	);
	if (force) target.searchParams.set('force', 'true');

	const resp = await fetch(target, { method: 'POST' });
	return json(await resp.json().catch(() => ({})), { status: resp.status });
};
