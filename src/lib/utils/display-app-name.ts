/** Normalize branding for display without changing the backend configuration. */
export function displayAppName(value: unknown, fallback: string): string {
	if (typeof value !== 'string' || !value.trim()) return fallback;
	const name = value
		.trim()
		.replace(/\s*\(Open\s*Web\s*UI\)\s*$/i, '')
		.trim();
	return !name || /^open[\s_-]*web[\s_-]*ui$/i.test(name) ? fallback : name;
}
