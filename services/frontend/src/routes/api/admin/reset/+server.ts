/**
 * Den Wissensbestand eines Use Case löschen.
 *
 * Betrifft ausschliesslich das, was hochgeladen wurde: die Collections mit den
 * Präfixen des Use Case, sein Agent-Gedächtnis, die Dokumentliste und die
 * abgelegten Originaldateien. Benutzer, Prompts, Skills und Konfiguration
 * bleiben unangetastet — und ebenso Chatverlauf, Anfragen und Feedback: Sie
 * sind die Auswertungsgrundlage und überleben ein Zurücksetzen des Bestands.
 *
 * Die Bestätigung läuft über das Passwort des angemeldeten Kontos, nicht über
 * ein zweites Geheimnis: Ein Extra-Passwort landet in einer Env-Datei, wird
 * geteilt und veraltet. Hier muss jemand beweisen, dass er gerade selbst an
 * der Tastatur sitzt.
 */
import { json } from '@sveltejs/kit';
import { SERVICES } from '$lib/server/services';
import type { RequestHandler } from './$types';

interface Doc {
	file_hash?: string;
	stored_path?: string;
	use_case?: string;
}

async function usePrefixes(useCase: string): Promise<string[]> {
	try {
		const resp = await fetch(
			`${SERVICES.evaluation}/v1/use-case/${encodeURIComponent(useCase)}/collections`
		);
		if (!resp.ok) return [];
		const prefixes = (await resp.json()).prefixes;
		return Array.isArray(prefixes) ? prefixes.filter((p): p is string => typeof p === 'string') : [];
	} catch {
		return [];
	}
}

export const POST: RequestHandler = async ({ request, locals }) => {
	const user = locals.user;
	if (!user) return json({ error: 'unauthorized' }, { status: 401 });

	const body = await request.json().catch(() => ({}));
	const useCase = typeof body.use_case === 'string' ? body.use_case.trim() : '';
	const password = typeof body.password === 'string' ? body.password : '';

	if (!useCase) return json({ error: 'no_use_case', detail: 'use_case fehlt' }, { status: 400 });
	if (!password) {
		return json({ error: 'no_password', detail: 'Passwort fehlt' }, { status: 400 });
	}

	// 1. Passwort gegen das angemeldete Konto prüfen.
	let auth: Response;
	try {
		auth = await fetch(`${SERVICES.evaluation}/v1/auth/login`, {
			method: 'POST',
			headers: { 'content-type': 'application/json' },
			body: JSON.stringify({ username: user.username, password })
		});
	} catch {
		return json(
			{ error: 'auth_unreachable', detail: 'Authentifizierungsdienst nicht erreichbar.' },
			{ status: 502 }
		);
	}
	if (auth.status === 401) {
		return json({ error: 'bad_password', detail: 'Passwort ist falsch.' }, { status: 401 });
	}
	if (!auth.ok) {
		return json(
			{ error: 'auth_failed', detail: `Prüfung fehlgeschlagen (HTTP ${auth.status}).` },
			{ status: 502 }
		);
	}

	const errors: string[] = [];

	// 2. Collections des Use Case ermitteln. Das Agent-Gedächtnis liegt unter
	//    "mem_<use_case>" und trägt die Präfixe des Use Case nicht — ohne
	//    diesen Zusatz bliebe es als einzige Spur zurück.
	const prefixes = await usePrefixes(useCase);
	const memory = `mem_${useCase}`;
	let collections: string[] = [];
	try {
		const resp = await fetch(`${SERVICES.vectordb}/v1/collections`);
		const names: string[] = ((await resp.json()).collections ?? []).map(
			(c: { name: string }) => c.name
		);
		collections = names.filter(
			(n) => n === memory || prefixes.some((p) => p && n.startsWith(p))
		);
	} catch {
		return json(
			{ error: 'vectordb_unreachable', detail: 'Vektordatenbank nicht erreichbar.' },
			{ status: 502 }
		);
	}

	// 3. Dokumentliste VOR dem Löschen lesen — danach ist nicht mehr zu
	//    ermitteln, welche Originaldateien wegzuräumen sind.
	let documents: Doc[] = [];
	try {
		const resp = await fetch(
			`${SERVICES.evaluation}/v1/admin/documents?use_case=${encodeURIComponent(useCase)}`
		);
		if (resp.ok) documents = (await resp.json()).documents ?? [];
	} catch {
		errors.push('Dokumentliste nicht lesbar');
	}

	// 4. Collections löschen.
	let collectionsDeleted = 0;
	for (const name of collections) {
		try {
			const resp = await fetch(
				`${SERVICES.vectordb}/v1/collection/${encodeURIComponent(name)}`,
				{ method: 'DELETE' }
			);
			if (resp.ok) collectionsDeleted++;
			else errors.push(`Collection ${name}: HTTP ${resp.status}`);
		} catch {
			errors.push(`Collection ${name}: nicht erreichbar`);
		}
	}

	// 5. Dokumentzeilen entfernen.
	const hashes = documents.map((d) => d.file_hash).filter((h): h is string => !!h);
	let removed: Doc[] = [];
	if (hashes.length) {
		try {
			const resp = await fetch(`${SERVICES.evaluation}/v1/ingest-log/delete`, {
				method: 'POST',
				headers: { 'content-type': 'application/json' },
				body: JSON.stringify({ file_hashes: hashes, use_case: useCase })
			});
			if (resp.ok) removed = (await resp.json()).documents ?? [];
			else errors.push(`Dokumentliste: HTTP ${resp.status}`);
		} catch {
			errors.push('Dokumentliste nicht erreichbar');
		}
	}

	// 6. Originaldateien wegräumen. Eine Restdatei auf der Platte ist
	//    ärgerlich, aber harmlos — der Index ist dann schon leer.
	let filesDeleted = 0;
	for (const doc of removed) {
		if (!doc.stored_path || !doc.file_hash) continue;
		const url = new URL(
			`${SERVICES.cleaning}/v1/documents/${encodeURIComponent(doc.use_case || useCase)}/${encodeURIComponent(doc.file_hash)}`
		);
		url.searchParams.set('stored_path', doc.stored_path);
		try {
			const resp = await fetch(url, { method: 'DELETE' });
			if (resp.ok) filesDeleted++;
		} catch {
			// bewusst still: siehe oben
		}
	}

	console.warn(
		`[admin] Wissensbestand von '${useCase}' gelöscht durch '${user.username}': ` +
			`${collectionsDeleted} Collection(s), ${removed.length} Dokument(e), ` +
			`${filesDeleted} Datei(en)` +
			(errors.length ? ` — Fehler: ${errors.join('; ')}` : '')
	);

	return json({
		status: errors.length ? 'partial' : 'ok',
		use_case: useCase,
		collections_deleted: collectionsDeleted,
		collections: collections,
		documents_deleted: removed.length,
		files_deleted: filesDeleted,
		errors
	});
};
