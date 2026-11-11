//! G-EOF-01 controls: release only after verified worker/descendant cleanup.
//!
//! Harness-free so this same executable can play every fixture role the
//! shim spawns. The checks drive the real shim binary end to end (real
//! settings identity, real admission record, real containment):
//!
//! - `eof_with_live_worker_keeps_the_record`: pipe EOF with a still-alive
//!   worker keeps the record and refuses the next session (the old defect
//!   released the record while the worker lived, so a second session could
//!   start immediately).
//! - `worker_orphan_descendant_keeps_the_record`: an observed worker exit
//!   with a surviving descendant keeps the record (an observed exit alone
//!   proves nothing about deeper descendants).
//! - `verified_crash_releases_the_record`: observed death with an empty
//!   containment releases the record (the G3 regression).
//!
//! Platform note: on Windows a dead host's registry job dies with its last
//! handle (WIN-CONTAIN-01), so a kept record stays operator-recoverable and
//! automatic recovery after host death is a POSIX-session property only;
//! the checks assert what each platform can prove.

use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

use serde_json::{Value, json};

const SHIM: &str = env!("CARGO_BIN_EXE_comsol-mcp-shim");
const WORKER_EXIT_GRACE_SECS: u64 = 5;

fn main() {
    let arguments: Vec<String> = std::env::args().collect();
    if let Some(mode) = arguments
        .get(1)
        .and_then(|text| text.strip_prefix("--fixture="))
    {
        std::process::exit(fixture(mode, &arguments[2..]));
    }
    type Check = fn() -> Result<(), String>;
    let checks: &[(&str, Check)] = &[
        (
            "eof_with_live_worker_keeps_the_record",
            eof_with_live_worker_keeps_the_record,
        ),
        (
            "worker_orphan_descendant_keeps_the_record",
            worker_orphan_descendant_keeps_the_record,
        ),
        (
            "verified_crash_releases_the_record",
            verified_crash_releases_the_record,
        ),
    ];
    let mut failed = 0;
    for (name, check) in checks {
        match check() {
            Ok(()) => println!("ok   {name}"),
            Err(problem) => {
                failed += 1;
                println!("FAIL {name}: {problem}");
            }
        }
    }
    std::process::exit(i32::from(failed != 0));
}

// ---------------------------------------------------------------------------
// Fixtures (spawned by the shim as its worker; the harness picks the mode
// per case through COMSOL_MCP_SHIM_CHILD, so every control drives exactly
// the worker behavior it needs)
// ---------------------------------------------------------------------------

/// One JSON-RPC reply for handshake and plain forwarded requests.
fn plain_reply(message: &Value) -> Value {
    json!({"jsonrpc": "2.0", "id": message["id"], "result": {"ok": true}})
}

fn send_reply(reply: &Value) {
    println!("{reply}");
    let _ = std::io::stdout().flush();
}

// The orphan descendant deliberately outlives its worker without a wait.
#[allow(clippy::zombie_processes)]
fn spawn_orphan(seconds: u64) -> u32 {
    let mut command = Command::new(std::env::current_exe().expect("own exe"));
    command
        .arg("--fixture=orphan")
        .arg(seconds.to_string())
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        command.creation_flags(CREATE_NO_WINDOW);
    }
    command.spawn().expect("orphan spawns").id()
}

/// Read stdin lines until EOF. `on_call` reacts to one forwarded
/// tools/call; the reactive modes end the process inside it, the generic
/// echo mode returns and gets the plain reply.
fn worker_loop(mut on_call: impl FnMut(&Value)) -> i32 {
    let stdin = std::io::stdin();
    for line in stdin.lock().lines() {
        let Ok(line) = line else { break };
        let trimmed = line.trim();
        if trimmed.is_empty() {
            continue;
        }
        let Ok(message) = serde_json::from_str::<Value>(trimmed) else {
            continue;
        };
        let method = message.get("method").and_then(Value::as_str);
        if method.is_none() {
            continue; // Client responses are not answered here.
        }
        if method == Some("initialize") || method == Some("server/discover") {
            send_reply(&plain_reply(&message));
            continue;
        }
        if method == Some("tools/call") {
            on_call(&message);
            send_reply(&plain_reply(&message));
        }
    }
    0
}

