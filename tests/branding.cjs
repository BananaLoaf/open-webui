const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const ts = require('typescript');

const root = path.resolve(__dirname, '..');
const themeSource = fs.readFileSync(path.join(root, 'static/themes/theme.js'), 'utf8');

function themeHarness({ theme, dark = false, denied = false } = {}) {
	const classes = new Set(['high-contrast']);
	const removed = [];
	const html = {
		classList: {
			add: (...names) => names.forEach((name) => classes.add(name)),
			remove: (...names) => names.forEach((name) => classes.delete(name))
		},
		dataset: {},
		style: { removeProperty: (name) => removed.push(name) }
	};
	let listener;
	let color;
	const media = {
		matches: dark,
		addEventListener: (event, callback) => {
			assert.equal(event, 'change');
			listener = callback;
		}
	};
	const window = { matchMedia: () => media };
	vm.runInNewContext(themeSource, {
		window,
		document: {
			documentElement: html,
			querySelector: () => ({ setAttribute: (name, value) => { color = value; } })
		},
		localStorage: { getItem: () => { if (denied) throw new Error('blocked'); return theme; } },
		getComputedStyle: () => ({ getPropertyValue: () => classes.has('dark') ? '#171715' : '#ffffff' })
	});
	return { window, html, classes, removed, color: () => color, changeSystem: (value) => {
		media.matches = value;
		listener();
	} };
}

for (const [theme, expected] of [
	['light', 'light'], ['dark', 'dark'], ['oled-dark', 'dark'], ['her', 'light']
]) {
	test(`initial ${theme} theme uses the expected palette`, () => {
		const h = themeHarness({ theme });
		assert.ok(h.classes.has(expected));
		assert.ok(h.classes.has('high-contrast'));
		assert.equal(h.html.dataset.theme, theme);
		assert.equal(h.html.dataset.palette, expected === 'dark' ? 'balance' : 'silver');
		assert.equal(h.color(), expected === 'dark' ? '#171715' : '#ffffff');
	});
}

test('system theme follows subsequent operating system changes', () => {
	const h = themeHarness();
	h.changeSystem(true);
	assert.ok(h.classes.has('dark'));
	h.changeSystem(false);
	assert.ok(h.classes.has('light'));
	assert.ok(!h.classes.has('dark'));
});

test('explicit theme does not follow operating system changes', () => {
	const h = themeHarness({ theme: 'light' });
	h.changeSystem(true);
	assert.ok(h.classes.has('light'));
});

test('switching from OLED to light removes stale classes and inline colors', () => {
	const h = themeHarness({ theme: 'oled-dark' });
	h.window.applyTheme('light');
	assert.ok(!h.classes.has('oled-dark'));
	assert.ok(!h.classes.has('dark'));
	assert.ok(h.classes.has('light'));
	assert.deepEqual(h.removed.slice(-4), [800, 850, 900, 950].map((n) => `--color-gray-${n}`));
});

test('invalid or inaccessible stored theme safely defaults to the system theme', () => {
	for (const options of [{ theme: 'invalid', dark: true }, { denied: true, dark: true }]) {
		const h = themeHarness(options);
		assert.equal(h.html.dataset.theme, 'system');
		assert.ok(h.classes.has('dark'));
	}
});

const exportsObject = {};
vm.runInNewContext(ts.transpileModule(
	fs.readFileSync(path.join(root, 'src/lib/utils/display-app-name.ts'), 'utf8'),
	{ compilerOptions: { module: ts.ModuleKind.CommonJS } }
).outputText, { exports: exportsObject });

test('legacy default app names use the new display name', () => {
	for (const name of [null, undefined, 123, '', ' ', 'Open WebUI', 'open-web-ui', 'Open WebUI (Open WebUI)']) {
		assert.equal(exportsObject.displayAppName(name, 'ИННОМАЙТ'), 'ИННОМАЙТ');
	}
});

test('custom app names remain intact without the legacy branding suffix', () => {
	assert.equal(exportsObject.displayAppName(' My workspace (Open WebUI) ', 'ИННОМАЙТ'), 'My workspace');
	assert.equal(exportsObject.displayAppName('Research', 'ИННОМАЙТ'), 'Research');
});

test('startup theme assets are loaded before the application', () => {
	const html = fs.readFileSync(path.join(root, 'src/app.html'), 'utf8');
	assert.ok(html.indexOf('/themes/theme.css') < html.indexOf('/themes/theme.js'));
	assert.ok(html.indexOf('/themes/theme.js') < html.indexOf('window.applyTheme()'));
	assert.ok(html.indexOf('/themes/theme.js') < html.indexOf('%sveltekit.head%'));
	assert.equal((html.match(/id="splash-screen"/g) || []).length, 1);
});
