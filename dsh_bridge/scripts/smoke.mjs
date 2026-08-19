import { createComsolConnection } from "../lib/mcp-client-core.mjs";
import { createJobMirror } from "../lib/job-mirror.mjs";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const HERE = dirname(fileURLToPath(import.meta.url));
const FIXTURE = join(HERE, "..", "fixtures", "fake-comsol-server.mjs");
const log = (...a) => console.log(`[smoke] ${a.join(" ")}`);

const child = spawn(process.execPath, [FIXTURE], { stdio: ["pipe", "pipe", "pipe"] });
const conn = createComsolConnection({ command: process.execPath, args: [FIXTURE], logger: console, reconnect: { enabled: false } });

const jobs = {
	started: [],
	start(spec) { const r = { id: `comsol-${this.started.length + 1}`, hooks: spec.run() }; this.started.push(r); return r.id; },
};

try {
	const ok = await conn.connect();
	if (!ok) throw new Error("connect failed");
	const tools = await conn.listTools();
	log(`discovered ${tools.length} tools`);
	const submit = await conn.callTool("job_submit", { spec: { job_type: "staged_sweep" } });
	log(`job_submit -> ${submit.text}`);
	const jobId = JSON.parse(submit.text).job_id;
	createJobMirror({ jobs, core: conn, jobId, jobType: "staged_sweep", opts: { pollIntervalMs: 50 }, logger: console });
	const rec = jobs.started[0];
	const outcome = await Promise.race([rec.hooks.done, new Promise((_, rej) => setTimeout(() => rej(new Error("timeout")), 8000))]);
	log(`mirror outcome: ${JSON.stringify(outcome)}`);
	const tail = rec.hooks.readOutput();
	log(`progress stream (${tail.length} chars): ${tail.split("\n").slice(0, 3).join(" | ")}`);
	if (outcome.status !== "completed") throw new Error("expected completed");
	if (tail.length === 0) throw new Error("expected progress stream");
	log("SMOKE PASS");
} catch (e) {
	log(`SMOKE FAIL: ${e.message}`);
	process.exitCode = 1;
} finally {
	conn.dispose();
	child.kill();
}