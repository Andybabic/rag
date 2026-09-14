<script lang="ts">
	import { onMount } from 'svelte';

	interface TokenRow {
		id: string;
		name: string;
		token_prefix: string;
		use_cases: string[];
		created_by: string | null;
		created_at: string | null;
		last_used_at: string | null;
		expires_at: string | null;
		revoked_at: string | null;
		active: boolean;
	}

	interface JobRow {
		request_id: string;
		use_case: string;
		session_id: string;
		status: 'queued' | 'running' | 'done' | 'error';
		query: string;
		error: string | null;
		token_name: string | null;
		created_at: string | null;
		finished_at: string | null;
	}

	interface UseCaseOption {
		id: string;
		label: string;
	}

	let tokens: TokenRow[] = $state([]);
	let jobs: JobRow[] = $state([]);
	let useCases: UseCaseOption[] = $state([]);
	let loading = $state(true);
	let error = $state('');

	// new-token form
	let newName = $state('');
	let newScope: Set<string> = $state(new Set());
	let newExpiry = $state('');
	let creating = $state(false);

	// The plaintext is returned exactly once — it exists nowhere else after
	// this, so it stays on screen until the admin explicitly dismisses it.
	let freshToken = $state('');
	let copied = $state(false);

	const STATUS_STYLE: Record<string, string> = {
		queued: 'bg-gray-100 text-gray-600',
		running: 'bg-blue-100 text-blue-700',
		done: 'bg-green-100 text-green-700',
		error: 'bg-red-100 text-red-700'
	};

	async function load() {
		loading = true;
		error = '';
		try {
			const [tResp, jResp, ucResp] = await Promise.all([
				fetch('/api/admin/api-tokens'),
				fetch('/api/admin/api-jobs?limit=25'),
				fetch('/api/admin/use-cases')
			]);
			if (!tResp.ok) throw new Error('Tokens konnten nicht geladen werden');
			tokens = (await tResp.json()).tokens ?? [];
			if (jResp.ok) jobs = (await jResp.json()).jobs ?? [];
			if (ucResp.ok) {
				const body = await ucResp.json();
				useCases = (body.use_cases ?? []).map((u: UseCaseOption) => ({
					id: u.id,
					label: u.label
				}));
			}
		} catch (e) {
			error = e instanceof Error ? e.message : 'Fehler';
		}
		loading = false;
	}

	function toggleScope(id: string) {
		if (newScope.has(id)) newScope.delete(id);
		else newScope.add(id);
		newScope = new Set(newScope);
	}

	async function createToken() {
		if (!newName.trim()) {
			error = 'Bitte einen Namen vergeben.';
			return;
		}
		creating = true;
		error = '';
		try {
			const resp = await fetch('/api/admin/api-tokens', {
				method: 'POST',
				headers: { 'content-type': 'application/json' },
				body: JSON.stringify({
					name: newName.trim(),
					use_cases: [...newScope],
					expires_in_days: newExpiry ? Number(newExpiry) : null
				})
			});
			const body = await resp.json().catch(() => ({}));
			if (!resp.ok) throw new Error(body.detail || 'Anlegen fehlgeschlagen');
			freshToken = body.token;
			copied = false;
			newName = '';
			newScope = new Set();
			newExpiry = '';
			await load();
		} catch (e) {
			error = e instanceof Error ? e.message : 'Fehler';
		}
		creating = false;
	}

	async function copyToken() {
		try {
			await navigator.clipboard.writeText(freshToken);
			copied = true;
		} catch {
			copied = false;
		}
	}

	async function revokeToken(t: TokenRow) {
		if (!confirm(`Token "${t.name}" sperren? Laufende Integrationen brechen sofort ab.`)) return;
		error = '';
		const resp = await fetch(`/api/admin/api-tokens/${t.id}/revoke`, { method: 'POST' });
		if (!resp.ok) {
			const body = await resp.json().catch(() => ({}));
			error = body.detail || 'Sperren fehlgeschlagen';
		}
		await load();
	}

	async function deleteToken(t: TokenRow) {
		if (!confirm(`Token "${t.name}" endgültig löschen?`)) return;
		error = '';
		const resp = await fetch(`/api/admin/api-tokens/${t.id}`, { method: 'DELETE' });
		if (!resp.ok && resp.status !== 204) {
			const body = await resp.json().catch(() => ({}));
			error = body.detail || 'Löschen fehlgeschlagen';
		}
		await load();
	}

	function fmt(iso: string | null): string {
		if (!iso) return '–';
		return new Date(iso).toLocaleString('de-DE', { dateStyle: 'short', timeStyle: 'short' });
	}

	function scopeLabel(t: TokenRow): string {
		if (!t.use_cases.length) return 'alle Use Cases';
		return t.use_cases.join(', ');
	}

	onMount(load);
