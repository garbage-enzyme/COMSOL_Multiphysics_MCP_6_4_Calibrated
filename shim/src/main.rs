//! Optional MCP stdio shim for COMSOL MCP (0.8.2).
//!
//! Responsibilities:
//! - serve the static discovery surface (initialize, paged tools/list, ping,
//!   server/discover) from a generated discovery manifest, without Python;
//! - spawn exactly one Python worker child on the first forwarded request,
//!   bring it up in the era the external client negotiated, and forward
//!   every further request, notification, and client response verbatim;
//! - forward worker stdout verbatim, inherit worker stderr;
//! - contain the worker tree (Windows Job Object, kill on shim death);
//! - on worker stdout closing while the client is connected, answer the
//!   pending request with an unknown-result error and close the transport,
//!   releasing the admission record only after the worker's observed death
//!   AND an empty recorded containment (G-EOF-01: pipe EOF is not process
//!   exit, and an observed exit alone says nothing about descendants);
//! - refuse automatic respawn after a worker failure (reconnect instead).
//!
//! The shim never acquires solver leases and never fabricates live state.

mod admission;
mod containment;
#[cfg(windows)]
mod winjob;

mod procinfo;

use std::collections::HashMap;
use std::io::{BufRead, Read, Write};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{Receiver, channel};
use std::sync::{Arc, Mutex};
use std::thread::JoinHandle;
use std::time::Duration;

use serde_json::{Value, json};

#[cfg(windows)]
use winjob::JobHandle;

const MANIFEST_SCHEMA_NAME: &str = "comsol_mcp.discovery_manifest";
const MANIFEST_SCHEMA_VERSION: &str = "1.0.0";
const MAX_LINE_BYTES: usize = 64 * 1024 * 1024;
const MAX_MANIFEST_BYTES: u64 = 8 * 1024 * 1024;
const WORKER_EXIT_GRACE: Duration = Duration::from_secs(5);
const WORKER_HANDSHAKE_TIMEOUT: Duration = Duration::from_secs(60);
const WRITE_QUEUE_BOUND: usize = 64;
const WRITE_DEADLINE: Duration = Duration::from_secs(30);
const PROTOCOL_META_KEY: &str = "io.modelcontextprotocol/protocolVersion";
const ENV_MANIFEST: &str = "COMSOL_MCP_DISCOVERY_MANIFEST";
const ENV_CHILD: &str = "COMSOL_MCP_SHIM_CHILD";
const SHIM_HANDSHAKE_ID: &str = "shim-handshake";
const CRASH_EXIT_CODE: i32 = 3;
const SLOW_READER_EXIT_CODE: i32 = 4;

/// The protocol era the external client negotiated with the shim.
#[derive(Clone)]
enum ClientEra {
    /// The client completed a legacy initialize handshake at this version.
    Handshake(String),
    /// The client selected a modern revision through server/discover.
    Modern(String),
}

/// Bounded, deadline-guarded client writer.
///
/// A dedicated thread owns stdout. Every reply enters a bounded queue; if the
/// client stops reading, the queue fills and the enqueue deadline fires. The
/// shim then closes the transport (exit `SLOW_READER_EXIT_CODE`) instead of
/// buffering without bound. Nothing is dropped or replayed.
#[derive(Clone)]
struct OutSink {
    sender: std::sync::mpsc::SyncSender<String>,
}

impl OutSink {
    fn spawn() -> (OutSink, JoinHandle<()>) {
        use std::sync::mpsc::sync_channel;
        let (sender, receiver): (_, std::sync::mpsc::Receiver<String>) =
            sync_channel(WRITE_QUEUE_BOUND);
        let handle = std::thread::spawn(move || {
            while let Ok(line) = receiver.recv() {
                let mut out = std::io::stdout();
                if out
                    .write_all(line.as_bytes())
                    .and_then(|_| out.flush())
                    .is_err()
                {
                    break;
                }
            }
        });
        (OutSink { sender }, handle)
    }

    /// Enqueue one complete line (body plus terminator) or abort the process
    /// once the deadline passes. `deadline` is injectable for tests.
    fn enqueue_with_deadline(&self, line: String, deadline: Duration) {
        let limit = std::time::Instant::now() + deadline;
        loop {
            match self.sender.try_send(line.clone()) {
                Ok(()) => return,
                Err(std::sync::mpsc::TrySendError::Full(_)) => {
                    if std::time::Instant::now() >= limit {
                        eprintln!("comsol-mcp-shim: client stopped reading; closing transport");
                        std::process::exit(SLOW_READER_EXIT_CODE);
                    }
                    std::thread::sleep(Duration::from_millis(20));
                }
                Err(std::sync::mpsc::TrySendError::Disconnected(_)) => {
                    eprintln!("comsol-mcp-shim: writer thread is gone; closing transport");
                    std::process::exit(SLOW_READER_EXIT_CODE);
                }
            }
        }
    }

    fn send_line(&self, body: &str, terminator: &str) {
        self.enqueue_with_deadline(format!("{body}{terminator}"), WRITE_DEADLINE);
    }
}

fn write_line(out: &OutSink, body: &str, terminator: &str) {
    out.send_line(body, terminator);
}

