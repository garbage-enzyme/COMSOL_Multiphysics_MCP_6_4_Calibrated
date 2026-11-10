//! Settings snapshot, binary identity, and host admission without Python.
//!
//! This module implements the contract of `comsol_mcp/host_admission.py`
//! (`comsol_mcp.host_admission` 1.1.0):
//!
//! * One settings snapshot: the bytes are read once, hashed, validated, and
//!   their digest is written into the admission record. After admission the
//!   file is hashed again; a change refuses startup.
//! * The configured binary identity is bound to the running image: the
//!   configured path must resolve to this executable, and its SHA-256,
//!   version and target triple must match this build.
//! * Records are published exclusively (temporary file + hard link) and
//!   removed only by an exact-bytes, opened-identity compare-and-delete, so a
//!   replacement owner is never erased.
//! * Owners are classified by birth identity (PID + creation time). The
//!   command signature is recorded for the Python reader; this reader does
//!   not compare it, so it can only report `active` where Python might
//!   report `uncertain` — both refuse.
//! * Stale recovery requires verified cleanup: every recorded owned process
//!   is absent, no live direct child of a recorded process remains except a
//!   verified durable job worker, and the solver lease has no live non-job
//!   owner and no live recorded COMSOL server. Missing evidence refuses.
//! * A fresh admission checks the solver lease too, so a live
//!   retained-version host that holds the lease blocks this domain.

use std::collections::hash_map::RandomState;
use std::fs;
use std::hash::{BuildHasher, Hasher};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};

use serde::de::{self, Deserialize, Deserializer, MapAccess, SeqAccess, Visitor};
use serde_json::{Map, Value, json};

use crate::procinfo::{self, Probe};
use crate::sha256_hex;

pub const SCHEMA_NAME: &str = "comsol_mcp.host_admission";
pub const SCHEMA_VERSION: &str = "1.1.0";
const READABLE_VERSIONS: [&str; 2] = ["1.0.0", SCHEMA_VERSION];
pub const LOCK_FILE: &str = "host_admission.lock";
const SOLVER_LEASE_FILE: &str = "solver_owner.json";
const SETTINGS_SCHEMA: &str = "comsol_mcp.settings";
const SETTINGS_VERSION: &str = "1.4.0";
const CREATE_TIME_TOLERANCE_SECONDS: f64 = 0.05;
const MAX_DOCUMENT_BYTES: usize = 64 * 1024;
const MAX_JOB_DIRECTORIES: usize = 4096;
const LEASE_READ_ATTEMPTS: u32 = 3;
const LEASE_READ_RETRY: std::time::Duration = std::time::Duration::from_millis(50);
pub const SHIM_VERSION: &str = env!("CARGO_PKG_VERSION");
pub const SHIM_PLATFORM: &str = env!("COMSOL_MCP_SHIM_TARGET");
pub const ENV_SETTINGS: &str = "COMSOL_MCP_SETTINGS_PATH";
pub const ENV_RUNTIME: &str = "COMSOL_MCP_RUNTIME_DIR";
pub const ENV_JOBS: &str = "COMSOL_MCP_JOBS_DIR";
pub const ENV_WORKER_HOST_PID: &str = "COMSOL_MCP_SHIM_HOST_PID";
pub const ENV_WORKER_TOKEN: &str = "COMSOL_MCP_SHIM_WORKER_TOKEN";

/// A startup refusal with a stable reason code.
#[derive(Debug)]
pub struct Refusal {
    pub code: &'static str,
    pub message: String,
}

impl std::fmt::Display for Refusal {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "[{}] {}", self.code, self.message)
    }
}

// Shared with comsol_mcp.host_admission (OPERATOR_RECOVERY_HINT and
// LEGACY_RECORD_UNKNOWN_MESSAGE): the reason code and these texts are the
// operator-facing contract for a stale record that cannot prove cleanup.
const OPERATOR_RECOVERY_HINT: &str = "the record was kept; confirm independently that \
nothing from the previous host remains, then run 'python -m comsol_mcp.host_admission \
--show <runtime-dir>' for the exact recovery command";

fn legacy_record_unknown_message() -> String {
    format!(
        "the stale admission record has no containment identity; a descendant of \
the previous host that outlived its own parent cannot be ruled out, so \
{OPERATOR_RECOVERY_HINT}"
    )
}

/// The refusal for a recorded containment that cannot be read. A lost job
/// name (the crash case) also names the explicit operator flow.
fn containment_unreadable_message(problem: &crate::containment::ContainmentError) -> String {
    if problem.is_name_lost() {
        format!(
            "the previous host's containment cannot be read: {problem}; {OPERATOR_RECOVERY_HINT}"
        )
    } else {
        format!("the previous host's containment cannot be read: {problem}")
    }
}

fn refusal<T>(code: &'static str, message: impl Into<String>) -> Result<T, Refusal> {
    Err(Refusal {
        code,
        message: message.into(),
    })
}

// ---------------------------------------------------------------------------
// Strict JSON (duplicate keys are ambiguous configuration)
// ---------------------------------------------------------------------------

struct Strict(Value);

impl<'de> Deserialize<'de> for Strict {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        deserializer.deserialize_any(StrictVisitor).map(Strict)
    }
}

struct StrictVisitor;

impl<'de> Visitor<'de> for StrictVisitor {
    type Value = Value;

    fn expecting(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("a JSON value without duplicate object keys")
    }

    fn visit_bool<E>(self, value: bool) -> Result<Value, E> {
        Ok(Value::Bool(value))
    }

    fn visit_i64<E>(self, value: i64) -> Result<Value, E> {
        Ok(Value::from(value))
    }

    fn visit_u64<E>(self, value: u64) -> Result<Value, E> {
        Ok(Value::from(value))
    }

    fn visit_f64<E>(self, value: f64) -> Result<Value, E> {
        Ok(serde_json::Number::from_f64(value).map_or(Value::Null, Value::Number))
    }

    fn visit_str<E>(self, value: &str) -> Result<Value, E> {
        Ok(Value::String(value.to_owned()))
    }

    fn visit_string<E>(self, value: String) -> Result<Value, E> {
        Ok(Value::String(value))
    }

    fn visit_unit<E>(self) -> Result<Value, E> {
        Ok(Value::Null)
    }

    fn visit_seq<A: SeqAccess<'de>>(self, mut sequence: A) -> Result<Value, A::Error> {
        let mut items = Vec::new();
        while let Some(Strict(item)) = sequence.next_element()? {
            items.push(item);
        }
        Ok(Value::Array(items))
    }

    fn visit_map<A: MapAccess<'de>>(self, mut access: A) -> Result<Value, A::Error> {
        let mut object = Map::new();
        while let Some(key) = access.next_key::<String>()? {
            if object.contains_key(&key) {
                return Err(de::Error::custom(format!("duplicate key {key:?}")));
            }
            let Strict(value) = access.next_value()?;
            object.insert(key, value);
        }
        Ok(Value::Object(object))
    }
}

fn parse_strict(bytes: &[u8]) -> Result<Value, String> {
    serde_json::from_slice::<Strict>(bytes)
        .map(|Strict(value)| value)
        .map_err(|error| error.to_string())
}

