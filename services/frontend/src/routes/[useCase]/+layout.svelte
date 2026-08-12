<script lang="ts">
	import { onMount, untrack } from 'svelte';
	import { page } from '$app/stores';
	import { goto } from '$app/navigation';
	import { initSession, resetChat, app } from '$lib/state.svelte';
	import { getUseCaseCollections } from '$lib/api';
	import { type UseCaseDef } from '$lib/use-cases';
	import FileUpload from '$lib/components/FileUpload.svelte';

	let { children, data } = $props();

	const uc: UseCaseDef = $derived(data.useCase);
	const useCases: UseCaseDef[] = $derived(data.useCases ?? []);
	const currentUser = $derived(data.user);

	async function logout() {
		await fetch('/api/auth/logout', { method: 'POST' });
		goto('/login');
	}

	onMount(() => {
		initSession();
	});

	// Sync route param → app state (hard-locked). On a real use-case switch the
	// chat is reset so messages never carry over from another use case. We
	// compare against the persisted app.useCase (module state) rather than a
	// component-local flag, so it also triggers when the user left the use-case
	// section entirely (e.g. via /datenbank) and returns to a different one.
	// Same-use-case navigation (tab switch, restoring a history conversation)
	// keeps app.useCase equal to uc.apiId, so the transcript is preserved.
	// untrack() reads app.useCase non-reactively so writing it can't loop.
	$effect(() => {
		const next = uc.apiId;
		if (untrack(() => app.useCase) !== next) {
			resetChat();
			app.useCase = next;
		}
	});

	// Load collections for this use case
	$effect(() => {
		app.collectionsLoading = true;
		getUseCaseCollections(uc.apiId)
			.then((d) => {
				app.availableCollections = d.collections ?? [];
			})
			.catch(() => {
				app.availableCollections = [];
			})
			.finally(() => {
				app.collectionsLoading = false;
			});
	});

	const tabs = [
		{ id: 'chat' as const, label: 'Chat', icon: '&#128172;', href: '' },
		{ id: 'tabelle' as const, label: 'Tabelle', icon: '&#128196;', href: '/tabelle' },
		{ id: 'documents' as const, label: 'Dokumente', icon: '&#128196;', href: '/documents' },
		{ id: 'history' as const, label: 'Verlauf', icon: '&#128337;', href: '/history' },
		{ id: 'eval' as const, label: 'Eval', icon: '&#128202;', href: '/eval' },
		{ id: 'settings' as const, label: 'Einstellungen', icon: '&#9881;', href: '/settings' }
	];

	const currentPath = $derived($page.url.pathname);

	function isActiveTab(href: string): boolean {
		const base = `/${uc.slug}`;
		if (href === '') return currentPath === base || currentPath === base + '/';
		return currentPath.startsWith(base + href);
	}
</script>

<svelte:head>
	<title>{uc.label} – RAG Platform</title>
</svelte:head>

