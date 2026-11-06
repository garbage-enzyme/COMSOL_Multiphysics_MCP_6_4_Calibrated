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
//! - on worker exit while the client is connected, answer the pending
//!   request with an unknown-result error and close the transport;
//! - refuse automatic respawn after a worker failure (reconnect instead).
//!
//! The shim never acquires solver leases and never fabricates live state.

#[cfg(windows)]
mod winjob;

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
const PROTOCOL_META_KEY: &str = "io.modelcontextprotocol/protocolVersion";
const ENV_MANIFEST: &str = "COMSOL_MCP_DISCOVERY_MANIFEST";
const ENV_CHILD: &str = "COMSOL_MCP_SHIM_CHILD";
const SHIM_HANDSHAKE_ID: &str = "shim-handshake";
const CRASH_EXIT_CODE: i32 = 3;

/// The protocol era the external client negotiated with the shim.
#[derive(Clone)]
enum ClientEra {
    /// The client completed a legacy initialize handshake at this version.
    Handshake(String),
    /// The client selected a modern revision through server/discover.
    Modern(String),
}

type StdoutLock = Arc<Mutex<std::io::Stdout>>;

fn write_line(out: &StdoutLock, body: &str, terminator: &str) -> std::io::Result<()> {
    let mut handle = out.lock().unwrap();
    handle.write_all(body.as_bytes())?;
    handle.write_all(terminator.as_bytes())?;
    handle.flush()
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
        Ok(Manifest {
            responses,
            latest_handshake,
            handshake_versions,
            terminator,
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

struct Worker {
    stdin: std::process::ChildStdin,
    child: std::process::Child,
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

fn trim_line(line: &str) -> &str {
    line.trim_end_matches(['\r', '\n'])
}

fn shim_client_info() -> Value {
    json!({"name": "comsol-mcp-shim", "version": env!("CARGO_PKG_VERSION")})
}

fn spawn_worker(out: &StdoutLock, terminator: &str, era: &ClientEra) -> Result<Worker, String> {
    let spec = std::env::var(ENV_CHILD).unwrap_or_else(|_| "[]".to_owned());
    let argv: Vec<String> = serde_json::from_str(&spec).map_err(|e| format!("{ENV_CHILD}: {e}"))?;
    let (program, args) = argv
        .split_first()
        .ok_or_else(|| format!("{ENV_CHILD} is empty"))?;
    let mut command = Command::new(program);
    command
        .args(args)
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
    let (handshake_tx, handshake_rx): (_, Receiver<String>) = channel();
    let writer = Arc::clone(out);
    let term = terminator.to_owned();
    let pending_id: Arc<Mutex<Option<Value>>> = Arc::new(Mutex::new(None));
    let client_gone = Arc::new(AtomicBool::new(false));
    let reader_pending = Arc::clone(&pending_id);
    let reader_gone = Arc::clone(&client_gone);
    let reader = std::thread::spawn(move || {
        let mut reader = std::io::BufReader::new(stdout);
        let mut first = true;
        while let LineRead::Line(line) = read_crlf_line(&mut reader) {
            if first {
                first = false;
                let _ = handshake_tx.send(trim_line(&line).to_owned());
                continue;
            }
            let _ = write_line(&writer, trim_line(&line), &term);
        }
        if reader_gone.load(Ordering::SeqCst) {
            return; // The client already left; the cleanup path owns the worker.
        }
        // Worker exit while the client is connected: the pending result is
        // unknown. Answer it, then close the transport. Never respawn.
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
            let _ = write_line(&writer, &body.to_string(), &term);
        }
        eprintln!("comsol-mcp-shim: worker exited while the client was connected");
        std::process::exit(CRASH_EXIT_CODE);
    });
    let mut stdin = child
        .stdin
        .take()
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
        child,
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

fn error_reply(id: Option<&Value>, message: &str, terminator: &str, out: &StdoutLock) {
    let id = id.cloned().unwrap_or(Value::Null);
    let body = json!({
        "jsonrpc": "2.0",
        "id": id,
        "error": {"code": -32603, "message": message},
    });
    let _ = write_line(out, &body.to_string(), terminator);
}

fn main() {
    let code = run();
    std::process::exit(code);
}

fn run() -> i32 {
    let out: StdoutLock = Arc::new(Mutex::new(std::io::stdout()));
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
    let mut worker: Option<Worker> = None;
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
                if let Err(problem) = write_line(&out, &body, &terminator) {
                    eprintln!("comsol-mcp-shim: stdout write failed: {problem}");
                    return 2;
                }
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
                    match spawn_worker(&out, &terminator, &bring_up_era) {
                        Ok(spawned) => worker = Some(spawned),
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
    if let Some(mut live) = worker.take() {
        live.client_gone.store(true, Ordering::SeqCst);
        let _ = live.stdin.flush();
        drop(live.stdin);
        let deadline = std::time::Instant::now() + WORKER_EXIT_GRACE;
        loop {
            match live.child.try_wait() {
                Ok(Some(_status)) => break,
                Ok(None) if std::time::Instant::now() < deadline => {
                    std::thread::sleep(Duration::from_millis(50))
                }
                _ => {
                    let _ = live.child.kill();
                    let _ = live.child.wait();
                    eprintln!("comsol-mcp-shim: worker did not exit within the grace period");
                    break;
                }
            }
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
}
