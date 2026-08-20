import { test, after } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, rmSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { spawnFakeServer, createFakeJobs, quietLogger, makeConnection } from "./helpers.mjs";
import { createJobMirror, createStateStore, isTerminal, mapOutcome, extractState, parseJson } from "../lib/job-mirror.mjs";

const server = spawnFakeServer({ FAKE_POLLS: "3" });
after(() => server.close());

const MIRROR_OPTS = { pollIntervalMs: 30, tailLines: 20, outputLimitBytes: 4096, cancelConfirmTimeoutMs: 5000 };

// ---- pure helper branches (deterministic, no timers) ----

test("isTerminal recognizes terminal states and rejects running", () => {
	const terms = ["completed", "failed", "cancelled", "killed", "done", "terminal"];
	assert.equal(isTerminal("running", terms), false);
	assert.equal(isTerminal("active", terms), false);
	assert.equal(isTerminal("completed", terms), true);
	assert.equal(isTerminal("failed", terms), true);
	assert.equal(isTerminal("cancelled", terms), true);
	assert.equal(isTerminal("job_submit_failed_after_attached_handoff", terms), true);
	assert.equal(isTerminal("COMPLETED", terms), true); // case-insensitive
	assert.equal(isTerminal(undefined, terms), false);
});

test("mapOutcome classifies terminal states", () => {
	assert.equal(mapOutcome("completed"), "completed");
	assert.equal(mapOutcome("cancelled"), "killed");
	assert.equal(mapOutcome("cancel_requested"), "killed");
	assert.equal(mapOutcome("failed"), "failed");
	assert.equal(mapOutcome("job_submit_failed_after_attached_handoff"), "failed");
});

test("extractState handles flat and nested shapes", () => {
	assert.equal(extractState({ state: "running" }), "running");
	assert.equal(extractState({ status: "completed" }), "completed");
	assert.equal(extractState({ state: { phase: "verifying" } }), "verifying");
	assert.equal(extractState({ state: { name: "full" } }), "full");
	assert.equal(extractState({}), undefined);
	assert.equal(extractState(parseJson("not json")), undefined);
});

// ---- mirror lifecycle against the fake server ----

test("mirror completes when the server reaches a terminal state", { timeout: 10000 }, async () => {
	const jobs = createFakeJobs();
	const conn = makeConnection(server, {});
	await conn.connect();
	try {
		createJobMirror({ jobs, core: conn, jobId: "job-1", jobType: "staged_sweep", opts: MIRROR_OPTS, logger: quietLogger() });
		assert.equal(jobs.started.length, 1);
		const rec = jobs.started[0];
		assert.equal(rec.spec.kind, "comsol");
		assert.equal(rec.spec.label, "comsol job job-1 (staged_sweep)");
		assert.equal(rec.spec.owner, undefined); // unowned mirror
		const outcome = await Promise.race([
			rec.done,
			new Promise((_, rej) => setTimeout(() => rej(new Error("mirror never settled")), 8000)),
		]);
		assert.equal(outcome.status, "completed");
		assert.match(outcome.detail ?? "", /state=/);
	} finally { conn.dispose(); }
});

test("mirror streams job_tail deltas through readOutput", { timeout: 10000 }, async () => {
	const jobs = createFakeJobs();
	const conn = makeConnection(server, {});
	await conn.connect();
	try {
		createJobMirror({ jobs, core: conn, jobId: "job-1", opts: MIRROR_OPTS, logger: quietLogger() });
		const rec = jobs.started[0];
		await rec.done;
		const out = rec.hooks.readOutput();
		assert.ok(out.length > 0, "expected tail progress in the output buffer");
		assert.match(out, /point 1 done/);
		// second read is drained (single cursor semantics)
		assert.equal(rec.hooks.readOutput(), "");
	} finally { conn.dispose(); }
});

