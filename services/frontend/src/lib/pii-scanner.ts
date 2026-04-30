import type { ParsedTable, CellValue } from './file-parser';

// OpenAI's privacy-filter PII model. Token-classification with 8 categories:
// private_person, private_email, private_phone, private_address, private_url,
// private_date, account_number, secret. ONNX + transformers.js compatible.
// Note: 1.5B params total / 50M active. First load is ~hundreds of MB and gets
// cached in browser IndexedDB. WebGPU is recommended for inference speed; on
// browsers without WebGPU support we fall back to WASM (slower).
let MODEL_ID = 'openai/privacy-filter';

export type PiiType =
	| 'NAME'
	| 'ORG'
	| 'LOC'
	| 'ADDRESS'
	| 'EMAIL'
	| 'PHONE'
	| 'URL'
	| 'DATE'
	| 'ACCOUNT'
	| 'SECRET'
	| 'MISC';

export interface PiiDetection {
	rowIdx: number;
	colIdx: number;
	start: number;
	end: number;
	type: PiiType;
	text: string;
	score: number;
}

export interface ScanProgress {
	processed: number;
	total: number;
	stage: 'loading-model' | 'scanning' | 'done';
}

let pipelinePromise: Promise<unknown> | null = null;

export function setPiiModel(id: string): void {
	if (id !== MODEL_ID) {
		MODEL_ID = id;
		pipelinePromise = null;
	}
}

export function getPiiModel(): string {
	return MODEL_ID;
}

function hasWebGPU(): boolean {
	return typeof navigator !== 'undefined' && 'gpu' in navigator;
}

type PipeFn = (
	input: string,
	opts: Record<string, unknown>
) => Promise<unknown>;

async function smokeTest(pipe: PipeFn, label: string): Promise<number> {
	const probe = 'My name is Harry Potter and my email is harry@hogwarts.edu.';
	try {
		const out = await pipe(probe, { aggregation_strategy: 'simple' });
		const len = Array.isArray(out) ? out.length : 0;
		console.info(`[pii] smoke test (${label})`, { entities: len });
		return len;
	} catch (err) {
		console.warn(`[pii] smoke test (${label}) failed`, err);
		return 0;
	}
}

async function tryBuildPipeline(opts: Record<string, unknown>) {
	const { pipeline, env } = await import('@huggingface/transformers');
	env.allowLocalModels = false;
	const p = (await pipeline('token-classification', MODEL_ID, opts)) as unknown as PipeFn;
	return p;
}

async function loadPipeline() {
	if (!pipelinePromise) {
		pipelinePromise = (async () => {
			// Try recommended config first (q4 + webgpu when available).
			const primary: Record<string, unknown> = { dtype: 'q4' };
			if (hasWebGPU()) primary.device = 'webgpu';
			console.info('[pii] loading pipeline', { model: MODEL_ID, opts: primary });
			let p: PipeFn;
			try {
				p = await tryBuildPipeline(primary);
				const found = await smokeTest(p, 'q4');
				if (found > 0) return p;
				console.warn('[pii] q4 returned 0 entities — falling back to fp32 wasm');
			} catch (err) {
				console.warn('[pii] primary pipeline failed, falling back to fp32 wasm', err);
			}
			// Fallback: full precision on WASM. Slower but most compatible.
			p = await tryBuildPipeline({ dtype: 'fp32' });
			await smokeTest(p, 'fp32');
			return p;
		})();
	}
	return pipelinePromise;
}

export async function preloadModel(): Promise<void> {
	await loadPipeline();
}

interface NerEntity {
	entity_group?: string;
	entity?: string;
	word?: string;
	start?: number;
	end?: number;
	score?: number;
}

