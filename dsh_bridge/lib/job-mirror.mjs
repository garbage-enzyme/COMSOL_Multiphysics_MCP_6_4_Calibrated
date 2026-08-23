// Job mirror: turns a comsol durable job (identified by its server-side
// job_id) into a DSH ctx.jobs native job so the model gets completion
// notices, a progress stream, and kill/wait through the harness job tools.
//
// The mirror is a supervisor, never the durable truth: the comsol worker and
// its SQLite/append-only journals own persistence and resume. If the bridge
// or host dies, the job keeps running server-side and can be re-attached
// later (rehydrate) or resumed with job_resume.

import { readFileSync, writeFileSync, mkdirSync, renameSync, unlinkSync } from "node:fs";
import { dirname } from "node:path";

export function parseJson(text) {
	try { return JSON.parse(text); } catch { return undefined; }
}

export function extractState(parsed) {
	if (!parsed || typeof parsed !== "object") return undefined;
	if (typeof parsed.state === "string") return parsed.state;
	if (typeof parsed.status === "string") return parsed.status;
	const nested = parsed.state;
	if (nested && typeof nested === "object") {
		if (typeof nested.phase === "string") return nested.phase;
		if (typeof nested.name === "string") return nested.name;
	}
	return undefined;
}

export function isTerminal(state, terminalStates) {
	const s = String(state ?? "").toLowerCase();
	return terminalStates.some((t) => s.includes(String(t).toLowerCase()));
}

export function mapOutcome(state) {
	const s = String(state ?? "").toLowerCase();
	if (s.includes("cancel")) return "killed";
	if (s.includes("fail")) return "failed";
	return "completed";
}

const MIRROR_DEFAULTS = {
	pollIntervalMs: 15000,
	tailLines: 20,
	outputLimitBytes: 262144,
	cancelConfirmTimeoutMs: 120000,
	terminalStates: ["completed", "failed", "cancelled", "killed", "done", "terminal"],
};

/**
 * Start one mirror via jobs.start(spec). `jobs` is the ctx.jobs registry
 * (or a test double). Returns nothing; hooks are owned by the registry.
 */
export function createJobMirror({ jobs, core, jobId, jobType, agent, opts = {}, logger = console, isDisposed = () => false, onTerminal = () => {} }) {
	const cfg = { ...MIRROR_DEFAULTS, ...opts };
	const log = (level, ...a) => {
		try { logger?.[level]?.(...a) } catch {}
	};

	jobs.start({
		kind: "comsol",
		label: `comsol job ${jobId}${jobType ? ` (${jobType})` : ""}`,
		...(agent ? { owner: agent } : {}),
		outputLimitBytes: cfg.outputLimitBytes,
		run: () => {
			let settled = false;
			let resolveDone;
			const done = new Promise((res) => { resolveDone = res; });
			let pollTimer = null;
			let tailTimer = null;
			let outputBuffer = "";
			let lastTail = "";
			let cancelDeadline = null;
			// The callTool timeout exceeds the poll interval, so each loop
			// must skip ticks while its previous call is still in flight;
			// otherwise overlapping tails duplicate progress deltas.
			let pollInFlight = false;
			let tailInFlight = false;

			const finish = (status, detail) => {
				if (settled) return;
				settled = true;
				if (pollTimer) clearInterval(pollTimer);
				if (tailTimer) clearInterval(tailTimer);
				onTerminal();
				resolveDone({ status, detail });
			};

			const poll = async () => {
				if (settled || pollInFlight) return;
				pollInFlight = true;
				try {
					if (isDisposed()) { finish("failed", "bridge disposed"); return; }
					try {
						const res = await core.callTool("job_status", { job_id: jobId }, { timeoutMs: 30000 });
						const state = extractState(parseJson(res.text));
						if (state !== undefined && isTerminal(state, cfg.terminalStates)) {
							finish(mapOutcome(state), `state=${state}`);
							return;
						}
					} catch (e) {
						if (isDisposed()) { finish("failed", "bridge disposed"); return; }
						if (core.budgetExhausted) {
							finish("failed", "bridge lost contact with comsol server; job may still be running (use job_resume after recovery)");
							return;
						}
						log("warn", `job_status poll failed for ${jobId}: ${e.message}`);
					}
					if (cancelDeadline !== null && Date.now() > cancelDeadline) {
						finish("killed", "cancel requested; outcome unconfirmed (server unreachable or slow)");
					}
				} finally {
					pollInFlight = false;
				}
			};

			const tail = async () => {
				if (settled || tailInFlight || isDisposed()) return;
				tailInFlight = true;
				try {
					const res = await core.callTool("job_tail", { job_id: jobId, n: cfg.tailLines }, { timeoutMs: 30000 });
					const text = res.text;
					if (text && text !== lastTail) {
						const delta = text.startsWith(lastTail) ? text.slice(lastTail.length) : text;
						lastTail = text;
						outputBuffer += delta;
						if (outputBuffer.length > cfg.outputLimitBytes) outputBuffer = outputBuffer.slice(-cfg.outputLimitBytes);
					}
				} catch {
					/* keep last buffer; server unreachable */
				} finally {
					tailInFlight = false;
				}
			};

			pollTimer = setInterval(() => void poll(), cfg.pollIntervalMs);
			tailTimer = setInterval(() => void tail(), cfg.pollIntervalMs);
			void poll();
			void tail();

			return {
				cancel: () => {
					cancelDeadline = Date.now() + cfg.cancelConfirmTimeoutMs;
					void core.callTool("job_cancel", { job_id: jobId }, { timeoutMs: 30000 }).catch(() => {});
				},
				done,
				readOutput: () => {
					const out = outputBuffer;
					outputBuffer = "";
					return out;
				},
			};
		},
	});
}