test("cancel settles the mirror as killed", { timeout: 10000 }, async () => {
	const jobs = createFakeJobs();
	const conn = makeConnection(server, {});
	await conn.connect();
	try {
		createJobMirror({ jobs, core: conn, jobId: "job-1", opts: MIRROR_OPTS, logger: quietLogger() });
		const rec = jobs.started[0];
		rec.hooks.cancel("caller cancelled");
		const outcome = await Promise.race([
			rec.done,
			new Promise((_, rej) => setTimeout(() => rej(new Error("cancel never settled")), 8000)),
		]);
		assert.equal(outcome.status, "killed");
	} finally { conn.dispose(); }
});

test("disposed bridge settles the mirror as failed", { timeout: 10000 }, async () => {
	const jobs = createFakeJobs();
	const conn = makeConnection(server, {});
	await conn.connect();
	const disposed = { value: false };
	try {
		createJobMirror({ jobs, core: conn, jobId: "job-1", opts: MIRROR_OPTS, logger: quietLogger(), isDisposed: () => disposed.value });
		const rec = jobs.started[0];
		disposed.value = true;
		const outcome = await Promise.race([
			rec.done,
			new Promise((_, rej) => setTimeout(() => rej(new Error("dispose never settled")), 8000)),
		]);
		assert.equal(outcome.status, "failed");
	} finally { conn.dispose(); }
});

test("outputLimitBytes caps the accumulated progress buffer", { timeout: 10000 }, async () => {
	const jobs = createFakeJobs();
	const conn = makeConnection(server, {});
	await conn.connect();
	try {
		const tiny = { ...MIRROR_OPTS, outputLimitBytes: 12 };
		createJobMirror({ jobs, core: conn, jobId: "job-1", opts: tiny, logger: quietLogger() });
		const rec = jobs.started[0];
		await rec.done;
		const out = rec.hooks.readOutput();
		assert.ok(out.length <= 12, `buffer exceeded limit: ${out.length}`);
	} finally { conn.dispose(); }
});

test("two mirrors on independent jobs both complete", { timeout: 10000 }, async () => {
	const jobs = createFakeJobs();
	const conn = makeConnection(server, {});
	await conn.connect();
	try {
		const r1 = await conn.callTool("job_submit", { spec: { job_type: "staged_sweep" } });
		const r2 = await conn.callTool("job_submit", { spec: { job_type: "staged_sweep" } });
		const j1 = JSON.parse(r1.text).job_id;
		const j2 = JSON.parse(r2.text).job_id;
		assert.notEqual(j1, j2);
		createJobMirror({ jobs, core: conn, jobId: j1, opts: MIRROR_OPTS, logger: quietLogger() });
		createJobMirror({ jobs, core: conn, jobId: j2, opts: MIRROR_OPTS, logger: quietLogger() });
		assert.equal(jobs.started.length, 2);
		const outcomes = await Promise.all([
			Promise.race([jobs.started[0].done, new Promise((_, rej) => setTimeout(() => rej(new Error("mirror 1 never settled")), 8000))]),
			Promise.race([jobs.started[1].done, new Promise((_, rej) => setTimeout(() => rej(new Error("mirror 2 never settled")), 8000))]),
		]);
		assert.deepEqual(outcomes.map((o) => o.status), ["completed", "completed"]);
		const stats = await conn.callTool("stats", {});
		assert.equal(JSON.parse(stats.text).jobCount, 2);
	} finally { conn.dispose(); }
});

// ---- state store ----

test("state store persists add/list/remove atomically", () => {
	const dir = mkdtempSync(join(tmpdir(), "bridge-state-"));
	try {
		const file = join(dir, "jobs.json");
		const store = createStateStore(file, quietLogger());
		assert.deepEqual(store.list(), []);
		store.add("job-1", "staged_sweep");
		store.add("job-1", "staged_sweep"); // duplicate is a no-op
		store.add("job-2", null);
		assert.deepEqual(store.list().map((r) => r.jobId), ["job-1", "job-2"]);
		const onDisk = JSON.parse(readFileSync(file, "utf8"));
		assert.equal(onDisk.length, 2);
		store.remove("job-1");
		assert.deepEqual(store.list().map((r) => r.jobId), ["job-2"]);
		store.remove("missing"); // no-op
	} finally { rmSync(dir, { recursive: true, force: true }); }
});