fn close_stdout() {
    #[cfg(windows)]
    {
        #[link(name = "kernel32")]
        unsafe extern "system" {
            fn GetStdHandle(nstdhandle: i32) -> *mut core::ffi::c_void;
            fn CloseHandle(handle: *mut core::ffi::c_void) -> i32;
        }
        const STD_OUTPUT_HANDLE: i32 = -11;
        unsafe {
            let handle = GetStdHandle(STD_OUTPUT_HANDLE);
            if !handle.is_null() {
                CloseHandle(handle);
            }
        }
    }
    #[cfg(unix)]
    {
        use std::os::fd::FromRawFd;
        // Take ownership of fd 1 and drop it: a real pipe EOF.
        unsafe { drop(std::os::fd::OwnedFd::from_raw_fd(1)) };
    }
}

// The orphan descendant itself: sleep, then exit.
fn orphan_sleeper(arguments: &[String]) -> i32 {
    let seconds = arguments.first().and_then(|t| t.parse().ok()).unwrap_or(4);
    std::thread::sleep(Duration::from_secs(seconds));
    0
}

fn fixture(mode: &str, arguments: &[String]) -> i32 {
    match mode {
        // Closes its stdout on the first forwarded call, then stays alive.
        "alive-close-worker" => worker_loop(|_| {
            eprintln!("WORKER-PID {}", std::process::id());
            let _ = std::io::stdout().flush();
            close_stdout();
            std::thread::sleep(Duration::from_secs(10));
            std::process::exit(0);
        }),
        // Answers the forwarded call, leaves an orphan, then exits: EOF
        // with a surviving descendant.
        "orphan-worker" => worker_loop(|message| {
            let orphan = spawn_orphan(4);
            eprintln!("ORPHAN-PID {orphan}");
            let reply =
                json!({"jsonrpc": "2.0", "id": message["id"], "result": {"orphan": orphan}});
            send_reply(&reply);
            std::process::exit(0);
        }),
        // A plain crash: no reply, no descendant.
        "die-worker" => worker_loop(|_| {
            eprintln!("DYING");
            std::process::exit(0);
        }),
        // The generic echo worker for recovery and clean-close sessions.
        "echo" => worker_loop(|_| {}),
        // The orphan descendant itself.
        "orphan" => orphan_sleeper(arguments),
        other => {
            eprintln!("unknown fixture mode: {other}");
            2
        }
    }
}

// ---------------------------------------------------------------------------
// Harness
// ---------------------------------------------------------------------------

/// The shim's stderr is drained live from session start: the fixture
/// worker inherits that pipe, so a post-exit drain would block until every
/// writer dies and skew the case timeline. Marker lines (WORKER-PID,
/// ORPHAN-PID, DYING) are recorded as they arrive.
struct Session {
    proc: Child,
    stdout: BufReader<std::process::ChildStdout>,
    markers: std::sync::Arc<std::sync::Mutex<Vec<(String, u32)>>>,
    stderr_text: std::sync::Arc<std::sync::Mutex<String>>,
}

fn sha256_file(path: &Path) -> String {
    let bytes = std::fs::read(path).expect("read file");
    use sha2::{Digest, Sha256};
    let digest = Sha256::digest(&bytes);
    digest.iter().map(|b| format!("{b:02x}")).collect()
}

fn case_dir(name: &str) -> PathBuf {
    let base = PathBuf::from(env!("CARGO_TARGET_TMPDIR")).join(format!(
        "geof-{}-{}",
        name,
        std::process::id()
    ));
    let runtime = base.join("runtime");
    std::fs::create_dir_all(&runtime).expect("runtime dir");
    base
}

fn write_settings(home: &Path) -> PathBuf {
    let runtime = home.join("runtime");
    let settings = json!({
        "schema_name": "comsol_mcp.settings",
        "schema_version": "1.4.0",
        "transport": "shim",
        "shim_binary": {
            "path": SHIM,
            "sha256": sha256_file(Path::new(SHIM)),
            "version": env!("CARGO_PKG_VERSION"),
            "platform": env!("COMSOL_MCP_SHIM_TARGET"),
        },
        "runtime": {"directory": runtime.to_string_lossy()},
    });
    let path = home.join("settings.json");
    std::fs::write(&path, serde_json::to_string(&settings).expect("settings"))
        .expect("write settings");
    path
}

