/**
 * Collect the files of a folder the user picked or dropped, keeping each
 * file's path relative to the dropped root.
 *
 * The path is the whole point: for the GW data set it carries category,
 * machine and product ("Fräsen groß/Chiron 243/11221.7199/…"). A plain
 * FileList from drag & drop has no paths and does not descend into folders,
 * so dropped directories are walked through the FileSystem entry API.
 */

export interface PickedFile {
	path: string;
	file: File;
}

/** From `<input type="file" webkitdirectory>` – paths come with the files. */
export function filesFromInput(list: FileList | null): PickedFile[] {
	return Array.from(list ?? []).map((file) => ({
		path: file.webkitRelativePath || file.name,
		file
	}));
}

/** From a drop event – descends into every dropped folder. */
export async function filesFromDrop(transfer: DataTransfer | null): Promise<PickedFile[]> {
	if (!transfer) return [];
	// Entries must be taken synchronously: the DataTransfer is emptied as soon
	// as the event handler yields.
	const entries = Array.from(transfer.items)
		.map((item) => item.webkitGetAsEntry?.())
		.filter((entry): entry is FileSystemEntry => !!entry);
	if (!entries.length) {
		return Array.from(transfer.files).map((file) => ({ path: file.name, file }));
	}
	const out: PickedFile[] = [];
	for (const entry of entries) await walk(entry, '', out);
	return out;
}

async function walk(entry: FileSystemEntry, prefix: string, out: PickedFile[]): Promise<void> {
	const path = prefix ? `${prefix}/${entry.name}` : entry.name;
	if (entry.isFile) {
		const file = await new Promise<File>((resolve, reject) =>
			(entry as FileSystemFileEntry).file(resolve, reject)
		);
		out.push({ path, file });
		return;
	}
	if (entry.isDirectory) {
		for (const child of await readAll(entry as FileSystemDirectoryEntry)) {
			await walk(child, path, out);
		}
	}
}

/** `readEntries` returns at most ~100 entries per call; keep reading until empty. */
async function readAll(dir: FileSystemDirectoryEntry): Promise<FileSystemEntry[]> {
	const reader = dir.createReader();
	const all: FileSystemEntry[] = [];
	for (;;) {
		const batch = await new Promise<FileSystemEntry[]>((resolve, reject) =>
			reader.readEntries(resolve, reject)
		);
		if (!batch.length) return all;
		all.push(...batch);
	}
}

/** Base64 of a file's bytes, for the JSON product upload. */
export async function toBase64(file: File): Promise<string> {
	const bytes = new Uint8Array(await file.arrayBuffer());
	let binary = '';
	// Chunked: String.fromCharCode(...bytes) overflows the call stack on large files.
	for (let i = 0; i < bytes.length; i += 0x8000) {
		binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
	}
	return btoa(binary);
}

/** The same file under another name (the name is what ends up in citations). */
export function renamed(file: File, name: string): File {
	return new File([file], name, { type: file.type, lastModified: file.lastModified });
}

/**
 * Shrink a photo to at most `maxEdge` pixels on its long side.
 *
 * Shop-floor photos are 4000×3000 and ~5 MB; the vision model gains nothing
 * from that resolution but takes longer per image, and 134 photos would be
 * 600 MB of upload. Files that cannot be decoded are sent unchanged.
 */
export async function shrinkImage(file: File, maxEdge = 2048, quality = 0.85): Promise<File> {
	let bitmap: ImageBitmap;
	try {
		bitmap = await createImageBitmap(file);
	} catch {
		return file;
	}
	const scale = Math.min(1, maxEdge / Math.max(bitmap.width, bitmap.height));
	if (scale === 1 && file.size < 2_000_000) {
		bitmap.close();
		return file;
	}
	const canvas = document.createElement('canvas');
	canvas.width = Math.round(bitmap.width * scale);
	canvas.height = Math.round(bitmap.height * scale);
	canvas.getContext('2d')?.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
	bitmap.close();
	const blob = await new Promise<Blob | null>((resolve) => canvas.toBlob(resolve, 'image/jpeg', quality));
	if (!blob) return file;
	return new File([blob], file.name.replace(/\.(png|jpe?g)$/i, '') + '.jpg', { type: 'image/jpeg' });
}
