// dsh-comsol-bridge: optional DSH compat layer for the standard comsol-mcp
// server. Registers the server's tools natively (clean names, per-tool
// approval configurable through the DSH approval layer) and turns job_submit
// results into ctx.jobs mirrors (completion notices, progress stream, kill).
//
// Design notes:
// - The plugin is the ONLY process in a DSH session that connects to the
//   comsol server (do not also configure a dsh-mcp-client entry for comsol).
// - All requests go through the connection's single-flight queue: the comsol
//   skill requires exactly one in-flight request per stdio server.
// - The mirror is a supervisor, never durable truth: comsol owns persistence
//   and resume; the mirror dies with the host and is rehydrated from the
//   state file (or a future job_list tool).

import { createHash } from "node:crypto";
import { join } from "node:path";
import { createComsolConnection, extractText } from "./mcp-client-core.mjs";
import { createJobMirror, createStateStore, parseJson, extractState, isTerminal, extractOwnerIdentity, ownerFromIdentity, mirrorDedupeKey } from "./job-mirror.mjs";

export const name = "comsol-bridge";
export const inject = ["tools"];

const DEFAULTS = {
	enabled: true,
	jobMirrorEnabled: true,
	command: "D:\\condaenvs\\comsol-mcp-py314\\Scripts\\comsol-mcp.exe",
	args: [],
	cwd: "D:\\comsol_runtime",
	env: { COMSOL_MCP_SETTINGS_PATH: "D:\\comsol_runtime\\settings.json" },
	pollIntervalMs: 15000,
	tailLines: 20,
	outputLimitBytes: 262144,
	toolCallTimeoutMs: 600000,
	initTimeoutMs: 60000,
	cancelConfirmTimeoutMs: 120000,
	failOnStartupError: false,
	reconnect: { enabled: true, initialDelayMs: 500, maxDelayMs: 30000, maxAttempts: 10 },
	terminalStates: ["completed", "failed", "cancelled", "killed", "done", "terminal", "interrupted"],
	// B02a: bounded rehydrate retries while an owner/controller comes up.
	rehydrateMaxAttempts: 5,
	rehydrateRetryDelayMs: 2000,
};

export function normalizeConfig(config) {
	const c = config ?? {};
	const base = { ...DEFAULTS, ...c };
	base.env = { ...DEFAULTS.env, ...(c.env ?? {}) };
	base.args = c.args ?? DEFAULTS.args;
	base.reconnect = { ...DEFAULTS.reconnect, ...(c.reconnect ?? {}) };
	base.terminalStates = c.terminalStates ?? DEFAULTS.terminalStates;
	base.stateFile = c.stateFile ?? join(base.cwd || DEFAULTS.cwd, ".dsh-comsol-bridge-jobs.json");
	if (typeof base.rehydrateMaxAttempts !== "number" || base.rehydrateMaxAttempts < 0) {
		base.rehydrateMaxAttempts = DEFAULTS.rehydrateMaxAttempts;
	}
	if (typeof base.rehydrateRetryDelayMs !== "number" || base.rehydrateRetryDelayMs < 0) {
		base.rehydrateRetryDelayMs = DEFAULTS.rehydrateRetryDelayMs;
	}
	return base;
}

// DeepSeek function-name contract: <=64 chars, [A-Za-z0-9_-]; a lossy
// normalization appends a deterministic identity hash (same rule as mcp-client).
const MAX_PUBLIC_NAME_LENGTH = 64;
const INVALID_NAME_CHARS = /[^A-Za-z0-9_-]/g;
const HASH_LENGTH = 12;

export function publicToolName(rawName) {
	const normalized = rawName.replace(INVALID_NAME_CHARS, "_");
	if (normalized === rawName && normalized.length <= MAX_PUBLIC_NAME_LENGTH) return normalized;
	const hash = createHash("sha256").update(`comsol\0${rawName}`).digest("hex").slice(0, HASH_LENGTH);
	return `${normalized.slice(0, MAX_PUBLIC_NAME_LENGTH - HASH_LENGTH - 1)}_${hash}`;
}

function createOutput(rawName) {
	return {
		schema: {
			type: "object",
			properties: {
				content: { type: "array", items: {} },
				structuredContent: {},
			},
			required: ["content"],
			additionalProperties: false,
		},
		render(_args, value) {
			return [{ type: "text", text: extractText(value?.content, rawName) }];
		},
	};
}

