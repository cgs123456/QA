//! Linux system-audio capture via the PulseAudio monitor source (cpal).
//!
//! The platform matrix is: Windows = WASAPI loopback + cpal mic, Linux = cpal
//! mic + this monitor, macOS = mic only in Phase 1b. `mod.rs` gates this module
//! on `target_os = "linux"`; the file is kept complete so the Linux CI job and
//! the documented matrix stay honest.
//!
//! A PulseAudio monitor is a *source* on the default sink, named
//! `<sink>.monitor`, which is why it is reachable through cpal's input path and
//! shares the whole mic pipeline. It is system audio, so it reports the
//! `loopback` wire label (R12 `path`).

use super::cpal_common::{CpalCapture, CpalTarget};

/// Wire label for this path (R12 `path` field on the sidecar side).
pub const PATH_LABEL: &str = "loopback";

/// Open the monitor of the current default output sink.
pub fn open_default_monitor() -> CpalCapture {
    CpalCapture::new(CpalTarget::DefaultSinkMonitor, PATH_LABEL)
}

/// Open a specific monitor source by name (device picker / tests).
pub fn open_monitor(name: &str) -> CpalCapture {
    CpalCapture::new(CpalTarget::Named(name.to_string()), PATH_LABEL)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::audio::loopback::{EventQueue, FrameQueue, LoopbackSource};

    #[test]
    fn monitor_reports_loopback_path_and_fails_fast_without_pulseaudio() {
        assert_eq!(PATH_LABEL, "loopback");
        let mut mon = open_default_monitor();
        let frames = FrameQueue::new(8);
        let events = EventQueue::new(8);
        match mon.start(frames, events) {
            Ok(()) => assert!(mon.current_device().to_lowercase().contains("monitor")),
            Err(e) => assert!(!e.to_string().is_empty()),
        }
        mon.stop();
    }
}
