//! Process birth identity for host admission.
//!
//! The values match psutil, which the Python entry point uses:
//!
//! * Windows: creation time from `GetProcessTimes` as epoch seconds; parent
//!   PIDs and image names from a Toolhelp32 snapshot. A PID whose process
//!   has exited (even while another process still holds a handle) is absent.
//! * Linux: `btime` from `/proc/stat` plus `starttime / CLK_TCK` from
//!   `/proc/<pid>/stat`. A zombie is absent.
//!
//! Every unreadable observation is `Unknown`, never `Absent`.

#[derive(Debug, Clone, Copy, PartialEq)]
pub enum Probe {
    /// No running process has this PID (never existed, exited or zombie).
    Absent,
    /// A running process has this PID and this creation time.
    Alive(f64),
    /// The evidence is unavailable, for example because access was denied.
    Unknown,
}

/// One row of a full process snapshot.
#[derive(Debug, Clone)]
pub struct Entry {
    pub pid: u32,
    pub ppid: u32,
    pub name: String,
}

pub use imp::{own_parent, probe, snapshot};

/// This process's own creation time.
pub fn own_create_time() -> Option<f64> {
    match probe(std::process::id()) {
        Probe::Alive(created) => Some(created),
        _ => None,
    }
}

#[cfg(windows)]
mod imp {
    use super::{Entry, Probe};
    use std::ffi::c_void;

    type Handle = *mut c_void;

    const PROCESS_QUERY_LIMITED_INFORMATION: u32 = 0x1000;
    const SYNCHRONIZE: u32 = 0x0010_0000;
    const WAIT_OBJECT_0: u32 = 0;
    const WAIT_TIMEOUT: u32 = 258;
    const STILL_ACTIVE: u32 = 259;
    const ERROR_INVALID_PARAMETER: i32 = 87;
    const ERROR_NO_MORE_FILES: i32 = 18;
    const TH32CS_SNAPPROCESS: u32 = 0x2;
    const FILETIME_EPOCH_OFFSET_SECONDS: f64 = 11_644_473_600.0;

    #[repr(C)]
    #[derive(Default)]
    struct FileTime {
        low: u32,
        high: u32,
    }

    #[repr(C)]
    struct ProcessEntry32W {
        size: u32,
        usage: u32,
        pid: u32,
        default_heap_id: usize,
        module_id: u32,
        threads: u32,
        parent_pid: u32,
        priority_base: i32,
        flags: u32,
        exe_file: [u16; 260],
    }

    #[link(name = "kernel32")]
    unsafe extern "system" {
        fn OpenProcess(access: u32, inherit: i32, pid: u32) -> Handle;
        fn CloseHandle(handle: Handle) -> i32;
        fn GetProcessTimes(
            handle: Handle,
            creation: *mut FileTime,
            exit: *mut FileTime,
            kernel: *mut FileTime,
            user: *mut FileTime,
        ) -> i32;
        fn WaitForSingleObject(handle: Handle, milliseconds: u32) -> u32;
        fn GetExitCodeProcess(handle: Handle, code: *mut u32) -> i32;
        fn CreateToolhelp32Snapshot(flags: u32, pid: u32) -> Handle;
        fn Process32FirstW(snapshot: Handle, entry: *mut ProcessEntry32W) -> i32;
        fn Process32NextW(snapshot: Handle, entry: *mut ProcessEntry32W) -> i32;
    }

    struct Owned(Handle);

    impl Drop for Owned {
        fn drop(&mut self) {
            unsafe {
                CloseHandle(self.0);
            }
        }
    }

    fn last_error() -> i32 {
        std::io::Error::last_os_error().raw_os_error().unwrap_or(0)
    }

