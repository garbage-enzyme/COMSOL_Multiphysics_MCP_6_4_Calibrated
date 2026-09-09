import { test, after } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, rmSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { spawnFakeServer, createFakeJobs, quietLogger, makeConnection } from "./helpers.mjs";
import { createJobMirror, createStateStore, isTerminal, mapOutcome, extractState, parseJson } from "../lib/job-mirror.mjs";

const server = spawnFakeServer({ FAKE_POLLS: "3" });
after(() => server.close());

const MIRROR_OPTS = { pollIntervalMs: 30, tailLines: 20, outputLimitBytes: 4096, cancelConfirmTimeoutMs: 5000 };

// ---- pure helper branches (deterministic, no timers) ----

test("isTerminal recognizes terminal states and rejects running", () => {
	const terms = ["completed", "failed", "cancelled", "killed", "done", "terminal", "interrupted"];
	assert.equal(isTerminal("running", terms), false);
	assert.equal(isTerminal("active", terms), false);
	assert.equal(isTerminal("completed", terms), true);
	assert.equal(isTerminal("failed", terms), true);
	assert.equal(isTerminal("cancelled", terms), true);
	assert.equal(isTerminal("interrupted", terms), true);
	assert.equal(isTerminal("COMPLETED", terms), true); // case-insensitive
	assert.equal(isTerminal(undefined, terms), false);
	assert.equal(isTerminal("", terms), false);
	// B03: substring guessing is forbidden
	assert.equal(isTerminal("job_submit_failed_after_attached_handoff", terms), false);
	assert.equal(isTerminal("not_completed", terms), false);
	assert.equal(isTerminal("nonterminal", terms), false);
	assert.equal(isTerminal("cancel_requested", terms), false);
	assert.equal(isTerminal("completedly wrong", terms), false);
});

