import { PDFDocument } from 'pdf-lib';
import { renamed } from './folder-files';
import type { PdfGroup } from './pdf-groups';

/** Concatenate PDF files in the given order into one multi-page document. */
export async function mergePdfFiles(files: File[], outName: string): Promise<File> {
	if (files.length === 1) return renamed(files[0], outName);
	const out = await PDFDocument.create();
	for (const file of files) {
		const src = await PDFDocument.load(await file.arrayBuffer(), { ignoreEncryption: true });
		const copied = await out.copyPages(src, src.getPageIndices());
		for (const page of copied) out.addPage(page);
	}
	const bytes = await out.save();
	return new File([bytes], outName, { type: 'application/pdf' });
}

export async function fileForPdfGroup(
	group: PdfGroup,
	filesByPath: Map<string, File>
): Promise<File> {
	const files = group.pages.map((p) => filesByPath.get(p.path)).filter((f): f is File => !!f);
	if (files.length !== group.pages.length) {
		throw new Error(`${files.length} von ${group.pages.length} Seiten in der Auswahl`);
	}
	if (files.length === 1) return renamed(files[0], group.fileName);
	return mergePdfFiles(files, group.fileName);
}