// ---------------------------------------------------------------------------
// Settings snapshot and binary identity
// ---------------------------------------------------------------------------

/// One validated settings snapshot with its domain and binary identity.
pub struct Startup {
    pub settings_path: PathBuf,
    pub settings_sha256: String,
    pub runtime_dir: PathBuf,
    pub executable: PathBuf,
    pub executable_sha256: String,
}

/// A literal absolute ASCII path: what both entry points resolve alike.
pub fn is_concrete_runtime_path(value: &str) -> bool {
    !value.is_empty()
        && value == value.trim()
        && value.is_ascii()
        && !value.chars().any(|c| c.is_ascii_control())
        && !value.contains('%')
        && !value.starts_with('~')
        && Path::new(value).is_absolute()
}

fn is_release_version(value: &str) -> bool {
    let (core, suffix) = match value.split_once('-') {
        Some((core, suffix)) => (core, Some(suffix)),
        None => (value, None),
    };
    let parts: Vec<&str> = core.split('.').collect();
    parts.len() == 3
        && parts
            .iter()
            .all(|part| !part.is_empty() && part.bytes().all(|b| b.is_ascii_digit()))
        && suffix.is_none_or(|text| {
            !text.is_empty() && text.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'.')
        })
        && value.len() <= 64
}

fn is_target_triple(value: &str) -> bool {
    let parts: Vec<&str> = value.split('-').collect();
    (3..=4).contains(&parts.len())
        && parts.iter().all(|part| {
            !part.is_empty()
                && part
                    .bytes()
                    .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'_')
        })
        && value.len() <= 64
}

