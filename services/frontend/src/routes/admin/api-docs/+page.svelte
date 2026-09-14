<script lang="ts">
	/**
	 * API reference, rendered from the description the pipeline generates about
	 * itself (`/api/openapi.json`). Nothing on this page is transcribed by hand,
	 * so it cannot describe an endpoint that no longer exists or miss a field
	 * that was just added.
	 */
	import { onMount } from 'svelte';
	import { marked } from 'marked';
	import {
		curlFor,
		listOperations,
		propertyRows,
		responseSchema,
		sampleValue,
		typeLabel,
		refName,
		type OpenApiSpec,
		type Operation
	} from '$lib/openapi';

	let spec: OpenApiSpec | null = $state(null);
	let loading = $state(true);
	let error = $state('');

	// Base URL and token only shape the examples on this page; the token is kept
	// in memory and never stored, so a shared browser cannot leak it.
	let baseUrl = $state('');
	let token = $state('');
	let testResult = $state('');
	let testOk = $state(false);
	let testing = $state(false);
	let copied = $state('');

	const operations = $derived(spec ? listOperations(spec) : ([] as Operation[]));

	const METHOD_STYLE: Record<string, string> = {
		GET: 'bg-sky-100 text-sky-800',
		POST: 'bg-emerald-100 text-emerald-800',
		PUT: 'bg-amber-100 text-amber-800',
		DELETE: 'bg-red-100 text-red-800'
	};

	function statusStyle(status: string): string {
		if (status.startsWith('2')) return 'text-emerald-700';
		if (status.startsWith('4')) return 'text-amber-700';
		return 'text-red-700';
	}

	function md(text: string): string {
		return marked.parse(text ?? '', { gfm: true, breaks: false }) as string;
	}

	function pretty(value: unknown): string {
		return JSON.stringify(value, null, 2);
	}

	async function copy(text: string, key: string) {
		try {
			await navigator.clipboard.writeText(text);
			copied = key;
			setTimeout(() => (copied = ''), 1500);
		} catch {
			copied = '';
		}
	}

	async function testToken() {
		if (!token.trim()) {
			testOk = false;
			testResult = 'Bitte zuerst ein Token einfügen.';
			return;
		}
		testing = true;
		testResult = '';
		try {
			const resp = await fetch('/api/v1/me', {
				headers: { Authorization: `Bearer ${token.trim()}` }
			});
			const body = await resp.json().catch(() => ({}));
			testOk = resp.ok;
			testResult = resp.ok
				? `Gültig · Bereich: ${body.use_cases?.length ? body.use_cases.join(', ') : 'alle Use Cases'}`
				: `HTTP ${resp.status} · ${body.detail ?? 'Token abgelehnt'}`;
		} catch (e) {
			testOk = false;
			testResult = e instanceof Error ? e.message : 'Anfrage fehlgeschlagen';
		}
		testing = false;
	}

	onMount(async () => {
		baseUrl = window.location.origin;
		try {
			const resp = await fetch('/api/openapi.json');
			if (!resp.ok) throw new Error(`Beschreibung nicht abrufbar (HTTP ${resp.status})`);
			spec = await resp.json();
		} catch (e) {
			error = e instanceof Error ? e.message : 'Fehler';
		}
		loading = false;
	});
</script>

<svelte:head><title>API-Dokumentation – Dashboard</title></svelte:head>