fn spawn_shim(home: &Path, worker_mode: &str) -> Session {
    let settings = write_settings(home);
    let mut environment: Vec<(String, String)> = std::env::vars()
        .filter(|(key, _)| !key.starts_with("COMSOL_MCP_"))
        .collect();
    let child_spec = json!([
        std::env::current_exe().expect("own exe").to_string_lossy(),
        format!("--fixture={worker_mode}")
    ]);
    environment.push((
        "COMSOL_MCP_SETTINGS_PATH".to_owned(),
        settings.to_string_lossy().into_owned(),
    ));
    environment.push((
        "COMSOL_MCP_RUNTIME_DIR".to_owned(),
        home.join("runtime").to_string_lossy().into_owned(),
    ));
    environment.push((
        "COMSOL_MCP_SHIM_CHILD".to_owned(),
        serde_json::to_string(&child_spec).expect("child spec"),
    ));
    let mut command = Command::new(SHIM);
    command
        .env_clear()
        .envs(environment)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        command.creation_flags(CREATE_NO_WINDOW);
    }
    let mut proc = command.spawn().expect("shim spawns");
    let stdout = BufReader::new(proc.stdout.take().expect("shim stdout"));
    let markers: std::sync::Arc<std::sync::Mutex<Vec<(String, u32)>>> =
        std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
    let stderr_text: std::sync::Arc<std::sync::Mutex<String>> =
        std::sync::Arc::new(std::sync::Mutex::new(String::new()));
    if let Some(pipe) = proc.stderr.take() {
        let sink_markers = std::sync::Arc::clone(&markers);
        let sink_text = std::sync::Arc::clone(&stderr_text);
        std::thread::spawn(move || {
            let reader = BufReader::new(pipe);
            for line in reader.lines().map_while(Result::ok) {
                if let Some((name, pid)) = marker_of(&line) {
                    sink_markers.lock().unwrap().push((name, pid));
                }
                let mut text = sink_text.lock().unwrap();
                text.push_str(&line);
                text.push('\n');
            }
        });
    }
    Session {
        proc,
        stdout,
        markers,
        stderr_text,
    }
}

/// ``WORKER-PID 123`` / ``ORPHAN-PID 456`` -> (name, pid).
fn marker_of(line: &str) -> Option<(String, u32)> {
    let (name, rest) = line.split_once(' ')?;
    if !name.ends_with("-PID") {
        return None;
    }
    Some((
        name.trim_end_matches("-PID").to_owned(),
        rest.trim().parse().ok()?,
    ))
}

impl Session {
    /// Best effort: a refusing shim may already be gone by the time a
    /// probe writes; the exit code and stderr carry the assertion.
    fn send(&mut self, payload: &Value) {
        if let Some(stdin) = self.proc.stdin.as_mut() {
            let _ = stdin.write_all(format!("{}\n", payload).as_bytes());
            let _ = stdin.flush();
        }
    }

    fn read(&mut self) -> Value {
        let mut line = String::new();
        self.stdout.read_line(&mut line).expect("read from shim");
        serde_json::from_str(line.trim()).expect("shim reply is JSON")
    }

    /// Wait for the shim's OWN close decision with the client still
    /// connected: stdin stays open, so the deliberate-shutdown path cannot
    /// race the reader's EOF verification (the G-EOF-01 controls observe
    /// the EOF decision, not a teardown the harness triggered).
    fn wait_exit(&mut self, timeout: Duration) -> (i32, String) {
        let code = self.wait_for_exit_code(timeout);
        (code, self.stderr_snapshot())
    }

    /// Wait for exit after closing the client transport (the ordinary
    /// clean-close flow).
    fn finish(&mut self, timeout: Duration) -> (i32, String) {
        self.proc.stdin = None;
        let code = self.wait_for_exit_code(timeout);
        (code, self.stderr_snapshot())
    }

    /// Wait for a fixture marker to appear on stderr.
    fn wait_marker(&self, name: &str, timeout: Duration) -> Option<u32> {
        let deadline = Instant::now() + timeout;
        loop {
            if let Some(pid) = self.marker_pid(name) {
                return Some(pid);
            }
            if Instant::now() >= deadline {
                return None;
            }
            std::thread::sleep(Duration::from_millis(20));
        }
    }

    fn marker_pid(&self, name: &str) -> Option<u32> {
        self.markers
            .lock()
            .unwrap()
            .iter()
            .find(|(marker, _)| marker == name)
            .map(|(_, pid)| *pid)
    }

    /// A short settle lets the live drain thread consume what the exiting
    /// process already wrote; it never waits for inherited writers.
    fn stderr_snapshot(&self) -> String {
        std::thread::sleep(Duration::from_millis(150));
        self.stderr_text.lock().unwrap().clone()
    }

