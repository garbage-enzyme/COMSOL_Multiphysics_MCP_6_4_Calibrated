//! Host containment registry (shared protocol with
//! ``comsol_mcp/host_containment.py``).
//!
//! Windows: the host assigns itself to a named job object with no limits and
//! records the name in the admission lock. Descendants inherit the job
//! unless they explicitly break away (independent owners do). A recovery
//! reader opens the job by name and asks the kernel for its member list:
//! complete at any depth, regardless of parent links. The job object dies
//! with its last member, so a name that no longer opens proves no member is
//! alive.
//!
//! POSIX: the host becomes a session leader and records the session id; the
//! recovery reader lists every process with that session. Unreadable
//! evidence is refused as uncertain, never treated as absence.

#[derive(Debug)]
pub enum ContainmentError {
    Unknown(String),
}

impl std::fmt::Display for ContainmentError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ContainmentError::Unknown(reason) => write!(f, "{reason}"),
        }
    }
}

/// One containment identity: ``{"kind": ..., "name"/"id": ...}``.
pub fn identity_for(pid: u32) -> serde_json::Value {
    if cfg!(windows) {
        serde_json::json!({
            "kind": "windows_job",
            "name": format!("Local\\comsol_mcp_host_{pid}_{}", crate::admission::worker_token()),
        })
    } else {
        serde_json::json!({"kind": "posix_session", "id": pid})
    }
}

#[cfg(windows)]
mod imp {
    use super::ContainmentError;
    use std::ffi::c_void;

    const JOB_OBJECT_QUERY: u32 = 0x0004;
    const JOB_OBJECT_PROCESS_ID_LIST: i32 = 3;
    const ERROR_FILE_NOT_FOUND: u32 = 2;
    const ERROR_INSUFFICIENT_BUFFER: u32 = 122;

    #[link(name = "kernel32")]
    unsafe extern "system" {
        fn CreateJobObjectW(attributes: *const core::ffi::c_void, name: *const u16) -> *mut c_void;
        fn OpenJobObjectW(access: u32, inherit: i32, name: *const u16) -> *mut c_void;
        fn AssignProcessToJobObject(job: *mut c_void, process: *mut c_void) -> i32;
        fn GetCurrentProcess() -> *mut c_void;
        fn CloseHandle(handle: *mut c_void) -> i32;
        fn QueryInformationJobObject(
            job: *mut c_void,
            class: i32,
            information: *mut c_void,
            length: u32,
            return_length: *mut u32,
        ) -> i32;
    }

    fn wide(text: &str) -> Vec<u16> {
        text.encode_utf16().chain(std::iter::once(0)).collect()
    }

    /// An owned handle to the host's registry job. Dropping it releases the
    /// name reference; the object lives on while any member process does.
    pub struct RegistryJob {
        handle: *mut c_void,
    }

    // The handle is owned exclusively and never aliased outside this type.
    unsafe impl Send for RegistryJob {}
    unsafe impl Sync for RegistryJob {}

    impl RegistryJob {
        pub fn adopt(name: &str) -> Result<RegistryJob, ContainmentError> {
            let encoded = wide(name);
            unsafe {
                let job = CreateJobObjectW(core::ptr::null(), encoded.as_ptr());
                if job.is_null() {
                    return Err(ContainmentError::Unknown(
                        "CreateJobObjectW failed".to_owned(),
                    ));
                }
                if AssignProcessToJobObject(job, GetCurrentProcess()) == 0 {
                    CloseHandle(job);
                    return Err(ContainmentError::Unknown(
                        "the host cannot join its registry job".to_owned(),
                    ));
                }
                Ok(RegistryJob { handle: job })
            }
        }
    }

    impl Drop for RegistryJob {
        fn drop(&mut self) {
            unsafe {
                CloseHandle(self.handle);
            }
        }
    }