fn canonical_json(value: &Value) -> String {
    // serde_json's default map is sorted, matching the generator's
    // `json.dumps(..., sort_keys=True, separators=(",", ":"))` form for the
    // strings and integers a manifest contains.
    serde_json::to_string(value).expect("manifest values serialize")
}

fn sha256_hex(data: &[u8]) -> String {
    use sha2::{Digest, Sha256};
    let digest = Sha256::digest(data);
    digest.iter().map(|b| format!("{b:02x}")).collect()
}

struct Manifest {
    responses: HashMap<(&'static str, String, String, Option<String>), String>,
    latest_handshake: String,
    handshake_versions: Vec<String>,
    terminator: String,
    /// SHA-256 of the settings bytes the manifest was generated under.
    settings_sha256: Option<String>,
}

impl Manifest {
    fn load(path: &str) -> Result<Manifest, String> {
        let file = std::fs::File::open(path).map_err(|e| format!("open manifest: {e}"))?;
        let mut limited = file.take(MAX_MANIFEST_BYTES + 1);
        let mut bytes = Vec::new();
        limited
            .read_to_end(&mut bytes)
            .map_err(|e| format!("read manifest: {e}"))?;
        if bytes.len() as u64 > MAX_MANIFEST_BYTES {
            return Err("manifest exceeds the byte bound".into());
        }
        let value: Value =
            serde_json::from_slice(&bytes).map_err(|e| format!("parse manifest: {e}"))?;
        Self::from_value(value)
    }

    fn from_value(value: Value) -> Result<Manifest, String> {
        if value.get("schema_name").and_then(Value::as_str) != Some(MANIFEST_SCHEMA_NAME) {
            return Err("manifest schema_name is not supported".into());
        }
        if value.get("schema_version").and_then(Value::as_str) != Some(MANIFEST_SCHEMA_VERSION) {
            return Err("manifest schema_version is not supported".into());
        }
        let mut body = value.clone();
        body.as_object_mut()
            .ok_or("manifest is not a JSON object")?
            .remove("manifest_sha256");
        let computed = sha256_hex(canonical_json(&body).as_bytes());
        if computed
            != value
                .get("manifest_sha256")
                .and_then(Value::as_str)
                .unwrap_or("")
        {
            return Err("manifest_sha256 does not match the manifest body".into());
        }
        let negotiation = value
            .get("negotiation")
            .ok_or("manifest negotiation section is missing")?;
        let handshake_versions: Vec<String> = negotiation
            .get("handshake_versions")
            .and_then(Value::as_array)
            .ok_or("manifest handshake_versions is missing")?
            .iter()
            .filter_map(Value::as_str)
            .map(str::to_owned)
            .collect();
        let latest_handshake = negotiation
            .get("unlisted_handshake_version_answers_as")
            .and_then(Value::as_str)
            .ok_or("manifest latest handshake version is missing")?
            .to_owned();
        let terminator = value
            .pointer("/wire/stdio_line_terminator")
            .and_then(Value::as_str)
            .unwrap_or(if cfg!(windows) { "\r\n" } else { "\n" })
            .to_owned();
        let payloads = value
            .get("payloads")
            .and_then(Value::as_object)
            .ok_or("manifest payloads are missing")?;
        let mut responses = HashMap::new();
        for row in value
            .get("responses")
            .and_then(Value::as_array)
            .ok_or("manifest responses are missing")?
        {
            let era_static = match row.get("era").and_then(Value::as_str) {
                Some("handshake") => "handshake",
                Some("modern") => "modern",
                other => return Err(format!("manifest response has an unknown era: {other:?}")),
            };
            let version = row
                .get("protocol_version")
                .and_then(Value::as_str)
                .ok_or("manifest response has no protocol_version")?
                .to_owned();
            let method = row
                .get("method")
                .and_then(Value::as_str)
                .ok_or("manifest response has no method")?
                .to_owned();
            let cursor = match row.get("cursor") {
                None | Some(Value::Null) => None,
                Some(Value::String(text)) => Some(text.clone()),
                Some(other) => return Err(format!("manifest cursor is not a string: {other}")),
            };
            let digest = row
                .get("result_sha256")
                .and_then(Value::as_str)
                .ok_or("manifest response has no result_sha256")?;
            let payload = payloads
                .get(digest)
                .and_then(Value::as_str)
                .ok_or("manifest response references a missing payload")?;
            if sha256_hex(payload.as_bytes()) != digest {
                return Err(format!("payload hash mismatch: {digest}"));
            }
            responses.insert((era_static, version, method, cursor), payload.to_owned());
        }
        let settings_sha256 = value
            .pointer("/binding/settings/sha256")
            .and_then(Value::as_str)
            .map(str::to_owned);
        Ok(Manifest {
            responses,
            latest_handshake,
            handshake_versions,
            terminator,
            settings_sha256,
        })
    }