/// Display form without the Windows verbatim prefix, as psutil reports it.
pub fn display_path(path: &Path) -> String {
    let text = path.to_string_lossy();
    if let Some(rest) = text.strip_prefix(r"\\?\UNC\") {
        return format!(r"\\{rest}");
    }
    text.strip_prefix(r"\\?\").unwrap_or(&text).to_owned()
}

fn read_bounded(path: &Path) -> std::io::Result<Vec<u8>> {
    let file = fs::File::open(path)?;
    let mut bytes = Vec::new();
    file.take(MAX_DOCUMENT_BYTES as u64 + 1)
        .read_to_end(&mut bytes)?;
    Ok(bytes)
}

/// Read one settings snapshot and verify every startup identity it names.
pub fn startup() -> Result<Startup, Refusal> {
    let Some(raw_path) = std::env::var_os(ENV_SETTINGS).filter(|v| !v.is_empty()) else {
        return refusal(
            "settings_unverifiable",
            format!(
                "{ENV_SETTINGS} is not set. The shim requires explicit settings to \
                 verify its transport, binary identity and ownership domain."
            ),
        );
    };
    let settings_path = PathBuf::from(raw_path);
    if !settings_path.is_absolute() {
        return refusal(
            "settings_unverifiable",
            format!("{ENV_SETTINGS} must be an absolute path"),
        );
    }
    let bytes = read_bounded(&settings_path).map_err(|error| Refusal {
        code: "settings_unverifiable",
        message: format!(
            "cannot read settings file {}: {error}. The shim refuses to start \
             without verified settings.",
            settings_path.display()
        ),
    })?;
    if bytes.is_empty() || bytes.len() > MAX_DOCUMENT_BYTES {
        return refusal(
            "settings_unverifiable",
            format!(
                "settings file {} must contain 1..{MAX_DOCUMENT_BYTES} bytes",
                settings_path.display()
            ),
        );
    }
    let settings_sha256 = sha256_hex(&bytes);
    let settings = parse_strict(&bytes).map_err(|problem| Refusal {
        code: "settings_unverifiable",
        message: format!(
            "settings file {} is not valid JSON ({problem}). The shim refuses to \
             start with malformed or ambiguous settings.",
            settings_path.display()
        ),
    })?;
    if !settings.is_object() {
        return refusal(
            "settings_unverifiable",
            format!(
                "settings file {} is not a JSON object",
                settings_path.display()
            ),
        );
    }
    let source = settings_path.display();
    if settings.get("schema_name").and_then(Value::as_str) != Some(SETTINGS_SCHEMA)
        || settings.get("schema_version").and_then(Value::as_str) != Some(SETTINGS_VERSION)
    {
        return refusal(
            "transport_setting_invalid",
            format!(
                "settings {source} must declare {SETTINGS_SCHEMA} {SETTINGS_VERSION} \
                 for shim transport"
            ),
        );
    }

    let configured = match settings.get("transport") {
        None => "python".to_owned(),
        Some(Value::String(text)) => text.trim().to_ascii_lowercase(),
        Some(_) => {
            return refusal(
                "transport_setting_invalid",
                format!("settings.transport in {source} must be \"python\" or \"shim\""),
            );
        }
    };
    match configured.as_str() {
        "shim" => {}
        "python" => {
            return refusal(
                "transport_mode_mismatch",
                format!(
                    "settings select transport 'python' but this host is 'shim'. \
                     Correct the client launch command (launch the Python entry \
                     point) or change the transport setting. Settings: {source}"
                ),
            );
        }
        other => {
            return refusal(
                "transport_setting_invalid",
                format!(
                    "settings.transport in {source} is {other:?}; it must be \
                     \"python\" or \"shim\""
                ),
            );
        }
    }

    let binary = settings.get("shim_binary").and_then(Value::as_object);
    let field = |name: &str| -> Result<String, Refusal> {
        binary
            .and_then(|map| map.get(name))
            .and_then(Value::as_str)
            .filter(|text| !text.trim().is_empty())
            .map(str::to_owned)
            .ok_or_else(|| Refusal {
                code: "transport_setting_invalid",
                message: format!(
                    "settings.shim_binary.{name} is missing in {source}. Fill the \
                     path, SHA-256, version and platform in the MCP Connection tab."
                ),
            })
    };
    let configured_path = field("path")?;
    let configured_sha256 = field("sha256")?.to_ascii_lowercase();
    let configured_version = field("version")?;
    let configured_platform = field("platform")?;
    if !Path::new(&configured_path).is_absolute() {
        return refusal(
            "transport_setting_invalid",
            "settings.shim_binary.path must be an absolute path",
        );
    }
    if configured_sha256.len() != 64 || !configured_sha256.bytes().all(|b| b.is_ascii_hexdigit()) {
        return refusal(
            "transport_setting_invalid",
            "settings.shim_binary.sha256 must be 64 hex characters",
        );
    }
    if !is_release_version(&configured_version) {
        return refusal(
            "transport_setting_invalid",
            "settings.shim_binary.version must be a release version such as 0.8.2-dev",
        );
    }
    if !is_target_triple(&configured_platform) {
        return refusal(
            "transport_setting_invalid",
            "settings.shim_binary.platform must be a target triple such as \
             x86_64-pc-windows-msvc",
        );
    }

    let settings_runtime = settings
        .pointer("/runtime/directory")
        .and_then(Value::as_str)
        .unwrap_or_default();
    if !is_concrete_runtime_path(settings_runtime) {
        return refusal(
            "transport_setting_invalid",
            format!(
                "settings.runtime.directory in {source} must be one literal absolute \
                 ASCII path without environment references for shim transport \
                 (found {settings_runtime:?})"
            ),
        );
    }
    // The same order as comsol_mcp.utils.runtime_paths.default_runtime_dir:
    // the environment runtime directory, the parent of the environment jobs
    // directory, then the settings runtime directory.
    let runtime_dir = if let Some(value) = std::env::var_os(ENV_RUNTIME).filter(|v| !v.is_empty()) {
        let path = PathBuf::from(value);
        if !path.is_absolute() {
            return refusal(
                "transport_setting_invalid",
                format!("{ENV_RUNTIME} must be an absolute path"),
            );
        }
        path
    } else if let Some(value) = std::env::var_os(ENV_JOBS).filter(|v| !v.is_empty()) {
        let jobs = PathBuf::from(value);
        match jobs.parent() {
            Some(parent) if jobs.is_absolute() => parent.to_path_buf(),
            _ => {
                return refusal(
                    "transport_setting_invalid",
                    format!("{ENV_JOBS} must be an absolute path with a parent"),
                );
            }
        }
    } else {
        PathBuf::from(settings_runtime)
    };

    // Bind the configured identity to this running image.
    let running = std::env::current_exe()
        .and_then(fs::canonicalize)
        .map_err(|error| Refusal {
            code: "shim_binary_mismatch",
            message: format!("cannot identify the running shim image: {error}"),
        })?;
    let named = fs::canonicalize(&configured_path).map_err(|error| Refusal {
        code: "shim_binary_mismatch",
        message: format!(
            "the configured shim binary '{configured_path}' cannot be resolved: \
             {error}. Check the path in the MCP Connection tab."
        ),
    })?;
    if named != running {
        return refusal(
            "shim_binary_mismatch",
            format!(
                "settings.shim_binary.path names '{}', but the running shim is \
                 '{}'. Launch the configured binary or update the MCP Connection tab.",
                display_path(&named),
                display_path(&running)
            ),
        );
    }
    let image = fs::read(&running).map_err(|error| Refusal {
        code: "shim_binary_mismatch",
        message: format!("cannot read the running shim image: {error}"),
    })?;
    let executable_sha256 = sha256_hex(&image);
    if executable_sha256 != configured_sha256 {
        return refusal(
            "shim_binary_mismatch",
            format!(
                "the running shim's SHA-256 is {executable_sha256}, but settings record \
                 {configured_sha256}. Update the MCP Connection tab after verifying the \
                 published hash."
            ),
        );
    }
    if configured_version != SHIM_VERSION {
        return refusal(
            "shim_binary_mismatch",
            format!(
                "settings.shim_binary.version is '{configured_version}', but this shim \
                 is '{SHIM_VERSION}'"
            ),
        );
    }
    if configured_platform != SHIM_PLATFORM {
        return refusal(
            "shim_binary_mismatch",
            format!(
                "settings.shim_binary.platform is '{configured_platform}', but this \
                 shim was built for '{SHIM_PLATFORM}'"
            ),
        );
    }
    Ok(Startup {
        settings_path,
        settings_sha256,
        runtime_dir,
        executable: running,
        executable_sha256,
    })
}

/// Whether the settings file still has the bytes this startup validated.
pub fn settings_unchanged(startup: &Startup) -> Result<bool, Refusal> {
    match read_bounded(&startup.settings_path) {
        Ok(bytes) => Ok(sha256_hex(&bytes) == startup.settings_sha256),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(false),
        Err(error) => refusal(
            "settings_unverifiable",
            format!("settings cannot be re-read after admission: {error}"),
        ),
    }
}

/// A fresh launch token: 64 hex characters from per-process OS-seeded keys.
pub fn worker_token() -> String {
    let mut material = Vec::with_capacity(64);
    for round in 0u64..4 {
        let mut hasher = RandomState::new().build_hasher();
        hasher.write_u64(round);
        hasher.write_u32(std::process::id());
        material.extend_from_slice(&hasher.finish().to_le_bytes());
    }
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    material.extend_from_slice(&nanos.to_le_bytes());
    sha256_hex(&material)
}

// ---------------------------------------------------------------------------
// Identity classification
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Liveness {
    Active,
    Stale,
    Uncertain,
}

fn identity_fields(record: &Value) -> Option<(u32, f64)> {
    let pid = record.get("pid")?;
    if pid.is_boolean() {
        return None;
    }
    let pid = u32::try_from(pid.as_u64()?).ok().filter(|&p| p > 0)?;
    let created = record.get("process_create_time")?.as_f64()?;
    (created.is_finite() && created > 0.0).then_some((pid, created))
}

fn classify(pid: u32, created: f64) -> (Liveness, &'static str) {
    match procinfo::probe(pid) {
        Probe::Absent => (Liveness::Stale, "the process no longer exists"),
        Probe::Unknown => (
            Liveness::Uncertain,
            "access to the process identity was denied",
        ),
        Probe::Alive(actual) if (actual - created).abs() > CREATE_TIME_TOLERANCE_SECONDS => (
            Liveness::Stale,
            "the PID now belongs to a different process",
        ),
        Probe::Alive(_) => (Liveness::Active, "PID and creation time match"),
    }
}

fn classify_record(record: &Value) -> (Liveness, &'static str) {
    match identity_fields(record) {
        Some((pid, created)) => classify(pid, created),
        None => (
            Liveness::Uncertain,
            "the recorded process identity is invalid",
        ),
    }
}

fn now_epoch() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

// ---------------------------------------------------------------------------
// Record files
// ---------------------------------------------------------------------------

fn document_bytes(document: &Value) -> Vec<u8> {
    let mut payload = serde_json::to_vec_pretty(document).expect("a JSON value serializes");
    payload.push(b'\n');
    payload
}

fn read_record(path: &Path) -> Result<Option<(Value, Vec<u8>)>, Refusal> {
    let bytes = match read_bounded(path) {
        Ok(bytes) => bytes,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(error) => {
            return refusal(
                "admission_lock_unreadable",
                format!("{} cannot be read: {error}", path.display()),
            );
        }
    };
    if bytes.len() > MAX_DOCUMENT_BYTES {
        return refusal(
            "admission_lock_corrupt",
            format!("{} exceeds {MAX_DOCUMENT_BYTES} bytes", path.display()),
        );
    }
    let Ok(document) = parse_strict(&bytes) else {
        return refusal(
            "admission_lock_corrupt",
            format!(
                "the host admission lock {} is not valid UTF-8 JSON; inspect it \
                 before starting a host",
                path.display()
            ),
        );
    };
    if !document.is_object() {
        return refusal(
            "admission_lock_corrupt",
            format!(
                "the host admission lock {} is not a JSON object",
                path.display()
            ),
        );
    }
    let schema = document.get("schema_name").and_then(Value::as_str);
    let version = document.get("schema_version").and_then(Value::as_str);
    if schema != Some(SCHEMA_NAME) || !version.is_some_and(|v| READABLE_VERSIONS.contains(&v)) {
        return refusal(
            "admission_lock_schema_unsupported",
            format!(
                "the host admission lock declares {schema:?} {version:?}; this host \
                 reads {SCHEMA_NAME} {}",
                READABLE_VERSIONS.join(", ")
            ),
        );
    }
    Ok(Some((document, bytes)))
}

/// Remove the file only when the opened file still holds `expected` bytes.
/// Returns false when the file is absent or holds different bytes.
pub fn unlink_if_content(path: &Path, expected: &[u8]) -> std::io::Result<bool> {
    #[cfg(windows)]
    {
        use std::os::windows::fs::OpenOptionsExt;
        use std::os::windows::io::AsRawHandle;

        const GENERIC_READ: u32 = 0x8000_0000;
        const DELETE: u32 = 0x0001_0000;
        const FILE_SHARE_ALL: u32 = 0x1 | 0x2 | 0x4;
        const FILE_FLAG_OPEN_REPARSE_POINT: u32 = 0x0020_0000;
        const FILE_DISPOSITION_INFO: i32 = 4;

        #[link(name = "kernel32")]
        unsafe extern "system" {
            fn SetFileInformationByHandle(
                handle: *mut std::ffi::c_void,
                class: i32,
                information: *const std::ffi::c_void,
                size: u32,
            ) -> i32;
        }

        let mut file = match fs::OpenOptions::new()
            .access_mode(GENERIC_READ | DELETE)
            .share_mode(FILE_SHARE_ALL)
            .custom_flags(FILE_FLAG_OPEN_REPARSE_POINT)
            .open(path)
        {
            Ok(file) => file,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(false),
            Err(error) => return Err(error),
        };
        if !file.metadata()?.is_file() {
            return Ok(false);
        }
        let mut observed = Vec::new();
        (&mut file)
            .take(expected.len() as u64 + 1)
            .read_to_end(&mut observed)?;
        if observed != expected {
            return Ok(false);
        }
        let delete: u8 = 1;
        let ok = unsafe {
            SetFileInformationByHandle(
                file.as_raw_handle(),
                FILE_DISPOSITION_INFO,
                (&delete as *const u8).cast(),
                1,
            )
        };
        if ok == 0 {
            return Err(std::io::Error::last_os_error());
        }
        Ok(true)
    }
    #[cfg(not(windows))]
    {
        use std::os::unix::fs::MetadataExt;

        let link = match fs::symlink_metadata(path) {
            Ok(link) => link,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(false),
            Err(error) => return Err(error),
        };
        if !link.file_type().is_file() {
            return Ok(false);
        }
        let mut file = match fs::File::open(path) {
            Ok(file) => file,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(false),
            Err(error) => return Err(error),
        };
        let opened = file.metadata()?;
        let mut observed = Vec::new();
        (&mut file)
            .take(expected.len() as u64 + 1)
            .read_to_end(&mut observed)?;
        if observed != expected {
            return Ok(false);
        }
        // Quarantine by rename, verify the moved inode is the opened one,
        // and restore without overwriting a newer file if it is not.
        let parent = path.parent().unwrap_or_else(|| Path::new("."));
        let quarantine = parent.join(format!(".unlink-{}", &worker_token()[..16]));
        fs::create_dir(&quarantine)?;
        let moved = quarantine.join("owned");
        if let Err(error) = fs::rename(path, &moved) {
            let _ = fs::remove_dir(&quarantine);
            return if error.kind() == std::io::ErrorKind::NotFound {
                Ok(false)
            } else {
                Err(error)
            };
        }
        let after = fs::symlink_metadata(&moved)?;
        if after.dev() == opened.dev() && after.ino() == opened.ino() {
            fs::remove_file(&moved)?;
            fs::remove_dir(&quarantine)?;
            return Ok(true);
        }
        match fs::hard_link(&moved, path) {
            Ok(()) => {
                fs::remove_file(&moved)?;
                fs::remove_dir(&quarantine)?;
                Ok(false)
            }
            Err(error) => Err(std::io::Error::new(
                error.kind(),
                format!(
                    "identity cleanup conflict: a replacement was retained in {}",
                    quarantine.display()
                ),
            )),
        }
    }
}

/// Publish complete bytes only while the destination is absent.
fn publish_exclusive(path: &Path, payload: &[u8]) -> Result<(), Refusal> {
    let parent = path.parent().unwrap_or_else(|| Path::new("."));
    let temporary = parent.join(format!(
        ".{LOCK_FILE}.{}.{}.tmp",
        std::process::id(),
        &worker_token()[..16]
    ));
    let written = (|| -> std::io::Result<()> {
        let mut file = fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&temporary)?;
        file.write_all(payload)?;
        file.sync_all()
    })();
    if let Err(error) = written {
        let _ = fs::remove_file(&temporary);
        return refusal(
            "admission_unpublishable",
            format!("cannot write the admission record: {error}"),
        );
    }
    let linked = fs::hard_link(&temporary, path);
    let _ = fs::remove_file(&temporary);
    match linked {
        Ok(()) => {
            #[cfg(not(windows))]
            if let Ok(directory) = fs::File::open(parent) {
                let _ = directory.sync_all();
            }
            Ok(())
        }
        Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => refusal(
            "admission_domain_busy",
            "another host acquired admission for this domain concurrently",
        ),
        Err(error) => refusal(
            "admission_unpublishable",
            format!("cannot publish the admission record: {error}"),
        ),
    }
}