<div class="flex h-full">
	<!-- Sidebar -->
	<aside class="flex h-full w-80 shrink-0 flex-col bg-gray-900 text-white">
		<!-- Header -->
		<div class="border-b border-gray-700 p-5">
			<h1 class="text-lg font-bold tracking-tight">RAG Platform</h1>
			<p class="mt-1 text-xs text-gray-400">Prototyp v0.1</p>
		</div>

		<!-- Use Case Navigation -->
		<div class="space-y-2 p-4">
			<p class="mb-2 text-xs uppercase tracking-wider text-gray-400">Use Case</p>
			{#each useCases as item}
				<a
					href="/{item.slug}"
					class="block w-full rounded-lg p-3 text-left transition-colors
						{item.slug === uc.slug
						? item.color + ' text-white shadow-lg'
						: 'bg-gray-800 text-gray-300 hover:bg-gray-700'}"
				>
					<div class="text-sm font-medium">{item.label}</div>
					<div class="text-xs opacity-75">{item.desc}</div>
				</a>
			{/each}
		</div>

		<!-- Locked Use Case Badge -->
		<div class="px-4 pb-2">
			<div
				class="flex items-center gap-2 rounded-lg border border-gray-700 bg-gray-800/50 px-3 py-2.5"
			>
				<svg class="h-4 w-4 shrink-0 text-red-400" viewBox="0 0 20 20" fill="currentColor">
					<path
						fill-rule="evenodd"
						d="M5 9V7a5 5 0 0110 0v2a2 2 0 012 2v5a2 2 0 01-2 2H5a2 2 0 01-2-2v-5a2 2 0 012-2zm8-2v2H7V7a3 3 0 016 0z"
						clip-rule="evenodd"
					/>
				</svg>
				<div class="min-w-0 flex-1">
					<div class="truncate text-xs font-semibold text-white">{uc.label}</div>
					<div class="text-[10px] text-gray-400">Use Case fixiert</div>
				</div>
				<span
					class="shrink-0 rounded bg-red-600/80 px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-wider text-white"
				>
					locked
				</span>
			</div>
		</div>

		<!-- Available Sub-Collections -->
		<div class="px-4 pb-4">
			<p class="mb-2 text-xs uppercase tracking-wider text-gray-400">
				Verfuegbare Quellen
				{#if app.collectionsLoading}
					<span class="ml-1 animate-pulse text-gray-500">...</span>
				{/if}
			</p>
			{#if app.availableCollections.length > 0}
				<div class="flex flex-wrap gap-1.5">
					{#each app.availableCollections as col}
						<span
							class="inline-flex items-center gap-1 rounded-full border border-gray-700 bg-gray-800 px-2.5 py-1 text-[11px] text-gray-300"
							title="{col.name}: {col.count} Chunks, {col.dimension}D"
						>
							<span class="h-1.5 w-1.5 rounded-full bg-emerald-400"></span>
							{col.name}
							<span class="text-gray-500">({col.count})</span>
						</span>
					{/each}
				</div>
				<p class="mt-2 text-[10px] text-gray-500">Der Agent sucht frei in diesen Quellen.</p>
			{:else if !app.collectionsLoading}
				<p class="text-xs text-gray-500">Keine Collections vorhanden.</p>
			{/if}
		</div>

		<!-- Role selector (Wiener Linien only) -->
		{#if uc.apiId === 'wiener_linien'}
			<div class="px-4 pb-4">
				<p class="mb-2 text-xs uppercase tracking-wider text-gray-400">Rolle</p>
				<select
					bind:value={app.role}
					class="w-full rounded-lg border border-gray-600 bg-gray-800 px-3 py-2 text-sm text-gray-200 focus:border-purple-400 focus:outline-none"
				>
					<option value="default">Fachpersonal</option>
					<option value="trainee">Auszubildender</option>
				</select>
			</div>
		{/if}

		<!-- File Upload -->
		<div class="px-4 pb-4">
			<FileUpload />
		</div>

		<!-- Datenbank Link -->
		<div class="px-4 pb-4">
			<a
				href="/datenbank"
				class="flex w-full items-center gap-2 rounded-lg border border-gray-700 bg-gray-800 p-3 text-left text-sm text-gray-300 transition-colors hover:bg-gray-700"
			>
				<svg class="h-4 w-4 text-gray-400" viewBox="0 0 20 20" fill="currentColor">
					<path
						d="M3 12v3c0 1.657 3.134 3 7 3s7-1.343 7-3v-3c0 1.657-3.134 3-7 3s-7-1.343-7-3z"
					/>
					<path
						d="M3 7v3c0 1.657 3.134 3 7 3s7-1.343 7-3V7c0 1.657-3.134 3-7 3S3 8.657 3 7z"
					/>
					<path d="M17 5c0 1.657-3.134 3-7 3S3 6.657 3 5s3.134-3 7-3 7 1.343 7 3z" />
				</svg>
				Datenbank
			</a>
		</div>

		<!-- Session Info -->
		<div class="mt-auto border-t border-gray-700 p-4">
			{#if currentUser?.role === 'admin'}
				<a
					href="/admin/use-cases"
					class="mb-2 block rounded-lg bg-gray-800 px-3 py-2 text-sm text-gray-200 hover:bg-gray-700"
				>
					⚙ Dashboard
				</a>
			{/if}
			{#if currentUser}
				<div class="flex items-center justify-between gap-2">
					<p class="truncate text-xs text-gray-400">{currentUser.username} · {currentUser.role}</p>
					<button onclick={logout} class="shrink-0 text-xs text-gray-400 hover:text-white">
						Abmelden
					</button>
				</div>
			{/if}
			<p class="mt-1 truncate text-xs text-gray-600">Session: {app.sessionId.slice(0, 8)}...</p>
		</div>
	</aside>

	<!-- Main content area -->
	<div class="flex flex-1 flex-col">
		<!-- Tab Bar -->
		<div class="flex border-b border-gray-200 bg-white px-4">
			{#each tabs as tab}
				{@const active = isActiveTab(tab.href)}
				<a
					href="/{uc.slug}{tab.href}"
					class="relative px-4 py-3 text-sm font-medium transition-colors
						{active ? 'text-blue-600' : 'text-gray-500 hover:text-gray-700'}"
				>
					<span class="mr-1.5">{@html tab.icon}</span>
					{tab.label}
					{#if active}
						<span class="absolute bottom-0 left-0 right-0 h-0.5 bg-blue-600"></span>
					{/if}
				</a>
			{/each}
		</div>

		<!-- Tab Content -->
		{@render children()}
	</div>
</div>