    fn creation_time(handle: Handle) -> Option<f64> {
        let mut creation = FileTime::default();
        let mut exit = FileTime::default();
        let mut kernel = FileTime::default();
        let mut user = FileTime::default();
        let ok =
            unsafe { GetProcessTimes(handle, &mut creation, &mut exit, &mut kernel, &mut user) };
        if ok == 0 {
            return None;
        }
        let ticks = (u64::from(creation.high) << 32) | u64::from(creation.low);
        Some(ticks as f64 / 1e7 - FILETIME_EPOCH_OFFSET_SECONDS)
    }

    pub fn probe(pid: u32) -> Probe {
        if pid == 0 {
            return Probe::Unknown;
        }
        let synchronized =
            unsafe { OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, 0, pid) };
        if !synchronized.is_null() {
            let handle = Owned(synchronized);
            return match unsafe { WaitForSingleObject(handle.0, 0) } {
                WAIT_OBJECT_0 => Probe::Absent,
                WAIT_TIMEOUT => creation_time(handle.0).map_or(Probe::Unknown, Probe::Alive),
                _ => Probe::Unknown,
            };
        }
        if last_error() == ERROR_INVALID_PARAMETER {
            return Probe::Absent;
        }
        // Some protected processes refuse SYNCHRONIZE but allow the limited
        // query right; fall back to the exit code for liveness.
        let limited = unsafe { OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid) };
        if limited.is_null() {
            return if last_error() == ERROR_INVALID_PARAMETER {
                Probe::Absent
            } else {
                Probe::Unknown
            };
        }
        let handle = Owned(limited);
        let mut code = 0u32;
        if unsafe { GetExitCodeProcess(handle.0, &mut code) } == 0 {
            return Probe::Unknown;
        }
        if code != STILL_ACTIVE {
            return Probe::Absent;
        }
        creation_time(handle.0).map_or(Probe::Unknown, Probe::Alive)
    }

    pub fn snapshot() -> Result<Vec<Entry>, String> {
        let raw = unsafe { CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0) };
        if raw.is_null() || raw as isize == -1 {
            return Err(format!(
                "process snapshot failed: {}",
                std::io::Error::last_os_error()
            ));
        }
        let snapshot = Owned(raw);
        let mut entry = ProcessEntry32W {
            size: std::mem::size_of::<ProcessEntry32W>() as u32,
            usage: 0,
            pid: 0,
            default_heap_id: 0,
            module_id: 0,
            threads: 0,
            parent_pid: 0,
            priority_base: 0,
            flags: 0,
            exe_file: [0; 260],
        };
        if unsafe { Process32FirstW(snapshot.0, &mut entry) } == 0 {
            return Err(format!(
                "process snapshot is empty: {}",
                std::io::Error::last_os_error()
            ));
        }
        let mut rows = Vec::new();
        loop {
            let length = entry
                .exe_file
                .iter()
                .position(|&unit| unit == 0)
                .unwrap_or(entry.exe_file.len());
            rows.push(Entry {
                pid: entry.pid,
                ppid: entry.parent_pid,
                name: String::from_utf16_lossy(&entry.exe_file[..length]),
            });
            if unsafe { Process32NextW(snapshot.0, &mut entry) } == 0 {
                let error = last_error();
                if error == ERROR_NO_MORE_FILES {
                    break;
                }
                return Err(format!(
                    "process snapshot iteration failed: {}",
                    std::io::Error::from_raw_os_error(error)
                ));
            }
        }
        Ok(rows)
    }

    pub fn own_parent() -> Option<u32> {
        let me = std::process::id();
        snapshot()
            .ok()?
            .into_iter()
            .find(|row| row.pid == me)
            .map(|row| row.ppid)
    }
}

#[cfg(not(windows))]
mod imp {
    use super::{Entry, Probe};

    // _SC_CLK_TCK on Linux (glibc and musl). std already links the C library.
    const SC_CLK_TCK: i32 = 2;

    unsafe extern "C" {
        fn sysconf(name: i32) -> i64;
    }

