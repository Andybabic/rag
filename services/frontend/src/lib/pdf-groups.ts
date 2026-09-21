/**
 * Group split single-page PDFs that belong to the same Einstellblatt.
 *
 * Shop-floor exports often look like ``Einstellblatt 3-1.pdf`` +
 * ``Einstellblatt 3-2.pdf``. Ingesting them separately splits tables across
 * documents; merging them first keeps one cited file with real page numbers.
 */

export interface PdfPage {
	path: string;
	fileName: string;
	page: number;
}

export interface PdfGroup {
	/** Display / ingest filename, e.g. ``Einstellblatt 3.pdf``. */
	fileName: string;
	/** Source files in page order. Length 1 → upload as-is. */
	pages: PdfPage[];
}

const PAGE_SUFFIX = /^(.+?)\s*-\s*(\d+)$/;

function fileNameOf(path: string): string {
	return path.split('/').pop() ?? path;
}

function stemOf(fileName: string): { stem: string; page: number | null; ext: string } {
	const extMatch = fileName.match(/(\.[^.]+)$/);
	const ext = extMatch ? extMatch[1] : '';
	const base = ext ? fileName.slice(0, -ext.length) : fileName;
	const m = base.match(PAGE_SUFFIX);
	if (!m) return { stem: base, page: null, ext };
	return { stem: m[1].trim(), page: parseInt(m[2], 10), ext };
}

/**
 * Cluster PDF paths. ``Einstellblatt 3-1.pdf`` + ``3-2.pdf`` become one group;
 * a lone ``Einstellblatt 1.pdf`` stays a group of one.
 *
 * A complete ``Einstellblatt 3.pdf`` next to split pages is left alone so the
 * merged file is named ``Einstellblatt 3 (Seiten).pdf``.
 */
export function groupPdfPages(paths: string[]): PdfGroup[] {
	const taken = new Set<string>();
	const buckets = new Map<string, { stem: string; ext: string; pages: PdfPage[] }>();
	const standalone: PdfGroup[] = [];

	for (const path of paths) {
		const fileName = fileNameOf(path);
		const { stem, page, ext } = stemOf(fileName);
		if (page == null) {
			standalone.push({ fileName, pages: [{ path, fileName, page: 1 }] });
			taken.add(fileName.toLowerCase());
			continue;
		}
		const key = `${stem.toLowerCase()}\0${ext.toLowerCase()}`;
		const bucket = buckets.get(key) ?? { stem, ext, pages: [] };
		bucket.pages.push({ path, fileName, page });
		buckets.set(key, bucket);
	}

	const grouped: PdfGroup[] = [];
	for (const bucket of buckets.values()) {
		bucket.pages.sort((a, b) => a.page - b.page || a.fileName.localeCompare(b.fileName, 'de'));
		if (bucket.pages.length < 2) {
			grouped.push({
				fileName: bucket.pages[0].fileName,
				pages: bucket.pages
			});
			continue;
		}
		const ext = bucket.ext || '.pdf';
		let fileName = `${bucket.stem}${ext}`;
		if (taken.has(fileName.toLowerCase())) {
			fileName = `${bucket.stem} (Seiten)${ext}`;
		}
		grouped.push({ fileName, pages: bucket.pages });
	}

	return [...standalone, ...grouped].sort((a, b) =>
		a.fileName.localeCompare(b.fileName, 'de', { numeric: true })
	);
}

export function pdfGroupCount(paths: string[]): { files: number; documents: number; merged: number } {
	const groups = groupPdfPages(paths);
	const merged = groups.filter((g) => g.pages.length > 1).length;
	return { files: paths.length, documents: groups.length, merged };
}
