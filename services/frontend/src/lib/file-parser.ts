import * as XLSX from 'xlsx';

export type CellValue = string | number | boolean | null;

export interface ParsedTable {
	headers: string[];
	rows: CellValue[][];
	fileName: string;
	sheetName?: string;
	sourceType: 'csv' | 'xlsx' | 'xls';
}

export class MultipleSheetsError extends Error {
	constructor(public sheetNames: string[]) {
		super(
			`Bitte nur Dateien mit genau einem Sheet hochladen. Gefundene Sheets: ${sheetNames.join(
				', '
			)}`
		);
		this.name = 'MultipleSheetsError';
	}
}

export class UnsupportedFileError extends Error {
	constructor(ext: string) {
		super(`Nicht unterstütztes Format: .${ext}. Erlaubt: .csv, .xlsx, .xls`);
		this.name = 'UnsupportedFileError';
	}
}

export class EmptyFileError extends Error {
	constructor() {
		super('Die Datei enthält keine Daten.');
		this.name = 'EmptyFileError';
	}
}

function getExtension(name: string): string {
	const idx = name.lastIndexOf('.');
	return idx >= 0 ? name.slice(idx + 1).toLowerCase() : '';
}

function rowsFromAoa(aoa: unknown[][]): { headers: string[]; rows: CellValue[][] } {
	const filtered = aoa.filter((r) => Array.isArray(r) && r.some((c) => c !== null && c !== ''));
	if (filtered.length === 0) throw new EmptyFileError();
	const rawHeaders = filtered[0] as unknown[];
	const headers = rawHeaders.map((h, i) =>
		h === null || h === undefined || h === '' ? `Spalte ${i + 1}` : String(h)
	);
	const width = headers.length;
	const rows: CellValue[][] = filtered.slice(1).map((r) => {
		const row: CellValue[] = [];
		for (let i = 0; i < width; i++) {
			const v = (r as unknown[])[i];
			if (v === undefined || v === null) row.push(null);
			else if (typeof v === 'number' || typeof v === 'boolean') row.push(v);
			else row.push(String(v));
		}
		return row;
	});
	return { headers, rows };
}

async function parseExcel(file: File, ext: 'xlsx' | 'xls'): Promise<ParsedTable> {
	const buffer = await file.arrayBuffer();
	const wb = XLSX.read(buffer, { type: 'array' });
	const sheetNames = wb.SheetNames;
	if (sheetNames.length !== 1) {
		throw new MultipleSheetsError(sheetNames);
	}
	const sheetName = sheetNames[0];
	const sheet = wb.Sheets[sheetName];
	const aoa = XLSX.utils.sheet_to_json<unknown[]>(sheet, {
		header: 1,
		defval: null,
		raw: true,
		blankrows: false
	});
	const { headers, rows } = rowsFromAoa(aoa);
	return { headers, rows, fileName: file.name, sheetName, sourceType: ext };
}

function parseCsvText(text: string): { headers: string[]; rows: CellValue[][] } {
	const lines: string[][] = [];
	let field = '';
	let row: string[] = [];
	let inQuotes = false;
	let i = 0;
	while (i < text.length) {
		const ch = text[i];
		if (inQuotes) {
			if (ch === '"') {
				if (text[i + 1] === '"') {
					field += '"';
					i += 2;
					continue;
				}
				inQuotes = false;
				i++;
				continue;
			}
			field += ch;
			i++;
			continue;
		}
		if (ch === '"') {
			inQuotes = true;
			i++;
			continue;
		}
		if (ch === ',' || ch === ';') {
			row.push(field);
			field = '';
			i++;
			continue;
		}
		if (ch === '\r') {
			i++;
			continue;
		}
		if (ch === '\n') {
			row.push(field);
			lines.push(row);
			field = '';
			row = [];
			i++;
			continue;
		}
		field += ch;
		i++;
	}
	if (field.length > 0 || row.length > 0) {
		row.push(field);
		lines.push(row);
	}
	const aoa = lines as unknown[][];
	return rowsFromAoa(aoa);
}

async function parseCsv(file: File): Promise<ParsedTable> {
	const text = await file.text();
	const { headers, rows } = parseCsvText(text);
	return { headers, rows, fileName: file.name, sourceType: 'csv' };
}

export async function parseFileToTable(file: File): Promise<ParsedTable> {
	const ext = getExtension(file.name);
	if (ext === 'csv') return parseCsv(file);
	if (ext === 'xlsx' || ext === 'xls') return parseExcel(file, ext);
	throw new UnsupportedFileError(ext || '?');
}