    fn wait_for_exit_code(&mut self, timeout: Duration) -> i32 {
        let deadline = Instant::now() + timeout;
        loop {
            match self.proc.try_wait() {
                Ok(Some(status)) => return status.code().unwrap_or(-1),
                Err(_) => {
                    let _ = self.proc.kill();
                    let _ = self.proc.wait();
                    return -1;
                }
                Ok(None) if Instant::now() < deadline => {
                    std::thread::sleep(Duration::from_millis(50));
                }
                Ok(None) => {
                    let _ = self.proc.kill();
                    let _ = self.proc.wait();
                    return -1;
                }
            }
        }
    }
}

fn initialize(session: &mut Session, id: i64) -> Value {
    session.send(&json!({
        "jsonrpc": "2.0", "id": id, "method": "initialize",
        "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                   "clientInfo": {"name": "geof", "version": "0"}}
    }));
    session.read()
}

fn lock_path(home: &Path) -> PathBuf {
    home.join("runtime").join("host_admission.lock")
}

fn pid_alive(pid: u32) -> bool {
    #[cfg(unix)]
    {
        Path::new(&format!("/proc/{pid}")).exists()
    }
    #[cfg(windows)]
    {
        // No liveness claim on Windows: the kill-on-close job makes the
        // worker tree die with the shim, so absence is expected there.
        let _ = pid;
        false
    }
}

fn wait_pid_gone(pid: u32, timeout: Duration) {
    let deadline = Instant::now() + timeout;
    while Instant::now() < deadline {
        if !pid_alive(pid) {
            return;
        }
        std::thread::sleep(Duration::from_millis(100));
    }
}

// ---------------------------------------------------------------------------
// Controls
// ---------------------------------------------------------------------------

/// Live-worker EOF control (the reviewer's fixture, automated): the worker
/// closes its stdout and stays alive. The shim must answer the pending
/// request with an unknown result, keep the admission record, and refuse a
/// second session while the record's cleanup is unverified.
fn eof_with_live_worker_keeps_the_record() -> Result<(), String> {
    let home = case_dir("eof-live");
    let mut first = spawn_shim(&home, "alive-close-worker");
    let hello = initialize(&mut first, 1);
    if hello.get("id") != Some(&json!(1)) {
        return Err(format!("worker bring-up failed: {hello}"));
    }
    first.send(&json!({
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "anything", "arguments": {}}
    }));
    let pending = first.read();
    let message = pending
        .pointer("/error/message")
        .and_then(Value::as_str)
        .unwrap_or_default();
    if pending.get("id") != Some(&json!(2)) || !message.contains("unknown") {
        return Err(format!(
            "pending request was not answered unknown: {pending}"
        ));
    }
    // The client stays connected: the shim's own EOF decision must keep
    // the record while the worker lives.
    let (code, stderr) = first.wait_exit(Duration::from_secs(WORKER_EXIT_GRACE_SECS + 25));
    if code != 3 {
        return Err(format!("expected crash exit 3, got {code}: {stderr}"));
    }
    if !lock_path(&home).exists() {
        return Err("the record was released while the worker was still alive".into());
    }
    let worker_pid = first
        .wait_marker("WORKER", Duration::from_secs(10))
        .ok_or("the fixture worker pid was not observed")?;
    if cfg!(unix) && !pid_alive(worker_pid) {
        return Err("control premise broken: the worker died early".into());
    }
    // A second session must NOT initialize while the record's cleanup is
    // unverified (the defect let it start immediately).
    let mut second = spawn_shim(&home, "echo");
    second.send(&json!({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-11-25", "capabilities": {}}
    }));
    let (second_code, second_stderr) = second.finish(Duration::from_secs(20));
    if second_code == 0 {
        return Err("a second session started while cleanup was unverified".into());
    }
    if !second_stderr.contains("refusing to start") {
        return Err(format!("second session did not refuse: {second_stderr}"));
    }
    if !lock_path(&home).exists() {
        return Err("the refusing session removed the kept record".into());
    }
    // POSIX: once the worker exits by itself, the stale record verifies
    // clean and a fresh session recovers automatically.
    if cfg!(unix) {
        wait_pid_gone(worker_pid, Duration::from_secs(20));
        let mut third = spawn_shim(&home, "echo");
        let hello = initialize(&mut third, 1);
        if hello.get("id") != Some(&json!(1)) {
            return Err(format!("recovery session failed: {hello}"));
        }
        let (third_code, third_stderr) = third.finish(Duration::from_secs(20));
        if third_code != 0 {
            return Err(format!(
                "recovery session exit {third_code}: {third_stderr}"
            ));
        }
        if lock_path(&home).exists() {
            return Err("the recovered session did not release its record".into());
        }
    }
    Ok(())
}