// ---------------------------------------------------------------------------
// Cleanup verification
// ---------------------------------------------------------------------------

/// Console hosts serve a console and hold no work of their own.
const CONSOLE_HOSTS: [&str; 2] = ["conhost.exe", "openconsole.exe"];

fn record_identity(record: Option<&Value>, pid_key: &str, time_key: &str) -> Option<(u32, f64)> {
    let record = record?;
    let pid = record.get(pid_key)?;
    if pid.is_boolean() {
        return None;
    }
    let pid = u32::try_from(pid.as_u64()?).ok().filter(|&p| p > 0)?;
    let created = record.get(time_key)?.as_f64()?;
    created.is_finite().then_some((pid, created))
}

/// Recorded independent owners of one domain: pid -> (create time, role).
///
/// Durable job workers of non-terminal jobs, cancel coordinators and wall
/// watchdogs outlive their host by contract (runtime contract section 8), so
/// live children of a stale host that match these exact birth identities
/// are not cleanup debt.
fn independent_owner_identities(
    runtime: &Path,
) -> Result<std::collections::HashMap<u32, (f64, &'static str)>, Refusal> {
    let mut owners = std::collections::HashMap::new();
    let jobs = runtime.join("jobs");
    let entries = match fs::read_dir(&jobs) {
        Ok(entries) => entries,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(owners),
        Err(error) => {
            return refusal(
                "admission_state_uncertain",
                format!("the durable job store cannot be listed: {error}"),
            );
        }
    };
    for (index, entry) in entries.enumerate() {
        if index >= MAX_JOB_DIRECTORIES {
            return refusal(
                "admission_state_uncertain",
                format!("the durable job store holds more than {MAX_JOB_DIRECTORIES} entries"),
            );
        }
        let Ok(entry) = entry else { continue };
        let directory = entry.path();
        let state = read_bounded(&directory.join("state.json"))
            .ok()
            .and_then(|bytes| serde_json::from_slice::<Value>(&bytes).ok());
        if let Some(state) = state.as_ref() {
            let status = state
                .get("status")
                .and_then(Value::as_str)
                .unwrap_or_default();
            if !matches!(status, "completed" | "failed" | "interrupted" | "cancelled")
                && let Some((pid, created)) =
                    record_identity(Some(state), "worker_pid", "worker_process_create_time")
            {
                owners.insert(pid, (created, "durable_job_worker"));
            }
            let coordinator = state
                .get("cancel")
                .and_then(|cancel| cancel.get("coordinator"));
            if let Some((pid, created)) = record_identity(coordinator, "pid", "process_create_time")
            {
                owners.insert(pid, (created, "cancel_coordinator"));
            }
        }
        let watchdog = read_bounded(&directory.join("wall-watchdog.json"))
            .ok()
            .and_then(|bytes| serde_json::from_slice::<Value>(&bytes).ok());
        if let Some(watchdog) = watchdog.as_ref()
            && let Some((pid, created)) = record_identity(
                watchdog.get("watchdog_identity"),
                "pid",
                "process_create_time",
            )
        {
            owners.insert(pid, (created, "wall_watchdog"));
        }
    }
    Ok(owners)
}

