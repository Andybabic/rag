<script lang="ts">
	export type StepStatus = 'pending' | 'current' | 'done' | 'error';

	export interface Step {
		id: string;
		label: string;
		status: StepStatus;
	}

	interface Props {
		steps: Step[];
	}

	let { steps }: Props = $props();
</script>

<ol class="flex w-full items-center gap-2">
	{#each steps as s, i}
		{@const isLast = i === steps.length - 1}
		<li class="flex flex-1 items-center gap-2">
			<div class="flex items-center gap-2">
				<span
					class="flex h-7 w-7 shrink-0 items-center justify-center rounded-full text-[11px] font-semibold transition-colors
						{s.status === 'done'
							? 'bg-emerald-600 text-white'
							: s.status === 'current'
							? 'bg-blue-600 text-white ring-4 ring-blue-100'
							: s.status === 'error'
							? 'bg-red-600 text-white'
							: 'bg-gray-100 text-gray-400'}"
				>
					{#if s.status === 'done'}
						✓
					{:else if s.status === 'error'}
						!
					{:else}
						{i + 1}
					{/if}
				</span>
				<span
					class="text-xs font-medium whitespace-nowrap
						{s.status === 'current'
							? 'text-blue-700'
							: s.status === 'done'
							? 'text-gray-700'
							: s.status === 'error'
							? 'text-red-700'
							: 'text-gray-400'}"
				>
					{s.label}
				</span>
			</div>
			{#if !isLast}
				<div
					class="h-px flex-1 transition-colors
						{s.status === 'done' ? 'bg-emerald-300' : 'bg-gray-200'}"
				></div>
			{/if}
		</li>
	{/each}
</ol>
