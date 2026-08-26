import { describe, expect, it, vi, afterEach } from 'vitest';
import { waitForImageDescriptions } from './image-descriptions';

const BASE = 'http://cleaning:8001';
const HASH = 'abc123def456';

type StatusStep = { described: number; pending: number; job_status?: string };

/**
 * Stub the two cleaning endpoints. Status responses are consumed one per poll,
 * the last one repeating, so a test can describe a progression.
 */
function stubCleaning(steps: StatusStep[], gallery: unknown[]) {
	let call = 0;
	const spy = vi.fn(async (input: RequestInfo | URL) => {
		const url = String(input);
		if (url.includes('/v1/images/status/')) {
			const step = steps[Math.min(call++, steps.length - 1)];
			return new Response(JSON.stringify(step), { status: 200 });
		}
		if (url.includes('/images')) {
			return new Response(JSON.stringify({ images: gallery }), { status: 200 });
		}
		return new Response('not found', { status: 404 });
	});
	vi.stubGlobal('fetch', spy);
	return spy;
}

const ORIGINAL = [
	{ image_id: 'img_p082_i01', page: 82, alt_text: '', url: '/u/1', stored_path: '/s/1.png' }
];

const DESCRIBED = [
	{
		image_id: 'img_p082_i01',
		page: 82,
		alt_text: 'Taste 5 = links, Taste 0 = rechts',
		url: '/u/1'
	}
];

afterEach(() => {
	vi.unstubAllGlobals();
	vi.useRealTimers();
});

describe('waitForImageDescriptions', () => {
	it('returns the finished descriptions once nothing is pending', async () => {
		stubCleaning([{ described: 1, pending: 0 }], DESCRIBED);

		const result = await waitForImageDescriptions(BASE, 'wl', HASH, ORIGINAL);

		expect(result.images[0].alt_text).toBe('Taste 5 = links, Taste 0 = rechts');
		expect(result.complete).toBe(true);
	});

	it('keeps stored_path, which the gallery listing does not carry', async () => {
		stubCleaning([{ described: 1, pending: 0 }], DESCRIBED);

		const result = await waitForImageDescriptions(BASE, 'wl', HASH, ORIGINAL);

		expect(result.images[0].stored_path).toBe('/s/1.png');
	});

	it('skips the wait entirely for a document without images', async () => {
		const spy = stubCleaning([{ described: 0, pending: 0 }], []);

		const result = await waitForImageDescriptions(BASE, 'wl', HASH, []);

		expect(result.complete).toBe(true);
		expect(spy).not.toHaveBeenCalled();
	});

	it('skips the wait when no file hash is known', async () => {
		const spy = stubCleaning([{ described: 0, pending: 0 }], []);

		const result = await waitForImageDescriptions(BASE, 'wl', '', ORIGINAL);

		expect(result.images).toEqual(ORIGINAL);
		expect(spy).not.toHaveBeenCalled();
	});

	it('stops waiting when the job reports done with images still undescribed', async () => {
		// Vision failed for some images; more waiting cannot fix that, and
		// blocking the ingest for the full budget would help nobody.
		stubCleaning([{ described: 0, pending: 1, job_status: 'done' }], DESCRIBED);

		const result = await waitForImageDescriptions(BASE, 'wl', HASH, ORIGINAL);

		expect(result.complete).toBe(false);
	});

	it('reports incompleteness rather than failing the ingest', async () => {
		stubCleaning([{ described: 0, pending: 1, job_status: 'done' }], []);

		const result = await waitForImageDescriptions(BASE, 'wl', HASH, ORIGINAL);

		// Falls back to the original entries so the document still gets indexed.
		expect(result.images).toEqual(ORIGINAL);
		expect(result.complete).toBe(false);
	});

	it('gives up on an unreachable status endpoint instead of waiting out the budget', async () => {
		const spy = vi.fn(async () => {
			throw new Error('ECONNREFUSED');
		});
		vi.stubGlobal('fetch', spy);

		const result = await waitForImageDescriptions(BASE, 'wl', HASH, ORIGINAL);

		expect(result.images).toEqual(ORIGINAL);
		expect(result.complete).toBe(false);
		// 5 status attempts + the gallery fetch — not a 30 s wait against a
		// service that is plainly down.
		expect(spy).toHaveBeenCalledTimes(6);
	}, 30_000);

	it('rides out a transient status failure', async () => {
		let call = 0;
		vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
			const url = String(input);
			if (url.includes('/v1/images/status/')) {
				call += 1;
				if (call === 1) throw new Error('ECONNRESET');
				return new Response(JSON.stringify({ described: 1, pending: 0 }), { status: 200 });
			}
			return new Response(JSON.stringify({ images: DESCRIBED }), { status: 200 });
		}));

		const result = await waitForImageDescriptions(BASE, 'wl', HASH, ORIGINAL);

		expect(result.complete).toBe(true);
		expect(result.images[0].alt_text).toBe('Taste 5 = links, Taste 0 = rechts');
	}, 30_000);

	it('counts only images that actually came back with a description', async () => {
		stubCleaning([{ described: 1, pending: 0 }], [
			...DESCRIBED,
			{ image_id: 'img_p083_i00', page: 83, alt_text: '   ', url: '/u/2' }
		]);

		const result = await waitForImageDescriptions(BASE, 'wl', HASH, ORIGINAL);

		expect(result.described).toBe(1);
	});
});
