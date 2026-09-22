// Fake comsol-mcp server for solver-free tests. Deterministic job semantics
// (per-job: POLLS_BEFORE_COMPLETE polls then completed; cancel → cancelled).
// FAKE_MODE env selects failure/corruption variants used by the regression suite:
//   exit-startup | garbage-first | unsupported-version | no-initialize |
//   malformed-tools | no-tools | call-error | ignore-cancel | split-write |
//   notify-list-changed
import { createInterface } from "node:readline";

const TOOLS = [
	{ name: "capabilities", description: "Deployment identity", inputSchema: { type: "object", properties: {}, additionalProperties: false } },
	{ name: "job_submit", description: "Submit a durable job", inputSchema: { type: "object", properties: { spec: { type: "object" } }, required: ["spec"] } },
	{ name: "job_status", description: "Job status", inputSchema: { type: "object", properties: { job_id: { type: "string" } }, required: ["job_id"] } },
	{ name: "job_tail", description: "Job log tail", inputSchema: { type: "object", properties: { job_id: { type: "string" }, n: { type: "number" } }, required: ["job_id"] } },
	{ name: "job_cancel", description: "Cancel a job", inputSchema: { type: "object", properties: { job_id: { type: "string" } }, required: ["job_id"] } },
	{ name: "stats", description: "Test stats", inputSchema: { type: "object", properties: {}, additionalProperties: false } },
	{ name: "die", description: "Exit the server (reconnect tests)", inputSchema: { type: "object", properties: {}, additionalProperties: false } },
];

const FAKE_MODE = process.env.FAKE_MODE ?? "";
const POLLS_BEFORE_COMPLETE = Number(process.env.FAKE_POLLS ?? 3);
const jobsState = new Map();
let jobSeq = 0;
let inFlight = 0;
let maxInFlight = 0;

function ensureJob(id) {
	if (!jobsState.has(id)) jobsState.set(id, { polls: 0, cancelled: false, tailCounter: 0 });
	return jobsState.get(id);
}

function writeOut(obj) {
	const payload = JSON.stringify(obj) + "\n";
	if (FAKE_MODE === "split-write") {
		const mid = Math.floor(payload.length / 2);
		process.stdout.write(payload.slice(0, mid));
		setTimeout(() => process.stdout.write(payload.slice(mid)), 5);
	} else {
		process.stdout.write(payload);
	}
}
function respond(id, result) { writeOut({ jsonrpc: "2.0", id, result }); }
function respondError(id, code, message) { writeOut({ jsonrpc: "2.0", id, error: { code, message } }); }

if (FAKE_MODE === "exit-startup") {
	process.stderr.write("fake: crashing at startup\n");
	process.exit(3);
}
if (FAKE_MODE === "garbage-first") {
	process.stdout.write("not json {{{{{\n{\"broken\":\n");
}
if (FAKE_MODE === "notify-list-changed") {
	setTimeout(() => process.stdout.write(JSON.stringify({ jsonrpc: "2.0", method: "notifications/tools/list_changed" }) + "\n"), 20);
}

function handleCall(params, id) {
	inFlight += 1;
	if (inFlight > maxInFlight) maxInFlight = inFlight;
	const name = params?.name;
	const args = params?.arguments ?? {};
	// Configurable handling delay so overlapping requests are observable.
	// FAKE_DELAY_MS overrides the default 10ms used by most tests.
	const delayMs = Number(process.env.FAKE_DELAY_MS ?? 10);
	setTimeout(() => {
		inFlight -= 1;
		switch (name) {
			case "capabilities":
				if (FAKE_MODE === "call-error") { respond(id, { content: [{ type: "text", text: "boom" }], isError: true }); break; }
				respond(id, { content: [{ type: "text", text: JSON.stringify({ success: true, package_version: "0.7.1-fake", tool_catalog_hash: "fake-hash" }) }] });
				break;
			case "job_submit": {
				jobSeq += 1;
				const jid = `job-${jobSeq}`;
				ensureJob(jid);
				respond(id, { content: [{ type: "text", text: JSON.stringify({ success: true, job_id: jid, job_type: "staged_sweep", state: "running" }) }] });
				break;
			}
			case "job_status": {
				const st = ensureJob(args.job_id);
				st.polls += 1;
				const state = FAKE_MODE === "ignore-cancel"
				? "running"
				: (st.cancelled ? "cancelled" : (st.polls >= POLLS_BEFORE_COMPLETE ? "completed" : "running"));
				respond(id, { content: [{ type: "text", text: JSON.stringify({ job_id: args.job_id, state, points: st.polls }) }] });
				break;
			}
			case "job_tail": {
				const st = ensureJob(args.job_id);
				st.tailCounter += 1;
				respond(id, { content: [{ type: "text", text: Array.from({ length: st.tailCounter }, (_, i) => `point ${i + 1} done`).join("\n") }] });
				break;
			}
			case "job_cancel":
				if (FAKE_MODE !== "ignore-cancel") ensureJob(args.job_id).cancelled = true;
				respond(id, { content: [{ type: "text", text: JSON.stringify({ job_id: args.job_id, state: "cancelling" }) }] });
				break;
			case "stats":
				respond(id, { content: [{ type: "text", text: JSON.stringify({ maxInFlight, jobCount: jobsState.size, jobs: [...jobsState.keys()] }) }] });
				break;
			case "die":
				respond(id, { content: [{ type: "text", text: "bye" }] });
				setTimeout(() => process.exit(0), 20);
				break;
			default:
				respondError(id, -32601, `unknown tool: ${name}`);
		}
	}, Number.isFinite(delayMs) && delayMs >= 0 ? delayMs : 10);
}

const rl = createInterface({ input: process.stdin, crlfDelay: Infinity });
rl.on("line", (line) => {
	const msg = JSON.parse(line);
	if (msg.id === undefined || msg.id === null) return; // notification
	switch (msg.method) {
		case "initialize":
			if (FAKE_MODE === "unsupported-version") { respond(msg.id, { protocolVersion: "1999-01-01", capabilities: {}, serverInfo: { name: "fake-comsol-mcp", version: "0.0.0" } }); break; }
			if (FAKE_MODE === "no-initialize") break; // never respond
			respond(msg.id, { protocolVersion: "2025-03-26", capabilities: { tools: { listChanged: true } }, serverInfo: { name: "fake-comsol-mcp", version: "0.0.0" } });
			break;
		case "tools/list": {
			if (FAKE_MODE === "malformed-tools") { respond(msg.id, { tools: [{ name: 123 }] }); break; }
			if (FAKE_MODE === "no-tools") { respond(msg.id, { tools: [] }); break; }
			const pageSize = Number(process.env.FAKE_PAGE_SIZE ?? 0);
			if (pageSize > 0) {
				const cursor = Number(msg.params?.cursor ?? 0);
				const slice = TOOLS.slice(cursor, cursor + pageSize);
				respond(msg.id, { tools: slice, ...(cursor + pageSize < TOOLS.length ? { nextCursor: String(cursor + pageSize) } : {}) });
			} else {
				respond(msg.id, { tools: TOOLS });
			}
			break;
		}
		case "tools/call":
			handleCall(msg.params, msg.id);
			break;
		default:
			respondError(msg.id, -32601, "method not found");
	}
});

process.on("uncaughtException", (e) => {
	process.stderr.write(`fake server uncaught: ${e.stack ?? e}\n`);
	process.exit(1);
});