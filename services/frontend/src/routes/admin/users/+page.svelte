<script lang="ts">
	import { onMount } from 'svelte';

	interface UserRow {
		id: string;
		username: string;
		role: 'admin' | 'user';
		created_at: string | null;
	}

	interface UseCaseOption {
		id: string;
		label: string;
		enabled?: boolean;
	}

	let users: UserRow[] = $state([]);
	let useCases: UseCaseOption[] = $state([]);
	let loading = $state(true);
	let error = $state('');

	// new-user form
	let newUsername = $state('');
	let newPassword = $state('');
	let newRole: 'admin' | 'user' = $state('user');
	let creating = $state(false);

	// per-user use-case assignment panel
	let expandedUser: string | null = $state(null);
	let selected: Set<string> = $state(new Set());
	let assignmentLoading = $state(false);
	let assignmentSaving = $state(false);
	let assignmentMsg = $state('');

	async function load() {
		loading = true;
		error = '';
		try {
			const [uResp, ucResp] = await Promise.all([
				fetch('/api/admin/users'),
				fetch('/api/admin/use-cases')
			]);
			if (!uResp.ok) throw new Error('Laden fehlgeschlagen');
			users = await uResp.json();
			if (ucResp.ok) {
				const body = await ucResp.json();
				useCases = (body.use_cases ?? []).map((u: UseCaseOption) => ({
					id: u.id,
					label: u.label,
					enabled: u.enabled
				}));
			}
		} catch (e) {
			error = e instanceof Error ? e.message : 'Fehler';
		}
		loading = false;
	}

	async function createUser() {
		if (!newUsername.trim() || !newPassword) {
			error = 'Benutzername und Passwort erforderlich.';
			return;
		}
		creating = true;
		error = '';
		try {
			const resp = await fetch('/api/admin/users', {
				method: 'POST',
				headers: { 'content-type': 'application/json' },
				body: JSON.stringify({ username: newUsername.trim(), password: newPassword, role: newRole })
			});
			if (!resp.ok) {
				const body = await resp.json().catch(() => ({}));
				throw new Error(body.detail || 'Anlegen fehlgeschlagen');
			}
			newUsername = '';
			newPassword = '';
			newRole = 'user';
			await load();
		} catch (e) {
			error = e instanceof Error ? e.message : 'Fehler';
		}
		creating = false;
	}

	async function changeRole(u: UserRow, role: 'admin' | 'user') {
		error = '';
		const resp = await fetch(`/api/admin/users/${encodeURIComponent(u.username)}`, {
			method: 'PATCH',
			headers: { 'content-type': 'application/json' },
			body: JSON.stringify({ role })
		});
		if (!resp.ok) {
			const body = await resp.json().catch(() => ({}));
			error = body.detail || 'Rollenänderung fehlgeschlagen';
		}
		await load();
	}

	async function resetPassword(u: UserRow) {
		const pw = prompt(`Neues Passwort für ${u.username}:`);
		if (!pw) return;
		error = '';
		const resp = await fetch(`/api/admin/users/${encodeURIComponent(u.username)}`, {
			method: 'PATCH',
			headers: { 'content-type': 'application/json' },
			body: JSON.stringify({ password: pw })
		});
		if (!resp.ok) {
			const body = await resp.json().catch(() => ({}));
			error = body.detail || 'Passwort-Reset fehlgeschlagen';
		}
	}

	async function deleteUser(u: UserRow) {
		if (!confirm(`Benutzer "${u.username}" wirklich löschen?`)) return;
		error = '';
		const resp = await fetch(`/api/admin/users/${encodeURIComponent(u.username)}`, {
			method: 'DELETE'
		});
		if (!resp.ok && resp.status !== 204) {
			const body = await resp.json().catch(() => ({}));
			error = body.detail || 'Löschen fehlgeschlagen';
		}
		if (expandedUser === u.username) expandedUser = null;
		await load();
	}

	// ── Use-case assignment ──────────────────────────────────────
	async function togglePanel(u: UserRow) {
		if (expandedUser === u.username) {
			expandedUser = null;
			return;
		}
		expandedUser = u.username;
		assignmentMsg = '';
		selected = new Set();
		if (u.role === 'admin') return; // admins have access to everything
		assignmentLoading = true;
		try {
			const resp = await fetch(`/api/admin/users/${encodeURIComponent(u.username)}/use-cases`);
			if (resp.ok) {
				const body = await resp.json();
				selected = new Set<string>(body.use_cases ?? []);
			}
		} catch {
			assignmentMsg = 'Zuordnung konnte nicht geladen werden.';
		}
		assignmentLoading = false;
	}

	function toggleUseCase(id: string) {
		if (selected.has(id)) selected.delete(id);
		else selected.add(id);
		selected = new Set(selected);
	}

	async function saveAssignment(username: string) {
		assignmentSaving = true;
		assignmentMsg = '';
		try {
			const resp = await fetch(`/api/admin/users/${encodeURIComponent(username)}/use-cases`, {
				method: 'PUT',
				headers: { 'content-type': 'application/json' },
				body: JSON.stringify({ use_cases: [...selected] })
			});
			if (!resp.ok) {
				const body = await resp.json().catch(() => ({}));
				throw new Error(body.detail || 'Speichern fehlgeschlagen');
			}
			assignmentMsg = 'Gespeichert ✓';
		} catch (e) {
			assignmentMsg = e instanceof Error ? e.message : 'Fehler';
		}
		assignmentSaving = false;
	}

	onMount(load);
</script>

<svelte:head><title>Benutzer – Dashboard</title></svelte:head>