export async function apply(ctx, config) {
	const cfg = normalizeConfig(config);
	if (!cfg.enabled) {
		ctx.logger?.info?.("comsol-bridge: disabled by config (enabled=false)");
		return;
	}
	const logger = ctx.logger ?? console;
	const stateStore = createStateStore(cfg.stateFile, logger);
	const mirrored = new Set();
	// B02a: dedupe by job/attempt so restart/reconnect/ready never spawn
	// a second poller for the same durable identity.
	const mirroredKeys = new Set();
	let generation = new Map();
	let disposedPlugin = false;
	let rehydrateTimer = null;

	const connection = createComsolConnection({
		...cfg,
		logger,
		onTools: syncGeneration,
	});

	async function syncGeneration(tools) {
		const next = new Map();
		for (const tool of tools) {
			const publicName = publicToolName(tool.name);
			if (next.has(publicName)) throw new Error(`comsol server listed tool "${tool.name}" more than once`);
			const rawName = tool.name;
			next.set(publicName, {
				name: publicName,
				description: tool.description ?? "",
				parameters: tool.inputSchema,
				output: createOutput(rawName),
				execute: createExecutor(rawName),
				...(cfg.toolCallTimeoutMs > 0 ? { timeoutMs: cfg.toolCallTimeoutMs } : {}),
			});
		}
		const disposers = new Map();
		try {
			for (const [publicName, def] of next) disposers.set(publicName, ctx.tools.register(def));
		} catch (e) {
			for (const d of disposers.values()) d();
			logger.error?.(`comsol-bridge: tool registration failed, no tools registered: ${e.message}`);
			return;
		}
		for (const d of generation.values()) d();
		generation = disposers;
	}

	function createExecutor(rawName) {
		return async (args, exec) => {
			const res = await connection.callTool(rawName, args, { signal: exec.signal });
			if (rawName === "job_submit" && cfg.jobMirrorEnabled) {
				// Prefer the structured result when the server provides one;
				// parsing res.text alone no-ops when content carries no text.
				const parsed =
					res.structuredContent !== undefined ? res.structuredContent : parseJson(res.text);
				const jobId = parsed?.job_id ?? parsed?.jobId;
				if (typeof jobId === "string" && jobId && !mirrored.has(jobId)) {
					const jobType = parsed?.job_type ?? parsed?.jobType;
					const attempt = parsed?.attempt ?? parsed?.attempt_id ?? null;
					const ownerIdentity = extractOwnerIdentity(exec?.agent);
					// Mark as mirrored only after the mirror actually started;
					// a failed start must stay retryable instead of being
					// pinned in the mirrored set with no onTerminal cleanup.
					stateStore.add(jobId, jobType);
					if (ownerIdentity) {
						stateStore.update(jobId, {
							ownerAgentKey: ownerIdentity.key,
							ownerSessionId: ownerIdentity.sessionId,
							ownerKind: ownerIdentity.kind,
							attempt,
						});
					} else if (attempt != null) {
						stateStore.update(jobId, { attempt });
					}
					if (startMirror(jobId, jobType, exec?.agent)) {
						mirrored.add(jobId);
						mirroredKeys.add(mirrorDedupeKey(jobId, attempt));
					} else {
						stateStore.remove(jobId);
					}
				}
			}
			return { content: res.content, ...(res.structuredContent !== undefined ? { structuredContent: res.structuredContent } : {}) };
		};
	}

	function startMirror(jobId, jobType, agent) {
		const jobs = ctx.get("jobs");
		if (!jobs) {
			logger.warn?.(`comsol-bridge: ctx.jobs unavailable; mirror skipped for ${jobId} (load dsh-jobs-local + dsh-tool-jobs)`);
			return false;
		}
		try {
			createJobMirror({
				jobs,
				core: connection,
				jobId,
				jobType,
				...(agent ? { agent } : {}),
				opts: {
					pollIntervalMs: cfg.pollIntervalMs,
					tailLines: cfg.tailLines,
					outputLimitBytes: cfg.outputLimitBytes,
					cancelConfirmTimeoutMs: cfg.cancelConfirmTimeoutMs,
					terminalStates: cfg.terminalStates,
				},
				logger,
				isDisposed: () => connection.disposed,
				onTerminal: (status, detail, meta = {}) => {
					// B02: only a server-confirmed terminal may delete the
					// durable tracking row. Unconfirmed settlements keep the
					// record and annotate the reason for rehydrate/retry.
					mirrored.delete(jobId);
					if (meta?.confirmedTerminal) {
						stateStore.remove(jobId);
					} else {
						stateStore.update(jobId, {
							unconfirmed: true,
							unconfirmedReason: detail ?? status,
							lastObservedState: meta?.lastObservedState ?? null,
							mirrorStatus: status,
						});
					}
				},
			});
		} catch (e) {
			logger.warn?.(`comsol-bridge: mirror start failed for ${jobId}: ${e.message}`);
			return false;
		}
		return true;
	}

	/**
	 * B02a rehydrate: query the original job first (never job_submit), restore
	 * the persisted owner identity, and wait (bounded) for that owner's
	 * controller. Missing owner never invents one — the record is kept and
	 * marked recovery-blocked so the user has a recovery entry point.
	 */
	async function rehydrateOnce() {
		const jobs = ctx.get("jobs");
		if (!jobs || !cfg.jobMirrorEnabled || disposedPlugin) return;
		for (const rec of stateStore.list()) {
			if (disposedPlugin) return;
			if (mirrored.has(rec.jobId)) continue;
			const dedupeKey = mirrorDedupeKey(rec.jobId, rec.attempt ?? null);
			if (mirroredKeys.has(dedupeKey) && mirrored.has(rec.jobId)) continue;
			try {
				// Query the original job. Never resubmit a calculation.
				const res = await connection.callTool("job_status", { job_id: rec.jobId }, { timeoutMs: 30000 });
				const state = extractState(
					res.structuredContent !== undefined ? res.structuredContent : parseJson(res.text)
				);
				if (state === undefined) continue;
				if (isTerminal(state, cfg.terminalStates)) {
					stateStore.remove(rec.jobId);
					mirroredKeys.delete(dedupeKey);
					continue;
				}
				const owner = ownerFromIdentity(rec);
				if (owner === undefined) {
					// Legacy row or submit without a usable agent identity.
					// Do not bind an arbitrary current agent.
					stateStore.update(rec.jobId, {
						recoveryBlocked: true,
						recoveryReason: "missing owner identity; rebind from the original session or resume via job_resume",
						lastObservedState: state,
					});
					logger.warn?.(`comsol-bridge: rehydrate blocked for ${rec.jobId}: missing owner identity`);
					continue;
				}
				// B02a: DSH jobs.start requires the live registered Agent
				// instance, not a string key. Resolve it from the agents
				// registry; never invent an agent or pass the raw string.
				const agents = ctx.get("agents");
				const liveOwner = agents?.get?.(owner);
				if (!liveOwner) {
					stateStore.update(rec.jobId, {
						recoveryBlocked: true,
						recoveryReason: `owner "${owner}" is not live in the agent registry; waiting for that session/controller`,
						lastObservedState: state,
					});
					logger.warn?.(`comsol-bridge: rehydrate waiting for live owner ${owner} of ${rec.jobId}`);
					continue;
				}
				if (rec.unconfirmed || rec.recoveryBlocked) {
					stateStore.update(rec.jobId, {
						unconfirmed: false,
						unconfirmedReason: null,
						recoveryBlocked: false,
						recoveryReason: null,
					});
				}
				// Restore the original owner so jobs.start routes to that
				// agent's controller (B02a). Failures leave the row retryable.
				if (startMirror(rec.jobId, rec.jobType, liveOwner)) {
					mirrored.add(rec.jobId);
					mirroredKeys.add(dedupeKey);
					stateStore.update(rec.jobId, { lastObservedState: state });
				} else {
					stateStore.update(rec.jobId, {
						recoveryBlocked: true,
						recoveryReason: "jobs.start rejected restored owner or controller not ready",
						lastObservedState: state,
					});
				}
			} catch {
				/* server busy or unreachable; leave the record for a later boot */
			}
		}
	}

	function scheduleRehydrateRetry(remaining) {
		if (disposedPlugin || remaining <= 0 || !cfg.jobMirrorEnabled) return;
		if (rehydrateTimer) return;
		const delay = Math.max(50, cfg.rehydrateRetryDelayMs);
		rehydrateTimer = setTimeout(() => {
			rehydrateTimer = null;
			if (disposedPlugin) return;
			void rehydrateOnce()
				.catch(() => {})
				.then(() => {
					const stillPending = stateStore.list().some((r) => !mirrored.has(r.jobId));
					if (stillPending) scheduleRehydrateRetry(remaining - 1);
				});
		}, delay);
	}

	async function rehydrate() {
		await rehydrateOnce();
		const stillPending = stateStore.list().some((r) => !mirrored.has(r.jobId));
		if (stillPending) scheduleRehydrateRetry(Math.max(0, cfg.rehydrateMaxAttempts - 1));
	}

	ctx.effect(() => () => {
		disposedPlugin = true;
		if (rehydrateTimer) {
			clearTimeout(rehydrateTimer);
			rehydrateTimer = null;
		}
		connection.dispose();
		for (const d of generation.values()) d();
		generation = new Map();
	}, "comsol-bridge.lifecycle");

	const connected = await connection.connect();
	if (!connected) {
		logger.error?.("comsol-bridge: initial connection failed; reconnecting in the background");
		if (cfg.failOnStartupError) throw new Error("comsol-bridge: initial connection failed");
		return;
	}
	await rehydrate();
}