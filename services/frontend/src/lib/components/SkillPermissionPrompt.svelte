<script lang="ts">
	/**
	 * "Skill X möchte Y ausführen — erlauben?"
	 *
	 * Shown inside a streaming answer while the server waits. The pipeline is
	 * genuinely blocked on this, so the component states what will run, with
	 * which arguments and against which hosts, and does not let the question
	 * be dismissed by ignoring it: no answer is a denial, and the countdown
	 * says so.
	 */
	import { untrack } from 'svelte';
	import { app, type PermissionPrompt } from '$lib/state.svelte';
	import { respondToPermission } from '$lib/api';

	let { prompt }: { prompt: PermissionPrompt } = $props();

	// The deadline is fixed when the question is asked, so this deliberately
	// reads the initial value only. untrack() states that outright rather than
	// leaving it to look like a missed reactive dependency.
	let remaining = $state(untrack(() => prompt.timeout_s ?? 120));

	$effect(() => {
		if (prompt.approved !== undefined) return;
		const timer = setInterval(() => {
			remaining = Math.max(0, remaining - 1);
			if (remaining === 0) clearInterval(timer);
		}, 1000);
		return () => clearInterval(timer);
	});

	function renderArg(value: unknown): string {
		if (typeof value === 'string') return `"${value}"`;
		if (value === null || value === undefined) return 'null';
		return String(value);
	}

	async function decide(approved: boolean) {
		if (prompt.submitting || prompt.approved !== undefined) return;
		prompt.submitting = true;
		prompt.error = undefined;
		try {
			const result = await respondToPermission(prompt.call_id, approved, app.sessionId);
			if (result.error) {
				// Almost always an expired request. The function did not run —
				// say that rather than leaving an inert dialog on screen.
				prompt.error = result.detail ?? result.error;
				prompt.approved = false;
			} else {
				prompt.approved = approved;
			}
		} catch (e) {
			prompt.error = e instanceof Error ? e.message : String(e);
		} finally {
			prompt.submitting = false;
		}
	}
</script>

<div
	class="my-2 rounded-xl border px-3.5 py-3 text-sm {prompt.approved === undefined
		? 'border-amber-300 bg-amber-50'
		: prompt.approved
			? 'border-emerald-200 bg-emerald-50'
			: 'border-gray-200 bg-gray-50'}"
>
	<div class="flex items-start gap-2.5">
		<span class="mt-0.5 text-base" aria-hidden="true">
			{prompt.approved === undefined ? '🔐' : prompt.approved ? '✅' : '🚫'}
		</span>
		<div class="min-w-0 flex-1">
			<p class="font-semibold text-gray-900">
				{#if prompt.approved === undefined}
					Freigabe erforderlich
				{:else if prompt.approved}
					Ausführung freigegeben
				{:else}
					Ausführung abgelehnt
				{/if}
			</p>

			<p class="mt-1 text-gray-700">
				Der Skill <span class="font-medium">{prompt.skill}</span> möchte
				<code class="rounded bg-white/70 px-1 py-0.5 font-mono text-[12.5px] text-gray-900">
					{prompt.function}({prompt.args.map(renderArg).join(', ')})
				</code>
				ausführen.
			</p>

			{#if prompt.description}
				<p class="mt-1 text-gray-600">{prompt.description}</p>
			{/if}

			{#if prompt.allowed_domains && prompt.allowed_domains.length > 0}
				<p class="mt-1 text-[12.5px] text-gray-500">
					Netzzugriff auf: {prompt.allowed_domains.join(', ')}
				</p>
			{:else}
				<p class="mt-1 text-[12.5px] text-gray-500">Kein Netzzugriff.</p>
			{/if}

			{#if prompt.error}
				<p class="mt-1.5 text-[12.5px] text-red-600">{prompt.error}</p>
			{/if}

			{#if prompt.approved === undefined}
				<div class="mt-2.5 flex items-center gap-2">
					<button
						onclick={() => decide(true)}
						disabled={prompt.submitting || remaining === 0}
						class="rounded-lg bg-gray-900 px-3 py-1.5 text-[13px] font-medium text-white hover:bg-black disabled:opacity-50"
					>
						Erlauben
					</button>
					<button
						onclick={() => decide(false)}
						disabled={prompt.submitting}
						class="rounded-lg border border-gray-300 bg-white px-3 py-1.5 text-[13px] font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-50"
					>
						Ablehnen
					</button>
					<span class="text-[12px] text-gray-500">
						{#if remaining > 0}
							Ohne Antwort wird in {remaining}s abgelehnt.
						{:else}
							Zeit abgelaufen — die Funktion wurde nicht ausgeführt.
						{/if}
					</span>
				</div>
			{/if}
		</div>
	</div>
</div>