function mapEntityToType(entity: string | undefined): PiiType | null {
	if (!entity) return null;
	// Strip BIOES prefix ("B-private_person" -> "private_person").
	const raw = entity.replace(/^[BILES]-/i, '');
	const lower = raw.toLowerCase();

	if (lower === 'private_person') return 'NAME';
	if (lower === 'private_email') return 'EMAIL';
	if (lower === 'private_phone') return 'PHONE';
	if (lower === 'private_address') return 'ADDRESS';
	if (lower === 'private_url') return 'URL';
	if (lower === 'private_date') return 'DATE';
	if (lower === 'account_number') return 'ACCOUNT';
	if (lower === 'secret') return 'SECRET';

	// Generic NER fallbacks for swap-in models (PER/ORG/LOC/MISC).
	const e = raw.toUpperCase();
	if (e === 'PER' || e === 'PERSON') return 'NAME';
	if (e === 'ORG' || e.includes('ORGANIZATION')) return 'ORG';
	if (e === 'LOC' || e === 'GPE' || e.includes('LOCATION')) return 'LOC';
	if (e === 'MISC') return 'MISC';
	return null;
}

async function nerDetections(
	text: string,
	pipe: unknown
): Promise<Array<Omit<PiiDetection, 'rowIdx' | 'colIdx'>>> {
	if (!text.trim()) return [];
	const fn = pipe as (
		input: string,
		opts: Record<string, unknown>
	) => Promise<NerEntity[] | NerEntity[][]>;

	const raw = await fn(text, {
		aggregation_strategy: 'simple',
		ignore_labels: ['O']
	});
	const list: NerEntity[] = Array.isArray(raw) ? (raw as NerEntity[]).flat() : [];

	const out: Array<Omit<PiiDetection, 'rowIdx' | 'colIdx'>> = [];

	// transformers.js with aggregation_strategy:'simple' often omits start/end
	// offsets and only returns `word` (with a leading space from BPE).
	// Reconstruct char offsets via indexOf, walking with a cursor so that
	// repeated tokens (e.g. "Müller" twice) match in document order.
	let cursor = 0;

	for (const ent of list) {
		const type = mapEntityToType(ent.entity_group ?? ent.entity);
		if (type === null) continue;

		let start = ent.start;
		let end = ent.end;

		if (start === undefined || end === undefined) {
			const needle = (ent.word ?? '').replace(/##/g, '').replace(/^\s+|\s+$/g, '');
			if (needle.length === 0) continue;
			let idx = text.indexOf(needle, cursor);
			if (idx === -1) idx = text.indexOf(needle);
			if (idx === -1) continue;
			start = idx;
			end = idx + needle.length;
		}

		if (end <= start) continue;
		cursor = end;

		out.push({
			start,
			end,
			type,
			text: text.slice(start, end),
			score: ent.score ?? 1
		});
	}
	return out;
}

function cellToText(v: CellValue): string {
	if (v === null || v === undefined) return '';
	return String(v);
}

export async function scanTable(
	table: ParsedTable,
	onProgress?: (p: ScanProgress) => void
): Promise<PiiDetection[]> {
	onProgress?.({ processed: 0, total: 0, stage: 'loading-model' });
	const pipe = await loadPipeline();

	const total = table.rows.length * table.headers.length;
	onProgress?.({ processed: 0, total, stage: 'scanning' });

	const detections: PiiDetection[] = [];
	let processed = 0;

	for (let r = 0; r < table.rows.length; r++) {
		const row = table.rows[r];
		for (let c = 0; c < table.headers.length; c++) {
			const text = cellToText(row[c]);
			processed++;
			if (text.length === 0) {
				if (processed % 25 === 0)
					onProgress?.({ processed, total, stage: 'scanning' });
				continue;
			}

			const hits = await nerDetections(text, pipe);
			for (const d of hits) {
				detections.push({ rowIdx: r, colIdx: c, ...d });
			}
			if (processed % 25 === 0) {
				onProgress?.({ processed, total, stage: 'scanning' });
				// Yield to the browser so the UI thread can repaint and stay
				// responsive during long scans.
				await new Promise((resolve) => setTimeout(resolve, 0));
			}
		}
	}

	onProgress?.({ processed: total, total, stage: 'done' });
	return detections;
}
