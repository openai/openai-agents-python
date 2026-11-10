//! Runtime executable search path for the SDK local executor.

pub(crate) const SYSTEM_EXECUTABLE_PATH: &str = "/usr/bin:/bin";

#[cfg(target_os = "linux")]
pub(crate) const APPARMOR_USERNS_PROBE_ARG: &str = "--sdk-apparmor-userns-probe";
