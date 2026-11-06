/* Loaded before application CSS/hydration so every entry point uses the same theme. */
(() => {
	const root = document.documentElement;
	const media = window.matchMedia('(prefers-color-scheme: dark)');
	const supported = ['system', 'light', 'dark', 'oled-dark', 'her'];
	let selected = 'system';
	function storedTheme() {
		try { return localStorage.getItem('theme') || 'system'; } catch { return 'system'; }
	}
	window.applyTheme = (requested = storedTheme()) => {
		selected = supported.includes(requested) ? requested : 'system';
		const dark = selected === 'dark' || selected === 'oled-dark' || (selected === 'system' && media.matches);
		root.classList.remove('light', 'dark', 'oled-dark', 'her');
		root.classList.add(dark ? 'dark' : 'light');
		if (selected === 'oled-dark' || selected === 'her') root.classList.add(selected);
		root.dataset.theme = selected;
		root.dataset.palette = dark ? 'balance' : 'silver';
		// Clear values written by the former OLED handler before resolving CSS roles.
		for (const shade of [800, 850, 900, 950]) root.style.removeProperty(`--color-gray-${shade}`);
		const canvas = getComputedStyle(root).getPropertyValue('--ow-canvas').trim();
		document.querySelector('meta[name="theme-color"]')?.setAttribute('content', canvas);
	};
	media.addEventListener('change', () => { if (selected === 'system') window.applyTheme('system'); });
	window.applyTheme();
})();
