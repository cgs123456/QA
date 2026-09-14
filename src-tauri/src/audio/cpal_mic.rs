//! Cross-platform microphone capture (cpal).
//!
//! Mic capture ships on all three platforms in Phase 1b. macOS is **mic-only**
//! in Phase 1b (no ScreenCaptureKit system audio until Phase 2) — the UI must
//! say so rather than silently capturing nothing.
//!
//! Device changes are not auto-rebuilt here (Phase 1b only requires that for
//! Windows loopback); a stream that dies surfaces as an error event so the user
//! can re-pick a device.

use super::cpal_common::{CpalCapture, CpalTarget};

/// Wire label for this path (R12 `path` field on the sidecar side).
pub const PATH_LABEL: &str = "mic";

/// Open the host's default input device as a capture source.
pub fn open_default_mic() -> CpalCapture {
    CpalCapture::new(CpalTarget::DefaultInput, PATH_LABEL)
}

/// Open a specific input device by name (device picker / tests).
pub fn open_mic(name: &str) -> CpalCapture {
    CpalCapture::new(CpalTarget::Named(name.to_string()), PATH_LABEL)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::audio::loopback::{EventQueue, FrameQueue, LoopbackSource};

    #[test]
    fn start_either_succeeds_or_fails_fast_and_stop_is_idempotent() {
        let mut mic = open_default_mic();
        assert_eq!(mic.current_device(), "");
        let frames = FrameQueue::new(8);
        let events = EventQueue::new(8);
        match mic.start(frames, events) {
            Ok(()) => {
                // A running source must know which device it opened.
                assert!(!mic.current_device().is_empty());
            }
            // Headless CI / no input device: must report why, promptly, and
            // must not leave the source wedged in a half-started state.
            Err(e) => assert!(!e.to_string().is_empty()),
        }
        mic.stop();
        mic.stop(); // idempotent
    }

    #[test]
    fn path_label_matches_the_wire_contract() {
        assert_eq!(PATH_LABEL, "mic");
    }
}