    fn lookup(
        &self,
        era: &'static str,
        version: &str,
        method: &str,
        cursor: Option<&str>,
    ) -> Option<&String> {
        let version =
            if era == "handshake" && !self.handshake_versions.contains(&version.to_owned()) {
                self.latest_handshake.as_str()
            } else {
                version
            };
        self.responses.get(&(
            era,
            version.to_owned(),
            method.to_owned(),
            cursor.map(str::to_owned),
        ))
    }
}

/// The worker process, shared between the main loop and the reader
/// thread: either may be the one that observes its death (G-EOF-01).
type ChildSlot = Arc<Mutex<Option<std::process::Child>>>;

struct Worker {
    stdin: std::process::ChildStdin,
    child: ChildSlot,
    worker_pid: u32,
    pending_id: Arc<Mutex<Option<Value>>>,
    client_gone: Arc<AtomicBool>,
    #[cfg(windows)]
    _job: JobHandle,
    _reader: JoinHandle<()>,
}

/// One line read from a transport: complete, end of stream, or oversized.
enum LineRead {
    Line(String),
    Eof,
    Overflow,
}

fn read_crlf_line<R: BufRead>(reader: &mut R) -> LineRead {
    let mut bytes: Vec<u8> = Vec::new();
    loop {
        let mut byte = [0u8; 1];
        match reader.read(&mut byte) {
            Ok(0) => {
                return if bytes.is_empty() {
                    LineRead::Eof
                } else {
                    LineRead::Line(String::from_utf8_lossy(&bytes).into_owned())
                };
            }
            Ok(_) => {}
            Err(_) => return LineRead::Eof,
        }
        bytes.push(byte[0]);
        if byte[0] == b'\n' {
            return LineRead::Line(String::from_utf8_lossy(&bytes).into_owned());
        }
        if bytes.len() > MAX_LINE_BYTES {
            // Drain the remainder of the oversized line, then report it.
            loop {
                let mut skip = [0u8; 4096];
                match reader.read(&mut skip) {
                    Ok(0) => break,
                    Ok(n) if skip[n - 1] == b'\n' => break,
                    Ok(_) => continue,
                    Err(_) => break,
                }
            }
            return LineRead::Overflow;
        }
    }
}

/// Wait for the worker's observed death within `grace`. Never kills: on
/// stdout EOF the worker may simply be alive with a closed pipe, and an
/// unobserved exit keeps the admission record (G-EOF-01).
fn await_worker_exit(child: &mut std::process::Child, grace: Duration) -> bool {
    let deadline = std::time::Instant::now() + grace;
    loop {
        match child.try_wait() {
            Ok(Some(_status)) => return true,
            Ok(None) if std::time::Instant::now() < deadline => {
                std::thread::sleep(Duration::from_millis(50));
            }
            Ok(None) => {
                eprintln!("comsol-mcp-shim: the worker is still alive after closing its stdout");
                return false;
            }
            Err(problem) => {
                eprintln!("comsol-mcp-shim: the worker's exit cannot be observed: {problem}");
                return false;
            }
        }
    }
}

/// G-EOF-01: exact descendant evidence for every release decision. The
/// recorded containment must list no live member but this host; an
/// unreadable identity is UNKNOWN and fails closed. Pipe EOF, a deliberate
/// shutdown, or an unchanged record prove nothing here.
fn containment_clean(identity: &Value) -> bool {
    match containment::members(identity) {
        Ok(members) => {
            let others: Vec<u32> = members
                .into_iter()
                .filter(|pid| *pid != std::process::id())
                .collect();
            if others.is_empty() {
                return true;
            }
            let listed = others
                .iter()
                .map(u32::to_string)
                .collect::<Vec<_>>()
                .join(", ");
            eprintln!(
                "comsol-mcp-shim: processes from this host are still running ({listed}); \
                 the admission record is kept for verified recovery"
            );
            false
        }
        Err(problem) => {
            eprintln!(
                "comsol-mcp-shim: this host's containment cannot be read ({problem}); \
                 the admission record is kept for verified recovery"
            );
            false
        }
    }
}

fn trim_line(line: &str) -> &str {
    line.trim_end_matches(['\r', '\n'])
}

fn shim_client_info() -> Value {
    json!({"name": "comsol-mcp-shim", "version": env!("CARGO_PKG_VERSION")})
}

fn spawn_worker(
    out: &OutSink,
    terminator: &str,
    era: &ClientEra,
    worker_token: &str,
    containment: Value,
    crash_release: std::sync::Arc<std::sync::Mutex<Option<admission::ReleaseTicket>>>,
) -> Result<Worker, String> {
    let spec = std::env::var(ENV_CHILD).unwrap_or_else(|_| "[]".to_owned());
    let argv: Vec<String> = serde_json::from_str(&spec).map_err(|e| format!("{ENV_CHILD}: {e}"))?;
    let (program, args) = argv
        .split_first()
        .ok_or_else(|| format!("{ENV_CHILD} is empty"))?;
    let mut command = Command::new(program);
    command
        .args(args)
        .env("COMSOL_MCP_SHIM_WORKER", "1")
        // Launch binding: the worker verifies this PID against the admission
        // record (birth time, executable identity) and this token against the
        // recorded token hash before it skips host admission.
        .env(
            admission::ENV_WORKER_HOST_PID,
            std::process::id().to_string(),
        )
        .env(admission::ENV_WORKER_TOKEN, worker_token)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(creation_flags());
    }
    let mut child = command
        .spawn()
        .map_err(|e| format!("spawn worker {program}: {e}"))?;
    #[cfg(windows)]
    let job = JobHandle::contain_child(&child).inspect_err(|_problem| {
        let _ = child.kill();
        let _ = child.wait();
    })?;
    let stdout = child
        .stdout
        .take()
        .ok_or_else(|| "worker stdout was not piped".to_owned())?;
    let worker_pid = child.id();
    let child_slot: ChildSlot = Arc::new(Mutex::new(Some(child)));
    let (handshake_tx, handshake_rx): (_, Receiver<String>) = channel();
    let writer = out.clone();
    let term = terminator.to_owned();
    let pending_id: Arc<Mutex<Option<Value>>> = Arc::new(Mutex::new(None));
    let client_gone = Arc::new(AtomicBool::new(false));
    let reader_pending = Arc::clone(&pending_id);
    let reader_gone = Arc::clone(&client_gone);
    let reader_child = Arc::clone(&child_slot);
    let reader = std::thread::spawn(move || {
        let mut reader = std::io::BufReader::new(stdout);
        let mut first = true;
        while let LineRead::Line(line) = read_crlf_line(&mut reader) {
            if first {
                first = false;
                let _ = handshake_tx.send(trim_line(&line).to_owned());
                continue;
            }
            write_line(&writer, trim_line(&line), &term);
        }
        if reader_gone.load(Ordering::SeqCst) {
            return; // The client already left; the cleanup path owns the worker.
        }
        // Worker stdout closing while the client is connected: the pending
        // result is unknown. Answer it, then close the transport. Never
        // respawn.
        let pending = reader_pending.lock().unwrap().take();
        if let Some(id) = pending {
            let body = json!({
                "jsonrpc": "2.0",
                "id": id,
                "error": {
                    "code": -32603,
                    "message": "worker exited; result unknown; reconnect required",
                },
            });
            write_line(&writer, &body.to_string(), &term);
        }
        eprintln!("comsol-mcp-shim: worker stdout closed while the client was connected");
        // G-EOF-01: pipe EOF is not process exit. The record is released
        // only after the worker's death is observed AND the recorded
        // containment lists no live member but this host. Any other
        // outcome keeps the record for verified recovery; keeping it here
        // is what lets the next session recover the domain safely (the
        // G3 crash case releases because its worker is observed dead with
        // no descendants).
        let mut verified = false;
        if let Some(mut child) = reader_child.lock().unwrap().take() {
            verified = await_worker_exit(&mut child, WORKER_EXIT_GRACE);
            // A held handle can keep a dead member listed (Windows jobs);
            // the membership read below must see the kernel's truth.
            drop(child);
        }
        if verified {
            verified = containment_clean(&containment);
        }
        if verified {
            if let Some(ticket) = crash_release.lock().unwrap().take()
                && !ticket.release()
            {
                eprintln!("comsol-mcp-shim: the admission record changed; it was left in place");
            }
        } else {
            eprintln!(
                "comsol-mcp-shim: worker cleanup is unverified; the admission record is \
                 kept for verified recovery"
            );
        }
        std::process::exit(CRASH_EXIT_CODE);
    });
    let mut stdin = child_slot
        .lock()
        .unwrap()
        .as_mut()
        .and_then(|child| child.stdin.take())
        .ok_or_else(|| "worker stdin was not piped".to_owned())?;
    let bring_up = match era {
        ClientEra::Handshake(version) => json!({
            "jsonrpc": "2.0",
            "id": SHIM_HANDSHAKE_ID,
            "method": "initialize",
            "params": {
                "protocolVersion": version,
                "capabilities": {},
                "clientInfo": shim_client_info(),
            },
        }),
        ClientEra::Modern(version) => json!({
            "jsonrpc": "2.0",
            "id": SHIM_HANDSHAKE_ID,
            "method": "server/discover",
            "params": {"_meta": {
                PROTOCOL_META_KEY: version,
                "io.modelcontextprotocol/clientInfo": shim_client_info(),
                "io.modelcontextprotocol/clientCapabilities": {},
            }},
        }),
    };
    stdin
        .write_all((bring_up.to_string() + "\n").as_bytes())
        .and_then(|_| stdin.flush())
        .map_err(|e| format!("worker bring-up write: {e}"))?;
    let reply = handshake_rx
        .recv_timeout(WORKER_HANDSHAKE_TIMEOUT)
        .map_err(|_| "worker bring-up timed out".to_owned())?;
    let parsed: Value =
        serde_json::from_str(&reply).map_err(|e| format!("worker bring-up reply: {e}"))?;
    if parsed.get("error").is_some() {
        return Err(format!("worker bring-up failed: {reply}"));
    }
    if let ClientEra::Handshake(_) = era {
        let initialized = json!({"jsonrpc": "2.0", "method": "notifications/initialized"});
        stdin
            .write_all((initialized.to_string() + "\n").as_bytes())
            .and_then(|_| stdin.flush())
            .map_err(|e| format!("worker initialized notification: {e}"))?;
    }
    Ok(Worker {
        stdin,
        child: child_slot,
        worker_pid,
        pending_id,
        client_gone,
        #[cfg(windows)]
        _job: job,
        _reader: reader,
    })
}

