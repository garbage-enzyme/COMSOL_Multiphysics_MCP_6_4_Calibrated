import { test, after } from "node:test";
import assert from "node:assert/strict";
import { spawnFakeServer, makeConnection } from "./helpers.mjs";

const server = spawnFakeServer();
after(() => server.close());

let syncCount = 0;
let lastTools = [];
const conn = makeConnection(server, {
	onTools: (tools) => { syncCount += 1; lastTools = tools; },
});

after(async () => { conn.dispose(); });

test("connect discovers the full tool surface", async () => {
	const ok = await conn.connect();
	assert.equal(ok, true);
	assert.equal(conn.connected, true);
	const names = lastTools.map((t) => t.name);
	for (const expected of ["capabilities", "job_submit", "job_status", "job_tail", "job_cancel", "stats", "die"]) {
		assert.ok(names.includes(expected), `missing tool ${expected}`);
	}
});

test("tools/call returns canonical text projection", async () => {
	const res = await conn.callTool("capabilities", {});
	assert.ok(res.text.includes("package_version"), res.text);
	assert.ok(Array.isArray(res.content));
});

test("serialization: never more than one in-flight request", async () => {
	await Promise.all(Array.from({ length: 10 }, () => conn.callTool("job_status", { job_id: "j" })));
	const stats = await conn.callTool("stats", {});
	const parsed = JSON.parse(stats.text);
	assert.equal(parsed.maxInFlight, 1, `expected maxInFlight=1, got ${parsed.maxInFlight}`);
});

test("aborted signal rejects the call", async () => {
	const ac = new AbortController();
	ac.abort();
	await assert.rejects(conn.callTool("capabilities", {}, { signal: ac.signal }), /aborted/);
	const res = await conn.callTool("capabilities", {});
	assert.ok(res.text.includes("package_version"));
});

test("per-call timeout rejects and leaves the connection usable", async () => {
	await assert.rejects(conn.callTool("capabilities", {}, { timeoutMs: 5 }), /timed out/);
	const res = await conn.callTool("capabilities", {});
	assert.ok(res.text.includes("package_version"));
});

test("dispose rejects pending and subsequent calls", async () => {
	const c2 = makeConnection(server, {});
	await c2.connect();
	c2.dispose();
	await assert.rejects(c2.callTool("capabilities", {}), /disposed|not connected/);
});

test("reconnect: server exit respawns and re-discovers tools", { timeout: 15000 }, async () => {
	const before = syncCount;
	await conn.callTool("die", {});
	const deadline = Date.now() + 12000;
	while (syncCount <= before && Date.now() < deadline) {
		await new Promise((r) => setTimeout(r, 100));
	}
	assert.ok(syncCount > before, `expected a re-sync, syncCount=${syncCount} before=${before}`);
	assert.equal(conn.connected, true);
	const res = await conn.callTool("capabilities", {});
	assert.ok(res.text.includes("package_version"));
});

test("pagination: cursor walk assembles the full tool list", { timeout: 10000 }, async () => {
	const paged = spawnFakeServer({ FAKE_PAGE_SIZE: "2" });
	try {
		let tools = [];
		const pc = makeConnection(paged, { onTools: (t) => { tools = t; } });
		await pc.connect();
		assert.equal(tools.length, 7, `expected 7 tools across pages, got ${tools.length}`);
		pc.dispose();
	} finally {
		await paged.close();
	}
});