/// Why one live child of a stale host is not cleanup debt, or None.
///
/// Only exact recorded birth identities qualify, plus a console host. A
/// virtual-environment launcher qualifies when its single child is a recorded
/// owner. The Python reader additionally compares the two command lines;
/// this reader has no command-line source, so it accepts the same launchers.
fn independent_role_by_probe(
    row: &procinfo::Entry,
    owners: &std::collections::HashMap<u32, (f64, &'static str)>,
    rows: &[procinfo::Entry],
) -> Option<String> {
    let Probe::Alive(created) = procinfo::probe(row.pid) else {
        return None;
    };
    independent_role(row, created, owners, rows)
}

fn independent_role(
    row: &procinfo::Entry,
    created: f64,
    owners: &std::collections::HashMap<u32, (f64, &'static str)>,
    rows: &[procinfo::Entry],
) -> Option<String> {
    if CONSOLE_HOSTS.contains(&row.name.to_ascii_lowercase().as_str()) {
        return Some("console_host".into());
    }
    if let Some((recorded, role)) = owners.get(&row.pid)
        && (recorded - created).abs() <= CREATE_TIME_TOLERANCE_SECONDS
    {
        return Some((*role).into());
    }
    let kids: Vec<&procinfo::Entry> = rows
        .iter()
        .filter(|kid| kid.ppid == row.pid && kid.pid != row.pid)
        .collect();
    let [kid] = kids.as_slice() else {
        return None;
    };
    let (recorded, role) = owners.get(&kid.pid)?;
    let Probe::Alive(kid_created) = procinfo::probe(kid.pid) else {
        return None;
    };
    ((recorded - kid_created).abs() <= CREATE_TIME_TOLERANCE_SECONDS
        && kid_created + CREATE_TIME_TOLERANCE_SECONDS >= created)
        .then(|| format!("{role}_launcher"))
}

/// Read-only solver-lease check shared by fresh admission and recovery.
fn solver_lease_evidence(runtime: &Path) -> Result<Value, Refusal> {
    let path = runtime.join(SOLVER_LEASE_FILE);
    let mut problem = String::from("unknown");
    let mut lease: Option<Value> = None;
    for attempt in 0..LEASE_READ_ATTEMPTS {
        match read_bounded(&path) {
            Ok(bytes) => match serde_json::from_slice::<Value>(&bytes) {
                Ok(document) if document.is_object() => {
                    lease = Some(document);
                    break;
                }
                Ok(_) => problem = "not a JSON object".into(),
                Err(error) => problem = format!("not valid JSON: {error}"),
            },
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                return Ok(json!({"state": "absent"}));
            }
            Err(error) => problem = error.to_string(),
        }
        if attempt + 1 < LEASE_READ_ATTEMPTS {
            std::thread::sleep(LEASE_READ_RETRY);
        }
    }
    let Some(lease) = lease else {
        return refusal(
            "admission_state_uncertain",
            format!("the solver lease cannot be read ({problem}); ownership cannot be verified"),
        );
    };
    let Some((pid, created)) = identity_fields(&lease) else {
        return refusal(
            "admission_state_uncertain",
            "the solver lease identity is invalid; ownership cannot be verified",
        );
    };
    let mode = lease
        .get("mode")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_owned();
    match classify(pid, created) {
        (Liveness::Uncertain, reason) => {
            return refusal(
                "admission_state_uncertain",
                format!("the solver lease owner cannot be verified: {reason}"),
            );
        }
        (Liveness::Active, _) if mode == "durable-job" => {
            return Ok(json!({"state": "active_independent_job", "pid": pid, "mode": mode}));
        }
        (Liveness::Active, _) => {
            return refusal(
                "admission_domain_busy",
                format!(
                    "a live solver owner (PID {pid}, mode {mode}) holds this ownership \
                     domain; close its client normally first"
                ),
            );
        }
        (Liveness::Stale, _) => {}
    }
    let servers = match lease.get("comsol_server_processes") {
        None | Some(Value::Null) => Vec::new(),
        Some(Value::Array(items)) => items.clone(),
        Some(_) => {
            return refusal(
                "admission_state_uncertain",
                "the stale solver lease records an invalid server list",
            );
        }
    };
    let mut verified = Vec::new();
    for server in &servers {
        let Some((server_pid, server_created)) = identity_fields(server) else {
            return refusal(
                "admission_state_uncertain",
                "the stale solver lease records an invalid server identity",
            );
        };
        match classify(server_pid, server_created) {
            (Liveness::Active, _) => {
                return refusal(
                    "admission_cleanup_unverified",
                    format!(
                        "COMSOL server PID {server_pid} recorded by the stale solver \
                         lease is still running"
                    ),
                );
            }
            (Liveness::Uncertain, reason) => {
                return refusal(
                    "admission_state_uncertain",
                    format!("COMSOL server PID {server_pid} cannot be verified: {reason}"),
                );
            }
            (Liveness::Stale, _) => verified.push(server_pid),
        }
    }
    Ok(json!({
        "state": "stale",
        "pid": pid,
        "mode": mode,
        "servers_verified_absent": verified,
        "note": "the stale lease remains until explicit recovery (solver_recover_stale_lease)",
    }))
}

/// Prove that a stale host left nothing running.
fn verify_stale_cleanup(lock: &Value, runtime: &Path) -> Result<Value, Refusal> {
    verify_stale_cleanup_inner(lock, runtime, &[])
}

fn verify_stale_cleanup_inner(
    lock: &Value,
    runtime: &Path,
    ignore_pids: &[u32],
) -> Result<Value, Refusal> {
    let Some((owner_pid, owner_created)) = identity_fields(lock) else {
        return refusal(
            "admission_state_uncertain",
            "the stale admission record has an invalid owner identity",
        );
    };
    let mut roots: Vec<(u32, f64, String)> = vec![(owner_pid, owner_created, "host".into())];
    let owned = match lock.get("owned_processes") {
        None | Some(Value::Null) => Vec::new(),
        Some(Value::Array(items)) => items.clone(),
        Some(_) => {
            return refusal(
                "admission_state_uncertain",
                "the stale admission record lists owned processes in an invalid form",
            );
        }
    };
    let mut owned_evidence = Vec::new();
    for record in &owned {
        let Some((pid, created)) = identity_fields(record) else {
            return refusal(
                "admission_state_uncertain",
                "the stale admission record contains an invalid owned-process identity",
            );
        };
        let role = record
            .get("role")
            .and_then(Value::as_str)
            .unwrap_or("owned process")
            .to_owned();
        match classify(pid, created) {
            (Liveness::Active, _) => {
                return refusal(
                    "admission_cleanup_unverified",
                    format!("{role} PID {pid} of the previous host is still running"),
                );
            }
            (Liveness::Uncertain, reason) => {
                return refusal(
                    "admission_state_uncertain",
                    format!("{role} PID {pid} cannot be verified: {reason}"),
                );
            }
            (Liveness::Stale, reason) => {
                owned_evidence
                    .push(json!({"pid": pid, "role": role, "state": "stale", "reason": reason}));
            }
        }
        roots.push((pid, created, role));
    }

    let rows = procinfo::snapshot().map_err(|problem| Refusal {
        code: "admission_state_uncertain",
        message: format!("the process inventory is incomplete: {problem}"),
    })?;
    let owners = independent_owner_identities(runtime)?;
    let mut descendants = Vec::new();
    let mut independent = Vec::new();
    let containment = lock.get("containment");
    let containment_kind = containment
        .and_then(Value::as_object)
        .and_then(|map| map.get("kind"))
        .and_then(Value::as_str);
    let scope = "kernel membership of the recorded host containment";
    if containment_kind.is_some() {
        // Kernel-authoritative membership: every descendant the host kept,
        // at any depth, regardless of parent links.
        let members = crate::containment::members(containment.expect("the kind was present"))
            .map_err(|problem| Refusal {
                code: "admission_state_uncertain",
                message: containment_unreadable_message(&problem),
            })?;
        for pid in members {
            if ignore_pids.contains(&pid) {
                continue;
            }
            let Some(row) = rows.iter().find(|row| row.pid == pid) else {
                // A member the inventory cannot see is still alive.
                descendants.push((pid, format!("pid-{pid}"), 0, "member".to_owned()));
                continue;
            };
            let Some(role) = independent_role_by_probe(row, &owners, &rows) else {
                descendants.push((row.pid, row.name.clone(), 0, "member".to_owned()));
                continue;
            };
            independent.push(json!({"pid": row.pid, "name": row.name, "role": role}));
        }
    } else {
        // Caller decision 2026-11-10: identical on every platform. A record
        // without a containment identity (only 1.0.0 development builds
        // wrote those) cannot prove its descendants are gone, and missing
        // evidence is never clean. Positive-evidence checks (owned
        // processes) have already run above; nothing is removed.
        let _ = (&rows, &roots);
        return refusal("admission_state_uncertain", legacy_record_unknown_message());
    }
    finish_stale_verification(
        owner_pid,
        owner_created,
        owned_evidence,
        independent,
        descendants,
        runtime,
        scope,
    )
}

fn finish_stale_verification(
    owner_pid: u32,
    owner_created: f64,
    owned_evidence: Vec<Value>,
    independent: Vec<Value>,
    descendants: Vec<(u32, String, u32, String)>,
    runtime: &Path,
    scope: &str,
) -> Result<Value, Refusal> {
    if !descendants.is_empty() {
        let listed = descendants
            .iter()
            .map(|(pid, name, _, _)| format!("PID {pid} ({name})"))
            .collect::<Vec<_>>()
            .join(", ");
        return refusal(
            "admission_cleanup_unverified",
            format!("processes started by the previous host are still running: {listed}"),
        );
    }
    let lease = solver_lease_evidence(runtime)?;
    Ok(json!({
        "owner": {"pid": owner_pid, "process_create_time": owner_created},
        "owned_processes": owned_evidence,
        "descendants": {
            "live": [],
            "independent_owners": independent,
            "scope": scope,
        },
        "solver_lease": lease,
        "listeners": {
            "method": "owned_process_absence",
            "note": "no recorded or descendant process is alive, so none can hold a listener",
        },
    }))
}

// ---------------------------------------------------------------------------
// Admission
// ---------------------------------------------------------------------------

/// The exact record this host published.
pub struct Claim {
    pub path: PathBuf,
    payload: Vec<u8>,
    document: Value,
    /// Keeps the named registry job referenced for the host lifetime.
    _containment: Option<crate::containment::RegistryHandle>,
}

fn owner_text(lock: &Value) -> String {
    let mode = lock
        .get("transport_mode")
        .and_then(Value::as_str)
        .unwrap_or("unknown");
    let pid = lock.get("pid").and_then(Value::as_u64).unwrap_or(0);
    let client = lock
        .pointer("/client/name")
        .and_then(Value::as_str)
        .map(|name| format!(", launched by {name}"))
        .unwrap_or_default();
    format!("{mode} host PID {pid}{client}")
}

fn client_identity(own_created: f64) -> Value {
    let Some(parent) = procinfo::own_parent() else {
        return Value::Null;
    };
    let Probe::Alive(created) = procinfo::probe(parent) else {
        return Value::Null;
    };
    if created > own_created + CREATE_TIME_TOLERANCE_SECONDS {
        return Value::Null; // The parent PID was reused after our launch.
    }
    let name = procinfo::snapshot()
        .ok()
        .and_then(|rows| rows.into_iter().find(|row| row.pid == parent))
        .map(|row| row.name)
        .unwrap_or_default();
    json!({"pid": parent, "process_create_time": created, "name": name})
}

fn own_command_signature() -> String {
    let arguments: Vec<String> = std::env::args_os()
        .map(|argument| argument.to_string_lossy().into_owned())
        .collect();
    sha256_hex(arguments.join("\0").as_bytes())
}

/// Acquire host admission for this startup's ownership domain.
pub fn acquire(startup: &Startup, worker_token_sha256: &str) -> Result<Claim, Refusal> {
    let runtime = &startup.runtime_dir;
    fs::create_dir_all(runtime).map_err(|error| Refusal {
        code: "admission_unpublishable",
        message: format!("cannot create {}: {error}", runtime.display()),
    })?;
    let path = runtime.join(LOCK_FILE);
    let mut recovered = Value::Null;
    if let Some((lock, bytes)) = read_record(&path)? {
        match classify_record(&lock) {
            (Liveness::Active, _) => {
                return refusal(
                    "admission_domain_busy",
                    format!(
                        "another {} is active in this ownership domain; close its \
                         client normally and retry",
                        owner_text(&lock)
                    ),
                );
            }
            (Liveness::Uncertain, reason) => {
                return refusal(
                    "admission_state_uncertain",
                    format!(
                        "the recorded {} cannot be verified: {reason}",
                        owner_text(&lock)
                    ),
                );
            }
            (Liveness::Stale, reason) => {
                let cleanup = verify_stale_cleanup(&lock, runtime)?;
                match unlink_if_content(&path, &bytes) {
                    Ok(true) => {}
                    Ok(false) => {
                        return refusal(
                            "admission_domain_busy",
                            "the admission record changed during stale recovery; another \
                             host may have been admitted",
                        );
                    }
                    Err(error) => {
                        return refusal(
                            "admission_state_uncertain",
                            format!("the stale admission record cannot be removed: {error}"),
                        );
                    }
                }
                recovered = json!({
                    "pid": lock.get("pid").cloned().unwrap_or(Value::Null),
                    "process_create_time": lock.get("process_create_time").cloned().unwrap_or(Value::Null),
                    "transport_mode": lock.get("transport_mode").cloned().unwrap_or(Value::Null),
                    "reason": reason,
                    "cleanup": cleanup,
                });
            }
        }
    } else {
        solver_lease_evidence(runtime)?;
    }

    let Some(own_created) = procinfo::own_create_time() else {
        return refusal(
            "admission_state_uncertain",
            "this shim's own creation time is unavailable",
        );
    };
    // Containment registry: the kernel, not parent links, decides which
    // descendants a later recovery must see.
    let containment = crate::containment::identity_for(std::process::id());
    let registry = crate::containment::adopt(&containment).map_err(|problem| Refusal {
        code: "admission_state_uncertain",
        message: format!("this host cannot adopt its containment identity: {problem}"),
    })?;
    crate::containment::members(&containment).map_err(|problem| Refusal {
        code: "admission_state_uncertain",
        message: format!("this host's containment identity cannot be read back: {problem}"),
    })?;
    let document = json!({
        "schema_name": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "state": "active",
        "transport_mode": "shim",
        "pid": std::process::id(),
        "containment": containment,
        "process_create_time": own_created,
        "command_signature": own_command_signature(),
        "executable": display_path(&startup.executable),
        "client": client_identity(own_created),
        "settings": {
            "path": startup.settings_path.to_string_lossy(),
            "sha256": startup.settings_sha256,
        },
        "domain": runtime.to_string_lossy(),
        "shim": {
            "version": SHIM_VERSION,
            "platform": SHIM_PLATFORM,
            "executable_sha256": startup.executable_sha256,
            "worker_token_sha256": worker_token_sha256,
        },
        "owned_processes": [],
        "recovered_from": recovered,
        "acquired_at_epoch": now_epoch(),
    });
    let payload = document_bytes(&document);
    publish_exclusive(&path, &payload)?;
    Ok(Claim {
        path,
        payload,
        document,
        _containment: Some(registry),
    })
}

impl Claim {
    /// Record the contained worker's birth identity for stale recovery.
    ///
    /// Only this host rewrites its own record, and only while the file still
    /// holds the exact bytes it last published.
    pub fn record_worker(&mut self, pid: u32) -> Result<(), String> {
        let Probe::Alive(created) = procinfo::probe(pid) else {
            return Err(format!("worker PID {pid} has no readable birth identity"));
        };
        let current = read_bounded(&self.path).map_err(|error| error.to_string())?;
        if current != self.payload {
            return Err("the admission record changed; it was not rewritten".into());
        }
        let mut document = self.document.clone();
        if let Some(owned) = document
            .get_mut("owned_processes")
            .and_then(Value::as_array_mut)
        {
            owned.push(json!({"role": "worker", "pid": pid, "process_create_time": created}));
        }
        let payload = document_bytes(&document);
        let temporary = self.path.with_file_name(format!(
            ".{LOCK_FILE}.{}.{}.update",
            std::process::id(),
            &worker_token()[..16]
        ));
        let written = (|| -> std::io::Result<()> {
            let mut file = fs::OpenOptions::new()
                .write(true)
                .create_new(true)
                .open(&temporary)?;
            file.write_all(&payload)?;
            file.sync_all()?;
            fs::rename(&temporary, &self.path)
        })();
        if let Err(error) = written {
            let _ = fs::remove_file(&temporary);
            return Err(error.to_string());
        }
        self.payload = payload;
        self.document = document;
        Ok(())
    }

    /// Remove only the exact record this host published.
    pub fn release(&self) -> bool {
        unlink_if_content(&self.path, &self.payload).unwrap_or(false)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn scratch(label: &str) -> PathBuf {
        let directory = std::env::temp_dir().join(format!(
            "shim-admission-{label}-{}-{}",
            std::process::id(),
            &worker_token()[..8]
        ));
        fs::create_dir_all(&directory).unwrap();
        directory
    }

    fn record(pid: u32, created: f64) -> Value {
        json!({
            "schema_name": SCHEMA_NAME,
            "schema_version": SCHEMA_VERSION,
            "state": "active",
            "transport_mode": "python",
            "pid": pid,
            "process_create_time": created,
            "owned_processes": [],
        })
    }

    #[test]
    fn duplicate_keys_are_refused() {
        assert!(parse_strict(br#"{"transport": "python", "transport": "shim"}"#).is_err());
        assert!(parse_strict(br#"{"a": {"b": 1, "b": 2}}"#).is_err());
        assert!(parse_strict(br#"{"a": [1, {"b": 2}], "c": 1.5}"#).is_ok());
    }

    #[test]
    fn concrete_runtime_paths_need_no_expansion() {
        let absolute = if cfg!(windows) {
            "D:/mcp_tests/runtime"
        } else {
            "/var/runtime"
        };
        assert!(is_concrete_runtime_path(absolute));
        assert!(!is_concrete_runtime_path(
            "%PROGRAMDATA%/comsol_mcp/runtime"
        ));
        assert!(!is_concrete_runtime_path("~/runtime"));
        assert!(!is_concrete_runtime_path(" D:/runtime"));
        assert!(!is_concrete_runtime_path("relative/runtime"));
    }

    #[test]
    fn identity_formats_match_the_settings_contract() {
        assert!(is_release_version("0.8.2-dev"));
        assert!(is_release_version("1.0.0"));
        assert!(!is_release_version("0.8"));
        assert!(!is_release_version("latest"));
        assert!(is_target_triple("x86_64-pc-windows-msvc"));
        assert!(is_target_triple("x86_64-unknown-linux-gnu"));
        assert!(!is_target_triple("windows"));
        assert!(!is_target_triple("X86_64-PC-WINDOWS-MSVC"));
        assert!(is_target_triple(SHIM_PLATFORM));
        assert!(is_release_version(SHIM_VERSION));
    }

    #[test]
    fn this_process_classifies_as_active_and_a_reused_pid_as_stale() {
        let created = procinfo::own_create_time().unwrap();
        let me = std::process::id();
        assert_eq!(classify(me, created).0, Liveness::Active);
        assert_eq!(classify(me, created - 10.0).0, Liveness::Stale);
        assert_eq!(classify_record(&json!({"pid": "x"})).0, Liveness::Uncertain);
        assert_eq!(
            classify_record(&json!({"pid": true, "process_create_time": 1.0})).0,
            Liveness::Uncertain
        );
    }

    #[test]
    fn release_removes_only_the_exact_published_bytes() {
        let directory = scratch("release");
        let path = directory.join(LOCK_FILE);
        let document = record(7, 1.0);
        let payload = document_bytes(&document);
        publish_exclusive(&path, &payload).unwrap();
        let claim = Claim {
            path: path.clone(),
            payload: payload.clone(),
            document,
            _containment: None,
        };
        let replacement = document_bytes(&record(9, 2.0));
        fs::write(&path, &replacement).unwrap();
        assert!(!claim.release());
        assert_eq!(fs::read(&path).unwrap(), replacement);
        fs::write(&path, &payload).unwrap();
        assert!(claim.release());
        assert!(!path.exists());
        let _ = fs::remove_dir_all(&directory);
    }

    #[test]
    fn exclusive_publication_refuses_an_existing_record() {
        let directory = scratch("publish");
        let path = directory.join(LOCK_FILE);
        publish_exclusive(&path, b"first\n").unwrap();
        let error = publish_exclusive(&path, b"second\n").unwrap_err();
        assert_eq!(error.code, "admission_domain_busy");
        assert_eq!(fs::read(&path).unwrap(), b"first\n");
        let leftovers = fs::read_dir(&directory).unwrap().count();
        assert_eq!(leftovers, 1, "no temporary file remains");
        let _ = fs::remove_dir_all(&directory);
    }

    #[test]
    fn worker_records_are_rewritten_only_over_the_owners_bytes() {
        let directory = scratch("worker");
        let path = directory.join(LOCK_FILE);
        let document = record(std::process::id(), procinfo::own_create_time().unwrap());
        let payload = document_bytes(&document);
        publish_exclusive(&path, &payload).unwrap();
        let mut claim = Claim {
            path: path.clone(),
            payload,
            document,
            _containment: None,
        };
        claim.record_worker(std::process::id()).unwrap();
        let stored: Value = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
        assert_eq!(stored["owned_processes"][0]["role"], "worker");
        assert!(claim.release(), "release follows the rewritten bytes");
        fs::write(&path, b"foreign\n").unwrap();
        assert!(claim.record_worker(std::process::id()).is_err());
        assert_eq!(fs::read(&path).unwrap(), b"foreign\n");
        let _ = fs::remove_dir_all(&directory);
    }

    #[test]
    fn legacy_record_message_matches_the_python_contract() {
        let message = legacy_record_unknown_message();
        assert!(message.contains("no containment identity"));
        assert!(message.contains("the record was kept"));
        assert!(message.contains("python -m comsol_mcp.host_admission --show"));
    }

    #[test]
    fn only_a_lost_job_name_names_the_operator_flow() {
        use crate::containment::{ContainmentError, JOB_NAME_LOST};
        let lost =
            containment_unreadable_message(&ContainmentError::Unknown(JOB_NAME_LOST.to_owned()));
        assert!(lost.contains("no longer opens"), "{lost}");
        assert!(lost.ends_with(OPERATOR_RECOVERY_HINT), "{lost}");
        let other = containment_unreadable_message(&ContainmentError::Unknown(
            "the host job cannot be opened: access denied".to_owned(),
        ));
        assert!(!other.contains("--show"), "{other}");
    }

    #[cfg(windows)]
    #[test]
    fn a_lost_windows_job_name_is_unknown_not_clean() {
        // WIN-CONTAIN-01: the job dies with its last handle, so a name that
        // no longer opens cannot prove its members are gone.
        let directory = scratch("job-name-lost");
        let mut lock = record(4242, 1.0);
        lock["containment"] = json!({
            "kind": "windows_job",
            "name": r"Local\comsol_mcp_host_4242_never_created",
        });
        let error = verify_stale_cleanup(&lock, &directory).unwrap_err();
        assert_eq!(error.code, "admission_state_uncertain");
        assert!(
            error.message.contains("containment cannot be read"),
            "{}",
            error.message
        );
        assert!(
            error.message.contains("no longer opens"),
            "{}",
            error.message
        );
        assert!(
            error
                .message
                .contains("python -m comsol_mcp.host_admission --show"),
            "{}",
            error.message
        );
        let _ = fs::remove_dir_all(&directory);
    }

    #[test]
    fn a_containment_less_stale_record_is_unknown_on_every_platform() {
        // Caller decision 2026-11-10: the refusal is identical on Windows,
        // where a legacy record previously walked parent links, and on POSIX.
        let directory = scratch("legacy-unknown");
        let lock = record(4242, 1.0);
        let error = verify_stale_cleanup(&lock, &directory).unwrap_err();
        assert_eq!(error.code, "admission_state_uncertain");
        assert!(error.message.contains("no containment identity"));
        assert!(error.message.contains("the record was kept"));
        assert!(
            error
                .message
                .contains("python -m comsol_mcp.host_admission --show"),
            "{}",
            error.message
        );
        let _ = fs::remove_dir_all(&directory);
    }

    #[test]
    fn stale_record_with_a_live_owned_process_is_not_recovered() {
        let directory = scratch("cleanup");
        let mut lock = record(u32::MAX - 7, 1.0);
        lock["owned_processes"] = json!([{
            "role": "worker",
            "pid": std::process::id(),
            "process_create_time": procinfo::own_create_time().unwrap(),
        }]);
        let error = verify_stale_cleanup(&lock, &directory).unwrap_err();
        assert_eq!(error.code, "admission_cleanup_unverified");
        let _ = fs::remove_dir_all(&directory);
    }

    #[test]
    fn live_non_job_lease_owner_blocks_and_a_job_owner_does_not() {
        let directory = scratch("lease");
        let me = json!({
            "pid": std::process::id(),
            "process_create_time": procinfo::own_create_time().unwrap(),
            "mode": "local-client",
        });
        fs::write(directory.join(SOLVER_LEASE_FILE), me.to_string()).unwrap();
        assert_eq!(
            solver_lease_evidence(&directory).unwrap_err().code,
            "admission_domain_busy"
        );
        let mut job = me.clone();
        job["mode"] = json!("durable-job");
        fs::write(directory.join(SOLVER_LEASE_FILE), job.to_string()).unwrap();
        assert_eq!(
            solver_lease_evidence(&directory).unwrap()["state"],
            "active_independent_job"
        );
        fs::write(directory.join(SOLVER_LEASE_FILE), "{").unwrap();
        assert_eq!(
            solver_lease_evidence(&directory).unwrap_err().code,
            "admission_state_uncertain"
        );
        let _ = fs::remove_dir_all(&directory);
    }

    #[test]
    fn worker_tokens_are_distinct_hex() {
        let first = worker_token();
        assert_eq!(first.len(), 64);
        assert!(first.bytes().all(|b| b.is_ascii_hexdigit()));
        assert_ne!(first, worker_token());
    }
}
