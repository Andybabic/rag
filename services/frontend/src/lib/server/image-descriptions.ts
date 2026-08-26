/**
 * Wait for a document's image descriptions before it gets chunked.
 *
 * The cleaning service answers `/v1/clean` immediately and generates alt-texts
 * in the background, so its image list carries `alt_text: ""`. Chunking that
 * list bakes the empty strings into the Qdrant payload permanently — the
 * finished descriptions only ever reach the sidecar the gallery reads. Images
 * then display in an answer but contribute nothing to it, and an agent reports
 * "not in the documents" while the answer sits in a floor plan.
 *
 * The vision cost is paid either way; this only fixes the ordering.
 */

const POLL_INTERVAL_MS = 3000;

/** Consecutive unreadable status responses before giving up. Bridges a restart
 *  or a blip without holding the ingest open against a service that is simply
 *  down — waiting out the full budget would help nobody there. */
const MAX_CONSECUTIVE_FAILURES = 5;

/** Per-image share of the wait budget. Vision on a busy endpoint runs a few
 *  seconds per image, and they are described concurrently. */
const BUDGET_PER_IMAGE_MS = 6000;
const BUDGET_FLOOR_MS = 30_000;

/** Hard ceiling, overridable via IMAGE_DESCRIPTION_TIMEOUT_MS. A document with
 *  hundreds of images must not hold the ingest request open indefinitely. */
const BUDGET_CEILING_MS = Number(
	process.env.IMAGE_DESCRIPTION_TIMEOUT_MS ?? 15 * 60_000
);

export type ImageEntry = {
	image_id: string;
	page?: unknown;
	alt_text?: string;
	url?: string;
	stored_path?: string;
};

export type DescriptionWait = {
	/** Image metadata to hand to the structuring service. */
	images: ImageEntry[];
	/** False when the budget ran out — the caller still ingests, with the
	 *  descriptions it has, rather than failing the upload. */
	complete: boolean;
	described: number;
	total: number;
};

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

async function getJson(url: string): Promise<Record<string, unknown> | null> {
	try {
		const resp = await fetch(url);
		if (!resp.ok) return null;
		return (await resp.json()) as Record<string, unknown>;
	} catch {
		return null;
	}
}

/**
 * Merge freshly described images over the clean response's entries.
 *
 * The gallery listing has the alt-texts but not `stored_path`, which only the
 * clean response carries — so neither side alone is complete.
 */
function mergeDescriptions(
	original: ImageEntry[],
	described: ImageEntry[]
): ImageEntry[] {
	const byId = new Map(described.map((img) => [img.image_id, img]));
	return original.map((img) => {
		const fresh = byId.get(img.image_id);
		if (!fresh) return img;
		return { ...img, ...fresh, stored_path: img.stored_path ?? fresh.stored_path };
	});
}

export async function waitForImageDescriptions(
	cleaningBase: string,
	useCase: string,
	fileHash: string,
	original: ImageEntry[]
): Promise<DescriptionWait> {
	const total = original.length;
	if (!fileHash || total === 0) {
		return { images: original, complete: true, described: 0, total };
	}

	const budgetMs = Math.min(
		BUDGET_CEILING_MS,
		Math.max(BUDGET_FLOOR_MS, total * BUDGET_PER_IMAGE_MS)
	);
	const deadline = Date.now() + budgetMs;

	const statusUrl =
		`${cleaningBase}/v1/images/status/${encodeURIComponent(useCase)}/` +
		encodeURIComponent(fileHash);

	let described = 0;
	let complete = false;
	let failures = 0;

	// Status is derived from the stored sidecars rather than in-memory job
	// state, so a cleaning-service restart mid-ingest does not strand this loop.
	for (;;) {
		const status = await getJson(statusUrl);
		if (status) {
			failures = 0;
			described = Number(status.described ?? 0);
			const pending = Number(status.pending ?? 0);
			// job_status "done" with pending>0 means some images genuinely failed
			// to describe; waiting longer cannot fix that.
			if (pending === 0 || status.job_status === 'done') {
				complete = pending === 0;
				break;
			}
		} else if (++failures >= MAX_CONSECUTIVE_FAILURES) {
			break;
		}
		if (Date.now() >= deadline) break;
		await sleep(Math.min(POLL_INTERVAL_MS, Math.max(0, deadline - Date.now())));
	}

	const gallery = await getJson(
		`${cleaningBase}/v1/documents/${encodeURIComponent(useCase)}/` +
			`${encodeURIComponent(fileHash)}/images`
	);
	const fresh = (gallery?.images ?? []) as ImageEntry[];
	if (!fresh.length) {
		return { images: original, complete, described, total };
	}

	return {
		images: mergeDescriptions(original, fresh),
		complete,
		described: fresh.filter((i) => (i.alt_text ?? '').trim()).length,
		total
	};
}