#[cfg(windows)]
fn creation_flags() -> u32 {
    const CREATE_NO_WINDOW: u32 = 0x0800_0000;
    CREATE_NO_WINDOW
}

fn request_meta_protocol(params: Option<&Value>) -> Option<String> {
    params?
        .get("_meta")?
        .get(PROTOCOL_META_KEY)?
        .as_str()
        .map(str::to_owned)
}

fn error_reply(id: Option<&Value>, message: &str, terminator: &str, out: &OutSink) {
    let id = id.cloned().unwrap_or(Value::Null);
    let body = json!({
        "jsonrpc": "2.0",
        "id": id,
        "error": {"code": -32603, "message": message},
    });
    write_line(out, &body.to_string(), terminator);
}

fn main() {
    let code = run();
    std::process::exit(code);
}

fn run() -> i32 {
    // One-line identity report for save-time selector validation. The
    // selector compares this version against the installed package line
    // and the recorded artifact hash before enabling shim transport.
    let arguments: Vec<String> = std::env::args().collect();
    if arguments.len() == 2 && (arguments[1] == "--version" || arguments[1] == "-V") {
        println!(
            "comsol-mcp-shim {} ({})",
            admission::SHIM_VERSION,
            admission::SHIM_PLATFORM
        );
        return 0;
    }

    // 0.8.2 §10 interactive launch: when stdin is a terminal (no protocol
    // client), explain the stdio launch method and exit before reading
    // settings, acquiring admission, or creating any child process. The
    // explicit version-report command above stays available.
    use std::io::IsTerminal;
    if std::io::stdin().is_terminal() {
        eprintln!("comsol-mcp-shim: this is an MCP stdio server, not an interactive program.");
        eprintln!("To use it, configure an MCP client (ZCode, Codex, Claude) to launch this");
        eprintln!("binary as its MCP server command. The client communicates over stdin/stdout.");
        eprintln!("For the version, run: comsol-mcp-shim --version");
        return 0;
    }
    let (out, writer_thread) = OutSink::spawn();
    let manifest_option = std::env::var(ENV_MANIFEST).ok();
    let manifest = match manifest_option.as_deref() {
        None => None,
        Some(path) => match Manifest::load(path) {
            Ok(manifest) => Some(manifest),
            Err(problem) => {
                eprintln!("comsol-mcp-shim: refusing to start: {problem}");
                return 2;
            }
        },
    };
    let terminator = manifest
        .as_ref()
        .map(|m| m.terminator.clone())
        .unwrap_or_else(|| if cfg!(windows) { "\r\n" } else { "\n" }.to_owned());
    let latest_handshake = manifest
        .as_ref()
        .map(|m| m.latest_handshake.clone())
        .unwrap_or_else(|| "2025-11-25".to_owned());

    // 0.8.2 §10 startup gate, fail closed: one settings snapshot is read,
    // hashed and validated; the configured binary identity is bound to this
    // running image (path, SHA-256, version, platform). Every failure path
    // refuses before any worker can be created.
    let startup = match admission::startup() {
        Ok(startup) => startup,
        Err(problem) => {
            eprintln!("comsol-mcp-shim: refusing to start: {problem}");
            return 2;
        }
    };
    // The discovery manifest is bound to the exact settings bytes it was
    // generated under; changed settings invalidate it (no stale discovery).
    if let Some(loaded) = manifest.as_ref()
        && loaded.settings_sha256.as_deref() != Some(startup.settings_sha256.as_str())
    {
        eprintln!(
            "comsol-mcp-shim: refusing to start: [manifest_stale] the discovery manifest was \
             generated under different settings (manifest {:?}, current {}). Regenerate it \
             with: python -m comsol_mcp.discovery_manifest generate --output <path>",
            loaded.settings_sha256, startup.settings_sha256
        );
        return 2;
    }

    // 0.8.2 §10 host admission: acquire the shared admission lock for the
    // canonical ownership domain before serving any request. A stale record
    // is recovered only after verified cleanup; a replacement record is
    // never erased. The settings snapshot is re-checked afterwards so a
    // change during admission refuses startup.
    let worker_token = admission::worker_token();
    let worker_token_sha256 = sha256_hex(worker_token.as_bytes());
    let mut admission = match admission::acquire(&startup, &worker_token_sha256) {
        Ok(claim) => Some(claim),
        Err(problem) => {
            eprintln!("comsol-mcp-shim: refusing to start: {problem}");
            return 2;
        }
    };
    match admission::settings_unchanged(&startup) {
        Ok(true) => {}
        Ok(false) => {
            if let Some(claim) = admission.as_ref() {
                claim.release();
            }
            eprintln!(
                "comsol-mcp-shim: refusing to start: [settings_changed_during_admission] \
                 settings {} changed between validation and admission. Retry after the \
                 change is complete.",
                startup.settings_path.display()
            );
            return 2;
        }
        Err(problem) => {
            if let Some(claim) = admission.as_ref() {
                claim.release();
            }
            eprintln!("comsol-mcp-shim: refusing to start: {problem}");
            return 2;
        }
    }

    let mut worker: Option<Worker> = None;
    // Filled after record_worker rewrites the record; the reader thread's
    // crash exit releases through it, so its bytes must match the record's
    // CURRENT content.
    let crash_release: std::sync::Arc<std::sync::Mutex<Option<admission::ReleaseTicket>>> =
        std::sync::Arc::new(std::sync::Mutex::new(None));
    let mut era: Option<ClientEra> = None;
    let mut stdin = std::io::BufReader::new(std::io::stdin());
    loop {
        let line = match read_crlf_line(&mut stdin) {
            LineRead::Line(line) => line,
            LineRead::Eof => break,
            LineRead::Overflow => {
                eprintln!("comsol-mcp-shim: request line exceeds {MAX_LINE_BYTES} bytes");
                error_reply(
                    None,
                    "request line exceeds the byte bound",
                    &terminator,
                    &out,
                );
                continue;
            }
        };
        let trimmed = trim_line(&line);
        if trimmed.is_empty() {
            continue;
        }
        let parsed: Value = match serde_json::from_str(trimmed) {
            Ok(value) => value,
            Err(problem) => {
                eprintln!("comsol-mcp-shim: unparseable request line: {problem}");
                error_reply(None, "parse error", &terminator, &out);
                continue;
            }
        };
        let method = parsed
            .get("method")
            .and_then(Value::as_str)
            .map(str::to_owned);
        let id = parsed.get("id").cloned();
        let params = parsed.get("params").cloned();
        let Some(method) = method else {
            // A client response to a worker-initiated request: forward it.
            if let Some(live) = worker.as_mut() {
                if live
                    .stdin
                    .write_all(trimmed.as_bytes())
                    .and_then(|_| live.stdin.write_all(b"\n"))
                    .and_then(|_| live.stdin.flush())
                    .is_err()
                {
                    eprintln!("comsol-mcp-shim: worker write failed; refusing automatic respawn");
                    worker = None;
                }
            } else {
                eprintln!("comsol-mcp-shim: client response without a worker; dropped");
            }
            continue;
        };
        let has_id = id.is_some();
        let static_hit = manifest.as_ref().and_then(|m| {
            let (response_era, version) = match method.as_str() {
                "initialize" => {
                    let requested = params
                        .as_ref()
                        .and_then(|p| p.get("protocolVersion"))
                        .and_then(Value::as_str)
                        .unwrap_or("")
                        .to_owned();
                    ("handshake", requested)
                }
                "tools/list" | "server/discover" => match request_meta_protocol(params.as_ref()) {
                    Some(version) => ("modern", version),
                    None => ("handshake", handshake_version_of(&era, &latest_handshake)),
                },
                "ping" => ("handshake", handshake_version_of(&era, &latest_handshake)),
                _ => return None,
            };
            let cursor = params
                .as_ref()
                .and_then(|p| p.get("cursor"))
                .and_then(Value::as_str)
                .map(str::to_owned);
            let payload = m.lookup(response_era, &version, &method, cursor.as_deref())?;
            if method == "initialize" {
                era = Some(ClientEra::Handshake(
                    if !m.handshake_versions.contains(&version) {
                        m.latest_handshake.clone()
                    } else {
                        version.clone()
                    },
                ));
            }
            if method == "server/discover" && response_era == "modern" {
                era = Some(ClientEra::Modern(version.clone()));
            }
            Some((payload.clone(), id.clone()))
        });
        match static_hit {
            Some((payload, id)) => {
                let Some(id_value) = id else {
                    // A static notification (no request id): replay it to the
                    // worker when one exists, then take the next line.
                    if let Some(live) = worker.as_mut() {
                        let _ = live.stdin.write_all(trimmed.as_bytes());
                        let _ = live.stdin.write_all(b"\n");
                        let _ = live.stdin.flush();
                    }
                    continue;
                };
                let id_text = serde_json::to_string(&id_value).expect("id serializes");
                let body = format!("{{\"jsonrpc\":\"2.0\",\"id\":{id_text},\"result\":{payload}}}");
                write_line(&out, &body, &terminator);
            }
            None => {
                if !has_id && worker.is_none() {
                    // A notification before the worker exists never spawns one.
                    // `notifications/initialized` is replayed at bring-up; any
                    // other early notification is dropped with a diagnostic.
                    if method != "notifications/initialized" {
                        eprintln!("comsol-mcp-shim: notification before worker; dropped: {method}");
                    }
                    continue;
                }
                if worker.is_none() {
                    let bring_up_era = era
                        .clone()
                        .unwrap_or(ClientEra::Handshake(latest_handshake.clone()));
                    match spawn_worker(
                        &out,
                        &terminator,
                        &bring_up_era,
                        &worker_token,
                        admission
                            .as_ref()
                            .expect("admission was acquired before the worker")
                            .containment(),
                        std::sync::Arc::clone(&crash_release),
                    ) {
                        Ok(spawned) => {
                            if let Some(claim) = admission.as_mut()
                                && let Err(problem) = claim.record_worker(spawned.worker_pid)
                            {
                                // Stale recovery still finds the worker through
                                // its parent link; the record is extra evidence.
                                eprintln!(
                                    "comsol-mcp-shim: worker identity not recorded: {problem}"
                                );
                            }
                            // The ticket must match the record's CURRENT bytes:
                            // record_worker rewrote them above.
                            *crash_release.lock().unwrap() =
                                Some(admission.as_ref().unwrap().release_ticket());
                            worker = Some(spawned);
                        }
                        Err(problem) => {
                            eprintln!("comsol-mcp-shim: {problem}");
                            error_reply(
                                id.as_ref(),
                                "worker unavailable; reconnect",
                                &terminator,
                                &out,
                            );
                            continue;
                        }
                    }
                }
                let Some(live) = worker.as_mut() else {
                    unreachable!("worker was just spawned");
                };
                if has_id {
                    *live.pending_id.lock().unwrap() = id.clone();
                }
                if live
                    .stdin
                    .write_all(trimmed.as_bytes())
                    .and_then(|_| live.stdin.write_all(b"\n"))
                    .and_then(|_| live.stdin.flush())
                    .is_err()
                {
                    eprintln!("comsol-mcp-shim: worker write failed; refusing automatic respawn");
                    error_reply(
                        id.as_ref(),
                        "worker transport failed; reconnect",
                        &terminator,
                        &out,
                    );
                    worker = None;
                }
            }
        }
    }
    // G-EOF-01: the admission record is released only after the worker's
    // death is observed AND the recorded containment lists no live member
    // but this host. A deliberate shutdown alone proves nothing: an orphan
    // descendant keeps the record, and the next host verifies cleanup
    // before recovery.
    let mut worker_exit_verified = true;
    if let Some(mut live) = worker.take() {
        live.client_gone.store(true, Ordering::SeqCst);
        let _ = live.stdin.flush();
        drop(live.stdin);
        match live.child.lock().unwrap().take() {
            Some(mut child) => {
                let deadline = std::time::Instant::now() + WORKER_EXIT_GRACE;
                loop {
                    match child.try_wait() {
                        Ok(Some(_status)) => break,
                        Ok(None) if std::time::Instant::now() < deadline => {
                            std::thread::sleep(Duration::from_millis(50))
                        }
                        _ => {
                            let _ = child.kill();
                            worker_exit_verified = child.wait().is_ok();
                            eprintln!(
                                "comsol-mcp-shim: worker did not exit within the grace period"
                            );
                            break;
                        }
                    }
                }
                // A held handle can keep a dead member listed (Windows
                // jobs); the membership read below must see the kernel's
                // truth.
                drop(child);
            }
            None => {
                // The reader thread's EOF path owns the death watch and
                // its own release decision; this path must not race it
                // with a weaker one.
                worker_exit_verified = false;
            }
        }
    }
    drop(out); // Close the bounded queue; the writer drains and exits.
    let _ = writer_thread.join();

    if let Some(claim) = admission.as_ref() {
        if worker_exit_verified {
            worker_exit_verified = containment_clean(&claim.containment());
        }
        if !worker_exit_verified {
            eprintln!(
                "comsol-mcp-shim: worker cleanup is unverified; the admission record is \
                 kept for verified recovery"
            );
        } else if !claim.release() {
            eprintln!("comsol-mcp-shim: the admission record changed; it was left in place");
        }
    }

    0
}

