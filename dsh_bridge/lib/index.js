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
import { createJobMirror, createStateStore, parseJson, extractState, isTerminal } from "./job-mirror.mjs";

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
	terminalStates: ["completed", "failed", "cancelled", "killed", "done", "terminal"],
};

export function normalizeConfig(config) {
	const c = config ?? {};
	const base = { ...DEFAULTS, ...c };
	base.env = { ...DEFAULTS.env, ...(c.env ?? {}) };
	base.args = c.args ?? DEFAULTS.args;
	base.reconnect = { ...DEFAULTS.reconnect, ...(c.reconnect ?? {}) };
	base.terminalStates = c.terminalStates ?? DEFAULTS.terminalStates;
	base.stateFile = c.stateFile ?? join(base.cwd || DEFAULTS.cwd, ".dsh-comsol-bridge-jobs.json");
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
	let generation = new Map();

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
					mirrored.add(jobId);
					const jobType = parsed?.job_type ?? parsed?.jobType;
					stateStore.add(jobId, jobType);
					startMirror(jobId, jobType, exec.agent);
				}
			}
			return { content: res.content, ...(res.structuredContent !== undefined ? { structuredContent: res.structuredContent } : {}) };
		};
	}

	function startMirror(jobId, jobType, agent) {
		const jobs = ctx.get("jobs");
		if (!jobs) {
			logger.warn?.(`comsol-bridge: ctx.jobs unavailable; mirror skipped for ${jobId} (load dsh-jobs-local + dsh-tool-jobs)`);
			return;
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
				onTerminal: () => { mirrored.delete(jobId); stateStore.remove(jobId); },
			});
		} catch (e) {
			logger.warn?.(`comsol-bridge: mirror start failed for ${jobId}: ${e.message}`);
		}
	}

	async function rehydrate() {
		const jobs = ctx.get("jobs");
		if (!jobs || !cfg.jobMirrorEnabled) return;
		for (const rec of stateStore.list()) {
			if (mirrored.has(rec.jobId)) continue;
			try {
				const res = await connection.callTool("job_status", { job_id: rec.jobId }, { timeoutMs: 30000 });
				const state = extractState(
					res.structuredContent !== undefined ? res.structuredContent : parseJson(res.text)
				);
				if (state === undefined) continue;
				if (isTerminal(state, cfg.terminalStates)) { stateStore.remove(rec.jobId); continue; }
				mirrored.add(rec.jobId);
				startMirror(rec.jobId, rec.jobType, undefined);
			} catch {
				/* server busy or unreachable; leave the record for a later boot */
			}
		}
	}

	ctx.effect(() => () => {
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