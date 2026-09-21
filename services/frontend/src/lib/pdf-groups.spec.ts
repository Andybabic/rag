import { describe, expect, it } from 'vitest';
import { PDFDocument, StandardFonts } from 'pdf-lib';
import { groupPdfPages, pdfGroupCount } from './pdf-groups';
import { mergePdfFiles } from './pdf-merge';

describe('groupPdfPages', () => {
	it('merges Einstellblatt 3-1 and 3-2 into one document, sorted by page', () => {
		const groups = groupPdfPages([
			'Matec 2/11221.7072/Einstellblatt 3-2.pdf',
			'Matec 2/11221.7072/Einstellblatt 3-1.pdf',
			'Matec 2/11221.7072/Einstellblatt 1.pdf'
		]);
		expect(groups.map((g) => [g.fileName, g.pages.map((p) => p.page)])).toEqual([
			['Einstellblatt 1.pdf', [1]],
			['Einstellblatt 3.pdf', [1, 2]]
		]);
	});

	it('keeps a lone page-suffixed file as-is', () => {
		const groups = groupPdfPages(['p/Einstellblatt 3-1.pdf']);
		expect(groups).toEqual([
			{
				fileName: 'Einstellblatt 3-1.pdf',
				pages: [{ path: 'p/Einstellblatt 3-1.pdf', fileName: 'Einstellblatt 3-1.pdf', page: 1 }]
			}
		]);
	});

	it('avoids colliding with an already complete Einstellblatt 3.pdf', () => {
		const groups = groupPdfPages([
			'p/Einstellblatt 3.pdf',
			'p/Einstellblatt 3-1.pdf',
			'p/Einstellblatt 3-2.pdf'
		]);
		const names = groups.map((g) => g.fileName);
		expect(names).toContain('Einstellblatt 3.pdf');
		expect(names).toContain('Einstellblatt 3 (Seiten).pdf');
	});

	it('sorts page 10 after page 2', () => {
		const groups = groupPdfPages(['a/Blatt-10.pdf', 'a/Blatt-2.pdf']);
		expect(groups[0].pages.map((p) => p.page)).toEqual([2, 10]);
	});
});

describe('pdfGroupCount', () => {
	it('counts merged documents separately from source files', () => {
		expect(
			pdfGroupCount(['a/Einstellblatt 3-1.pdf', 'a/Einstellblatt 3-2.pdf', 'a/Einstellblatt 1.pdf'])
		).toEqual({ files: 3, documents: 2, merged: 1 });
	});
});

async function onePagePdf(label: string): Promise<File> {
	const doc = await PDFDocument.create();
	const page = doc.addPage([200, 200]);
	const font = await doc.embedFont(StandardFonts.Helvetica);
	page.drawText(label, { x: 20, y: 100, size: 12, font });
	const bytes = await doc.save();
	return new File([bytes], `${label}.pdf`, { type: 'application/pdf' });
}

describe('mergePdfFiles', () => {
	it('concatenates pages in order', async () => {
		const a = await onePagePdf('A');
		const b = await onePagePdf('B');
		const merged = await mergePdfFiles([a, b], 'Einstellblatt 3.pdf');
		expect(merged.name).toBe('Einstellblatt 3.pdf');
		const loaded = await PDFDocument.load(await merged.arrayBuffer());
		expect(loaded.getPageCount()).toBe(2);
	});
});
