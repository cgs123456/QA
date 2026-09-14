//! Audio pipeline: capture → mono → 16 kHz → 30 ms dual frames.
//!
//! - [`frame`]: pure frame math + R10 wire encoding.
//! - [`loopback`]: the `LoopbackSource` contract, bounded drop-oldest queues
//!   (R13), and the shared [`loopback::CapturePipeline`].
//! - [`resample`]: native-rate N-channel f32 → mono → 16 kHz 480-frames (rubato).
//! - [`cpal_common`]: shared cpal capture core (mic and monitor are one pipeline
//!   with two device-selection rules).
//! - [`cpal_mic`]: cross-platform microphone (cpal).
//! - [`cpal_monitor`] (Linux): PulseAudio monitor pick.
//! - [`wasapi_loopback`] (Windows): default render-device loopback.
//!
//! Platform matrix (Phase 1b): Windows = loopback + mic, Linux = monitor + mic,
//! macOS = mic only — the UI must say so. VAD consumes `int16` downstream
//! (Rust side, R11); the WS uplink takes `float32` (1b-3).

pub mod cpal_common;
pub mod cpal_mic;
pub mod frame;
pub mod loopback;
pub mod resample;

#[cfg(target_os = "linux")]
pub mod cpal_monitor;
#[cfg(target_os = "windows")]
pub mod wasapi_loopback;

pub use cpal_common::{CpalCapture, CpalTarget};
pub use frame::{
    encode_ws_frame, f32_to_i16, i16_to_f32, Frame16k, F32_BYTES, FRAME_MS, FRAME_SAMPLES,
    HEADER_BYTES, SAMPLE_RATE, TYPE_AUDIO, WIRE_BYTES,
};
pub use loopback::{
    AudioError, CaptureEvent, CapturePipeline, CaptureStats, DropOldestQueue, EventQueue,
    FrameQueue, LoopbackSource, EVENT_QUEUE_CAPACITY, FRAME_QUEUE_CAPACITY,
};
pub use resample::{
    decode_interleaved_f32_le_to_mono, downmix_interleaved_f32_to_mono,
    downmix_interleaved_i16_to_mono_f32, downmix_stereo_f32_to_mono, MonoResampler16k,
};

#[cfg(target_os = "linux")]
pub use cpal_monitor::{open_default_monitor, PATH_LABEL as MONITOR_PATH_LABEL};
#[cfg(target_os = "windows")]
pub use wasapi_loopback::{open_default_loopback, WasapiLoopback};
