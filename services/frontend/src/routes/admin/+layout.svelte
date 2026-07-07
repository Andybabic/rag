<script lang="ts">
	import { page } from '$app/stores';
	import { goto } from '$app/navigation';

	let { children, data } = $props();

	const links = [
		{ href: '/admin/use-cases', label: 'Use Cases' },
		{ href: '/admin/users', label: 'Benutzer' },
		{ href: '/admin/feedback', label: 'Feedback' }
	];

	function isActive(href: string): boolean {
		return $page.url.pathname.startsWith(href);
	}

	async function logout() {
		await fetch('/api/auth/logout', { method: 'POST' });
		goto('/login');
	}
</script>

<div class="flex h-full">
	<aside class="flex h-full w-72 shrink-0 flex-col bg-gray-900 text-white">
		<div class="border-b border-gray-700 p-5">
			<h1 class="text-lg font-bold tracking-tight">Dashboard</h1>
			<p class="mt-1 text-xs text-gray-400">Verwaltung</p>
		</div>

		<nav class="space-y-1 p-4">
			{#each links as link}
				<a
					href={link.href}
					class="block rounded-lg px-3 py-2 text-sm font-medium transition-colors
						{isActive(link.href)
						? 'bg-blue-600 text-white'
						: 'text-gray-300 hover:bg-gray-800'}"
				>
					{link.label}
				</a>
			{/each}
			<a
				href="/"
				class="mt-2 block rounded-lg px-3 py-2 text-sm text-gray-400 hover:bg-gray-800"
			>
				← Zur App
			</a>
		</nav>

		<div class="mt-auto border-t border-gray-700 p-4">
			{#if data.user}
				<p class="mb-2 truncate text-xs text-gray-400">
					{data.user.username} · {data.user.role}
				</p>
			{/if}
			<button
				onclick={logout}
				class="w-full rounded-lg bg-gray-800 px-3 py-2 text-sm text-gray-200 hover:bg-gray-700"
			>
				Abmelden
			</button>
		</div>
	</aside>

	<main class="flex-1 overflow-y-auto bg-gray-50">
		{@render children()}
	</main>
</div>
