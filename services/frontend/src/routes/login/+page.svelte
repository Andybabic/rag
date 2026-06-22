<script lang="ts">
	import { enhance } from '$app/forms';
	import type { ActionData } from './$types';

	let { form }: { form: ActionData } = $props();
	let submitting = $state(false);
</script>

<svelte:head>
	<title>Anmeldung</title>
</svelte:head>

<div class="flex min-h-screen items-center justify-center bg-gray-100 px-4">
	<div class="w-full max-w-sm rounded-2xl bg-white p-8 shadow-lg">
		<h1 class="mb-1 text-2xl font-semibold text-gray-900">Anmeldung</h1>
		<p class="mb-6 text-sm text-gray-500">Bitte melde dich an, um fortzufahren.</p>

		<form
			method="POST"
			use:enhance={() => {
				submitting = true;
				return async ({ update }) => {
					await update();
					submitting = false;
				};
			}}
			class="space-y-4"
		>
			<div>
				<label for="username" class="mb-1 block text-sm font-medium text-gray-700">
					Benutzername
				</label>
				<input
					id="username"
					name="username"
					type="text"
					autocomplete="username"
					required
					value={form?.username ?? ''}
					class="w-full rounded-lg border border-gray-300 px-3 py-2 text-gray-900 focus:border-blue-500 focus:ring-1 focus:ring-blue-500 focus:outline-none"
				/>
			</div>

			<div>
				<label for="password" class="mb-1 block text-sm font-medium text-gray-700">
					Passwort
				</label>
				<input
					id="password"
					name="password"
					type="password"
					autocomplete="current-password"
					required
					class="w-full rounded-lg border border-gray-300 px-3 py-2 text-gray-900 focus:border-blue-500 focus:ring-1 focus:ring-blue-500 focus:outline-none"
				/>
			</div>

			{#if form?.error}
				<p class="rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700" role="alert">
					{form.error}
				</p>
			{/if}

			<button
				type="submit"
				disabled={submitting}
				class="w-full rounded-lg bg-blue-600 px-4 py-2 font-medium text-white transition hover:bg-blue-700 disabled:opacity-60"
			>
				{submitting ? 'Anmelden…' : 'Anmelden'}
			</button>
		</form>
	</div>
</div>