    fn clock_ticks() -> Option<f64> {
        let value = unsafe { sysconf(SC_CLK_TCK) };
        (value > 0).then_some(value as f64)
    }

    fn boot_time() -> Option<f64> {
        let stat = std::fs::read_to_string("/proc/stat").ok()?;
        stat.lines()
            .find_map(|line| line.strip_prefix("btime "))
            .and_then(|value| value.trim().parse::<f64>().ok())
    }

    struct Stat {
        state: char,
        ppid: u32,
        starttime: u64,
        name: String,
    }

    /// Ok(None): the PID does not exist. Err: the file exists but is unreadable.
    fn stat(pid: u32) -> Result<Option<Stat>, ()> {
        let text = match std::fs::read_to_string(format!("/proc/{pid}/stat")) {
            Ok(text) => text,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
            Err(_) => return Err(()),
        };
        let open = text.find('(').ok_or(())?;
        let close = text.rfind(')').ok_or(())?;
        let name = text.get(open + 1..close).ok_or(())?.to_owned();
        let fields: Vec<&str> = text
            .get(close + 1..)
            .ok_or(())?
            .split_whitespace()
            .collect();
        let state = fields.first().and_then(|f| f.chars().next()).ok_or(())?;
        let ppid = fields.get(1).and_then(|f| f.parse().ok()).ok_or(())?;
        let starttime = fields.get(19).and_then(|f| f.parse().ok()).ok_or(())?;
        Ok(Some(Stat {
            state,
            ppid,
            starttime,
            name,
        }))
    }

    pub fn probe(pid: u32) -> Probe {
        if pid == 0 {
            return Probe::Unknown;
        }
        match stat(pid) {
            Ok(None) => Probe::Absent,
            Ok(Some(found)) if found.state == 'Z' => Probe::Absent,
            Ok(Some(found)) => match (boot_time(), clock_ticks()) {
                (Some(boot), Some(ticks)) => Probe::Alive(boot + found.starttime as f64 / ticks),
                _ => Probe::Unknown,
            },
            Err(()) => Probe::Unknown,
        }
    }

    pub fn snapshot() -> Result<Vec<Entry>, String> {
        let entries =
            std::fs::read_dir("/proc").map_err(|e| format!("process snapshot failed: {e}"))?;
        let mut rows = Vec::new();
        for entry in entries {
            let entry = entry.map_err(|e| format!("process snapshot failed: {e}"))?;
            let Some(pid) = entry
                .file_name()
                .to_str()
                .and_then(|n| n.parse::<u32>().ok())
            else {
                continue;
            };
            match stat(pid) {
                Ok(Some(found)) if found.state != 'Z' => rows.push(Entry {
                    pid,
                    ppid: found.ppid,
                    name: found.name,
                }),
                Ok(_) => {}
                Err(()) => return Err(format!("process {pid} is unreadable")),
            }
        }
        Ok(rows)
    }

    pub fn own_parent() -> Option<u32> {
        stat(std::process::id())
            .ok()
            .flatten()
            .map(|found| found.ppid)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn this_process_is_alive_with_a_recent_creation_time() {
        let created = own_create_time().expect("own creation time");
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_secs_f64();
        assert!(created <= now + 1.0 && created > now - 86_400.0 * 30.0);
    }

    #[test]
    fn an_exited_child_is_absent() {
        let mut child = std::process::Command::new(std::env::current_exe().unwrap())
            .arg("--list")
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .spawn()
            .unwrap();
        let pid = child.id();
        child.wait().unwrap();
        // The Child handle is still open here; an exited process must
        // nevertheless be reported as absent.
        assert_eq!(probe(pid), Probe::Absent);
    }

    #[test]
    fn the_snapshot_contains_this_process_and_its_parent() {
        let rows = snapshot().expect("snapshot");
        let me = rows
            .iter()
            .find(|row| row.pid == std::process::id())
            .expect("this process is listed");
        assert_eq!(Some(me.ppid), own_parent());
    }
}
