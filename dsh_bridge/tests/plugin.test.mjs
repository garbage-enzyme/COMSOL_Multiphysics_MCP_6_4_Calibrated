import { test, after } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { publicToolName, normalizeConfig, apply } from "../lib/index.js";
import { extractText } from "../lib/mcp-client-core.mjs";
import { extractOwnerIdentity, ownerFromIdentity, mirrorDedupeKey } from "../lib/job-mirror.mjs";
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

function makeCtx({ jobs, agents } = {}) {
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
		get: (key) => {
			if (key === "jobs") return jobs;
			if (key === "agents") return agents;
			return undefined;
		},
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

// ---- B02a: owner identity extraction and restore ----

test("B02a: extractOwnerIdentity handles string, object, and unusable agents", () => {
	assert.deepEqual(extractOwnerIdentity("agent-1"), { kind: "agent_key", key: "agent-1", sessionId: "agent-1" });
	assert.deepEqual(
		extractOwnerIdentity({ id: "a-9", sessionId: "sess-3" }),
		{ kind: "agent_object", key: "a-9", sessionId: "sess-3" },
	);
	assert.deepEqual(
		extractOwnerIdentity({ name: "n1" }),
		{ kind: "agent_object", key: "n1", sessionId: null },
	);
	assert.equal(extractOwnerIdentity(null), null);
	assert.equal(extractOwnerIdentity(undefined), null);
	assert.equal(extractOwnerIdentity(""), null);
	assert.equal(extractOwnerIdentity("   "), null);
	assert.equal(extractOwnerIdentity({}), null);
	assert.equal(extractOwnerIdentity(42), null);
});

test("B02a: ownerFromIdentity restores only persisted keys; never invents", () => {
	assert.equal(ownerFromIdentity({ ownerAgentKey: "agent-1" }), "agent-1");
	assert.equal(ownerFromIdentity({ ownerAgentKey: "  agent-2  " }), "agent-2");
	assert.equal(ownerFromIdentity({ ownerAgentKey: null }), undefined);
	assert.equal(ownerFromIdentity({}), undefined);
	assert.equal(ownerFromIdentity(null), undefined);
	// legacy row without owner fields
	assert.equal(ownerFromIdentity({ jobId: "job-1", jobType: "staged_sweep" }), undefined);
});

test("B02a: mirrorDedupeKey includes attempt", () => {
	assert.equal(mirrorDedupeKey("job-1", null), "job-1::");
	assert.equal(mirrorDedupeKey("job-1", undefined), "job-1::");
	assert.equal(mirrorDedupeKey("job-1", 2), "job-1::2");
	assert.notEqual(mirrorDedupeKey("job-1", 1), mirrorDedupeKey("job-1", 2));
});

test("B02a: submit persists owner identity for rehydrate", { timeout: 20000 }, async () => {
	const stateDir = mkdtempSync(join("D:\\mcp_tests", "b02aown"));
	try {
		const jobs = createFakeJobs();
		const ctx = makeCtx({ jobs });
		await apply(ctx, {
			command: process.execPath,
			args: [FIXTURE],
			env: fakeServer.extraEnv,
			cwd: "D:\\mcp_tests",
			stateFile: join(stateDir, "jobs.json"),
			pollIntervalMs: 30,
			cancelConfirmTimeoutMs: 50,
			reconnect: { enabled: false },
			initTimeoutMs: 5000,
			failOnStartupError: true,
			rehydrateMaxAttempts: 0,
		});
		const submit = ctx.registered.find((d) => d.name === "job_submit");
		const result = await submitOnce(submit, "agent-1");
		const jobId = JSON.parse(result.content.map((b) => b.text ?? "").join("\n")).job_id;
		const rows = JSON.parse(readFileSync(join(stateDir, "jobs.json"), "utf8"));
		assert.equal(rows.length, 1);
		assert.equal(rows[0].jobId, jobId);
		assert.equal(rows[0].ownerAgentKey, "agent-1");
		assert.equal(rows[0].ownerKind, "agent_key");
		assert.equal(jobs.started[0].spec.owner, "agent-1");
		jobs.started[0].hooks.cancel();
		ctx.dispose();
		await Promise.race([jobs.started[0].done, sleep(2000)]);
	} finally { rmSync(stateDir, { recursive: true, force: true }); }
});

test("B02a: rehydrate restores owner and does not resubmit", { timeout: 25000 }, async () => {
	const stateDir = mkdtempSync(join("D:\\mcp_tests", "b02areh"));
	try {
		const stateFile = join(stateDir, "jobs.json");
		// Simulate a prior boot that submitted a job owned by agent-1.
		writeFileSync(stateFile, JSON.stringify([{
			jobId: "job-1",
			jobType: "staged_sweep",
			at: Date.now(),
			schemaVersion: 2,
			ownerAgentKey: "agent-1",
			ownerSessionId: "agent-1",
			ownerKind: "agent_key",
			attempt: null,
			unconfirmed: true,
			unconfirmedReason: "bridge disposed",
			lastObservedState: "running",
		}], null, 2));

		const jobs = createFakeJobs();
		// DSH requires the live registered Agent instance for jobs.start owner.
		const liveAgent = { id: "agent-1" };
		const agents = { get: (id) => (id === "agent-1" ? liveAgent : undefined) };
		const ctx = makeCtx({ jobs, agents });
		await apply(ctx, {
			command: process.execPath,
			args: [FIXTURE],
			env: fakeServer.extraEnv,
			cwd: "D:\\mcp_tests",
			stateFile,
			pollIntervalMs: 30,
			reconnect: { enabled: false },
			initTimeoutMs: 5000,
			failOnStartupError: true,
			rehydrateMaxAttempts: 0,
		});

		// Rehydrate must start exactly one mirror with the restored live owner.
		assert.equal(jobs.started.length, 1, `expected 1 rehydrated mirror, got ${jobs.started.length}`);
		assert.equal(jobs.started[0].spec.owner, liveAgent);
		assert.equal(jobs.started[0].spec.label, "comsol job job-1 (staged_sweep)");

		const rows = JSON.parse(readFileSync(stateFile, "utf8"));
		assert.equal(rows.length, 1);
		assert.equal(rows[0].jobId, "job-1");
		// recovery flags cleared once the restored mirror is running
		assert.equal(rows[0].unconfirmed ?? false, false);

		// job_submit must not have been invoked during rehydrate.
		const submit = ctx.registered.find((d) => d.name === "job_submit");
		assert.ok(submit);
		// No new submit happened: state still has only the original job-1 row.
		assert.deepEqual(JSON.parse(readFileSync(stateFile, "utf8")).map((r) => r.jobId), ["job-1"]);

		ctx.dispose();
		await Promise.race([jobs.started[0].done, sleep(2000)]);
	} finally { rmSync(stateDir, { recursive: true, force: true }); }
});

test("B02a: rehydrate with missing owner keeps the row and blocks recovery", { timeout: 25000 }, async () => {
	const stateDir = mkdtempSync(join("D:\\mcp_tests", "b02aown2"));
	try {
		const stateFile = join(stateDir, "jobs.json");
		// Legacy-style row without owner identity.
		writeFileSync(stateFile, JSON.stringify([{
			jobId: "job-legacy",
			jobType: "staged_sweep",
			at: Date.now(),
		}], null, 2));

		const jobs = createFakeJobs();
		// Owner key is persisted but the agent is not live — recovery must block.
		const agents = { get: () => undefined };
		const ctx = makeCtx({ jobs, agents });
		await apply(ctx, {
			command: process.execPath,
			args: [FIXTURE],
			env: fakeServer.extraEnv,
			cwd: "D:\\mcp_tests",
			stateFile,
			pollIntervalMs: 30,
			reconnect: { enabled: false },
			initTimeoutMs: 5000,
			failOnStartupError: true,
			rehydrateMaxAttempts: 0,
		});

		// Must NOT invent an owner or start an unowned mirror.
		assert.equal(jobs.started.length, 0, "must not start a mirror without a live restored owner");
		const rows = JSON.parse(readFileSync(stateFile, "utf8"));
		assert.equal(rows.length, 1, "row must be retained");
		assert.equal(rows[0].jobId, "job-legacy");
		assert.equal(rows[0].recoveryBlocked, true);
		assert.match(rows[0].recoveryReason ?? "", /missing owner/i);
		ctx.dispose();
	} finally { rmSync(stateDir, { recursive: true, force: true }); }
});

test("B02a: rehydrate with owner key but agent not live keeps the row blocked", { timeout: 25000 }, async () => {
	const stateDir = mkdtempSync(join("D:\\mcp_tests", "b02await"));
	try {
		const stateFile = join(stateDir, "jobs.json");
		writeFileSync(stateFile, JSON.stringify([{
			jobId: "job-wait",
			jobType: "staged_sweep",
			schemaVersion: 2,
			ownerAgentKey: "agent-1",
		}], null, 2));
		const jobs = createFakeJobs();
		const agents = { get: () => undefined };
		const ctx = makeCtx({ jobs, agents });
		await apply(ctx, {
			command: process.execPath,
			args: [FIXTURE],
			env: fakeServer.extraEnv,
			cwd: "D:\\mcp_tests",
			stateFile,
			pollIntervalMs: 30,
			reconnect: { enabled: false },
			initTimeoutMs: 5000,
			failOnStartupError: true,
			rehydrateMaxAttempts: 0,
		});
		assert.equal(jobs.started.length, 0);
		const rows = JSON.parse(readFileSync(stateFile, "utf8"));
		assert.equal(rows[0].recoveryBlocked, true);
		assert.match(rows[0].recoveryReason ?? "", /not live in the agent registry/i);
		ctx.dispose();
	} finally { rmSync(stateDir, { recursive: true, force: true }); }
});

test("B02a: rehydrate removes rows already terminal on the server", { timeout: 25000 }, async () => {
	const stateDir = mkdtempSync(join("D:\\mcp_tests", "b02aterm"));
	try {
		const stateFile = join(stateDir, "jobs.json");
		writeFileSync(stateFile, JSON.stringify([{
			jobId: "job-done",
			jobType: "staged_sweep",
			schemaVersion: 2,
			ownerAgentKey: "agent-1",
		}], null, 2));

		// Fake server: FAKE_POLLS=1 means the first job_status returns completed.
		const termServer = spawnFakeServer({ FAKE_POLLS: "1" });
		try {
			const jobs = createFakeJobs();
			const ctx = makeCtx({ jobs });
			await apply(ctx, {
				command: process.execPath,
				args: [FIXTURE],
				env: termServer.extraEnv,
				cwd: "D:\\mcp_tests",
				stateFile,
				pollIntervalMs: 30,
				reconnect: { enabled: false },
				initTimeoutMs: 5000,
				failOnStartupError: true,
				rehydrateMaxAttempts: 0,
			});
			assert.equal(jobs.started.length, 0, "terminal job must not get a new mirror");
			const rows = JSON.parse(readFileSync(stateFile, "utf8"));
			assert.deepEqual(rows, [], "confirmed terminal row must be removed");
			ctx.dispose();
		} finally {
			await termServer.close();
		}
	} finally { rmSync(stateDir, { recursive: true, force: true }); }
});