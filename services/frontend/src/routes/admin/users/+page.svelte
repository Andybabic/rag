<script lang="ts">
	import { onMount } from 'svelte';

	interface UserRow {
		id: string;
		username: string;
		role: 'admin' | 'user';
		created_at: string | null;
	}

	let users: UserRow[] = $state([]);
	let loading = $state(true);
	let error = $state('');

	// new-user form
	let newUsername = $state('');
	let newPassword = $state('');
	let newRole: 'admin' | 'user' = $state('user');
	let creating = $state(false);

	async function load() {
		loading = true;
		error = '';
		try {
			const resp = await fetch('/api/admin/users');
			if (!resp.ok) throw new Error('Laden fehlgeschlagen');
			users = await resp.json();
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
		await load();
	}

	onMount(load);
</script>

<svelte:head><title>Benutzer – Dashboard</title></svelte:head>

<div class="mx-auto max-w-3xl space-y-8 p-6">
	<div>
		<h2 class="text-xl font-bold text-gray-800">Benutzer</h2>
		<p class="mt-1 text-sm text-gray-500">Konten für Login und Dashboard-Zugriff.</p>
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
					{#each users as u}
						<tr>
							<td class="px-4 py-3 font-medium text-gray-800">{u.username}</td>
							<td class="px-4 py-3">
								<select
									value={u.role}
									onchange={(e) => changeRole(u, e.currentTarget.value as 'admin' | 'user')}
									class="rounded border border-gray-300 px-2 py-1 text-xs"
								>
									<option value="user">user</option>
									<option value="admin">admin</option>
								</select>
							</td>
							<td class="px-4 py-3 text-right">
								<button onclick={() => resetPassword(u)} class="mr-2 text-xs text-blue-600 hover:underline">
									Passwort
								</button>
								<button onclick={() => deleteUser(u)} class="text-xs text-red-600 hover:underline">
									Löschen
								</button>
							</td>
						</tr>
					{/each}
					{#if users.length === 0}
						<tr><td colspan="3" class="px-4 py-6 text-center text-gray-400">Keine Benutzer</td></tr>
					{/if}
				</tbody>
			</table>
		</div>
	{/if}
</div>