<div class="mx-auto max-w-4xl space-y-8 p-6">
	<div>
		<h2 class="text-xl font-bold text-gray-800">
			{spec?.info?.title ?? 'API-Dokumentation'}
		</h2>
		<p class="mt-1 text-sm text-gray-500">
			Direkt aus der laufenden Pipeline erzeugt — Endpunkte, Felder und Antwortformate
			stammen aus dem Code, nicht aus einer gepflegten Kopie.
			{#if spec?.info?.version}
				<span class="ml-1 rounded bg-gray-100 px-1.5 py-0.5 font-mono text-xs text-gray-600">
					v{spec.info.version}
				</span>
			{/if}
			<a href="/api/v1/openapi.json" class="ml-1 text-blue-600 hover:underline" target="_blank" rel="noreferrer">
				openapi.json
			</a>
		</p>
	</div>

	{#if error}
		<p class="rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700">{error}</p>
	{/if}

	{#if loading}
		<p class="animate-pulse text-sm text-gray-400">Lade Beschreibung…</p>
	{:else if spec}
		{#if spec.info?.description}
			<div class="prose prose-sm max-w-none rounded-xl border border-gray-200 bg-white p-5 shadow-sm prose-headings:text-gray-800 prose-code:rounded prose-code:bg-gray-100 prose-code:px-1 prose-code:py-0.5 prose-code:before:content-none prose-code:after:content-none">
				<!-- eslint-disable-next-line svelte/no-at-html-tags -->
				{@html md(spec.info.description)}
			</div>
		{/if}

		<!-- Playground settings -->
		<div class="rounded-xl border border-gray-200 bg-white p-5 shadow-sm">
			<h3 class="mb-1 text-sm font-semibold text-gray-700">Beispiele anpassen</h3>
			<p class="mb-3 text-xs text-gray-500">
				Basis-URL und Token werden in die Beispiele unten eingesetzt. Das Token bleibt nur im
				Arbeitsspeicher dieser Seite und wird nirgends gespeichert.
				<a href="/admin/api-tokens" class="text-blue-600 hover:underline">Token verwalten →</a>
			</p>
			<div class="flex flex-wrap items-end gap-3">
				<div class="flex-1">
					<label for="base" class="mb-1 block text-xs text-gray-500">Basis-URL</label>
					<input
						id="base"
						bind:value={baseUrl}
						class="w-full rounded-lg border border-gray-300 px-3 py-2 font-mono text-xs"
					/>
				</div>
				<div class="flex-1">
					<label for="tok" class="mb-1 block text-xs text-gray-500">Token (optional)</label>
					<input
						id="tok"
						bind:value={token}
						type="password"
						placeholder="kmt_…"
						class="w-full rounded-lg border border-gray-300 px-3 py-2 font-mono text-xs"
					/>
				</div>
				<button
					onclick={testToken}
					disabled={testing}
					class="rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-60"
				>
					Token testen
				</button>
			</div>
			{#if testResult}
				<p class="mt-3 rounded-lg px-3 py-2 text-sm {testOk ? 'bg-green-50 text-green-700' : 'bg-red-50 text-red-700'}">
					{testResult}
				</p>
			{/if}
		</div>

		<!-- Jump marks -->
		<div class="flex flex-wrap gap-2">
			{#each operations as op}
				<a
					href="#{op.id}"
					class="flex items-center gap-2 rounded-lg border border-gray-200 bg-white px-3 py-1.5 text-xs shadow-sm hover:border-blue-300"
				>
					<span class="rounded px-1.5 py-0.5 font-mono text-[10px] font-bold {METHOD_STYLE[op.method]}">
						{op.method}
					</span>
					<span class="text-gray-600">{op.summary}</span>
				</a>
			{/each}
		</div>

		<!-- Endpoints -->
		{#each operations as op (op.id)}
			{@const request = op.requestSchema}
			{@const response = responseSchema(op)}
			{@const responseRows = response ? propertyRows(response, spec) : []}
			{@const pathParams = op.parameters.filter((p) => p.in === 'path' || p.in === 'query')}
			{@const curl = curlFor(op, spec, baseUrl, token)}
			<section id={op.id} class="scroll-mt-4 space-y-4 rounded-xl border border-gray-200 bg-white p-5 shadow-sm">
				<div>
					<div class="flex flex-wrap items-center gap-2">
						<span class="rounded px-2 py-1 font-mono text-xs font-bold {METHOD_STYLE[op.method]}">
							{op.method}
						</span>
						<code class="font-mono text-sm text-gray-800">{op.path}</code>
					</div>
					<h3 class="mt-2 text-base font-semibold text-gray-800">{op.summary}</h3>
				</div>

				{#if op.description}
					<div class="prose prose-sm max-w-none text-gray-600 prose-code:rounded prose-code:bg-gray-100 prose-code:px-1 prose-code:py-0.5 prose-code:before:content-none prose-code:after:content-none">
						<!-- eslint-disable-next-line svelte/no-at-html-tags -->
						{@html md(op.description)}
					</div>
				{/if}

				{#if pathParams.length}
					<div>
						<h4 class="mb-2 text-xs font-semibold uppercase tracking-wider text-gray-500">
							Parameter
						</h4>
						<table class="w-full text-sm">
							<tbody class="divide-y divide-gray-100">
								{#each pathParams as param}
									<tr>
										<td class="py-2 pr-3 align-top font-mono text-xs text-gray-800">{param.name}</td>
										<td class="py-2 pr-3 align-top font-mono text-xs text-gray-500">
											{typeLabel(param.schema, spec)}
										</td>
										<td class="py-2 align-top text-xs text-gray-600">
											{param.description ?? param.schema?.description ?? ''}
										</td>
									</tr>
								{/each}
							</tbody>
						</table>
					</div>
				{/if}

				{#if request}
					<div>
						<h4 class="mb-2 text-xs font-semibold uppercase tracking-wider text-gray-500">
							Request-Body
							{#if refName(request)}
								<span class="ml-1 font-mono text-[10px] normal-case text-gray-400">
									{refName(request)}
								</span>
							{/if}
						</h4>
						<table class="w-full text-sm">
							<thead class="text-left text-[10px] uppercase text-gray-400">
								<tr>
									<th class="pb-1 pr-3 font-medium">Feld</th>
									<th class="pb-1 pr-3 font-medium">Typ</th>
									<th class="pb-1 font-medium">Beschreibung</th>
								</tr>
							</thead>
							<tbody class="divide-y divide-gray-100">
								{#each propertyRows(request, spec) as row}
									<tr>
										<td class="whitespace-nowrap py-2 pr-3 align-top">
											<code class="font-mono text-xs text-gray-800">{row.name}</code>
											{#if row.required}
												<span class="ml-1 text-[10px] font-semibold text-red-600">Pflicht</span>
											{/if}
										</td>
										<td class="py-2 pr-3 align-top font-mono text-xs text-gray-500">{row.type}</td>
										<td class="py-2 align-top text-xs text-gray-600">
											<!-- eslint-disable-next-line svelte/no-at-html-tags -->
											<span class="[&_code]:rounded [&_code]:bg-gray-100 [&_code]:px-1 [&_p]:m-0">
												{@html md(row.description)}
											</span>
											{#if row.defaultValue}
												<span class="ml-1 font-mono text-[10px] text-gray-400">
													Standard: {row.defaultValue}
												</span>
											{/if}
										</td>
									</tr>
								{/each}
							</tbody>
						</table>
					</div>
				{/if}

				{#if responseRows.length}
					<div>
						<h4 class="mb-2 text-xs font-semibold uppercase tracking-wider text-gray-500">
							Antwort
							{#if refName(response)}
								<span class="ml-1 font-mono text-[10px] normal-case text-gray-400">
									{refName(response)}
								</span>
							{/if}
						</h4>
						<table class="w-full text-sm">
							<tbody class="divide-y divide-gray-100">
								{#each responseRows as row}
									<tr>
										<td class="py-2 pr-3 align-top font-mono text-xs text-gray-800">{row.name}</td>
										<td class="py-2 pr-3 align-top font-mono text-xs text-gray-500">{row.type}</td>
										<td class="py-2 align-top text-xs text-gray-600">
											<!-- eslint-disable-next-line svelte/no-at-html-tags -->
											<span class="[&_code]:rounded [&_code]:bg-gray-100 [&_code]:px-1 [&_p]:m-0">
												{@html md(row.description)}
											</span>
										</td>
									</tr>
								{/each}
							</tbody>
						</table>
					</div>
				{/if}

				<div>
					<h4 class="mb-2 text-xs font-semibold uppercase tracking-wider text-gray-500">
						Statuscodes
					</h4>
					<ul class="space-y-1 text-xs">
						{#each op.responses as resp}
							<li class="flex gap-3">
								<code class="w-10 shrink-0 font-mono font-semibold {statusStyle(resp.status)}">
									{resp.status}
								</code>
								<span class="text-gray-600">{resp.description}</span>
							</li>
						{/each}
					</ul>
				</div>

				<div>
					<div class="mb-2 flex items-center justify-between">
						<h4 class="text-xs font-semibold uppercase tracking-wider text-gray-500">Beispiel</h4>
						<button
							onclick={() => copy(curl, op.id)}
							class="text-xs text-blue-600 hover:underline"
						>
							{copied === op.id ? 'Kopiert ✓' : 'Kopieren'}
						</button>
					</div>
					<pre class="overflow-x-auto rounded-lg bg-gray-900 p-4 font-mono text-xs leading-relaxed text-gray-100"><code>{curl}</code></pre>
				</div>

				{#if responseRows.length}
					<details class="group">
						<summary class="cursor-pointer text-xs font-semibold uppercase tracking-wider text-gray-500 hover:text-gray-700">
							Beispiel-Antwort
						</summary>
						<pre class="mt-2 overflow-x-auto rounded-lg border border-gray-200 bg-gray-50 p-4 font-mono text-xs leading-relaxed text-gray-700"><code>{pretty(sampleValue(response, spec))}</code></pre>
					</details>
				{/if}
			</section>
		{/each}
	{/if}
</div>
