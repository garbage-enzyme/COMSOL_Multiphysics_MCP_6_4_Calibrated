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
//! - `reply_then_die_answers_exactly_once` (G-DUP-01): a worker reply is
//!   delivered exactly once; the crash close never answers the same id again.
//! - `worker_orphan_descendant_keeps_the_record`: an observed worker exit
//!   with a surviving descendant keeps the record (an observed exit alone
//!   proves nothing about deeper descendants).
//! - `verified_crash_releases_the_record`: observed death with an empty
//!   containment releases the record (the G3 regression).
//! - watchdog controls (decision 2, identity-bound after independent
//!   review): the launcher wedge (the reported interpreter dies while the
//!   launcher silently holds the pipe: no EOF, no data) closes exactly
//!   once via the watchdog on the interpreter's verified death; a NORMAL
//!   helper's exit during a healthy quiet call never fires; a worker that
//!   does not report its interpreter identity disarms the watchdog for the
//!   session instead of ever guessing; a healthy quiet call that outlives
//!   the window never fires; the watchdog is off unless
//!   `COMSOL_MCP_SHIM_WATCHDOG_IDLE` is set; and with the watchdog armed,
//!   a true EOF still owns the close (exit 3, one unknown answer).
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
            "reply_then_die_answers_exactly_once",
            reply_then_die_answers_exactly_once,
        ),
        (
            "watchdog_closes_when_the_reported_interpreter_dies",
            watchdog_closes_when_the_reported_interpreter_dies,
        ),
        (
            "watchdog_ignores_normal_helper_exit_during_a_call",
            watchdog_ignores_normal_helper_exit_during_a_call,
        ),
        (
            "watchdog_disarms_when_the_worker_does_not_report_identity",
            watchdog_disarms_when_the_worker_does_not_report_identity,
        ),
        (
            "watchdog_leaves_a_healthy_quiet_call_alone",
            watchdog_leaves_a_healthy_quiet_call_alone,
        ),
        (
            "watchdog_is_disabled_by_default",
            watchdog_is_disabled_by_default,
        ),
        (
            "watchdog_defers_to_the_eof_close",
            watchdog_defers_to_the_eof_close,
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

/// The half-open wedge descendant: unlike `spawn_orphan`, this one KEEPS
/// the worker's stdout write end open (stdout stays inherited). The worker
/// then exits without answering, so the shim sees neither EOF nor data —
/// the exact shape a venv launcher leaves when the underlying interpreter
/// dies mid-stream.
#[allow(clippy::zombie_processes)]
fn spawn_holder(seconds: u64) -> u32 {
    let mut command = Command::new(std::env::current_exe().expect("own exe"));
    command
        .arg("--fixture=holder")
        .arg(seconds.to_string())
        .stdin(Stdio::null());
    // stdout and stderr deliberately stay inherited: the holder must hold
    // the worker's stdout pipe open for the whole control.
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        command.creation_flags(CREATE_NO_WINDOW);
    }
    command.spawn().expect("holder spawns").id()
}

/// Decision-2 identity contract: write THIS process's PID to the file the
/// shim named, the way the shipped Python worker does at startup.
fn write_identity_file() {
    if let Ok(path) = std::env::var("COMSOL_MCP_SHIM_WORKER_IDENTITY_FILE") {
        let _ = std::fs::write(&path, format!("{}\n", std::process::id()));
    }
}

fn report_identity_to(path: &str, pid: u32) {
    let _ = std::fs::write(path, format!("{pid}\n"));
}

