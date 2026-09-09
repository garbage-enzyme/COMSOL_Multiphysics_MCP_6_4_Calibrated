import { test, after } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { join } from "node:path";
import { publicToolName, normalizeConfig, apply } from "../lib/index.js";
import { extractText } from "../lib/mcp-client-core.mjs";
import { spawnFakeServer, createFakeJobs, quietLogger, FIXTURE } from "./helpers.mjs";

test("publicToolName keeps clean comsol names verbatim", () => {
	for (const name of ["job_submit", "job_status", "comsol_start", "wave_optics_preflight"]) {
		assert.equal(publicToolName(name), name);
	}
});

test("publicToolName normalizes invalid characters with a deterministic hash", () => {
	const a = publicToolName("weird name!");
	assert.match(a, /^weird_name__[0-9a-f]{12}$/);
	assert.equal(publicToolName("weird name!"), a); // deterministic
	const b = publicToolName("weird-name!");
	assert.notEqual(a, b); // distinct identities never collapse
});

test("publicToolName truncates over-long names with a hash suffix", () => {
	const long = "x".repeat(80);
	const a = publicToolName(long);
	assert.ok(a.length <= 64);
	assert.match(a, /_[0-9a-f]{12}$/);
});

test("normalizeConfig merges defaults, env, and stateFile", () => {
	const cfg = normalizeConfig({ command: "C:\\x\\mcp.exe", cwd: "D:\\rt" });
	assert.equal(cfg.command, "C:\\x\\mcp.exe");
	assert.equal(cfg.cwd, "D:\\rt");
	assert.equal(cfg.env.COMSOL_MCP_SETTINGS_PATH, "D:\\comsol_runtime\\settings.json"); // default env kept
	assert.equal(cfg.stateFile, "D:\\rt\\.dsh-comsol-bridge-jobs.json");
	assert.equal(cfg.toolCallTimeoutMs, 600000);
	assert.equal(cfg.jobMirrorEnabled, true);
});

test("normalizeConfig honors overrides and disables", () => {
	const cfg = normalizeConfig({ enabled: false, env: { EXTRA: "1" }, reconnect: { maxAttempts: 2 }, terminalStates: ["completed"] });
	assert.equal(cfg.enabled, false);
	assert.equal(cfg.env.EXTRA, "1");
	assert.equal(cfg.env.COMSOL_MCP_SETTINGS_PATH, "D:\\comsol_runtime\\settings.json");
	assert.equal(cfg.reconnect.maxAttempts, 2);
	assert.equal(cfg.reconnect.initialDelayMs, 500); // sibling default kept
	assert.deepEqual(cfg.terminalStates, ["completed"]);
});

test("extractText projects MCP content blocks and discards binaries", () => {
	const blocks = [
		{ type: "text", text: "a" },
		{ type: "text", text: "b" },
		{ type: "image", mimeType: "image/png" },
		{ type: "resource" },
		{ type: "audio" },
		{ type: "custom" },
		42,
	];
	const out = extractText(blocks, "t");
	assert.ok(out.startsWith("a\nb"));
	assert.ok(out.includes("[image:"));
	assert.ok(out.includes("[resource:"));
	assert.ok(out.includes("[audio:"));
	assert.ok(out.includes("[unsupported content type: custom]"));
	assert.equal(extractText([], "t"), "(t returned no text content)");
});

// ---- plugin-level mirror bookkeeping (mirror pinning only after start) ----

const fakeServer = spawnFakeServer({ FAKE_POLLS: "120" });
after(() => fakeServer.close());

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function makeCtx({ jobs } = {}) {
	const registered = [];
	const disposers = [];
	return {
		registered,
		logger: quietLogger(),
		tools: {
			register(def) {
				registered.push(def);
				return () => {};
			},
		},
		get: (key) => (key === "jobs" ? jobs : undefined),
		effect: (factory) => disposers.push(factory()),
		dispose() {
			for (const d of disposers.splice(0)) {
				try { d(); } catch {}
			}
		},
	};
}

async function submitOnce(def, agent) {
	return def.execute(
		{ spec: { job_type: "staged_sweep" } },
		{ signal: new AbortController().signal, ...(agent ? { agent } : {}) },
	);
}

