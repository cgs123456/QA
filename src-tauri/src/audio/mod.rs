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
//! - [`vad`]: the `Vad` trait, the webrtc-vad provider, and the silero stub.
//! - [`endpoint`]: the frozen segment-endpoint state machine (M2-7) —
//!   decisions in, `segment_start` / `segment_end` out.
//! - [`uplink`]: the WS client to the sidecar's `/audio/stream` (R9–R14) and
//!   the downlink → Tauri event bridge.
//! - [`service`]: the capture service — one VAD worker thread per path,
//!   endpoint wiring, startup self-check, and the `capture://toggle` consumer.
//! - [`synthetic`] (`audio-testharness` only): the injection test harness —
//!   a WAV / generated signal presented as an ordinary `LoopbackSource`, so
//!   everything downstream of the frame queue stays production code.
//!
//! Platform matrix (Phase 1b): Windows = loopback + mic, Linux = monitor + mic,
//! macOS = mic only — the UI must say so. VAD consumes `int16` downstream
//! (Rust side, R11); the WS uplink takes `float32` (1b-3).

pub mod cpal_common;
pub mod cpal_mic;
pub mod endpoint;
pub mod frame;
pub mod loopback;
pub mod resample;
pub mod service;
pub mod uplink;
pub mod vad;

#[cfg(feature = "audio-testharness")]
pub mod synthetic;

#[cfg(target_os = "linux")]
pub mod cpal_monitor;
#[cfg(target_os = "windows")]
pub mod wasapi_loopback;

pub use cpal_common::{CpalCapture, CpalTarget};
pub use endpoint::{
    detect_segments, Close, Decisions, EndpointState, EndpointStats, Heartbeat, Segment,
    END_SILENCE_FRAMES, HOLD_CAPACITY, MAX_SEG_FRAMES, MIN_KEEP_FRAMES, START_MIN_VOICED,
    START_WINDOW, VAD_HEARTBEAT_MS,
};
pub use frame::{
    encode_ws_event, encode_ws_frame, f32_to_i16, i16_to_f32, Frame16k, F32_BYTES, FRAME_MS,
    FRAME_SAMPLES, HEADER_BYTES, SAMPLE_RATE, TYPE_AUDIO, TYPE_EVENT, WIRE_BYTES,
};
pub use loopback::{
    AudioError, CaptureEvent, CapturePipeline, CaptureStats, DropOldestQueue, EventQueue,
    FrameQueue, LoopbackSource, EVENT_QUEUE_CAPACITY, FRAME_QUEUE_CAPACITY,
};
pub use resample::{
    decode_interleaved_f32_le_to_mono, downmix_interleaved_f32_to_mono,
    downmix_interleaved_i16_to_mono_f32, downmix_stereo_f32_to_mono, MonoResampler16k,
};
pub use service::{
    platform_source_factory, CaptureService, DecisionTap, PathSnapshot, SelfCheck, SelfCheckStatus,
    ServiceSnapshot, SourceFactory, NOMINAL_FRAME_RATE, PROBE_MS, WORKER_TICK,
};
#[cfg(feature = "audio-testharness")]
pub use synthetic::{expected_frames, read_wav, Pace, SyntheticSource, WavMaterial};
pub use uplink::{
    tauri_event_name, AudioUplink, EventSink, PathLabel, TauriSink, UplinkConfig, UplinkError,
    UplinkSnapshot, EVT_ASR_ERROR, EVT_ASR_FINAL, EVT_ASR_PARTIAL, EVT_ASR_START,
    EVT_CAPTURE_DEGRADED, OUT_QUEUE_CAPACITY, RECONNECT_BACKOFF,
};
pub use vad::{open_default as open_default_vad, Vad, VadError, WebrtcVad, DEFAULT_AGGRESSIVENESS};

#[cfg(target_os = "linux")]
pub use cpal_monitor::{open_default_monitor, PATH_LABEL as MONITOR_PATH_LABEL};
#[cfg(target_os = "windows")]
pub use wasapi_loopback::{open_default_loopback, WasapiLoopback};