/// Orphan-descendant control: the worker exits (stdout closes with its
/// death), but it leaves a descendant running. The observed exit alone must
/// not release the record.
fn worker_orphan_descendant_keeps_the_record() -> Result<(), String> {
    let home = case_dir("orphan");
    let mut first = spawn_shim(&home, "orphan-worker");
    let hello = initialize(&mut first, 1);
    if hello.get("id") != Some(&json!(1)) {
        return Err(format!("worker bring-up failed: {hello}"));
    }
    first.send(&json!({
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "anything", "arguments": {}}
    }));
    let reply = first.read();
    if reply.get("id") != Some(&json!(2)) {
        return Err(format!("orphan fixture did not answer: {reply}"));
    }
    // The client stays connected: the observed exit plus a surviving
    // descendant must keep the record.
    let (code, stderr) = first.wait_exit(Duration::from_secs(WORKER_EXIT_GRACE_SECS + 25));
    if code != 3 {
        return Err(format!("expected crash exit 3, got {code}: {stderr}"));
    }
    if !lock_path(&home).exists() {
        return Err("the record was released while a descendant was still running".into());
    }
    let orphan_pid = first
        .wait_marker("ORPHAN", Duration::from_secs(10))
        .ok_or("the orphan pid was not observed")?;
    if cfg!(unix) && !pid_alive(orphan_pid) {
        return Err("control premise broken: the orphan died before the check".into());
    }
    // POSIX: recovery stays refused while the orphan lives, then succeeds
    // after it exits by itself.
    if cfg!(unix) {
        let mut blocked = spawn_shim(&home, "echo");
        blocked.send(&json!({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-11-25", "capabilities": {}}
        }));
        let (blocked_code, blocked_stderr) = blocked.finish(Duration::from_secs(20));
        if blocked_code == 0 {
            return Err("a session started while the orphan descendant lived".into());
        }
        if !blocked_stderr.contains("still running") {
            return Err(format!(
                "refusal did not name the descendant: {blocked_stderr}"
            ));
        }
        wait_pid_gone(orphan_pid, Duration::from_secs(20));
        let mut third = spawn_shim(&home, "echo");
        let hello = initialize(&mut third, 1);
        if hello.get("id") != Some(&json!(1)) {
            return Err(format!("recovery session failed: {hello}"));
        }
        let (third_code, third_stderr) = third.finish(Duration::from_secs(20));
        if third_code != 0 {
            return Err(format!(
                "recovery session exit {third_code}: {third_stderr}"
            ));
        }
        if lock_path(&home).exists() {
            return Err("the recovered session did not release its record".into());
        }
    }
    Ok(())
}

/// Verified-crash control (G3 regression): the worker dies with no
/// descendants. The record IS released, so the domain is immediately
/// reusable.
fn verified_crash_releases_the_record() -> Result<(), String> {
    let home = case_dir("crash");
    let mut first = spawn_shim(&home, "die-worker");
    let hello = initialize(&mut first, 1);
    if hello.get("id") != Some(&json!(1)) {
        return Err(format!("worker bring-up failed: {hello}"));
    }
    first.send(&json!({
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "anything", "arguments": {}}
    }));
    let pending = first.read();
    let message = pending
        .pointer("/error/message")
        .and_then(Value::as_str)
        .unwrap_or_default();
    if pending.get("id") != Some(&json!(2)) || !message.contains("unknown") {
        return Err(format!(
            "pending request was not answered unknown: {pending}"
        ));
    }
    // The client stays connected: the crash close itself must release.
    let (code, stderr) = first.wait_exit(Duration::from_secs(WORKER_EXIT_GRACE_SECS + 25));
    if code != 3 {
        return Err(format!("expected crash exit 3, got {code}: {stderr}"));
    }
    if lock_path(&home).exists() {
        return Err("a verified-clean crash kept the record".into());
    }
    let mut second = spawn_shim(&home, "echo");
    let hello = initialize(&mut second, 1);
    if hello.get("id") != Some(&json!(1)) {
        return Err(format!(
            "next session failed on the released domain: {hello}"
        ));
    }
    let (second_code, second_stderr) = second.finish(Duration::from_secs(20));
    if second_code != 0 {
        return Err(format!("next session exit {second_code}: {second_stderr}"));
    }
    if lock_path(&home).exists() {
        return Err("the closing session did not release its record".into());
    }
    Ok(())
}