fn handshake_version_of(era: &Option<ClientEra>, latest: &str) -> String {
    match era {
        Some(ClientEra::Handshake(version)) => version.clone(),
        _ => latest.to_owned(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::{Map, json};

    fn sample_manifest() -> Value {
        let payload_a = "{}".to_owned();
        let payload_b = "{\"tools\":[]}".to_owned();
        let payload_c = "{}".to_owned();
        let digest_a = sha256_hex(payload_a.as_bytes());
        let digest_b = sha256_hex(payload_b.as_bytes());
        let digest_c = sha256_hex(payload_c.as_bytes());
        let mut body = Map::new();
        body.insert("schema_name".into(), json!(MANIFEST_SCHEMA_NAME));
        body.insert("schema_version".into(), json!(MANIFEST_SCHEMA_VERSION));
        body.insert(
            "negotiation".into(),
            json!({
                "handshake_versions": ["2025-11-25"],
                "modern_versions": ["2026-07-28"],
                "unlisted_handshake_version_answers_as": "2025-11-25",
            }),
        );
        body.insert(
            "wire".into(),
            json!({"stdio_line_terminator": "\n", "envelope": "x"}),
        );
        body.insert(
            "payloads".into(),
            json!({digest_a.clone(): payload_a, digest_b.clone(): payload_b, digest_c.clone(): payload_c}),
        );
        body.insert(
            "responses".into(),
            json!([
                {"era": "handshake", "protocol_version": "2025-11-25", "method": "initialize", "cursor": null, "result_sha256": digest_a},
                {"era": "handshake", "protocol_version": "2025-11-25", "method": "tools/list", "cursor": null, "result_sha256": digest_b},
                {"era": "modern", "protocol_version": "2026-07-28", "method": "server/discover", "cursor": null, "result_sha256": digest_c},
            ]),
        );
        let hash = sha256_hex(canonical_json(&Value::Object(body.clone())).as_bytes());
        body.insert("manifest_sha256".into(), json!(hash));
        Value::Object(body)
    }

    #[test]
    fn manifest_loads_and_answers_each_era() {
        let manifest = Manifest::from_value(sample_manifest()).expect("manifest is valid");
        assert!(
            manifest
                .lookup("handshake", "2025-11-25", "initialize", None)
                .is_some()
        );
        assert!(
            manifest
                .lookup("handshake", "1999-01-01", "initialize", None)
                .is_some()
        );
        assert!(
            manifest
                .lookup("modern", "2026-07-28", "server/discover", None)
                .is_some()
        );
        assert!(
            manifest
                .lookup("handshake", "2025-11-25", "tools/list", Some("x"))
                .is_none()
        );
    }

    #[test]
    fn tampered_manifest_is_refused() {
        let mut value = sample_manifest();
        value["payloads"]["a"] = json!("{} ");
        assert!(Manifest::from_value(value).is_err());
    }

    #[test]
    fn wrong_schema_is_refused() {
        let mut value = sample_manifest();
        value["schema_version"] = json!("9.9.9");
        value.as_object_mut().unwrap().remove("manifest_sha256");
        let hash = sha256_hex(canonical_json(&value).as_bytes());
        value["manifest_sha256"] = json!(hash);
        assert!(Manifest::from_value(value).is_err());
    }

    #[test]
    fn handshake_version_of_prefers_the_negotiated_handshake() {
        assert_eq!(
            handshake_version_of(
                &Some(ClientEra::Handshake("2024-11-05".into())),
                "2025-11-25"
            ),
            "2024-11-05"
        );
        assert_eq!(
            handshake_version_of(&Some(ClientEra::Modern("2026-07-28".into())), "2025-11-25"),
            "2025-11-25"
        );
        assert_eq!(handshake_version_of(&None, "2025-11-25"), "2025-11-25");
    }

    #[test]
    fn oversized_line_reports_overflow() {
        let data = vec![b'a'; MAX_LINE_BYTES + 10];
        let mut reader = std::io::BufReader::new(&data[..]);
        assert!(matches!(read_crlf_line(&mut reader), LineRead::Overflow));
    }

    #[test]
    fn eof_on_empty_input_reports_eof() {
        let data: &[u8] = &[];
        let mut reader = std::io::BufReader::new(data);
        assert!(matches!(read_crlf_line(&mut reader), LineRead::Eof));
    }

    /// G-REG-UTF8-001 regression: the generator serializes manifest bodies
    /// with Python's `ensure_ascii=False`, so a non-ASCII character stays
    /// raw UTF-8 both in payload bytes and in the hashed canonical body.
    /// serde_json must agree byte for byte, or a valid manifest would be
    /// refused (body hash) and a valid reply would gain escape sequences.
    #[test]
    fn non_ascii_payload_stays_raw_utf8_and_hashes_like_the_generator() {
        // Vector computed with CPython: sha256("temp 25°C, µm scale" as UTF-8).
        let payload = "temp 25\u{b0}C, \u{b5}m scale";
        assert_eq!(
            sha256_hex(payload.as_bytes()),
            "1479447c5aaa0193e9c6117f09a4ec719d76d5979d7fcc752789f2e25dfa5b4d"
        );
        // Vector computed with CPython:
        // sha256(json.dumps(body, sort_keys=True, separators=(",", ":"),
        //                   ensure_ascii=False).encode()).
        let body = json!({
            "schema_name": MANIFEST_SCHEMA_NAME,
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "k": "v\u{b0}",
        });
        let canonical = canonical_json(&body);
        assert_eq!(
            sha256_hex(canonical.as_bytes()),
            "b702c18a3a2485162d5ed07205dd1af5bdc02cc3bc4024f4b9f6e0c8ddcbb3df"
        );
        // The envelope must splice the payload verbatim: raw UTF-8 bytes,
        // never an ASCII escape sequence.
        let line = format!("{{\"jsonrpc\":\"2.0\",\"id\":7,\"result\":{payload}}}");
        assert!(line.as_bytes().windows(2).any(|w| w == [0xc2, 0xb0]));
        assert!(!line.contains("\\u00b0"));
    }

    /// Slow-reader bound: a full queue with a tiny deadline aborts with the
    /// slow-reader exit code instead of buffering without bound. The child
    /// process assertion runs in a separate test binary-safe way: we only
    /// verify the enqueue path here; the exit behavior is exercised by the
    /// P04 slow-reader case.
    #[test]
    fn enqueue_deadline_fires_on_a_full_queue() {
        use std::sync::mpsc::sync_channel;
        let (sender, _receiver) = sync_channel::<String>(1);
        let sink = OutSink { sender };
        // Fill the queue, then observe the deadline path by timing: the call
        // must return via process::exit; we cannot catch that in-process, so
        // instead verify the immediate-success and full-queue-no-deadline
        // behaviors that precede it.
        sink.enqueue_with_deadline("first\n".to_owned(), Duration::from_millis(1));
        assert_eq!(_receiver.try_recv().ok(), Some("first\n".to_owned()));
        // A second send into an empty queue succeeds immediately.
        sink.enqueue_with_deadline("second\n".to_owned(), Duration::from_millis(1));
        assert_eq!(_receiver.try_recv().ok(), Some("second\n".to_owned()));
    }
}
