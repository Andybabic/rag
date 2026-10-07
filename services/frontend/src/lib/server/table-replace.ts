/**
 * Eine Tabelle durch ihren neuen Export ersetzen.
 *
 * Bestandslisten kommen regelmässig neu. Würde der Import nur ergänzen,
 * stünden zu einem Werkzeug mehrere Bestände nebeneinander – die Suche fände
 * beide, und die Antwort nennt womöglich den Stand von vorletztem Quartal.
 * Also: alte Zeilen derselben Tabelle raus, dann die neuen rein.
 */
import { SERVICES } from '$lib/server/services';

interface Payload {
	file_hash?: unknown;
	[key: string]: unknown;
}

async function scrollPayloads(
	collection: string,
	filters: Record<string, unknown>,
	limit = 1000
): Promise<Payload[]> {
	const resp = await fetch(`${SERVICES.vectordb}/v1/scroll`, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ collection, filters, limit })
	});
	if (!resp.ok) return [];
	return ((await resp.json()).points ?? []) as Payload[];
}

/**
 * Entfernt alle Punkte der Tabelle ``key`` aus ``collection`` und räumt die
 * Dokumentzeilen und Originaldateien der ersetzten Exporte weg.
 *
 * Ein fehlgeschlagenes Aufräumen bricht den Import NICHT ab: Ein verwaister
 * Eintrag in der Dokumentliste ist ärgerlich, ein abgebrochener Import mit
 * halb gelöschtem Bestand wäre schlimmer.
 */
export async function replaceTableRows(
	collection: string,
	key: string,
	useCase: string
): Promise<{ points: number; documents: number }> {
	const filters = { table_key: key };

	// Die Datei-Hashes der Vorgänger, solange ihre Punkte noch da sind.
	const hashes = new Set<string>();
	for (const point of await scrollPayloads(collection, filters)) {
		if (typeof point.file_hash === 'string' && point.file_hash) hashes.add(point.file_hash);
	}

	let points = 0;
	try {
		const resp = await fetch(`${SERVICES.vectordb}/v1/delete`, {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ collection, filters })
		});
		if (resp.ok) points = Number((await resp.json()).deleted ?? 0);
	} catch {
		return { points: 0, documents: 0 };
	}

	if (!hashes.size) return { points, documents: 0 };

	// Nur Hashes, auf die nichts mehr zeigt. Ein byte-gleicher Export, der
	// unter anderem Namen auch als eigene Tabelle liegt, behält seine Datei.
	const orphaned: string[] = [];
	for (const hash of hashes) {
		if (!(await scrollPayloads(collection, { file_hash: hash }, 1)).length) orphaned.push(hash);
	}
	if (!orphaned.length) return { points, documents: 0 };

	let removed: Array<{ file_hash: string; stored_path: string; use_case: string }> = [];
	try {
		const logResp = await fetch(`${SERVICES.evaluation}/v1/ingest-log/delete`, {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ file_hashes: orphaned, use_case: useCase })
		});
		if (logResp.ok) removed = (await logResp.json()).documents ?? [];
	} catch {
		return { points, documents: 0 };
	}

	for (const doc of removed) {
		if (!doc.stored_path) continue;
		const url = new URL(
			`${SERVICES.cleaning}/v1/documents/${encodeURIComponent(doc.use_case || useCase)}/${encodeURIComponent(doc.file_hash)}`
		);
		url.searchParams.set('stored_path', doc.stored_path);
		try {
			await fetch(url, { method: 'DELETE' });
		} catch {
			// Eine Restdatei auf der Platte ist harmlos; der Index ist sauber.
		}
	}
	return { points, documents: removed.length };
}
