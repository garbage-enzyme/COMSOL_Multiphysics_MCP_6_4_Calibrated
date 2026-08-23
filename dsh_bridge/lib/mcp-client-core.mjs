// MCP stdio client core for the comsol-mcp server. Context-free (no Cordis
// imports): owns process spawn, line framing, JSON-RPC dispatch, the
// single-flight serialization queue (one request at a time to the server,
// per the comsol skill's transport rule), tool discovery, and bounded
// reconnect with exponential backoff.

import { spawn } from "node:child_process";

export const MCP_PROTOCOL_VERSION = "2025-03-26";

/** Protocol revisions this client accepts from the server (fail closed otherwise). */
export const SUPPORTED_PROTOCOL_VERSIONS = new Set(["2025-03-26", "2025-06-18"]);

/** Project MCP content blocks into one text line, mirroring dsh-mcp-client. */
export function extractText(mcpContent, toolName) {
	const parts = [];
	for (const value of mcpContent ?? []) {
		if (typeof value !== "object" || value === null || Array.isArray(value)) {
			parts.push("[unsupported content type: unknown]");
			continue;
		}
		switch (value.type) {
			case "text":
				parts.push(value.text ?? "");
				break;
			case "image":
				parts.push(`[image: ${value.mimeType ?? "unknown"}, content discarded]`);
				break;
			case "audio":
				parts.push("[audio: content discarded]");
				break;
			case "resource":
			case "resource_link":
				parts.push("[resource: content discarded]");
				break;
			default:
				parts.push(`[unsupported content type: ${value.type}]`);
		}
	}
	return parts.join("\n") || `(${toolName} returned no text content)`;
}

/**
 * One comsol-mcp connection. All server requests funnel through one promise
 * chain so the server sees exactly one in-flight request at a time.
 */
