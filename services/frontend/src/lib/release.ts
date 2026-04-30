import type { ParsedTable, CellValue } from './file-parser';
import type {
	AnonymizeMode,
	ChunkingStrategy,
	ReleaseSnapshot
} from './state.svelte';
import type { PiiDetection } from './pii-scanner';
import { buildCellTransform, PlaceholderMap } from './pii-anonymizer';
import { uploadFile } from './api';

export interface BuildSnapshotInput {
	table: ParsedTable;
	mode: AnonymizeMode;
	strategy: ChunkingStrategy;
	detections: PiiDetection[];
	approvals: Set<string>;
	placeholders: PlaceholderMap;
}

function cellToText(v: CellValue): string {
	if (v === null || v === undefined) return '';
	if (typeof v === 'boolean') return v ? 'true' : 'false';
	return String(v);
}

/**
 * Materializes the current view of the table into an immutable snapshot.
 * If `mode` is 'off', the snapshot contains the original strings. Otherwise
 * each cell is run through the cell transform so the snapshot rows are exactly
 * what the user sees in the preview.
 *
 * The returned object carries `releasedAt` — the marker the import gate
 * checks before sending anything outbound.
 */
export function buildSnapshot(input: BuildSnapshotInput): ReleaseSnapshot {
	const { table, mode, strategy, detections, approvals, placeholders } = input;
	const transform =
		mode === 'off'
			? null
			: buildCellTransform({ detections, approvals, placeholders });

	const rows: string[][] = table.rows.map((row, r) =>
		table.headers.map((_, c) => {
			const original = cellToText(row[c]);
			if (!transform) return original;
			return transform(r, c, original);
		})
	);

	return {
		fileName: table.fileName,
		sourceType: table.sourceType,
		headers: [...table.headers],
		rows,
		rowCount: rows.length,
		columnCount: table.headers.length,
		anonymized: mode !== 'off',
		anonymizeMode: mode,
		detectionTotal: detections.length,
		detectionApproved: approvals.size,
		chunkingStrategy: strategy,
		releasedAt: new Date().toISOString()
	};
}

function csvField(s: string): string {
	if (s.includes('"') || s.includes(',') || s.includes('\n') || s.includes('\r')) {
		return `"${s.replace(/"/g, '""')}"`;
	}
	return s;
}

function snapshotToCsv(s: ReleaseSnapshot): string {
	const lines = [s.headers.map(csvField).join(',')];
	for (const row of s.rows) lines.push(row.map(csvField).join(','));
	return lines.join('\n');
}

function snapshotToCsvFile(s: ReleaseSnapshot): File {
	const csv = snapshotToCsv(s);
	const baseName = s.fileName.replace(/\.[^.]+$/, '');
	const suffix = s.anonymized ? '.released-anon' : '.released';
	const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' });
	return new File([blob], `${baseName}${suffix}.csv`, { type: 'text/csv' });
}

/**
 * Build per-row chunks. Each row becomes one chunk like
 *   "Header1: value1\nHeader2: value2\n..."
 * Empty values are skipped to avoid noise in embeddings.
 */
function snapshotToRowChunks(s: ReleaseSnapshot): Array<{
	text: string;
	metadata: Record<string, unknown>;
}> {
	return s.rows.map((row, i) => {
		const lines: string[] = [];
		for (let c = 0; c < s.headers.length; c++) {
			const value = row[c];
			if (value === undefined || value === null || value === '') continue;
			lines.push(`${s.headers[c]}: ${value}`);
		}
		return {
			text: lines.join('\n'),
			metadata: {
				row_index: i + 1,
				file_name: s.fileName,
				anonymized: s.anonymized,
				source_type: s.sourceType
			}
		};
	}).filter((c) => c.text.length > 0);
}

async function uploadTableChunks(
	snapshot: ReleaseSnapshot,
	useCase: string
): Promise<{ chunks?: number; storedPath?: string }> {
	const file = snapshotToCsvFile(snapshot);
	const chunks = snapshotToRowChunks(snapshot);

	const form = new FormData();
	form.append('file', file);
	form.append('use_case', useCase);
	form.append('anonymized', snapshot.anonymized ? '1' : '0');
	form.append('chunks', JSON.stringify(chunks));

	const resp = await fetch('/api/ingest/table', { method: 'POST', body: form });
	if (!resp.ok) {
		const detail = await resp.text();
		throw new Error(detail || `Import fehlgeschlagen (HTTP ${resp.status}).`);
	}
	const result = await resp.json();
	return {
		chunks: typeof result?.chunks === 'number' ? result.chunks : undefined,
		storedPath: typeof result?.stored_path === 'string' ? result.stored_path : undefined
	};
}

/**
 * Import gate. The ONLY function in this codebase that sends table data over
 * the network. Refuses to run unless given a snapshot that was finalized via
 * buildSnapshot() (i.e. has releasedAt). All other code paths read from
 * `app.parsedTable` locally and never serialize it for transport.
 */
export async function importSnapshot(
	snapshot: ReleaseSnapshot,
	useCase: string
): Promise<{ chunks?: number; storedPath?: string }> {
	if (!snapshot || !snapshot.releasedAt) {
		throw new Error('Import blockiert: Snapshot wurde nicht freigegeben.');
	}
	if (snapshot.rows.length === 0) {
		throw new Error('Import abgebrochen: keine Zeilen im Snapshot.');
	}
	if (snapshot.chunkingStrategy === 'per-row') {
		return uploadTableChunks(snapshot, useCase);
	}
	// whole-file: send synthesized CSV through the standard ingest pipeline
	const file = snapshotToCsvFile(snapshot);
	const result = await uploadFile(file, useCase);
	return {
		chunks: typeof result?.chunks === 'number' ? result.chunks : undefined,
		storedPath: typeof result?.stored_path === 'string' ? result.stored_path : undefined
	};
}

/** Convenience for the UI: serialize a snapshot for human inspection / download. */
export function snapshotToCsvString(s: ReleaseSnapshot): string {
	return snapshotToCsv(s);
}

/** For the UI: peek what the per-row chunks would look like. */
export function snapshotPreviewChunks(s: ReleaseSnapshot, max = 3): string[] {
	return snapshotToRowChunks(s).slice(0, max).map((c) => c.text);
}