test("apply mirrors job_submit only after the mirror starts", { timeout: 20000 }, async () => {
	// ASCII root per repo convention; the fake-server child's spawn cwd stays
	// outside this directory so Windows rmSync cannot hit a cwd EPERM.
	const stateDir = mkdtempSync(join("D:\\mcp_tests", "b16plug"));
	try {
		const jobs = createFakeJobs();
		const ctx = makeCtx({ jobs });
		await apply(ctx, {
			command: process.execPath,
			args: [FIXTURE],
			env: fakeServer.extraEnv,
			cwd: "D:\\mcp_tests",
			stateFile: join(stateDir, "jobs-a.json"),
			pollIntervalMs: 30,
			cancelConfirmTimeoutMs: 50,
			reconnect: { enabled: false },
			initTimeoutMs: 5000,
			failOnStartupError: true,
		});
		const submit = ctx.registered.find((d) => d.name === "job_submit");
		assert.ok(submit, "job_submit must be registered natively");

		const result = await submitOnce(submit, "agent-1");
		const text = result.content.map((b) => b.text ?? "").join("\n");
		const jobId = JSON.parse(text).job_id;
		assert.match(jobId, /^job-\d+$/);

		// jobs registry available -> exactly one mirror started
		assert.equal(jobs.started.length, 1);
		// the state row is durable only after the successful start
		const rowsOnDisk = JSON.parse(readFileSync(join(stateDir, "jobs-a.json"), "utf8"));
		assert.deepEqual(rowsOnDisk.map((r) => r.jobId), [jobId]);

		jobs.started[0].hooks.cancel();
		ctx.dispose();
		await Promise.race([jobs.started[0].done, sleep(3000)]);
	} finally { rmSync(stateDir, { recursive: true, force: true }); }
});

test("apply keeps failed mirror starts retryable in the state file", { timeout: 20000 }, async () => {
	const stateDir = mkdtempSync(join("D:\\mcp_tests", "b16plug"));
	try {
		const ctx = makeCtx(); // no ctx.jobs registry available
		await apply(ctx, {
			command: process.execPath,
			args: [FIXTURE],
			env: fakeServer.extraEnv,
			cwd: "D:\\mcp_tests",
			stateFile: join(stateDir, "jobs-b.json"),
			reconnect: { enabled: false },
			initTimeoutMs: 5000,
			failOnStartupError: true,
		});
		const submit = ctx.registered.find((d) => d.name === "job_submit");
		assert.ok(submit, "job_submit must still register without ctx.jobs");
		const result = await submitOnce(submit);
		const text = result.content.map((b) => b.text ?? "").join("\n");
		assert.match(JSON.parse(text).job_id, /^job-\d+$/);
		ctx.dispose();
		// a failed start must stay retryable: no pinned row survives
		let rows = [];
		try { rows = JSON.parse(readFileSync(join(stateDir, "jobs-b.json"), "utf8")); } catch {}
		assert.deepEqual(rows.map((r) => r.jobId), []);
	} finally { rmSync(stateDir, { recursive: true, force: true }); }
});

test("B02: unconfirmed dispose keeps the durable state row", { timeout: 20000 }, async () => {
	const stateDir = mkdtempSync(join("D:\\mcp_tests", "b02plug"));
	try {
		const jobs = createFakeJobs();
		const ctx = makeCtx({ jobs });
		// Server never reaches terminal (FAKE_POLLS=120); dispose will settle
		// the mirror as unconfirmed and must NOT delete the state row.
		await apply(ctx, {
			command: process.execPath,
			args: [FIXTURE],
			env: fakeServer.extraEnv,
			cwd: "D:\\mcp_tests",
			stateFile: join(stateDir, "jobs-c.json"),
			pollIntervalMs: 30,
			cancelConfirmTimeoutMs: 50,
			reconnect: { enabled: false },
			initTimeoutMs: 5000,
			failOnStartupError: true,
		});
		const submit = ctx.registered.find((d) => d.name === "job_submit");
		const result = await submitOnce(submit, "agent-1");
		const jobId = JSON.parse(result.content.map((b) => b.text ?? "").join("\n")).job_id;
		assert.equal(jobs.started.length, 1);

		// Dispose while the job is still running server-side.
		ctx.dispose();
		await Promise.race([jobs.started[0].done, sleep(3000)]);

		const rows = JSON.parse(readFileSync(join(stateDir, "jobs-c.json"), "utf8"));
		assert.equal(rows.length, 1, "unconfirmed settle must keep the durable row");
		assert.equal(rows[0].jobId, jobId);
		assert.equal(rows[0].unconfirmed, true);
		assert.match(rows[0].unconfirmedReason ?? "", /disposed|unconfirmed/i);
	} finally { rmSync(stateDir, { recursive: true, force: true }); }
});