/** Atomic JSON state store for mirrored job ids (rehydrate after restart).
 * add/remove return true when the state file was persisted and false when
 * persistence failed (the failure is always logged; the caller decides
 * whether a false result matters for its retry semantics). */
export function createStateStore(file, logger = console) {
	const log = (level, ...a) => {
		try { logger?.[level]?.(...a) } catch {}
	};
	function read() {
		try { return JSON.parse(readFileSync(file, "utf8")); } catch { return []; }
	}
	function write(rows) {
		let payload;
		try {
			payload = JSON.stringify(rows, null, 2);
			mkdirSync(dirname(file), { recursive: true });
		} catch (e) {
			log("warn", `state persist failed: ${e.message}`);
			return false;
		}
		const tmp = `${file}.${process.pid}.tmp`;
		try {
			writeFileSync(tmp, payload);
		} catch (e) {
			log("warn", `state persist failed: ${e.message}`);
			try { unlinkSync(tmp); } catch {}
			return false;
		}
		// Windows sharing violations on the destination are transient; retry
		// a bounded number of times synchronously so the outcome is known
		// before returning, and never leave the tmp file behind.
		const maxAttempts = 3;
		for (let attempt = 1; attempt <= maxAttempts; attempt++) {
			try {
				renameSync(tmp, file);
				return true;
			} catch (e) {
				if (attempt === maxAttempts) {
					log("warn", `state rename failed after ${attempt} attempts: ${e.message}`);
					try { unlinkSync(tmp); } catch {}
					return false;
				}
				log("warn", `state rename failed (${e.message}); retrying (${attempt}/${maxAttempts - 1})`);
				try {
					// Bounded 50ms sleep without leaving the event loop.
					Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 50);
				} catch {}
			}
		}
		return false;
	}
	return {
		list: read,
		add(jobId, jobType) {
			const rows = read();
			if (!rows.some((r) => r.jobId === jobId)) {
				rows.push({ jobId, jobType: jobType ?? null, at: Date.now() });
				return write(rows);
			}
			return true;
		},
		remove(jobId) {
			return write(read().filter((r) => r.jobId !== jobId));
		},
	};
}