/**
 * Runner entry point (JavaScript) — executes exactly one skill function.
 *
 * The JS twin of entrypoint.py: one JSON job on stdin, one JSON result on
 * stdout, then exit. The container is ephemeral (`docker run --rm`), so
 * nothing survives the call.
 *
 * The skill file is evaluated in a `vm` context rather than `require`d, so it
 * gets a clean global object per call. As on the Python side, the isolation
 * that counts is the container's; the vm module is not a security boundary and
 * is not treated as one. `process` and `require` are exposed on purpose —
 * skills legitimately need `fetch`, and the network is fenced by the sandbox's
 * egress proxy, not by hiding APIs from the guest.
 */

'use strict';

const vm = require('vm');

const MAX_RESULT_BYTES = 64 * 1024;

/**
 * Report a failure and stop.
 *
 * `error` is what a user reads next to the failed call in the answer, so it is
 * the error's own message, not its stack. The stack goes in its own field.
 */
function fail(message, kind = 'error', stack = '') {
	const payload = { ok: false, error: message, kind };
	if (stack) payload.traceback = String(stack).slice(0, 4000);
	process.stdout.write(JSON.stringify(payload));
	process.exit(1);
}

/** One-line, human-readable form of a thrown value. */
function message(err) {
	if (err && typeof err.message === 'string' && err.message.trim()) {
		return err.message.trim().split('\n')[0].slice(0, 300);
	}
	return String(err).split('\n')[0].slice(0, 300);
}

function readStdin() {
	return new Promise((resolve, reject) => {
		let data = '';
		process.stdin.setEncoding('utf8');
		process.stdin.on('data', (chunk) => {
			data += chunk;
		});
		process.stdin.on('end', () => resolve(data));
		process.stdin.on('error', reject);
	});
}

async function main() {
	let job;
	try {
		job = JSON.parse(await readStdin());
	} catch (err) {
		return fail(`Ungültiger Job: ${err.message}`, 'protocol');
	}

	const code = job.code || '';
	const functionName = job.function || '';
	const args = Array.isArray(job.args) ? job.args : [];
	const variables = job.variables && typeof job.variables === 'object' ? job.variables : {};

	if (!functionName) return fail('Kein Funktionsname übergeben.', 'protocol');

	// A leading underscore marks a private helper, and the import step never
	// writes a database row for one — so a call naming it cannot resolve and
	// never reaches here. Refusing it again is a second lock on the same door:
	// the container's contract is "exactly the one function that was approved".
	if (functionName.startsWith('_')) {
		return fail(`'${functionName}' ist ein privater Helfer und nicht aufrufbar.`, 'private_function');
	}

	for (const [key, value] of Object.entries(variables)) {
		if (/^[A-Za-z_$][\w$]*$/.test(key)) {
			process.env[key] = value === null || value === undefined ? '' : String(value);
		}
	}

	// console.log inside a skill must not corrupt the result protocol, which
	// owns stdout. Collect it and hand it back as a diagnostic field.
	const captured = [];
	const capture = (...parts) => captured.push(parts.map(String).join(' '));

	// A function with an allowlist reaches the network only through the
	// sandbox's egress proxy, and Node's global fetch ignores the proxy
	// environment entirely. Route through undici's ProxyAgent so an
	// allowlisted JS skill works the same way its Python twin does.
	let skillFetch = fetch;
	if (process.env.SKILL_PROXY_URL) {
		try {
			const { ProxyAgent, fetch: undiciFetch } = require('undici');
			const proxyUrl = new URL(process.env.SKILL_PROXY_URL);
			const credentials = Buffer.from(
				`${decodeURIComponent(proxyUrl.username)}:${decodeURIComponent(proxyUrl.password)}`
			).toString('base64');
			const agent = new ProxyAgent({
				uri: `${proxyUrl.protocol}//${proxyUrl.host}`,
				token: `Basic ${credentials}`,
			});
			skillFetch = (input, init = {}) => undiciFetch(input, { ...init, dispatcher: agent });
		} catch (err) {
			return fail(`Proxy-Setup fehlgeschlagen: ${err.message}`, 'proxy');
		}
	}

	const sandbox = {
		SKILL_VARS: variables,
		console: { log: capture, info: capture, warn: capture, error: capture, debug: capture },
		fetch: skillFetch,
		URL,
		URLSearchParams,
		TextEncoder,
		TextDecoder,
		AbortController,
		setTimeout,
		clearTimeout,
		Buffer,
		process,
		require,
		module: { exports: {} },
	};
	sandbox.exports = sandbox.module.exports;
	sandbox.globalThis = sandbox;

	let context;
	try {
		context = vm.createContext(sandbox);
		// `export` is ESM syntax and is not valid in a vm script. Skills are
		// written with it because that is the idiom the docs teach, so strip
		// the keyword here rather than forcing a second file format on authors.
		const script = new vm.Script(code.replace(/^\s*export\s+(?=(async\s+)?(function|const|let|var)\s)/gm, ''), {
			filename: 'skill.js',
		});
		script.runInContext(context, { timeout: 5000 });
	} catch (err) {
		return fail(`Fehler beim Laden der Skill-Datei: ${err.message}`, 'load');
	}

	const target =
		typeof sandbox[functionName] === 'function'
			? sandbox[functionName]
			: typeof sandbox.module.exports?.[functionName] === 'function'
				? sandbox.module.exports[functionName]
				: null;

	if (!target) return fail(`Funktion '${functionName}' existiert nicht.`, 'missing_function');

	let result;
	try {
		result = await target(...args);
	} catch (err) {
		return fail(message(err), 'runtime', err && err.stack ? String(err.stack) : '');
	}

	let payload;
	try {
		payload = JSON.stringify({ ok: true, result, stdout: captured.join('\n').slice(0, 2000) });
	} catch (err) {
		return fail(`Ergebnis ist nicht JSON-serialisierbar: ${err.message}`, 'result');
	}
	if (payload === undefined) {
		payload = JSON.stringify({ ok: true, result: null, stdout: captured.join('\n').slice(0, 2000) });
	}
	if (Buffer.byteLength(payload, 'utf8') > MAX_RESULT_BYTES) {
		return fail(`Ergebnis überschreitet ${MAX_RESULT_BYTES / 1024} KB.`, 'result_too_large');
	}

	process.stdout.write(payload);
}

main().catch((err) => fail(String(err && err.message ? err.message : err), 'internal'));
