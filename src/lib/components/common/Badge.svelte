<script lang="ts">
	export let type = 'info';
	export let content = '';

	const classNames: Record<string, string> = {
		info: 'bg-brand-soft text-brand-text dark:text-brand-text ',
		success: 'bg-green-500/20 text-green-700 dark:text-green-200',
		warning: 'bg-brand-soft text-brand-text dark:text-brand-text',
		error: 'bg-red-500/20 text-red-700 dark:text-red-200',
		muted: 'bg-gray-500/20 text-gray-700 dark:text-gray-200'
	};
</script>

<div
	class=" text-xs font-normal {classNames[type] ??
		classNames['info']} w-fit px-[0.3125rem] rounded-lg uppercase line-clamp-1 mr-0.5"
>
	{content}
</div>
