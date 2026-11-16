//! Optional Windows Job Object containment for the shim worker (0.8.2 P04).
//!
//! The shim assigns its worker to a Job Object with
//! `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`. The job handle stays open for the
//! shim's lifetime, so the worker tree dies with the shim even on a hard
//! kill of the shim process. Graceful shutdown still closes the worker
//! stdin first; closing the job handle is the final, tree-wide kill.
//!
//! The job also sets `JOB_OBJECT_LIMIT_BREAKAWAY_OK`. Only a process created
//! with an explicit `CREATE_BREAKAWAY_FROM_JOB` leaves the job; the worker
//! requests it solely for the independent owners the 0.8.1 runtime contract
//! (section 8) keeps alive across a host exit: durable job workers, their
//! watchdog and cancel coordinator, the standalone driver, and the settings
//! window. Every other descendant stays contained.

#![cfg(windows)]

use std::os::windows::io::AsRawHandle;
use std::process::Child;

const JOB_OBJECT_LIMIT_BREAKAWAY_OK: u32 = 0x0800;
const JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE: u32 = 0x2000;
const JOB_OBJECT_EXTENDED_LIMIT_INFORMATION: i32 = 9;

#[repr(C)]
#[allow(clippy::struct_field_names)]
struct ExtendedLimitInformation {
    per_process_user_time_limit: i64,
    per_job_user_time_limit: i64,
    limit_flags: u32,
    minimum_working_set_size: usize,
    maximum_working_set_size: usize,
    active_process_limit: u32,
    affinity: usize,
    priority_class: u32,
    scheduling_class: u32,
    read_operation_count: u64,
    write_operation_count: u64,
    other_operation_count: u64,
    read_transfer_count: u64,
    write_transfer_count: u64,
    other_transfer_count: u64,
    process_memory_limit: usize,
    job_memory_limit: usize,
    peak_process_memory_used: usize,
    peak_job_memory_used: usize,
}

#[link(name = "kernel32")]
unsafe extern "system" {
    fn CreateJobObjectW(
        attributes: *const core::ffi::c_void,
        name: *const u16,
    ) -> *mut core::ffi::c_void;
    fn SetInformationJobObject(
        job: *mut core::ffi::c_void,
        class: i32,
        information: *mut core::ffi::c_void,
        length: u32,
    ) -> i32;
    fn AssignProcessToJobObject(
        job: *mut core::ffi::c_void,
        process: *mut core::ffi::c_void,
    ) -> i32;
    fn CloseHandle(handle: *mut core::ffi::c_void) -> i32;
    fn GetLastError() -> u32;
}

/// Own one Job Object handle. Dropping it kills every assigned process.
pub struct JobHandle {
    handle: *mut core::ffi::c_void,
}

impl JobHandle {
    /// Assign one child to a kill-on-close job and return the owning handle.
    pub fn contain_child(child: &Child) -> Result<JobHandle, String> {
        unsafe {
            let job = CreateJobObjectW(core::ptr::null(), core::ptr::null());
            if job.is_null() {
                return Err("CreateJobObjectW failed".to_owned());
            }
            let mut information = ExtendedLimitInformation {
                per_process_user_time_limit: 0,
                per_job_user_time_limit: 0,
                limit_flags: JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_BREAKAWAY_OK,
                minimum_working_set_size: 0,
                maximum_working_set_size: 0,
                active_process_limit: 0,
                affinity: 0,
                priority_class: 0,
                scheduling_class: 0,
                read_operation_count: 0,
                write_operation_count: 0,
                other_operation_count: 0,
                read_transfer_count: 0,
                write_transfer_count: 0,
                other_transfer_count: 0,
                process_memory_limit: 0,
                job_memory_limit: 0,
                peak_process_memory_used: 0,
                peak_job_memory_used: 0,
            };
            if SetInformationJobObject(
                job,
                JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                &mut information as *mut _ as *mut core::ffi::c_void,
                std::mem::size_of::<ExtendedLimitInformation>() as u32,
            ) == 0
            {
                let code = GetLastError();
                CloseHandle(job);
                return Err(format!("SetInformationJobObject failed: {code}"));
            }
            if AssignProcessToJobObject(job, child.as_raw_handle() as *mut _) == 0 {
                let code = GetLastError();
                CloseHandle(job);
                return Err(format!("AssignProcessToJobObject failed: {code}"));
            }
            Ok(JobHandle { handle: job })
        }
    }
}

impl Drop for JobHandle {
    fn drop(&mut self) {
        unsafe {
            CloseHandle(self.handle);
        }
    }
}