</script>

<svelte:head><title>API-Tokens – Dashboard</title></svelte:head>

<div class="mx-auto max-w-4xl space-y-8 p-6">
	<div>
		<h2 class="text-xl font-bold text-gray-800">API-Tokens</h2>
		<p class="mt-1 text-sm text-gray-500">
			Zugangsschlüssel für die externe Pipeline-API unter <code class="rounded bg-gray-100 px-1 py-0.5 text-xs">/api/v1</code>.
			Ein Token ohne Use-Case-Auswahl darf alle Use Cases nutzen.
			<a href="/admin/api-docs" class="text-blue-600 hover:underline">Zur API-Dokumentation →</a>
		</p>
	</div>

	{#if error}
		<p class="rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700">{error}</p>
	{/if}

	{#if freshToken}
		<div class="rounded-xl border border-amber-300 bg-amber-50 p-5">
			<h3 class="text-sm font-semibold text-amber-900">Token einmalig sichtbar</h3>
			<p class="mt-1 text-xs text-amber-800">
				Jetzt kopieren und sicher hinterlegen — gespeichert wird nur ein Hash, ein
				zweites Mal lässt sich der Schlüssel nicht anzeigen.
			</p>
			<div class="mt-3 flex items-center gap-2">
				<code class="flex-1 overflow-x-auto rounded-lg border border-amber-300 bg-white px-3 py-2 font-mono text-xs text-gray-800">
					{freshToken}
				</code>
				<button
					onclick={copyToken}
					class="rounded-lg bg-amber-600 px-3 py-2 text-sm font-medium text-white hover:bg-amber-700"
				>
					{copied ? 'Kopiert ✓' : 'Kopieren'}
				</button>
				<button
					onclick={() => (freshToken = '')}
					class="rounded-lg border border-amber-300 px-3 py-2 text-sm text-amber-900 hover:bg-amber-100"
				>
					Schließen
				</button>
			</div>
		</div>
	{/if}

	<!-- Create -->
	<div class="rounded-xl border border-gray-200 bg-white p-5 shadow-sm">
		<h3 class="mb-3 text-sm font-semibold text-gray-700">Neues Token anlegen</h3>
		<div class="flex flex-wrap items-end gap-3">
			<div class="flex-1">
				<label for="tn" class="mb-1 block text-xs text-gray-500">Name (wofür?)</label>
				<input
					id="tn"
					bind:value={newName}
					placeholder="z. B. Intranet-Integration"
					class="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm"
				/>
			</div>
			<div>
				<label for="te" class="mb-1 block text-xs text-gray-500">Gültig für (Tage)</label>
				<input
					id="te"
					type="number"
					min="1"
					bind:value={newExpiry}
					placeholder="unbegrenzt"
					class="w-36 rounded-lg border border-gray-300 px-3 py-2 text-sm"
				/>
			</div>
			<button
				onclick={createToken}
				disabled={creating}
				class="rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-60"
			>
				Anlegen
			</button>
		</div>

		<p class="mb-2 mt-4 text-xs font-semibold uppercase tracking-wider text-gray-500">
			Use Cases – nichts auswählen heißt: alle
		</p>
		{#if useCases.length === 0}
			<p class="text-sm text-gray-400">Keine Use Cases vorhanden.</p>
		{:else}
			<div class="grid grid-cols-1 gap-2 sm:grid-cols-2">
				{#each useCases as uc}
					<label class="flex items-center gap-2 rounded-lg border border-gray-200 px-3 py-2 text-sm hover:bg-gray-50">
						<input
							type="checkbox"
							checked={newScope.has(uc.id)}
							onchange={() => toggleScope(uc.id)}
							class="h-4 w-4 rounded border-gray-300"
						/>
						<span class="font-medium text-gray-700">{uc.label}</span>
						<span class="ml-auto font-mono text-[10px] text-gray-400">{uc.id}</span>
					</label>
				{/each}
			</div>
		{/if}
	</div>

	<!-- Tokens -->
	{#if loading}
		<p class="animate-pulse text-sm text-gray-400">Lade…</p>
	{:else}
		<div class="overflow-hidden rounded-xl border border-gray-200 bg-white shadow-sm">
			<table class="w-full text-sm">
				<thead class="bg-gray-50 text-left text-xs uppercase text-gray-500">
					<tr>
						<th class="px-4 py-3">Name</th>
						<th class="px-4 py-3">Schlüssel</th>
						<th class="px-4 py-3">Bereich</th>
						<th class="px-4 py-3">Zuletzt genutzt</th>
						<th class="px-4 py-3 text-right">Aktionen</th>
					</tr>
				</thead>
				<tbody class="divide-y divide-gray-100">
					{#each tokens as t (t.id)}
						<tr class={t.active ? '' : 'bg-gray-50 text-gray-400'}>
							<td class="px-4 py-3 font-medium {t.active ? 'text-gray-800' : ''}">
								{t.name}
								{#if !t.active}
									<span class="ml-2 rounded bg-red-100 px-1.5 py-0.5 text-[10px] text-red-700">
										{t.revoked_at ? 'gesperrt' : 'abgelaufen'}
									</span>
								{/if}
								<span class="mt-0.5 block text-[11px] text-gray-400">
									angelegt {fmt(t.created_at)}{t.created_by ? ` von ${t.created_by}` : ''}
									{#if t.expires_at}· gültig bis {fmt(t.expires_at)}{/if}
								</span>
							</td>
							<td class="px-4 py-3 font-mono text-xs">{t.token_prefix}…</td>
							<td class="px-4 py-3 text-xs">{scopeLabel(t)}</td>
							<td class="px-4 py-3 text-xs">{fmt(t.last_used_at)}</td>
							<td class="px-4 py-3 text-right">
								{#if t.active}
									<button onclick={() => revokeToken(t)} class="mr-2 text-xs text-amber-700 hover:underline">
										Sperren
									</button>
								{/if}
								<button onclick={() => deleteToken(t)} class="text-xs text-red-600 hover:underline">
									Löschen
								</button>
							</td>
						</tr>
					{:else}
						<tr><td colspan="5" class="px-4 py-6 text-center text-sm text-gray-400">
							Noch keine Tokens angelegt.
						</td></tr>
					{/each}
				</tbody>
			</table>
		</div>

		<!-- Recent API traffic -->
		<div>
			<h3 class="mb-2 text-sm font-semibold text-gray-700">Letzte API-Anfragen</h3>
			<div class="overflow-hidden rounded-xl border border-gray-200 bg-white shadow-sm">
				<table class="w-full text-sm">
					<thead class="bg-gray-50 text-left text-xs uppercase text-gray-500">
						<tr>
							<th class="px-4 py-3">Status</th>
							<th class="px-4 py-3">Frage</th>
							<th class="px-4 py-3">Use Case</th>
							<th class="px-4 py-3">Token</th>
							<th class="px-4 py-3">Zeit</th>
						</tr>
					</thead>
					<tbody class="divide-y divide-gray-100">
						{#each jobs as j (j.request_id)}
							<tr>
								<td class="px-4 py-3">
									<span class="rounded px-1.5 py-0.5 text-[11px] font-medium {STATUS_STYLE[j.status]}">
										{j.status}
									</span>
								</td>
								<td class="max-w-xs truncate px-4 py-3 text-gray-700" title={j.error ?? j.query}>
									{j.query}
								</td>
								<td class="px-4 py-3 text-xs text-gray-500">{j.use_case}</td>
								<td class="px-4 py-3 text-xs text-gray-500">{j.token_name ?? '–'}</td>
								<td class="px-4 py-3 text-xs text-gray-500">{fmt(j.created_at)}</td>
							</tr>
						{:else}
							<tr><td colspan="5" class="px-4 py-6 text-center text-sm text-gray-400">
								Noch keine API-Anfragen.
							</td></tr>
						{/each}
					</tbody>
				</table>
			</div>
		</div>
	{/if}
</div>
