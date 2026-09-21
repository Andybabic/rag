/**
 * Der Löschknopf ist die einzige Funktion, deren Fehler niemand zurückholen
 * kann. Geprüft wird deshalb nicht, dass sie löscht, sondern WAS sie nicht
 * anfasst: fremde Use Cases, und alles ohne gültiges Passwort.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { POST } from './+server';

const COLLECTIONS = [
	{ name: 'gw_cnc_steps' },
	{ name: 'gw_ruest_data' },
	{ name: 'gw_stpoelten_default' },
	{ name: 'mem_gw_stpoelten' },
	{ name: 'neumann_machines' },
	{ name: 'mem_neumann' }
];

const DOCS = [
	{ file_hash: 'h1', stored_path: 'gw/1.pdf', use_case: 'gw_stpoelten' },
	{ file_hash: 'h2', stored_path: 'gw/2.csv', use_case: 'gw_stpoelten' }
];

interface Call {
	url: string;
	method: string;
}

let calls: Call[] = [];

function mockFetch(opts: { password?: string } = {}) {
	const good = opts.password ?? 'geheim';
	return vi.fn(async (input: string | URL, init?: RequestInit) => {
		const url = String(input);
		const method = init?.method ?? 'GET';
		calls.push({ url, method });

		if (url.includes('/v1/auth/login')) {
			const body = JSON.parse(String(init?.body ?? '{}'));
			return new Response(
				body.password === good ? JSON.stringify({ username: body.username, role: 'admin' }) : '',
				{ status: body.password === good ? 200 : 401 }
			);
		}
		if (url.includes('/v1/use-case/') && url.includes('/collections')) {
			return new Response(JSON.stringify({ prefixes: ['gw_'] }), { status: 200 });
		}
		if (url.endsWith('/v1/collections')) {
			return new Response(JSON.stringify({ collections: COLLECTIONS }), { status: 200 });
		}
		if (url.includes('/v1/admin/documents')) {
			return new Response(JSON.stringify({ documents: DOCS }), { status: 200 });
		}
		if (url.includes('/v1/ingest-log/delete')) {
			return new Response(JSON.stringify({ documents: DOCS }), { status: 200 });
		}
		return new Response('{}', { status: 200 });
	});
}

function call(body: unknown, user: { username: string; role: string } | null = {
	username: 'admin',
	role: 'admin'
}) {
	return POST({
		request: new Request('http://localhost/api/admin/reset', {
			method: 'POST',
			body: JSON.stringify(body)
		}),
		locals: { user }
		// eslint-disable-next-line @typescript-eslint/no-explicit-any
	} as any);
}

const deleted = () =>
	calls.filter((c) => c.method === 'DELETE' && c.url.includes('/v1/collection/'));

describe('Wissensbestand löschen', () => {
	beforeEach(() => {
		calls = [];
		vi.stubGlobal('fetch', mockFetch());
	});

	it('löscht nur Collections des gewählten Use Case', async () => {
		const resp = await call({ use_case: 'gw_stpoelten', password: 'geheim' });
		const body = await resp.json();

		expect(body.collections.sort()).toEqual([
			'gw_cnc_steps',
			'gw_ruest_data',
			'gw_stpoelten_default',
			'mem_gw_stpoelten'
		]);
		expect(body.collections_deleted).toBe(4);
		// Der Nachbar-Use-Case bleibt unberührt – der teuerste denkbare Fehler.
		expect(deleted().some((c) => c.url.includes('neumann'))).toBe(false);
	});

	it('nimmt das Agent-Gedächtnis mit, obwohl es die Prefixes nicht trägt', async () => {
		const resp = await call({ use_case: 'gw_stpoelten', password: 'geheim' });
		expect((await resp.json()).collections).toContain('mem_gw_stpoelten');
	});

	it('räumt Dokumentliste und Originaldateien weg', async () => {
		const body = await (await call({ use_case: 'gw_stpoelten', password: 'geheim' })).json();
		expect(body.documents_deleted).toBe(2);
		expect(body.files_deleted).toBe(2);
	});

	it('löscht nichts, wenn das Passwort falsch ist', async () => {
		const resp = await call({ use_case: 'gw_stpoelten', password: 'daneben' });
		expect(resp.status).toBe(401);
		expect(deleted()).toHaveLength(0);
	});

	it('löscht nichts ohne Passwort', async () => {
		const resp = await call({ use_case: 'gw_stpoelten' });
		expect(resp.status).toBe(400);
		expect(calls).toHaveLength(0);
	});

	it('löscht nichts ohne angemeldeten Benutzer', async () => {
		const resp = await call({ use_case: 'gw_stpoelten', password: 'geheim' }, null);
		expect(resp.status).toBe(401);
		expect(calls).toHaveLength(0);
	});

	it('meldet Teilerfolg, statt Fehler zu verschlucken', async () => {
		vi.stubGlobal(
			'fetch',
			vi.fn(async (input: string | URL, init?: RequestInit) => {
				const url = String(input);
				calls.push({ url, method: init?.method ?? 'GET' });
				if (url.includes('/v1/auth/login')) return new Response('{}', { status: 200 });
				if (url.includes('/v1/use-case/'))
					return new Response(JSON.stringify({ prefixes: ['gw_'] }), { status: 200 });
				if (url.endsWith('/v1/collections'))
					return new Response(JSON.stringify({ collections: COLLECTIONS }), { status: 200 });
				if (url.includes('/v1/admin/documents'))
					return new Response(JSON.stringify({ documents: [] }), { status: 200 });
				if (url.includes('/v1/collection/')) return new Response('', { status: 500 });
				return new Response('{}', { status: 200 });
			})
		);

		const body = await (await call({ use_case: 'gw_stpoelten', password: 'geheim' })).json();
		expect(body.status).toBe('partial');
		expect(body.errors.length).toBe(4);
	});
});