    /// Live member PIDs of one named registry job.
    pub fn members(name: &str) -> Result<Vec<u32>, ContainmentError> {
        let encoded = wide(name);
        unsafe {
            let job = OpenJobObjectW(JOB_OBJECT_QUERY, 0, encoded.as_ptr());
            if job.is_null() {
                let error = std::io::Error::last_os_error()
                    .raw_os_error()
                    .unwrap_or_default() as u32;
                if error == ERROR_FILE_NOT_FOUND {
                    return Ok(Vec::new()); // Died with its last member.
                }
                let _ = error;
                return Err(ContainmentError::Unknown(format!(
                    "the host job cannot be opened: {}",
                    std::io::Error::last_os_error()
                )));
            }
            struct Guard(*mut c_void);
            impl Drop for Guard {
                fn drop(&mut self) {
                    unsafe {
                        CloseHandle(self.0);
                    }
                }
            }
            let _guard = Guard(job);
            // Header is two DWORDs; entries are ULONG_PTR.
            let header = 8usize;
            let entry = std::mem::size_of::<usize>();
            let mut capacity = header + 4096 * entry;
            loop {
                let mut buffer = vec![0u8; capacity];
                let mut needed: u32 = 0;
                if QueryInformationJobObject(
                    job,
                    JOB_OBJECT_PROCESS_ID_LIST,
                    buffer.as_mut_ptr().cast(),
                    capacity as u32,
                    &mut needed,
                ) != 0
                {
                    let count =
                        u32::from_le_bytes([buffer[4], buffer[5], buffer[6], buffer[7]]) as usize;
                    let room = (capacity - header) / entry;
                    let mut pids = Vec::with_capacity(count.min(room));
                    for index in 0..count.min(room) {
                        let offset = header + index * entry;
                        let value = usize::from_le_bytes(
                            buffer[offset..offset + entry].try_into().unwrap(),
                        );
                        pids.push(value as u32);
                    }
                    return Ok(pids);
                }
                let error = std::io::Error::last_os_error()
                    .raw_os_error()
                    .unwrap_or_default() as u32;
                if error == ERROR_INSUFFICIENT_BUFFER && needed as usize > capacity {
                    capacity = needed as usize;
                    continue;
                }
                return Err(ContainmentError::Unknown(format!(
                    "the host job members cannot be listed: {}",
                    std::io::Error::from_raw_os_error(error as i32)
                )));
            }
        }
    }
}

#[cfg(not(windows))]
mod imp {
    use super::ContainmentError;

    pub struct RegistryJob;

    // EPERM: this process already leads a process group (or a session).
    const EPERM: i32 = 1;

    unsafe extern "C" {
        fn setsid() -> i32;
        fn getsid(pid: i32) -> i32;
    }

    /// A record may name only the session this host actually leads.
    pub fn session_matches(effective: i32, recorded: u32) -> Result<(), ContainmentError> {
        if u32::try_from(effective).ok() == Some(recorded) {
            Ok(())
        } else {
            Err(ContainmentError::Unknown(format!(
                "this host leads no session it can record (its session is {effective}, \
                 the record would name {recorded}); start it as a normal child process, \
                 not as a shell job"
            )))
        }
    }

    impl RegistryJob {
        pub fn adopt_session(recorded: u32) -> Result<RegistryJob, ContainmentError> {
            unsafe {
                if setsid() < 0 {
                    let error = std::io::Error::last_os_error().raw_os_error();
                    if error != Some(EPERM) {
                        return Err(ContainmentError::Unknown(
                            "the host cannot become a session leader".to_owned(),
                        ));
                    }
                }
            }
            // EPERM alone proves nothing: a process-group leader is refused
            // setsid() without leading any session. Read the session back.
            let effective = unsafe { getsid(0) };
            session_matches(effective, recorded)?;
            Ok(RegistryJob)
        }
    }

    /// Live member PIDs of one session.
    pub fn members_of_session(session: u32) -> Result<Vec<u32>, ContainmentError> {
        let entries = std::fs::read_dir("/proc")
            .map_err(|error| ContainmentError::Unknown(format!("/proc: {error}")))?;
        let mut pids = Vec::new();
        for entry in entries {
            let entry =
                entry.map_err(|error| ContainmentError::Unknown(format!("/proc: {error}")))?;
            let name = entry.file_name();
            let Some(pid) = name.to_str().and_then(|text| text.parse::<u32>().ok()) else {
                continue;
            };
            let stat = std::fs::read_to_string(format!("/proc/{pid}/stat"))
                .map_err(|error| ContainmentError::Unknown(format!("/proc/{pid}/stat: {error}")))?;
            let Some(tail) = stat.rsplit(')').next() else {
                continue;
            };
            let fields: Vec<&str> = tail.split_whitespace().collect();
            // state(0) ppid(1) pgrp(2) session(3)
            if fields.len() > 3 && fields[3].parse::<u32>().ok() == Some(session) {
                pids.push(pid);
            }
        }
        Ok(pids)
    }
}