export function createComsolConnection(opts) {
	const {
		command,
		args = [],
		cwd,
		env = {},
		logger = console,
		toolCallTimeoutMs = 600000,
		initTimeoutMs = 60000,
		reconnect = { enabled: true, initialDelayMs: 500, maxDelayMs: 30000, maxAttempts: 10 },
		onTools = async () => {},
	} = opts;

	let child = null;
	let alive = false;
	let ready = false;
	let stdoutBuf = "";
	let seq = 0;
	const pending = new Map();
	let queue = Promise.resolve();
	let disposed = false;
	let reconnectAttempts = 0;
	let reconnectTimer = null;
	let budgetExhausted = false;
	let connectedAt = 0;
	let syncInFlight = false;
	let resyncPending = false;

	const log = (level, ...a) => {
		try { logger?.[level]?.(...a) } catch {}
	};

	const enqueue = (fn) => {
		const run = queue.then(fn);
		queue = run.then(() => undefined, () => undefined);
		return run;
	};

	function rejectAllPending(reason) {
		for (const [id, entry] of pending) {
			if (entry.timer) clearTimeout(entry.timer);
			entry.cleanup?.();
			entry.reject(new Error(reason));
		}
		pending.clear();
	}

	function rpc(method, params, { timeoutMs = toolCallTimeoutMs, signal } = {}) {
		return enqueue(() => new Promise((resolve, reject) => {
			if (disposed) { reject(new Error("comsol-bridge disposed")); return; }
			if (!child || !alive) { reject(new Error("comsol server not connected")); return; }
			const id = ++seq;
			const entry = { resolve, reject, timer: null, cleanup: null };
			if (timeoutMs > 0) {
				entry.timer = setTimeout(() => {
					if (!pending.has(id)) return;
					pending.delete(id);
					reject(new Error(`comsol call ${method} timed out after ${timeoutMs}ms`));
				}, timeoutMs);
			}
			if (signal) {
				const onAbort = () => {
					if (!pending.has(id)) return;
					pending.delete(id);
					if (entry.timer) clearTimeout(entry.timer);
					reject(new Error("tool call aborted"));
				};
				if (signal.aborted) {
					if (entry.timer) clearTimeout(entry.timer);
					reject(new Error("tool call aborted"));
					return;
				}
				signal.addEventListener("abort", onAbort, { once: true });
				entry.cleanup = () => signal.removeEventListener("abort", onAbort);
			}
			pending.set(id, entry);
			child.stdin.write(JSON.stringify({ jsonrpc: "2.0", id, method, params }) + "\n");
		}));
	}

	function onMessage(msg) {
		if (!msg || typeof msg !== "object") return;
		if (msg.id !== undefined && msg.id !== null) {
			const entry = pending.get(msg.id);
			if (!entry) return;
			pending.delete(msg.id);
			if (entry.timer) clearTimeout(entry.timer);
			entry.cleanup?.();
			if (msg.error) entry.reject(new Error(`comsol server error ${msg.error.code ?? ""}: ${msg.error.message ?? "unknown"}`));
			else entry.resolve(msg.result);
			return;
		}
		if (msg.method === "notifications/tools/list_changed") {
			log("info", "server announced tools/list_changed; re-syncing");
			// No outer enqueue: syncTools issues rpc calls that are themselves
			// serialized on the queue; wrapping it would deadlock the queue.
			void syncTools().catch(() => {});
			return;
		}		if (msg.method === "notifications/message") {
			log("info", `server message ${msg.params?.level ?? "info"}: ${msg.params?.data ?? ""}`);
		}
	}

	function onStdoutData(chunk) {
		stdoutBuf += chunk;
		let idx;
		while ((idx = stdoutBuf.indexOf("\n")) >= 0) {
			const line = stdoutBuf.slice(0, idx).trim();
			stdoutBuf = stdoutBuf.slice(idx + 1);
			if (!line) continue;
			try { onMessage(JSON.parse(line)); } catch { log("warn", "ignoring unparseable server line"); }
		}
	}

	function onChildLost() {
		if (!alive) return;
		alive = false;
		ready = false;
		rejectAllPending("comsol server process lost");
		log("warn", "server process lost");
		scheduleReconnect();
	}

	function scheduleReconnect() {
		if (disposed || !reconnect.enabled) return;
		if (reconnectTimer) return;
		if (reconnectAttempts >= reconnect.maxAttempts) {
			if (!budgetExhausted) {
				budgetExhausted = true;
				log("error", "reconnect budget exhausted; comsol tools unregistered (restart DSH or reload the patch to recover)");
				Promise.resolve(onTools([])).catch(() => {});
			}
			return;
		}
		const delay = Math.min(reconnect.initialDelayMs * 2 ** reconnectAttempts, reconnect.maxDelayMs);
		reconnectAttempts += 1;
		log("warn", `reconnecting in ${delay}ms (attempt ${reconnectAttempts}/${reconnect.maxAttempts})`);
		reconnectTimer = setTimeout(async () => {
			reconnectTimer = null;
			if (disposed) return;
			try {
				await connectOnce();
				log("info", "reconnected");
			} catch (e) {
				log("warn", `reconnect attempt failed: ${e.message}`);
				scheduleReconnect();
			}
		}, delay);
	}

	function startChild() {
		return new Promise((resolve, reject) => {
			let settled = false;
			let c;
			try {
				c = spawn(command, args, { cwd, env: { ...process.env, ...env }, windowsHide: true, stdio: ["pipe", "pipe", "pipe"] });
			} catch (e) { reject(e); return; }
			child = c;
			alive = true;
			c.stdout.setEncoding("utf8");
			c.stdout.on("data", onStdoutData);
			// An unhandled stdin 'error' (EPIPE when the server dies mid-write)
			// would crash the whole DSH host; treat it as process loss instead.
			c.stdin.on("error", () => { onChildLost(); });
			c.stderr.setEncoding("utf8");
			let stderrBuf = "";
			c.stderr.on("data", (chunk) => {
				stderrBuf += chunk;
				if (stderrBuf.length > 8192) stderrBuf = stderrBuf.slice(-4096);
			});
			c.on("error", (e) => {
				alive = false;
				if (!settled) { settled = true; reject(e); } else onChildLost();
			});
			c.on("exit", () => {
				if (!settled) { settled = true; alive = false; reject(new Error("server exited during startup")); } else onChildLost();
			});
			rpc("initialize", {
				protocolVersion: MCP_PROTOCOL_VERSION,
				capabilities: {},
				clientInfo: { name: "dsh-comsol-bridge", version: "0.1.0" },
			}, { timeoutMs: initTimeoutMs })
				.then((result) => {
					const version = result?.protocolVersion;
					if (typeof version !== "string" || !SUPPORTED_PROTOCOL_VERSIONS.has(version)) {
						alive = false;
						if (!settled) { settled = true; reject(new Error(`unsupported MCP protocol version: ${String(version)}`)); }
						return;
					}
					child?.stdin.write(JSON.stringify({ jsonrpc: "2.0", method: "notifications/initialized" }) + "\n");
					if (!settled) { settled = true; ready = true; resolve(); }
				})
				.catch((e) => {
					alive = false;
					if (!settled) { settled = true; reject(e); }
				});
		});
	}

	async function listTools() {
		let cursor;
		const tools = [];
		do {
			const res = await rpc("tools/list", cursor ? { cursor } : {}, { timeoutMs: initTimeoutMs });
			for (const tool of res?.tools ?? []) {
				if (!tool || typeof tool.name !== "string" || tool.name.length === 0) throw new Error("comsol server returned a malformed tool list");
				tools.push(tool);
			}
			cursor = res?.nextCursor;
		} while (cursor);
		return tools;
	}

	async function syncTools() {
		// Coalesce overlapping syncs: the rpc queue serializes individual
		// tools/list calls but cannot order two interleaved pagination runs,
		// so a notification arriving mid-sync schedules exactly one follow-up
		// pass after the in-flight sync completes instead of racing it.
		if (syncInFlight) {
			resyncPending = true;
			return;
		}
		syncInFlight = true;
		try {
			do {
				resyncPending = false;
				const tools = await listTools();
				await onTools(tools);
				if (Date.now() - connectedAt > reconnect.maxDelayMs) reconnectAttempts = 0;
			} while (resyncPending && !disposed);
		} finally {
			syncInFlight = false;
		}
	}

	async function connectOnce() {
		try {
			await startChild();
			connectedAt = Date.now();
			await syncTools();
		} catch (e) {
			// Startup or tool discovery failed after spawning: tear the child
			// down and clear readiness so the connected getter stays truthful
			// and a scheduled reconnect cannot run beside a live orphan.
			ready = false;
			alive = false;
			if (child) {
				try { child.kill(); } catch { /* already gone */ }
				child = null;
			}
			throw e;
		}
	}

	/** Attempt initial connection; failures schedule background reconnects. */
	async function connect() {
		try {
			await connectOnce();
			return true;
		} catch (e) {
			log("warn", `initial connect failed: ${e.message}`);
			scheduleReconnect();
			return false;
		}
	}

	function callTool(rawName, args, opts = {}) {
		return rpc("tools/call", { name: rawName, arguments: args ?? {} }, opts).then((result) => {
			const content = Array.isArray(result?.content) ? result.content : [];
			const text = extractText(content, rawName);
			if (result?.isError === true) throw new Error(text);
			return { content, text, structuredContent: result?.structuredContent };
		});
	}

	function dispose() {
		if (disposed) return;
		disposed = true;
		if (reconnectTimer) clearTimeout(reconnectTimer);
		rejectAllPending("comsol-bridge disposed");
		child?.kill();
		child = null;
		alive = false;
	}

	return {
		connect,
		callTool,
		listTools,
		dispose,
		get connected() { return ready && alive; },
		get budgetExhausted() { return budgetExhausted; },
		get disposed() { return disposed; },
	};
}
