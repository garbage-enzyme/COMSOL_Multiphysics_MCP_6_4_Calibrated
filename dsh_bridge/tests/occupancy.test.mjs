// B01 occupancy regression: caller timeout/abort must not free the single-
// flight connection slot while a sent request may still be executing.
import { test, after } from "node:test";
import assert from "node:assert/strict";
import { spawnFakeServer, makeConnection } from "./helpers.mjs";

const DELAY_MS = 100;
const WAITER_TIMEOUT_MS = 10;
// Absolute upper bound for occupancy release after a delayed response.
const ABSOLUTE_WAIT_MS = 3000;

function sleep(ms) {
	return new Promise((r) => setTimeout(r, ms));
}

async function waitFor(fn, { timeoutMs = ABSOLUTE_WAIT_MS, stepMs = 20 } = {}) {
	const deadline = Date.now() + timeoutMs;
	while (Date.now() < deadline) {
		const v = await fn();
		if (v) return v;
		await sleep(stepMs);
	}
	return null;
}

test("B01: timeout holds occupancy; second request does not overlap execution", { timeout: 10000 }, async () => {
	const fake = spawnFakeServer({ FAKE_DELAY_MS: String(DELAY_MS) });
	try {
		const conn = makeConnection(fake, {});
		await conn.connect();

		const t0 = Date.now();
		const first = conn.callTool("capabilities", {}, { timeoutMs: WAITER_TIMEOUT_MS });
		// Issue the second call immediately after the first has been queued.
		const second = conn.callTool("capabilities", {});

		await assert.rejects(first, /timed out/);
		const secondRes = await second;
		assert.ok(secondRes.text.includes("package_version"));

		const stats = await conn.callTool("stats", {});
		const parsed = JSON.parse(stats.text);
		assert.equal(parsed.maxInFlight, 1, `expected maxInFlight=1, got ${parsed.maxInFlight}`);
		// The second call cannot complete before the first server response frees
		// occupancy: at least ~DELAY_MS must have elapsed from the first send.
		assert.ok(Date.now() - t0 >= DELAY_MS - 5, "second call returned before occupancy could be held");
		conn.dispose();
	} finally {
		await fake.close();
	}
});

test("B01: abort after send keeps occupancy until the late response arrives", { timeout: 10000 }, async () => {
	const fake = spawnFakeServer({ FAKE_DELAY_MS: String(DELAY_MS) });
	try {
		const conn = makeConnection(fake, {});
		await conn.connect();

		const ac = new AbortController();
		const first = conn.callTool("capabilities", {}, { signal: ac.signal, timeoutMs: 0 });
		await sleep(15);
		ac.abort();
		await assert.rejects(first, /aborted/);

		const second = conn.callTool("capabilities", {});
		const res = await second;
		assert.ok(res.text.includes("package_version"));

		const stats = JSON.parse((await conn.callTool("stats", {})).text);
		assert.equal(stats.maxInFlight, 1);
		conn.dispose();
	} finally {
		await fake.close();
	}
});

test("B01: abort before send does not occupy the connection", { timeout: 10000 }, async () => {
	const fake = spawnFakeServer({});
	try {
		const conn = makeConnection(fake, {});
		await conn.connect();

		// Occupy the connection with a normal call first.
		const occupy = conn.callTool("capabilities", {});
		const ac = new AbortController();
		ac.abort();
		// Queued behind occupancy, aborted before it is sent.
		const cancelled = conn.callTool("capabilities", {}, { signal: ac.signal });
		await assert.rejects(cancelled, /aborted/);
		await occupy;

		const stats = JSON.parse((await conn.callTool("stats", {})).text);
		// Only the occupy call (and later stats) should have executed.
		assert.equal(stats.maxInFlight, 1);
		conn.dispose();
	} finally {
		await fake.close();
	}
});

test("B01: late response does not resolve a different waiter", { timeout: 10000 }, async () => {
	const fake = spawnFakeServer({ FAKE_DELAY_MS: String(DELAY_MS) });
	try {
		const conn = makeConnection(fake, {});
		await conn.connect();

		const first = conn.callTool("capabilities", {}, { timeoutMs: WAITER_TIMEOUT_MS });
		await assert.rejects(first, /timed out/);

		// Wait past the server delay so the late response is discarded.
		await sleep(DELAY_MS + 50);
		const res = await conn.callTool("capabilities", {});
		assert.ok(res.text.includes("package_version"));
		const stats = JSON.parse((await conn.callTool("stats", {})).text);
		assert.equal(stats.maxInFlight, 1);
		conn.dispose();
	} finally {
		await fake.close();
	}
});

test("B01: dispose frees occupancy for already-sent request", { timeout: 10000 }, async () => {
	const fake = spawnFakeServer({ FAKE_DELAY_MS: String(DELAY_MS) });
	try {
		const conn = makeConnection(fake, {});
		await conn.connect();
		const p = conn.callTool("capabilities", {}, { timeoutMs: 0 });
		await sleep(10);
		conn.dispose();
		await assert.rejects(p, /disposed|process lost|not connected/);
	} finally {
		await fake.close();
	}
});
