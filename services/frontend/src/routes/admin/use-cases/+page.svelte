<script lang="ts">
	import { onMount } from 'svelte';

	interface EditUC {
		id: string;
		isNew: boolean;
		slug: string;
		label: string;
		description: string;
		color: string;
		accent: string;
		default_collection: string;
		rolesStr: string;
		actionsStr: string;
		prefixesStr: string;
		enabled: boolean;
		prompts: Record<string, string>;
		saving?: boolean;
	}

	let items: EditUC[] = $state([]);
	let loading = $state(true);
	let error = $state('');
	let notice = $state('');

	/** Use Case, dessen Bestand gerade gelöscht werden soll (null = Dialog zu). */
	let wipeTarget: string | null = $state(null);
	let wipePassword = $state('');
	let wipeBusy = $state(false);
	let wipeError = $state('');

	function askWipe(id: string) {
		wipeTarget = id;
		wipePassword = '';
		wipeError = '';
	}

	function closeWipe() {
		wipeTarget = null;
		wipePassword = '';
		wipeError = '';
	}

	async function confirmWipe() {
		if (!wipeTarget || !wipePassword) return;
		wipeBusy = true;
		wipeError = '';
		try {
			const resp = await fetch('/api/admin/reset', {
				method: 'POST',
				headers: { 'content-type': 'application/json' },
				body: JSON.stringify({ use_case: wipeTarget, password: wipePassword })
			});
			const body = await resp.json().catch(() => ({}));
			if (!resp.ok) {
				wipeError = body.detail || `Löschen fehlgeschlagen (HTTP ${resp.status}).`;
				return;
			}
			notice =
				`Wissensbestand von „${wipeTarget}" gelöscht: ` +
				`${body.collections_deleted} Collection(s), ${body.documents_deleted} Dokument(e), ` +
				`${body.files_deleted} Datei(en).` +
				(body.errors?.length ? ` Mit Fehlern: ${body.errors.join('; ')}` : '');
			closeWipe();
		} catch {
			wipeError = 'Dienst nicht erreichbar.';
		} finally {
			wipeBusy = false;
		}
	}

	function splitCsv(s: string): string[] {
		return s
			.split(',')
			.map((x) => x.trim())
			.filter(Boolean);
	}

	function fromApi(uc: any): EditUC {
		return {
			id: uc.id,
			isNew: false,
			slug: uc.slug ?? '',
			label: uc.label ?? '',
			description: uc.description ?? '',
			color: uc.color ?? '',
			accent: uc.accent ?? '',
			default_collection: uc.default_collection ?? '',
			rolesStr: (uc.roles ?? ['default']).join(', '),
			actionsStr: (uc.agent_action_names ?? []).join(', '),
			prefixesStr: (uc.collection_prefixes ?? []).join(', '),
			enabled: uc.enabled ?? true,
			prompts: { ...(uc.prompts ?? {}) }
		};
	}

	function toConfig(it: EditUC) {
		const roles = splitCsv(it.rolesStr);
		const prompts: Record<string, string> = {};
		for (const r of roles) prompts[r] = it.prompts[r] ?? '';
		return {
			id: it.id,
			slug: it.slug,
			label: it.label,
			description: it.description,
			color: it.color,
			accent: it.accent,
			enabled: it.enabled,
			roles,
			agent_action_names: splitCsv(it.actionsStr),
			default_collection: it.default_collection,
			collection_prefixes: splitCsv(it.prefixesStr),
			prompts
		};
	}

	async function load() {
		loading = true;
		error = '';
		try {
			const resp = await fetch('/api/admin/use-cases');
			if (!resp.ok) throw new Error('Laden fehlgeschlagen');
			const body = await resp.json();
			items = (body.use_cases ?? []).map(fromApi);
		} catch (e) {
			error = e instanceof Error ? e.message : 'Fehler';
		}
		loading = false;
	}

	function rolesOf(it: EditUC): string[] {
		return splitCsv(it.rolesStr);
	}

	function addNew() {
		items = [
			...items,
			{
				id: '',
				isNew: true,
				slug: '',
				label: '',
				description: '',
				color: 'bg-slate-600',
				accent: '#475569',
				default_collection: '',
				rolesStr: 'default',
				actionsStr: 'SEARCH, REFINE_QUERY, CLARIFY, RECALL_MEMORY, LOOKUP_SOURCES, FINAL_ANSWER',
				prefixesStr: '',
				enabled: true,
				prompts: { default: '' }
			}
		];
	}

	async function save(it: EditUC) {
		error = '';
		notice = '';
		if (!it.id || !/^[a-z][a-z0-9_]*$/.test(it.id)) {
			error = `Ungültige ID "${it.id}" (erlaubt: a–z, 0–9, _ ; Beginn mit Buchstabe).`;
			return;
		}
		if (!it.slug || !it.label) {
			error = 'slug und label sind Pflichtfelder.';
			return;
		}
		// Without prefixes the use case searches "<id>_…" – a default collection
		// named like that is always searchable, so it is the safe default.
		if (!it.default_collection.trim()) it.default_collection = `${it.id}_default`;
		it.saving = true;
		try {
			const resp = await fetch(`/api/admin/use-cases/${encodeURIComponent(it.id)}`, {
				method: 'PUT',
				headers: { 'content-type': 'application/json' },
				body: JSON.stringify(toConfig(it))
			});
			if (!resp.ok) {
				const body = await resp.json().catch(() => ({}));
				throw new Error(body.detail || 'Speichern fehlgeschlagen');
			}
			notice = `"${it.id}" gespeichert.`;
			await load();
		} catch (e) {
			error = e instanceof Error ? e.message : 'Fehler';
		}
	}

	async function remove(it: EditUC) {
		if (it.isNew) {
			items = items.filter((x) => x !== it);
			return;
		}
		if (!confirm(`Use Case "${it.id}" löschen? (Daten/Collections bleiben bestehen.)`)) return;
		error = '';
		const resp = await fetch(`/api/admin/use-cases/${encodeURIComponent(it.id)}`, {
			method: 'DELETE'
		});
		if (!resp.ok) {
			const body = await resp.json().catch(() => ({}));
			error = body.detail || 'Löschen fehlgeschlagen';
			return;
		}
		await load();
	}

	function exportJson() {
		const doc = { version: 1, use_cases: items.filter((i) => !i.isNew).map(toConfig) };
		const blob = new Blob([JSON.stringify(doc, null, 2) + '\n'], { type: 'application/json' });
		const url = URL.createObjectURL(blob);
		const a = document.createElement('a');
		a.href = url;
		a.download = 'use_cases.json';
		a.click();
		URL.revokeObjectURL(url);
	}

	onMount(load);
