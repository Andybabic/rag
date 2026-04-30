import type { PiiDetection, PiiType } from './pii-scanner';

export interface PlaceholderEntry {
	type: PiiType;
	original: string;
	placeholder: string;
}

export class PlaceholderMap {
	private counters: Partial<Record<PiiType, number>> = {};
	private byKey = new Map<string, string>();
	private entries: PlaceholderEntry[] = [];

	private keyOf(type: PiiType, original: string): string {
		return `${type}:${original.toLowerCase()}`;
	}

	placeholderFor(type: PiiType, original: string): string {
		const key = this.keyOf(type, original);
		const existing = this.byKey.get(key);
		if (existing) return existing;
		const next = (this.counters[type] ?? 0) + 1;
		this.counters[type] = next;
		const placeholder = `[${type}_${next}]`;
		this.byKey.set(key, placeholder);
		this.entries.push({ type, original, placeholder });
		return placeholder;
	}

	all(): PlaceholderEntry[] {
		return [...this.entries];
	}

	clear(): void {
		this.counters = {};
		this.byKey.clear();
		this.entries = [];
	}
}

export function detectionKey(d: Pick<PiiDetection, 'rowIdx' | 'colIdx' | 'start' | 'end' | 'type'>): string {
	return `${d.rowIdx}:${d.colIdx}:${d.start}:${d.end}:${d.type}`;
}

export interface AnonymizeOptions {
	detections: PiiDetection[];
	approvals: Set<string>; // keys from detectionKey()
	placeholders: PlaceholderMap;
}

/**
 * Returns a function that, given (rowIdx, colIdx, originalText), returns the
 * masked rendering. Detections that are not in `approvals` keep their original
 * text. The placeholder map is mutated as new entities are encountered.
 */
export function buildCellTransform(opts: AnonymizeOptions): (
	rowIdx: number,
	colIdx: number,
	text: string
) => string {
	const byCell = new Map<string, PiiDetection[]>();
	for (const d of opts.detections) {
		const key = `${d.rowIdx}:${d.colIdx}`;
		const list = byCell.get(key) ?? [];
		list.push(d);
		byCell.set(key, list);
	}

	return (rowIdx: number, colIdx: number, text: string): string => {
		const list = byCell.get(`${rowIdx}:${colIdx}`);
		if (!list || list.length === 0) return text;
		const ordered = [...list].sort((a, b) => b.start - a.start);
		let out = text;
		for (const d of ordered) {
			if (!opts.approvals.has(detectionKey(d))) continue;
			const placeholder = opts.placeholders.placeholderFor(d.type, d.text);
			out = out.slice(0, d.start) + placeholder + out.slice(d.end);
		}
		return out;
	};
}

export function approveAll(detections: PiiDetection[]): Set<string> {
	const s = new Set<string>();
	for (const d of detections) s.add(detectionKey(d));
	return s;
}