<div class="mx-auto max-w-3xl space-y-8 p-6">
	<div>
		<h2 class="text-xl font-bold text-gray-800">Benutzer</h2>
		<p class="mt-1 text-sm text-gray-500">
			Konten für Login und Dashboard-Zugriff. Klicke einen Benutzer an, um seine Use Cases festzulegen.
		</p>
	</div>

	{#if error}
		<p class="rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700">{error}</p>
	{/if}

	<!-- Create -->
	<div class="rounded-xl border border-gray-200 bg-white p-5 shadow-sm">
		<h3 class="mb-3 text-sm font-semibold text-gray-700">Neuen Benutzer anlegen</h3>
		<div class="flex flex-wrap items-end gap-3">
			<div class="flex-1">
				<label for="nu" class="mb-1 block text-xs text-gray-500">Benutzername</label>
				<input id="nu" bind:value={newUsername} class="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm" />
			</div>
			<div class="flex-1">
				<label for="np" class="mb-1 block text-xs text-gray-500">Passwort</label>
				<input id="np" type="password" bind:value={newPassword} class="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm" />
			</div>
			<div>
				<label for="nr" class="mb-1 block text-xs text-gray-500">Rolle</label>
				<select id="nr" bind:value={newRole} class="rounded-lg border border-gray-300 px-3 py-2 text-sm">
					<option value="user">user</option>
					<option value="admin">admin</option>
				</select>
			</div>
			<button
				onclick={createUser}
				disabled={creating}
				class="rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-60"
			>
				Anlegen
			</button>
		</div>
	</div>

	<!-- List -->
	{#if loading}
		<p class="animate-pulse text-sm text-gray-400">Lade…</p>
	{:else}
		<div class="overflow-hidden rounded-xl border border-gray-200 bg-white shadow-sm">
			<table class="w-full text-sm">
				<thead class="bg-gray-50 text-left text-xs uppercase text-gray-500">
					<tr>
						<th class="px-4 py-3">Benutzername</th>
						<th class="px-4 py-3">Rolle</th>
						<th class="px-4 py-3 text-right">Aktionen</th>
					</tr>
				</thead>
				<tbody class="divide-y divide-gray-100">
					{#each users as u (u.username)}
						<tr class="cursor-pointer hover:bg-gray-50" onclick={() => togglePanel(u)}>
							<td class="px-4 py-3 font-medium text-gray-800">
								<span class="mr-1 inline-block text-gray-400 transition-transform {expandedUser === u.username ? 'rotate-90' : ''}">▸</span>
								{u.username}
							</td>
							<td class="px-4 py-3">
								<select
									value={u.role}
									onclick={(e) => e.stopPropagation()}
									onchange={(e) => changeRole(u, e.currentTarget.value as 'admin' | 'user')}
									class="rounded border border-gray-300 px-2 py-1 text-xs"
								>
									<option value="user">user</option>
									<option value="admin">admin</option>
								</select>
							</td>
							<td class="px-4 py-3 text-right">
								<button onclick={(e) => { e.stopPropagation(); resetPassword(u); }} class="mr-2 text-xs text-blue-600 hover:underline">
									Passwort
								</button>
								<button onclick={(e) => { e.stopPropagation(); deleteUser(u); }} class="text-xs text-red-600 hover:underline">
									Löschen
								</button>
							</td>
						</tr>
						{#if expandedUser === u.username}
							<tr class="bg-gray-50/70">
								<td colspan="3" class="px-4 py-4">
									<p class="mb-2 text-xs font-semibold uppercase tracking-wider text-gray-500">
										Use Cases – wo darf {u.username} interagieren?
									</p>
									{#if u.role === 'admin'}
										<p class="rounded-lg bg-blue-50 px-3 py-2 text-sm text-blue-700">
											Administratoren haben Zugriff auf alle Use Cases.
										</p>
									{:else if assignmentLoading}
										<p class="animate-pulse text-sm text-gray-400">Lade Zuordnung…</p>
									{:else if useCases.length === 0}
										<p class="text-sm text-gray-400">Keine Use Cases vorhanden.</p>
									{:else}
										<div class="grid grid-cols-1 gap-2 sm:grid-cols-2">
											{#each useCases as uc}
												<label class="flex items-center gap-2 rounded-lg border border-gray-200 bg-white px-3 py-2 text-sm hover:bg-gray-50">
													<input
														type="checkbox"
														checked={selected.has(uc.id)}
														onchange={() => toggleUseCase(uc.id)}
														class="h-4 w-4 rounded border-gray-300"
													/>
													<span class="font-medium text-gray-700">{uc.label}</span>
													<span class="ml-auto font-mono text-[10px] text-gray-400">{uc.id}</span>
													{#if uc.enabled === false}
														<span class="rounded bg-gray-100 px-1.5 py-0.5 text-[10px] text-gray-500">deaktiviert</span>
													{/if}
												</label>
											{/each}
										</div>
										<div class="mt-3 flex items-center gap-3">
											<button
												onclick={() => saveAssignment(u.username)}
												disabled={assignmentSaving}
												class="rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-60"
											>
												{assignmentSaving ? 'Speichern…' : 'Zuordnung speichern'}
											</button>
											<span class="text-xs text-gray-500">{selected.size} ausgewählt</span>
											{#if assignmentMsg}
												<span class="text-xs {assignmentMsg.includes('✓') ? 'text-green-600' : 'text-red-600'}">{assignmentMsg}</span>
											{/if}
										</div>
									{/if}
								</td>
							</tr>
						{/if}
					{/each}
					{#if users.length === 0}
						<tr><td colspan="3" class="px-4 py-6 text-center text-gray-400">Keine Benutzer</td></tr>
					{/if}
				</tbody>
			</table>
		</div>
	{/if}
</div>