</script>

<svelte:head><title>Use Cases – Dashboard</title></svelte:head>

<div class="mx-auto max-w-4xl space-y-6 p-6">
	<div class="flex items-center justify-between">
		<div>
			<h2 class="text-xl font-bold text-gray-800">Use Cases</h2>
			<p class="mt-1 text-sm text-gray-500">
				Konfiguration der Use Cases. Änderungen wirken sofort.
			</p>
		</div>
		<div class="flex gap-2">
			<button onclick={exportJson} class="rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm font-medium text-gray-700 hover:bg-gray-50">
				Export JSON
			</button>
			<button onclick={addNew} class="rounded-lg bg-blue-600 px-3 py-2 text-sm font-medium text-white hover:bg-blue-700">
				+ Neuer Use Case
			</button>
		</div>
	</div>

	{#if error}
		<p class="rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700">{error}</p>
	{/if}
	{#if notice}
		<p class="rounded-lg bg-green-50 px-3 py-2 text-sm text-green-700">{notice}</p>
	{/if}

	{#if loading}
		<p class="animate-pulse text-sm text-gray-400">Lade…</p>
	{:else}
		{#each items as it (it.isNew ? 'new-' + items.indexOf(it) : it.id)}
			<div class="rounded-xl border border-gray-200 bg-white p-5 shadow-sm">
				<div class="mb-3 flex items-center justify-between">
					<div class="flex items-center gap-2">
						{#if it.isNew}
							<input
								bind:value={it.id}
								placeholder="id (z.B. acme)"
								class="rounded-lg border border-gray-300 px-2 py-1 font-mono text-sm"
							/>
						{:else}
							<span class="font-mono text-sm font-bold text-gray-800">{it.id}</span>
						{/if}
						<label class="ml-2 flex items-center gap-1 text-xs text-gray-500">
							<input type="checkbox" bind:checked={it.enabled} /> aktiv
						</label>
					</div>
					<div class="flex gap-2">
						<button onclick={() => save(it)} disabled={it.saving} class="rounded-lg bg-blue-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-blue-700 disabled:opacity-60">
							Speichern
						</button>
						<button onclick={() => remove(it)} class="rounded-lg border border-red-200 px-3 py-1.5 text-xs font-medium text-red-600 hover:bg-red-50">
							{it.isNew ? 'Verwerfen' : 'Löschen'}
						</button>
					</div>
				</div>

				<div class="grid gap-3 sm:grid-cols-2">
					{#each [['slug', 'Slug (URL)'], ['label', 'Label'], ['description', 'Beschreibung'], ['default_collection', 'Default-Collection'], ['color', 'Farbe (Tailwind-Klasse)'], ['accent', 'Akzent (#hex)']] as [key, lbl]}
						<label class="block">
							<span class="mb-1 block text-xs text-gray-500">{lbl}</span>
							<input
								value={(it as any)[key]}
								oninput={(e) => ((it as any)[key] = e.currentTarget.value)}
								placeholder={key === 'default_collection' ? `${it.id || '<id>'}_default` : ''}
								class="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm"
							/>
						</label>
					{/each}
					<label class="block">
						<span class="mb-1 block text-xs text-gray-500">Rollen (Komma-getrennt)</span>
						<input bind:value={it.rolesStr} class="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm" />
					</label>
					<label class="block">
						<span class="mb-1 block text-xs text-gray-500">Collection-Prefixes (Komma)</span>
						<input
							bind:value={it.prefixesStr}
							placeholder={`leer = ${it.id || '<id>'}_`}
							class="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm"
						/>
					</label>
					<p class="text-[11px] text-gray-400 sm:col-span-2">
						Hochgeladene Dokumente landen in der Default-Collection. Der Agent durchsucht nur
						Collections, die mit einem der Prefixes beginnen – die Default-Collection muss also
						dazu passen. Leer lassen ergibt „{it.id || '<id>'}_default" mit Prefix „{it.id || '<id>'}_".
					</p>
					<label class="block sm:col-span-2">
						<span class="mb-1 block text-xs text-gray-500">Agent-Actions (Komma)</span>
						<input bind:value={it.actionsStr} class="w-full rounded-lg border border-gray-300 px-3 py-2 font-mono text-xs" />
					</label>
				</div>

				{#if !it.isNew}
					<div class="mt-4 rounded-lg border border-red-200 bg-red-50/60 p-3">
						<div class="flex flex-wrap items-center justify-between gap-2">
							<div>
								<p class="text-xs font-semibold uppercase text-red-700">Gefahrenzone</p>
								<p class="mt-1 text-xs text-red-800">
									Löscht den gesamten Wissensbestand dieses Use Case: alle Collections mit
									seinen Prefixes, das Agent-Gedächtnis, die Dokumentliste und die abgelegten
									Originaldateien. Konfiguration, Prompts und Benutzer bleiben, ebenso
									Chatverlauf und Feedback. <strong>Nicht rückgängig zu machen.</strong>
								</p>
							</div>
							<button
								onclick={() => askWipe(it.id)}
								class="rounded-lg bg-red-600 px-3 py-1.5 text-xs font-semibold text-white hover:bg-red-700"
							>
								Daten löschen
							</button>
						</div>
					</div>
				{/if}

				<div class="mt-4 space-y-3">
					<p class="text-xs font-semibold uppercase text-gray-400">System-Prompts</p>
					{#each rolesOf(it) as role}
						<label class="block">
							<span class="mb-1 block text-xs text-gray-500">Rolle: {role}</span>
							<textarea
								rows="5"
								value={it.prompts[role] ?? ''}
								oninput={(e) => (it.prompts[role] = e.currentTarget.value)}
								class="w-full rounded-lg border border-gray-300 px-3 py-2 font-mono text-xs"
							></textarea>
						</label>
					{/each}
				</div>
			</div>
		{/each}
	{/if}
</div>

{#if wipeTarget}
	<div class="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4">
		<div class="w-full max-w-md rounded-xl bg-white p-5 shadow-xl">
			<h3 class="text-base font-bold text-gray-900">
				Wissensbestand von „{wipeTarget}" löschen
			</h3>
			<p class="mt-2 text-sm text-gray-600">
				Alle Collections, Dokumente und hochgeladenen Dateien dieses Use Case werden
				entfernt. Das lässt sich nicht rückgängig machen — der Bestand muss danach neu
				hochgeladen werden.
			</p>
			<label class="mt-4 block">
				<span class="mb-1 block text-xs text-gray-500">
					Zur Bestätigung das eigene Passwort eingeben
				</span>
				<!-- svelte-ignore a11y_autofocus -->
				<input
					type="password"
					autofocus
					bind:value={wipePassword}
					onkeydown={(e) => e.key === 'Enter' && confirmWipe()}
					class="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm"
				/>
			</label>
			{#if wipeError}
				<p class="mt-2 rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700">{wipeError}</p>
			{/if}
			<div class="mt-4 flex justify-end gap-2">
				<button
					onclick={closeWipe}
					disabled={wipeBusy}
					class="rounded-lg border border-gray-300 px-3 py-2 text-sm text-gray-700 hover:bg-gray-50"
				>
					Abbrechen
				</button>
				<button
					onclick={confirmWipe}
					disabled={wipeBusy || !wipePassword}
					class="rounded-lg bg-red-600 px-3 py-2 text-sm font-semibold text-white hover:bg-red-700 disabled:opacity-50"
				>
					{wipeBusy ? 'Löscht …' : 'Endgültig löschen'}
				</button>
			</div>
		</div>
	</div>
{/if}