test("mapOutcome classifies terminal states", () => {
	assert.equal(mapOutcome("completed"), "completed");
	assert.equal(mapOutcome("complete"), "completed");
	assert.equal(mapOutcome("done"), "completed");
	assert.equal(mapOutcome("cancelled"), "killed");
	assert.equal(mapOutcome("canceled"), "killed");
	assert.equal(mapOutcome("killed"), "killed");
	assert.equal(mapOutcome("failed"), "failed");
	assert.equal(mapOutcome("failure"), "failed");
	assert.equal(mapOutcome("error"), "failed");
	// B03: interrupted is a non-success terminal
	assert.equal(mapOutcome("interrupted"), "failed");
	// Unknown / ambiguous tokens fail closed — never completed
	assert.equal(mapOutcome("terminal"), "failed");
	assert.equal(mapOutcome("cancel_requested"), "failed");
	assert.equal(mapOutcome("job_submit_failed_after_attached_handoff"), "failed");
	assert.equal(mapOutcome(undefined), "failed");
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

test("state store reports persistence outcomes as booleans", () => {
	const dir = mkdtempSync(join(tmpdir(), "bridge-state-"));
	try {
		const okStore = createStateStore(join(dir, "jobs.json"), quietLogger());
		assert.equal(okStore.add("job-a", "staged_sweep"), true);
		// duplicate add is a no-op that must not report a persistence failure
		assert.equal(okStore.add("job-a", "staged_sweep"), true);
		assert.equal(okStore.remove("missing-id"), true); // persists an emptied row set
		assert.equal(okStore.remove("job-a"), true);

		// failure path: a parent path component is an existing file, so the
		// recursive mkdir fails; add/remove must return false instead of
		// throwing or pretending the row was durably persisted.
		writeFileSync(join(dir, "blocker"), "not a directory");
		const blocked = createStateStore(join(dir, "blocker", "jobs.json"), quietLogger());
		assert.equal(blocked.add("job-b", null), false);
		assert.equal(blocked.remove("job-b"), false);
	} finally { rmSync(dir, { recursive: true, force: true }); }
});

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

test("mirror skips tail ticks while the previous tail is still in flight", { timeout: 15000 }, async () => {
	const jobs = createFakeJobs();
	const conn = makeConnection(server, {});
	await conn.connect();
	let activeTail = 0;
	let maxActiveTail = 0;
	const originalCall = conn.callTool.bind(conn);
	// Hold each job_tail call for 90ms after its response arrives so several
	// 30ms ticks would overlap if the mirror did not guard in-flight tails.
	conn.callTool = async (name, args, opts) => {
		if (name === "job_tail") {
			activeTail += 1;
			if (activeTail > maxActiveTail) maxActiveTail = activeTail;
		}
		try {
			const res = await originalCall(name, args, opts);
			if (name === "job_tail") await sleep(90);
			return res;
		} finally {
			if (name === "job_tail") activeTail -= 1;
		}
	};
	try {
		createJobMirror({ jobs, core: conn, jobId: "job-1", opts: MIRROR_OPTS, logger: quietLogger() });
		await sleep(400);
	} finally { conn.dispose(); }
	await Promise.race([jobs.started[0].done, sleep(2000)]);
	assert.equal(maxActiveTail, 1);
});

// ---- regression: OCR-0022 terminal notification must survive hook throws ----

test("throwing onTerminal still resolves done with the terminal status", async () => {
	const jobs = createFakeJobs();
	let calls = 0;
	createJobMirror({
		jobs,
		core: {
			callTool: async (name) => {
				calls += 1;
				if (name === "job_status") {
					return { text: JSON.stringify({ state: "completed" }) };
				}
				return { text: "[]" };
			},
		},
		jobId: "job-hook",
		jobType: "staged_sweep",
		opts: MIRROR_OPTS,
		logger: quietLogger(),
		onTerminal: () => {
			throw new Error("hook exploded");
		},
	});
	const rec = jobs.started[0];
	const outcome = await Promise.race([
		rec.done,
		sleep(2000).then(() => { throw new Error("done never resolved"); }),
	]);
	assert.equal(outcome.status, "completed");
	assert.ok(calls >= 1);
});

// ---- regression: OCR-0020 cancel deadline enforced without a poll tick ----

test("cancel confirmation window fires even when job_status hangs", async () => {
	const jobs = createFakeJobs();
	createJobMirror({
		jobs,
		core: {
			callTool: (_name, _args) => new Promise(() => {}), // never settles
		},
		jobId: "job-hang",
		jobType: "staged_sweep",
		opts: { ...MIRROR_OPTS, pollIntervalMs: 60_000, cancelConfirmTimeoutMs: 80 },
		logger: quietLogger(),
	});
	const rec = jobs.started[0];
	rec.hooks.cancel();
	const started = Date.now();
	const outcome = await Promise.race([
		rec.done,
		sleep(3000).then(() => { throw new Error("cancel deadline never fired"); }),
	]);
	assert.equal(outcome.status, "killed");
	// B02: cancel-deadline settlement is observer-unconfirmed, not a server terminal
	assert.equal(outcome.confirmedTerminal, false);
	assert.ok(Date.now() - started < 2500, "deadline overrun");
});

// ---- B02: unconfirmed settlements must not imply server terminal ----

test("B02: dispose settlement is unconfirmed", { timeout: 10000 }, async () => {
	const jobs = createFakeJobs();
	const disposed = { value: false };
	const metas = [];
	createJobMirror({
		jobs,
		core: {
			callTool: async (name) => {
				if (name === "job_status") return { text: JSON.stringify({ state: "running" }) };
				return { text: "[]" };
			},
		},
		jobId: "job-disp",
		opts: MIRROR_OPTS,
		logger: quietLogger(),
		isDisposed: () => disposed.value,
		onTerminal: (status, detail, meta) => { metas.push({ status, detail, ...meta }); },
	});
	const rec = jobs.started[0];
	disposed.value = true;
	const outcome = await Promise.race([
		rec.done,
		sleep(2000).then(() => { throw new Error("dispose never settled"); }),
	]);
	assert.equal(outcome.status, "failed");
	assert.equal(outcome.confirmedTerminal, false);
	assert.equal(metas[0]?.confirmedTerminal, false);
});

test("B02: server-confirmed completed is confirmedTerminal", { timeout: 10000 }, async () => {
	const jobs = createFakeJobs();
	const metas = [];
	createJobMirror({
		jobs,
		core: {
			callTool: async (name) => {
				if (name === "job_status") return { text: JSON.stringify({ state: "completed" }) };
				return { text: "[]" };
			},
		},
		jobId: "job-ok",
		opts: MIRROR_OPTS,
		logger: quietLogger(),
		onTerminal: (status, detail, meta) => { metas.push({ status, detail, ...meta }); },
	});
	const outcome = await Promise.race([
		jobs.started[0].done,
		sleep(2000).then(() => { throw new Error("never settled"); }),
	]);
	assert.equal(outcome.status, "completed");
	assert.equal(outcome.confirmedTerminal, true);
	assert.equal(outcome.lastObservedState, "completed");
	assert.equal(metas[0]?.confirmedTerminal, true);
});

test("B02: state store update annotates without dropping the row", () => {
	const dir = mkdtempSync(join(tmpdir(), "bridge-b02-"));
	try {
		const file = join(dir, "jobs.json");
		const store = createStateStore(file, quietLogger());
		assert.equal(store.add("job-x", "staged_sweep"), true);
		assert.equal(store.update("job-x", {
			unconfirmed: true,
			unconfirmedReason: "bridge disposed",
			lastObservedState: "running",
			mirrorStatus: "failed",
		}), true);
		const rows = JSON.parse(readFileSync(file, "utf8"));
		assert.equal(rows.length, 1);
		assert.equal(rows[0].jobId, "job-x");
		assert.equal(rows[0].unconfirmed, true);
		assert.equal(rows[0].unconfirmedReason, "bridge disposed");
		assert.equal(rows[0].lastObservedState, "running");
		assert.equal(store.update("missing", { unconfirmed: true }), false);
	} finally { rmSync(dir, { recursive: true, force: true }); }
});

test("B03: interrupted maps to failed and is terminal", () => {
	const terms = ["completed", "failed", "cancelled", "killed", "done", "terminal", "interrupted"];
	assert.equal(isTerminal("interrupted", terms), true);
	assert.equal(mapOutcome("interrupted"), "failed");
	assert.equal(isTerminal("not_completed", terms), false);
	assert.equal(isTerminal("cancel_requested", terms), false);
	assert.equal(isTerminal("nonterminal", terms), false);
});