/// Read stdin lines until EOF. `on_init` runs before the handshake reply
/// (identity/fixture setup); `on_call` reacts to one forwarded tools/call;
/// the reactive modes end the process inside it, the generic echo mode
/// returns and gets the plain reply.
fn worker_loop_with(mut on_init: impl FnMut(&Value), mut on_call: impl FnMut(&Value)) -> i32 {
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
            on_init(&message);
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

fn worker_loop(on_call: impl FnMut(&Value)) -> i32 {
    worker_loop_with(|_| {}, on_call)
}

/// The reporting variant every decision-2 fixture uses unless the control
/// specifically tests the non-reporting (disarmed) path.
fn reporting_worker_loop(on_call: impl FnMut(&Value)) -> i32 {
    write_identity_file();
    worker_loop_with(|_| {}, on_call)
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
        "alive-close-worker" => reporting_worker_loop(|_| {
            eprintln!("WORKER-PID {}", std::process::id());
            let _ = std::io::stdout().flush();
            close_stdout();
            std::thread::sleep(Duration::from_secs(10));
            std::process::exit(0);
        }),
        // Answers the forwarded call, leaves an orphan, then exits: EOF
        // with a surviving descendant.
        "orphan-worker" => reporting_worker_loop(|message| {
            let orphan = spawn_orphan(4);
            eprintln!("ORPHAN-PID {orphan}");
            let reply =
                json!({"jsonrpc": "2.0", "id": message["id"], "result": {"orphan": orphan}});
            send_reply(&reply);
            std::process::exit(0);
        }),
        // A plain crash: no reply, no descendant.
        "die-worker" => reporting_worker_loop(|_| {
            eprintln!("DYING");
            std::process::exit(0);
        }),
        // G-DUP-01: answers the forwarded call, then dies. The reply was
        // already delivered, so the crash close must not answer the same
        // id a second time.
        "reply-die-worker" => reporting_worker_loop(|message| {
            let reply =
                json!({"jsonrpc": "2.0", "id": message["id"], "result": {"answered": true}});
            send_reply(&reply);
            eprintln!("DYING");
            std::process::exit(0);
        }),
        // The generic echo worker for recovery and clean-close sessions.
        "echo" => reporting_worker_loop(|_| {}),
        // Decision-2 launcher wedge with a bound identity: this process is
        // the LAUNCHER. It reports the holder (the "interpreter") as the
        // session's executing interpreter, the holder keeps the worker's
        // stdout open and dies after two seconds, and the launcher lives
        // on silently with its own copy of the pipe — no EOF, no data.
        "wedge-launcher-worker" => {
            write_identity_file();
            worker_loop_with(
                |_| {
                    let holder = spawn_holder(2);
                    eprintln!("HOLDER-PID {holder}");
                    if let Ok(path) = std::env::var("COMSOL_MCP_SHIM_WORKER_IDENTITY_FILE") {
                        report_identity_to(&path, holder);
                    }
                },
                |_| {
                    std::thread::sleep(Duration::from_secs(30));
                },
            )
        }
        // The independent review's counterexample: a NORMAL helper (null
        // stdio, started before the handshake reply) exits on its own
        // while the worker serves a healthy slow call and then replies.
        "helper-turnover-worker" => {
            write_identity_file();
            worker_loop_with(
                |_| {
                    let helper = spawn_orphan(2);
                    eprintln!("HELPER-PID {helper}");
                },
                |_| {
                    std::thread::sleep(Duration::from_secs(4));
                },
            )
        }
        // The wedge shape WITHOUT an identity report: the worker exits
        // unanswered while a descendant holds its stdout. An unprotected
        // session must stay un-fired, never be closed on a guess. The
        // holder lives six seconds: past the disarm observation window,
        // short enough that POSIX's deliberate close (which waits for a
        // pipe-holding descendant) still finishes inside the control.
        "silent-holder-no-identity" => worker_loop(|_| {
            let holder = spawn_holder(6);
            eprintln!("HOLDER-PID {holder}");
            std::process::exit(0);
        }),
        // A healthy slow call: silent for seconds, then the plain reply.
        "quiet-worker" => reporting_worker_loop(|_| {
            std::thread::sleep(Duration::from_secs(3));
        }),
        // The orphan descendant itself (also the normal short-lived helper).
        "orphan" => orphan_sleeper(arguments),
        // The pipe-holding descendant: alive, silent, holding stdout.
        "holder" => orphan_sleeper(arguments),
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
    spawn_shim_with_env(home, worker_mode, &[])
}

fn spawn_shim_with_env(home: &Path, worker_mode: &str, extra: &[(&str, &str)]) -> Session {
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
    for (key, value) in extra {
        environment.push(((*key).to_owned(), (*value).to_owned()));
    }
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

/// True when the PID has EXITED: gone from /proc, or a not-yet-reaped
/// zombie (an exited process whose parent has not waited on it — the
/// normal helper's parent sleeps through the call, so the slot lingers).
fn pid_exited(pid: u32) -> bool {
    #[cfg(unix)]
    {
        match std::fs::read_to_string(format!("/proc/{pid}/stat")) {
            Err(_) => true,
            Ok(stat) => stat
                .rsplit(')')
                .next()
                .and_then(|tail| tail.split_whitespace().next())
                .is_some_and(|state| state == "Z"),
        }
    }
    #[cfg(windows)]
    {
        let _ = pid;
        true
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

/// G-DUP-01 control: the worker answers the pending request and then
/// dies. The client must receive EXACTLY ONE response for that id (the
/// reply); the crash close must not send a second unknown-result answer
/// for the already-answered id. The record is released after the
/// verified-clean crash (worker dead, no descendants).
fn reply_then_die_answers_exactly_once() -> Result<(), String> {
    let home = case_dir("reply-die");
    let mut first = spawn_shim(&home, "reply-die-worker");
    let hello = initialize(&mut first, 1);
    if hello.get("id") != Some(&json!(1)) {
        return Err(format!("worker bring-up failed: {hello}"));
    }
    first.send(&json!({
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "anything", "arguments": {}}
    }));
    let reply = first.read();
    if reply.get("id") != Some(&json!(2)) || reply.pointer("/result/answered") != Some(&json!(true))
    {
        return Err(format!("the worker reply did not arrive: {reply}"));
    }
    // The client stays connected while the shim makes its own close
    // decision. Read everything the shim still sends until its stdout
    // closes: a duplicate answer for id 2 would appear here.
    let mut tail = Vec::new();
    let mut buffer = [0u8; 4096];
    let deadline = Instant::now() + Duration::from_secs(30);
    loop {
        let empty = first
            .stdout
            .fill_buf()
            .map(|slice| slice.is_empty())
            .unwrap_or(true);
        if empty || Instant::now() > deadline {
            break;
        }
        let read = std::io::Read::read(&mut first.stdout, &mut buffer).unwrap_or(0);
        if read == 0 {
            break;
        }
        tail.extend_from_slice(&buffer[..read]);
    }
    let (code, stderr) = first.wait_exit(Duration::from_secs(WORKER_EXIT_GRACE_SECS + 25));
    let tail_text = String::from_utf8_lossy(&tail).into_owned();
    if code != 3 {
        return Err(format!("expected crash exit 3, got {code}: {stderr}"));
    }
    if !tail_text.trim().is_empty() {
        return Err(format!(
            "unexpected extra output after the reply: {tail_text}"
        ));
    }
    if lock_path(&home).exists() {
        return Err("a verified-clean reply-then-die crash kept the record".into());
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

// ---------------------------------------------------------------------------
// Watchdog controls (decision 2, identity-bound)
// ---------------------------------------------------------------------------

/// Decision-2 launcher wedge control: the worker (the launcher) reports
/// the holder as its executing interpreter; the holder keeps the worker's
/// stdout open and dies two seconds in, while the launcher lives on
/// silently holding its own copy of the pipe — no EOF, no data, no queue
/// pressure. With the window elapsed AND the reported interpreter
/// verifiably gone, the shim must answer the pending request exactly once
/// (a watchdog unknown), close with exit 5, and keep the record: the
/// surviving launcher makes cleanup unverified, like any unproven close.
fn watchdog_closes_when_the_reported_interpreter_dies() -> Result<(), String> {
    let home = case_dir("wdg-wedge");
    let mut first = spawn_shim_with_env(
        &home,
        "wedge-launcher-worker",
        &[("COMSOL_MCP_SHIM_WATCHDOG_IDLE", "1")],
    );
    let hello = initialize(&mut first, 1);
    if hello.get("id") != Some(&json!(1)) {
        return Err(format!("worker bring-up failed: {hello}"));
    }
    let started = Instant::now();
    first.send(&json!({
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "anything", "arguments": {}}
    }));
    let answer = first.read();
    let message = answer
        .pointer("/error/message")
        .and_then(Value::as_str)
        .unwrap_or_default();
    if answer.get("id") != Some(&json!(2)) || !message.contains("watchdog") {
        return Err(format!("the watchdog answer did not arrive: {answer}"));
    }
    // The interpreter lives ~2 s: a close before its death would mean the
    // watchdog fired on silence alone, not on the bound identity's exit.
    if started.elapsed() < Duration::from_millis(1500) {
        return Err(format!(
            "the watchdog fired before the reported interpreter died ({:?})",
            started.elapsed()
        ));
    }
    // Exactly one answer (G-DUP-01 discipline): the next read must be the
    // transport close, not a second response for the same id.
    let mut extra = String::new();
    let read = first.stdout.read_line(&mut extra).unwrap_or(0);
    if read > 0 && !extra.trim().is_empty() {
        return Err(format!(
            "unexpected output after the watchdog answer: {extra}"
        ));
    }
    let (code, stderr) = first.wait_exit(Duration::from_secs(WORKER_EXIT_GRACE_SECS + 25));
    if code != 5 {
        return Err(format!("expected watchdog exit 5, got {code}: {stderr}"));
    }
    if !stderr.contains("watchdog armed") || !stderr.contains("verifiably gone") {
        return Err(format!(
            "the close was not attributed to the bound interpreter: {stderr}"
        ));
    }
    if !lock_path(&home).exists() {
        return Err("the watchdog released the record while the launcher lived".into());
    }
    let _holder = first
        .wait_marker("HOLDER", Duration::from_secs(10))
        .ok_or("the holder pid was not observed")?;
    Ok(())
}

/// The independent review's counterexample, now a regression control: a
/// NORMAL helper started before the handshake reply exits on its own while
/// the worker serves a healthy slow call. With the watchdog armed on the
/// worker's reported interpreter (which keeps running), the helper's exit
/// must not matter: the reply arrives intact and the session closes
/// cleanly with the record released.
fn watchdog_ignores_normal_helper_exit_during_a_call() -> Result<(), String> {
    let home = case_dir("wdg-helper");
    let mut session = spawn_shim_with_env(
        &home,
        "helper-turnover-worker",
        &[("COMSOL_MCP_SHIM_WATCHDOG_IDLE", "1")],
    );
    let hello = initialize(&mut session, 1);
    if hello.get("id") != Some(&json!(1)) {
        return Err(format!("worker bring-up failed: {hello}"));
    }
    let started = Instant::now();
    session.send(&json!({
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "anything", "arguments": {}}
    }));
    let reply = session.read();
    if reply.get("id") != Some(&json!(2))
        || reply.get("error").is_some()
        || reply.pointer("/result/ok") != Some(&json!(true))
    {
        return Err(format!(
            "a normal helper exit closed a healthy call: {reply}"
        ));
    }
    if started.elapsed() < Duration::from_secs(4) {
        return Err(format!(
            "control premise broken: the reply came before the helper turnover ({:?})",
            started.elapsed()
        ));
    }
    let helper = session
        .wait_marker("HELPER", Duration::from_secs(10))
        .ok_or("the helper pid was not observed")?;
    if cfg!(unix) && !pid_exited(helper) {
        return Err("control premise broken: the helper outlived the call".into());
    }
    let (code, stderr) = session.finish(Duration::from_secs(20));
    if code != 0 {
        return Err(format!("expected clean close 0, got {code}: {stderr}"));
    }
    if !stderr.contains("watchdog armed") || stderr.contains("watchdog closing") {
        return Err(format!(
            "the watchdog was not armed-and-silent through the helper exit: {stderr}"
        ));
    }
    if lock_path(&home).exists() {
        return Err("the clean close kept the record".into());
    }
    Ok(())
}

/// Identity-ambiguity control: the wedge shape (worker exits unanswered, a
/// descendant holds its stdout) from a worker that does NOT report its
/// interpreter identity. The watchdog must disarm for the session rather
/// than guess which process matters: no close for three seconds past a
/// one-second window, a disarm attribution line, and the ordinary
/// deliberate close afterwards (record kept: the holder still runs).
fn watchdog_disarms_when_the_worker_does_not_report_identity() -> Result<(), String> {
    let home = case_dir("wdg-noid");
    let mut session = spawn_shim_with_env(
        &home,
        "silent-holder-no-identity",
        &[("COMSOL_MCP_SHIM_WATCHDOG_IDLE", "1")],
    );
    let hello = initialize(&mut session, 1);
    if hello.get("id") != Some(&json!(1)) {
        return Err(format!("worker bring-up failed: {hello}"));
    }
    session.send(&json!({
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "anything", "arguments": {}}
    }));
    // Every answer on this path accompanies a transport close, so a shim
    // that is still running three seconds in has neither answered nor
    // closed on a guess.
    let deadline = Instant::now() + Duration::from_secs(3);
    while Instant::now() < deadline {
        if let Ok(Some(status)) = session.proc.try_wait() {
            return Err(format!(
                "an unprotected session was closed anyway (exit {:?}): {}",
                status.code(),
                session.stderr_snapshot()
            ));
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    let (code, stderr) = session.finish(Duration::from_secs(20));
    if !stderr.contains("did not report its interpreter identity") {
        return Err(format!("the disarm was not attributed: {stderr}"));
    }
    if stderr.contains("watchdog armed") || stderr.contains("watchdog closing") {
        return Err(format!("the watchdog acted without an identity: {stderr}"));
    }
    // The deliberate close is platform-specific in TIMING but converges:
    // Windows closes the kill-on-close job with the session (the holder
    // dies immediately); POSIX waits for the pipe-holding descendant (the
    // six-second holder) because the reader cannot see EOF before it. In
    // both cases the exit is 0 and the verified-clean record is released.
    if code != 0 {
        return Err(format!("expected deliberate close 0, got {code}: {stderr}"));
    }
    if lock_path(&home).exists() {
        return Err("the deliberate close kept the record after the holder died".into());
    }
    Ok(())
}

/// Decision-2 healthy-call control: the worker stays silent well past the
/// armed window while its reported interpreter keeps running. The watchdog
/// must NOT fire — the reply arrives intact after the silent stretch, and
/// the session closes cleanly.
fn watchdog_leaves_a_healthy_quiet_call_alone() -> Result<(), String> {
    let home = case_dir("wdg-healthy");
    let mut session = spawn_shim_with_env(
        &home,
        "quiet-worker",
        &[("COMSOL_MCP_SHIM_WATCHDOG_IDLE", "1")],
    );
    let hello = initialize(&mut session, 1);
    if hello.get("id") != Some(&json!(1)) {
        return Err(format!("worker bring-up failed: {hello}"));
    }
    let started = Instant::now();
    session.send(&json!({
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "anything", "arguments": {}}
    }));
    let reply = session.read();
    if reply.get("id") != Some(&json!(2))
        || reply.get("error").is_some()
        || reply.pointer("/result/ok") != Some(&json!(true))
    {
        return Err(format!(
            "the quiet worker's reply was not delivered intact: {reply}"
        ));
    }
    if started.elapsed() < Duration::from_secs(3) {
        return Err(format!(
            "control premise broken: the reply came before the quiet stretch ({:?})",
            started.elapsed()
        ));
    }
    let (code, stderr) = session.finish(Duration::from_secs(20));
    if code != 0 {
        return Err(format!("expected clean close 0, got {code}: {stderr}"));
    }
    if lock_path(&home).exists() {
        return Err("the clean close kept the record".into());
    }
    Ok(())
}

/// Default-off control: the same silent stretch with the watchdog env
/// absent never closes early. The window only exists when configured.
fn watchdog_is_disabled_by_default() -> Result<(), String> {
    let home = case_dir("wdg-off");
    let mut session = spawn_shim(&home, "quiet-worker");
    let hello = initialize(&mut session, 1);
    if hello.get("id") != Some(&json!(1)) {
        return Err(format!("worker bring-up failed: {hello}"));
    }
    session.send(&json!({
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "anything", "arguments": {}}
    }));
    let reply = session.read();
    if reply.get("id") != Some(&json!(2)) || reply.get("error").is_some() {
        return Err(format!(
            "the quiet worker's reply was not delivered intact: {reply}"
        ));
    }
    let (code, stderr) = session.finish(Duration::from_secs(20));
    if code != 0 {
        return Err(format!("expected clean close 0, got {code}: {stderr}"));
    }
    if lock_path(&home).exists() {
        return Err("the clean close kept the record".into());
    }
    Ok(())
}

/// EOF-precedence control with the watchdog armed: when the worker's stdout
/// truly closes, the EOF close owns the outcome — exit 3, one unknown
/// answer, record released, domain reusable — and the armed watchdog
/// neither converts nor duplicates it.
fn watchdog_defers_to_the_eof_close() -> Result<(), String> {
    let home = case_dir("wdg-eof");
    let mut first = spawn_shim_with_env(
        &home,
        "die-worker",
        &[("COMSOL_MCP_SHIM_WATCHDOG_IDLE", "1")],
    );
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
    if pending.get("id") != Some(&json!(2))
        || !message.contains("worker exited")
        || message.contains("watchdog")
    {
        return Err(format!("the EOF close did not own the answer: {pending}"));
    }
    let mut extra = String::new();
    let read = first.stdout.read_line(&mut extra).unwrap_or(0);
    if read > 0 && !extra.trim().is_empty() {
        return Err(format!(
            "unexpected output after the unknown answer: {extra}"
        ));
    }
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
    Ok(())
}