#[cfg(windows)]
pub use imp::RegistryJob as RegistryHandle;
#[cfg(not(windows))]
pub use imp::RegistryJob as RegistryHandle;

/// Live member PIDs of one recorded containment identity.
pub fn members(identity: &serde_json::Value) -> Result<Vec<u32>, ContainmentError> {
    let kind = identity.get("kind").and_then(serde_json::Value::as_str);
    if kind == Some("windows_job") {
        if !cfg!(windows) {
            return Err(ContainmentError::Unknown(
                "a windows job record cannot be read on this platform".to_owned(),
            ));
        }
        let Some(name) = identity.get("name").and_then(serde_json::Value::as_str) else {
            return Err(ContainmentError::Unknown(
                "the recorded job name is missing".to_owned(),
            ));
        };
        #[cfg(windows)]
        {
            imp::members(name)
        }
        #[cfg(not(windows))]
        {
            let _ = name;
            unreachable!("guarded above")
        }
    } else if kind == Some("posix_session") {
        let Some(session) = identity.get("id").and_then(serde_json::Value::as_u64) else {
            return Err(ContainmentError::Unknown(
                "the recorded session id is missing".to_owned(),
            ));
        };
        #[cfg(not(windows))]
        {
            imp::members_of_session(session as u32)
        }
        #[cfg(windows)]
        {
            let _ = session;
            Err(ContainmentError::Unknown(
                "a posix session record cannot be read on this platform".to_owned(),
            ))
        }
    } else {
        Err(ContainmentError::Unknown(
            "the record carries no supported containment identity".to_owned(),
        ))
    }
}

/// Adopt the identity; the returned handle keeps the registry job named.
pub fn adopt(identity: &serde_json::Value) -> Result<imp::RegistryJob, ContainmentError> {
    if cfg!(windows) {
        let name = identity
            .get("name")
            .and_then(serde_json::Value::as_str)
            .ok_or_else(|| ContainmentError::Unknown("job name missing".to_owned()))?;
        #[cfg(windows)]
        {
            imp::RegistryJob::adopt(name)
        }
        #[cfg(not(windows))]
        {
            let _ = name;
            unreachable!()
        }
    } else {
        #[cfg(not(windows))]
        {
            let recorded = identity
                .get("id")
                .and_then(serde_json::Value::as_u64)
                .and_then(|value| u32::try_from(value).ok())
                .ok_or_else(|| ContainmentError::Unknown("session id missing".to_owned()))?;
            imp::RegistryJob::adopt_session(recorded)
        }
        #[cfg(windows)]
        {
            unreachable!()
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn identity_shape_matches_the_python_contract() {
        let value = identity_for(7);
        if cfg!(windows) {
            assert_eq!(value["kind"], "windows_job");
            assert!(
                value["name"]
                    .as_str()
                    .unwrap()
                    .starts_with(r"Local\comsol_mcp_host_7_")
            );
        } else {
            assert_eq!(value["kind"], "posix_session");
            assert_eq!(value["id"], 7);
        }
    }

    #[test]
    fn adopt_then_read_back_lists_this_process() {
        let identity = identity_for(std::process::id());
        let job = adopt(&identity).expect("adopt");
        let found = members(&identity).expect("members");
        assert!(found.contains(&std::process::id()));
        let _ = job;
    }

    #[cfg(not(windows))]
    #[test]
    fn a_session_this_host_does_not_lead_is_refused() {
        assert!(imp::session_matches(26, 26).is_ok());
        // A process-group leader keeps its inherited session after EPERM.
        let refused = imp::session_matches(11, 26).expect_err("foreign session");
        assert!(
            refused
                .to_string()
                .contains("leads no session it can record")
        );
        assert!(imp::session_matches(-1, 26).is_err());
    }

    #[test]
    fn a_child_of_this_process_is_a_member() {
        let identity = identity_for(std::process::id());
        let _job = adopt(&identity).expect("adopt");
        let mut child = std::process::Command::new(std::env::current_exe().unwrap())
            .arg("--list")
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .spawn()
            .unwrap();
        let found = members(&identity).expect("members");
        let _ = child.kill();
        let _ = child.wait();
        assert!(found.contains(&child.id()));
    }

    #[test]
    fn unknown_records_refuse_rather_than_claim_absence() {
        let empty = serde_json::json!({});
        assert!(members(&empty).is_err());
    }
}
