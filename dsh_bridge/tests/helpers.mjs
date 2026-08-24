import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { createComsolConnection } from "../lib/mcp-client-core.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));
export const FIXTURE = join(HERE, "..", "fixtures", "fake-comsol-server.mjs");

export function spawnFakeServer(extraEnv = {}) {
	const child = spawn(process.execPath, [FIXTURE], {
		env: { ...process.env, ...extraEnv },
		stdio: ["pipe", "pipe", "pipe"],
	});
	child.stderr.setEncoding("utf8");
	let stderrBuf = "";
	child.stderr.on("data", (c) => { stderrBuf += c; });
	return {
		child,
		extraEnv,
		stderr: () => stderrBuf,
		close: async () => {
			// A signal-terminated child keeps exitCode null but has already
			// fired its exit event; check signalCode too so close() cannot
			// wait on an exit that will never fire again.
			if (child.exitCode === null && child.signalCode === null) child.kill();
			await new Promise((res) => {
				if (child.exitCode !== null || child.signalCode !== null) return res();
				child.once("exit", res);
			});
		},
	};
}

/** Minimal ctx.jobs double: records specs, runs the producer synchronously. */
export function createFakeJobs() {
	const started = [];
	return {
		started,
		start(spec) {
			const id = `comsol-${started.length + 1}`;
			const record = { id, spec, hooks: null, done: null };
			started.push(record);
			record.hooks = spec.run();
			record.done = record.hooks.done;
			return id;
		},
	};
}

export function quietLogger() {
	return { info: () => {}, warn: () => {}, error: () => {} };
}

export function makeConnection(fake, opts = {}) {
	return createComsolConnection({
		command: process.execPath,
		args: [FIXTURE],
		env: fake?.extraEnv ?? {},
		logger: quietLogger(),
		reconnect: { enabled: true, initialDelayMs: 200, maxDelayMs: 2000, maxAttempts: 10 },
		...opts,
	});
}