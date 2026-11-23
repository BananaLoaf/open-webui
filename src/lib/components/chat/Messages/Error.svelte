<script context="module" lang="ts">
	export const getErrorMessage = (value: unknown): string => {
		if (typeof value === 'string') {
			return value;
		}

		if (typeof value === 'object' && value !== null) {
			const error = 'error' in value ? value.error : null;

			if (
				typeof error === 'object' &&
				error !== null &&
				'message' in error &&
				typeof error.message === 'string'
			) {
				return error.message;
			}

			if ('detail' in value && typeof value.detail === 'string') {
				return value.detail;
			}

			if ('message' in value && typeof value.message === 'string') {
				return value.message;
			}

			return JSON.stringify(value) ?? String(value);
		}

		return JSON.stringify(value) ?? String(value);
	};
</script>

<script lang="ts">
	import { getContext } from 'svelte';
	import type { Writable } from 'svelte/store';
	import type { i18n as I18n } from 'i18next';
	import Info from '$lib/components/icons/Info.svelte';

	export let content: unknown = '';

	const i18n = getContext<Writable<I18n>>('i18n');

	$: rawMessage = getErrorMessage(content);
	$: message =
		rawMessage === 'unknown_execution'
			? $i18n.t('The outcome of the previous request is unknown. Start a new chat to continue.')
			: rawMessage === 'response_stream_interrupted' ||
				  rawMessage.startsWith('Response payload is not completed:')
				? $i18n.t('The connection was interrupted before the response finished.')
				: rawMessage || 'Error submitting message';
</script>

<div
	class="my-1.5 flex w-full items-start gap-2 rounded-2xl bg-black/[0.03] px-3 py-2 text-gray-500 dark:bg-white/[0.04] dark:text-gray-400"
>
	<Info className="mt-0.5 size-4 shrink-0 text-gray-400 dark:text-gray-500" strokeWidth="1.8" />

	<div class="min-w-0 break-words text-[0.8125rem] leading-5">
		{message}
	</div>
</div>
