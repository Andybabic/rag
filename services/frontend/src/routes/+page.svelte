<script lang="ts">
	import type { UseCaseDef } from '$lib/use-cases';

	// Merged layout + page data. If the page load found a use case it already
	// redirected, so reaching here means the list is empty.
	let { data } = $props();
	const useCases: UseCaseDef[] = $derived(data.useCases ?? []);
	const user = $derived(data.user);
</script>

{#if useCases.length > 0}
	<p class="p-6 text-sm text-gray-500">Weiterleitung…</p>
{:else}
	<div class="flex h-full items-center justify-center p-6">
		<div class="max-w-md rounded-xl border border-gray-200 bg-white p-6 text-center shadow-sm">
			<h1 class="text-lg font-bold text-gray-800">Keine Use Cases verfügbar</h1>
			{#if user?.role === 'admin'}
				<p class="mt-2 text-sm text-gray-500">
					Es sind aktuell keine Use Cases angelegt. Lege im Dashboard einen an.
				</p>
				<a
					href="/admin/use-cases"
					class="mt-4 inline-block rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700"
				>
					Zum Dashboard
				</a>
			{:else}
				<p class="mt-2 text-sm text-gray-500">
					Dir wurde noch kein Use Case zugewiesen. Bitte wende dich an eine:n Administrator:in.
				</p>
			{/if}
		</div>
	</div>
{/if}
