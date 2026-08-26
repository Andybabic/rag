<script lang="ts">
	import './layout.css';

	let { children } = $props();
</script>

<div class="h-full overflow-hidden">
	{@render children()}
</div>
