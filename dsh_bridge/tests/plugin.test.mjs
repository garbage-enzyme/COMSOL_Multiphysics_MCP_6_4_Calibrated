import { test } from "node:test";
import assert from "node:assert/strict";
import { publicToolName, normalizeConfig } from "../lib/index.js";
import { extractText } from "../lib/mcp-client-core.mjs";

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