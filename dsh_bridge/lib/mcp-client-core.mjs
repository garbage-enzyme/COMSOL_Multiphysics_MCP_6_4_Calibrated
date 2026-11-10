// MCP stdio client core for the comsol-mcp server. Context-free (no Cordis
// imports): owns process spawn, line framing, JSON-RPC dispatch, the
// single-flight serialization queue (one request at a time to the server,
// per the comsol skill's transport rule), tool discovery, and bounded
// reconnect with exponential backoff.

import { spawn } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";

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
		shutdownTimeoutMs = 15000,
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
	let disposePromise = null;
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
			// Transport loss is terminal for occupancy: no response can arrive.
			entry.onTransportLost(new Error(reason));
		}
		pending.clear();
	}

	/**
	 * Issue one JSON-RPC request.
	 *
	 * Occupancy contract (B01): the single-flight queue advances only after
	 * the server response (or transport loss). Caller timeout/abort settles
	 * the waiter promise only; the already-sent request keeps the connection
	 * busy so a second tool call cannot overlap server execution. A late
	 * response is discarded and never resolves a different waiter.
	 */
	function rpc(method, params, { timeoutMs = toolCallTimeoutMs, signal } = {}) {
		return new Promise((outerResolve, outerReject) => {
			enqueue(() => new Promise((releaseOccupancy) => {
				if (disposed) {
					outerReject(new Error("comsol-bridge disposed"));
					releaseOccupancy();
					return;
				}
				if (!child || !alive) {
					outerReject(new Error("comsol server not connected"));
					releaseOccupancy();
					return;
				}
				if (signal?.aborted) {
					// Never sent: abort before send does not occupy the connection.
					outerReject(new Error("tool call aborted"));
					releaseOccupancy();
					return;
				}
				const id = ++seq;
				let waiterSettled = false;
				const settleWaiter = (kind, value) => {
					if (waiterSettled) return;
					waiterSettled = true;
					if (kind === "resolve") outerResolve(value);
					else outerReject(value);
				};
				const entry = {
					timer: null,
					cleanup: null,
					onResult(result) {
						settleWaiter("resolve", result);
						releaseOccupancy();
					},
					onError(err) {
						settleWaiter("reject", err);
						releaseOccupancy();
					},
					onTransportLost(err) {
						settleWaiter("reject", err);
						releaseOccupancy();
					},
				};
				if (timeoutMs > 0) {
					entry.timer = setTimeout(() => {
						// Waiter only: keep the pending entry so occupancy holds
						// until the real response or transport loss.
						settleWaiter("reject", new Error(`comsol call ${method} timed out after ${timeoutMs}ms`));
					}, timeoutMs);
				}
				if (signal) {
					const onAbort = () => {
						settleWaiter("reject", new Error("tool call aborted"));
					};
					signal.addEventListener("abort", onAbort, { once: true });
					entry.cleanup = () => signal.removeEventListener("abort", onAbort);
					if (signal.aborted) {
						// Abort raced the listener registration: free occupancy
						// only if the request was never written.
						if (entry.timer) clearTimeout(entry.timer);
						entry.cleanup();
						pending.delete(id);
						settleWaiter("reject", new Error("tool call aborted"));
						releaseOccupancy();
						return;
					}
				}
				pending.set(id, entry);
				child.stdin.write(JSON.stringify({ jsonrpc: "2.0", id, method, params }) + "\n");
			})).catch((err) => {
				outerReject(err);
			});
		});
	}

	function onMessage(msg) {
		if (!msg || typeof msg !== "object") return;
		if (msg.id !== undefined && msg.id !== null) {
			const entry = pending.get(msg.id);
			// Unknown or already-discarded id: never attach a late response
			// to a different waiter.
			if (!entry) return;
			pending.delete(msg.id);
			if (entry.timer) clearTimeout(entry.timer);
			entry.cleanup?.();
			if (msg.error) entry.onError(new Error(`comsol server error ${msg.error.code ?? ""}: ${msg.error.message ?? "unknown"}`));
			else entry.onResult(msg.result);
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
			c.stdout.on("data", (chunk) => { if (c === child) onStdoutData(chunk); });
			// An unhandled stdin 'error' (EPIPE when the server dies mid-write)
			// would crash the whole DSH host; treat it as process loss instead.
			// Every handler is guarded by child identity: a terminated child's
			// late exit or error event must not tear down its replacement.
			c.stdin.on("error", () => { if (c === child) onChildLost(); });
			c.stderr.setEncoding("utf8");
			let stderrBuf = "";
			c.stderr.on("data", (chunk) => {
				stderrBuf += chunk;
				if (stderrBuf.length > 8192) stderrBuf = stderrBuf.slice(-4096);
			});
			c.on("error", (e) => {
				if (c === child) alive = false;
				if (!settled) { settled = true; reject(e); } else if (c === child) onChildLost();
			});
			c.on("exit", () => {
				if (!settled) { settled = true; alive = false; reject(new Error("server exited during startup")); } else if (c === child) onChildLost();
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
		if (disposePromise) {
			// A previous disposal may still be awaiting the host's normal
			// shutdown; a restart begun earlier would meet a live owner.
			await disposePromise.catch(() => {});
			disposePromise = null;
		}
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
			// connectOnce can fail while an RPC waiter has already timed out.
			// In that case the RPC remains pending to preserve single-flight
			// occupancy until a response or transport loss. Startup teardown is
			// our transport-loss boundary, so release those queue entries here.
			rejectAllPending("comsol server startup failed");
			if (child) {
				const dying = child;
				child = null;
				// A child that already exited never emits "exit" again.
				if (dying.exitCode === null && dying.signalCode === null) {
					try { dying.kill(); } catch { /* already gone */ }
					// Bounded termination: a replacement starting before this
					// process is gone met a live owner's runtime state.
					await new Promise((resolve) => {
						const timer = setTimeout(resolve, shutdownTimeoutMs);
						dying.once("exit", () => { clearTimeout(timer); resolve(); });
					});
				}
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
		if (disposePromise) return disposePromise;
		disposed = true;
		if (reconnectTimer) clearTimeout(reconnectTimer);
		reconnectTimer = null;
		rejectAllPending("comsol-bridge disposed");
		disposePromise = shutdownGracefully();
		return disposePromise;
	}

	// BRIDGE-CLOSE-01: disposal closes the server's stdin and awaits its
	// bounded, verified normal shutdown. Killing here instead would leave the
	// host's admission record behind with a lost Windows job name, and the
	// next connection in the same runtime would correctly refuse with
	// admission_state_uncertain until an operator recovers it.
	async function shutdownGracefully() {
		const c = child;
		child = null;
		alive = false;
		ready = false;
		if (!c) return;
		// A child that already exited never emits "exit" again.
		if (c.exitCode !== null || c.signalCode !== null) {
			if (c.signalCode === null && c.exitCode === 0) {
				await waitForAdmissionRelease();
			} else {
				log(
					"warn",
					`server had already exited (code ${String(c.exitCode)}, signal ${String(c.signalCode)}); its admission record, if any, remains`,
				);
			}
			return;
		}
		let exitCode = null;
		const exited = await new Promise((resolve) => {
			const timer = setTimeout(() => resolve(false), shutdownTimeoutMs);
			c.once("exit", (code) => {
				exitCode = code;
				clearTimeout(timer);
				resolve(true);
			});
			// EPIPE when the server died first: the exit event settles us.
			c.stdin.on("error", () => {});
			try {
				c.stdin.end();
			} catch { /* already closed */ }
		});
		if (!exited) {
			log(
				"warn",
				`server did not exit within ${shutdownTimeoutMs}ms of stdin EOF; terminating it; its admission record, if any, remains and may need the operator recovery flow`,
			);
			try { c.kill(); } catch { /* already gone */ }
			return;
		}
		if (exitCode !== 0) {
			log(
				"warn",
				`server exited with code ${exitCode} during shutdown; its admission record, if any, remains`,
			);
			return;
		}
		await waitForAdmissionRelease();
	}

	// Verified cleanup, best effort: the host's normal shutdown removes its
	// admission record. When the settings passed to the child name a runtime
	// directory, wait bounded for that record to disappear. The record is
	// never removed or rewritten here: one that remains must surface as
	// UNKNOWN to the next start, not vanish.
	async function waitForAdmissionRelease() {
		const settingsPath = env.COMSOL_MCP_SETTINGS_PATH;
		if (!settingsPath || typeof settingsPath !== "string") return;
		let lockPath;
		try {
			const parsed = JSON.parse(readFileSync(settingsPath, "utf-8"));
			const dir = parsed?.runtime?.directory;
			if (typeof dir !== "string" || dir.length === 0) return;
			lockPath = join(dir, "host_admission.lock");
		} catch {
			return; // Settings unreadable: the host's exit 0 is our verification.
		}
		const deadline = Date.now() + shutdownTimeoutMs;
		while (existsSync(lockPath)) {
			if (Date.now() >= deadline) {
				log("warn", `admission record still present after shutdown: ${lockPath}`);
				return;
			}
			await new Promise((resolve) => setTimeout(resolve, 50));
		}
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
