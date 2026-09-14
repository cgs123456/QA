//! Windows WASAPI loopback capture — the default render device's mix (what you hear).
//!
//! The three PRD §3.5 pitfalls are all handled here:
//!
//! 1. **COM is per-thread.** `initialize_mta()` runs on the capture thread
//!    itself, never inherited from the caller.
//! 2. **The native format is read, not assumed.** Sample rate and channel count
//!    come from `Device::get_device_format()`. We then ask the engine for the
//!    *same* rate and channels but with an `f32` container and `autoconvert`
//!    enabled, so only the sample container is normalised; rate and channel
//!    conversion stay in rubato where they are testable.
//! 3. **Device changes rebuild the stream.** `IMMNotificationClient`'s default
//!    render callback only flips an atomic (it runs on a Windows audio thread
//!    and must not touch the enumerator); the capture thread notices it, tears
//!    the stream down and rebuilds against the new default device.
//!
//! Loopback itself is expressed as a `Render` device initialised with
//! `Direction::Capture` — the `wasapi` crate then sets
//! `AUDCLNT_STREAMFLAGS_LOOPBACK` for us.
//!
//! Two error regimes, deliberately different:
//! - **Startup** failure is reported synchronously through `start()`'s return
//!   value (the shell can render the degraded page without waiting for events).
//! - **Mid-session** failure is self-healing: bounded retries with backoff, and
//!   only after `MAX_SESSION_FAILURES` consecutive failures does it surface as
//!   an error event.
//!
//! R14: every string produced here is content-free (device names, error kinds,
//! counters) — no audio, no transcript text.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc;
use std::sync::{Arc, Mutex, MutexGuard};
use std::thread::{self, JoinHandle};
use std::time::Duration;

use wasapi::{
    AudioCaptureClient, AudioClient, DeviceEnumerator, DeviceEventCallbacks,
    DeviceEventRegistration, Direction, Handle, SampleType, StreamMode, WaveFormat,
};

use super::loopback::{
    AudioError, CaptureEvent, CapturePipeline, CaptureStats, EventQueue, FrameQueue, LoopbackSource,
};
use super::resample::decode_interleaved_f32_le_to_mono;

/// Poll granularity for the stop flag / device-change flag. The audio event
/// still drives data flow; this only bounds reaction latency to a device swap.
const WAIT_TIMEOUT_MS: u32 = 100;
/// Consecutive failed sessions before we stop retrying and report a hard error.
const MAX_SESSION_FAILURES: u32 = 5;
const RETRY_BACKOFF: Duration = Duration::from_millis(500);
/// How long `start()` waits for the first session to come up.
const START_TIMEOUT: Duration = Duration::from_secs(6);

fn lock<T>(m: &Mutex<T>) -> MutexGuard<'_, T> {
    m.lock().unwrap_or_else(|e| e.into_inner())
}

/// WasapiError → our platform-neutral error. Messages carry device/HRESULT
/// detail only (R14).
fn werr(e: wasapi::WasapiError) -> AudioError {
    AudioError::Stream(e.to_string())
}

/// How a capture session ended.
enum SessionEnd {
    Stopped,
    DeviceChanged,
}

/// One open stream plus everything that must stay alive with it.
struct Session {
    client: AudioClient,
    capture: AudioCaptureClient,
    h_event: Handle,
    /// Held for the lifetime of the session; dropping unregisters.
    _registration: DeviceEventRegistration,
    changed: Arc<AtomicBool>,
    rate: u32,
    channels: u16,
    block_align: usize,
    name: String,
}

impl Session {
    /// Open a loopback stream on the current default render device.
    fn open(device_name: &Mutex<String>) -> Result<Self, AudioError> {
        let enumerator = DeviceEnumerator::new().map_err(werr)?;
        let device = enumerator
            .get_default_device(&Direction::Render)
            .map_err(werr)?;
        let name = device
            .get_friendlyname()
            .unwrap_or_else(|_| "default render".to_string());
        *lock(device_name) = name.clone();

        // Pitfall 2: read the device's own mix format instead of assuming 48k/stereo.
        let mix = device.get_device_format().map_err(werr)?;
        let rate = mix.get_samplespersec();
        let channels = mix.get_nchannels();
        if rate == 0 || channels == 0 {
            return Err(AudioError::Unsupported(
                "device mix format reports 0 rate or 0 channels".into(),
            ));
        }
        let desired = WaveFormat::new(
            32,
            32,
            &SampleType::Float,
            rate as usize,
            channels as usize,
            None,
        );
        let block_align = desired.get_blockalign() as usize;
        if block_align == 0 {
            return Err(AudioError::Unsupported("device block align is 0".into()));
        }

        let mut client = device.get_iaudioclient().map_err(werr)?;
        let (_default_period, min_period) = client.get_device_period().map_err(werr)?;
        let mode = StreamMode::EventsShared {
            autoconvert: true,
            buffer_duration_hns: min_period,
        };
        // Render device + Direction::Capture ⇒ the crate sets the LOOPBACK flag.
        client
            .initialize_client(&desired, &Direction::Capture, &mode)
            .map_err(werr)?;
        let h_event = client.set_get_eventhandle().map_err(werr)?;
        let capture = client.get_audiocaptureclient().map_err(werr)?;

        // Pitfall 3: watch for default render changes. The callback must be
        // trivial and must not call back into the enumerator it registered on.
        let changed = Arc::new(AtomicBool::new(false));
        let flag = changed.clone();
        let mut callbacks = DeviceEventCallbacks::new();
        callbacks.set_default_device_callback(move |direction, _role, _id| {
            if matches!(direction, Direction::Render) {
                flag.store(true, Ordering::SeqCst);
            }
        });
        let registration = enumerator
            .register_notification_callback(callbacks)
            .map_err(werr)?;

        client.start_stream().map_err(werr)?;

        Ok(Self {
            client,
            capture,
            h_event,
            _registration: registration,
            changed,
            rate,
            channels,
            block_align,
            name,
        })
    }

    /// Pump frames until stopped, the device changes, or the stream dies.
    fn pump(
        &self,
        pipeline: &mut CapturePipeline,
        events: &EventQueue,
        stop: &AtomicBool,
    ) -> Result<SessionEnd, AudioError> {
        events.push(CaptureEvent::Started {
            device: self.name.clone(),
            sample_rate: self.rate,
            channels: self.channels,
        });

        let mut scratch: Vec<u8> = Vec::new();
        loop {
            if stop.load(Ordering::SeqCst) {
                return Ok(SessionEnd::Stopped);
            }
            if self.changed.load(Ordering::SeqCst) {
                return Ok(SessionEnd::DeviceChanged);
            }

            match self.h_event.wait_for_event(WAIT_TIMEOUT_MS) {
                Ok(()) => {}
                Err(wasapi::WasapiError::EventTimeout) => continue,
                Err(e) => return Err(werr(e)),
            }

            // Drain every packet the engine has queued; one event can cover several.
            loop {
                let pending = match self.capture.get_next_packet_size().map_err(werr)? {
                    Some(n) if n > 0 => n as usize,
                    _ => break,
                };
                let need = pending * self.block_align;
                if scratch.len() < need {
                    scratch.resize(need, 0);
                }
                let (read, _info) = self
                    .capture
                    .read_from_device(&mut scratch[..need])
                    .map_err(werr)?;
                if read == 0 {
                    break;
                }
                let mono = decode_interleaved_f32_le_to_mono(
                    &scratch[..read as usize * self.block_align],
                    self.channels,
                );
                pipeline.push(&mono)?;
            }
        }
    }
}

impl Drop for Session {
    fn drop(&mut self) {
        let _ = self.client.stop_stream();
    }
}

/// WASAPI loopback capture for the default render device (system mix).
pub struct WasapiLoopback {
    handle: Option<JoinHandle<()>>,
    stop_flag: Arc<AtomicBool>,
    /// Device name of the currently/last opened stream (read by the shell).
    device: Arc<Mutex<String>>,
    stats: Arc<Mutex<CaptureStats>>,
}

impl WasapiLoopback {
    pub fn new() -> Self {
        Self {
            handle: None,
            stop_flag: Arc::new(AtomicBool::new(false)),
            device: Arc::new(Mutex::new(String::new())),
            stats: Arc::new(Mutex::new(CaptureStats::default())),
        }
    }
}

impl Default for WasapiLoopback {
    fn default() -> Self {
        Self::new()
    }
}

impl Drop for WasapiLoopback {
    fn drop(&mut self) {
        self.stop();
    }
}

impl LoopbackSource for WasapiLoopback {
    fn start(
        &mut self,
        frames: Arc<FrameQueue>,
        events: Arc<EventQueue>,
    ) -> Result<(), AudioError> {
        if self.handle.is_some() {
            return Err(AudioError::Stream("already running".into()));
        }
        self.stop_flag.store(false, Ordering::SeqCst);
        let stop = self.stop_flag.clone();
        let stats = self.stats.clone();
        let device = self.device.clone();
        let reporter = events.clone();
        let (ready_tx, ready_rx) = mpsc::sync_channel::<Result<(), AudioError>>(1);

        let handle = thread::Builder::new()
            .name("wasapi-loopback".to_string())
            .spawn(move || {
                let mut ready = Some(ready_tx);
                let result = run(frames, events, &mut ready, &stop, &stats, &device);
                if let Err(e) = result {
                    // If the failure happened before the first session came up,
                    // `start()` is still waiting: hand it the real reason.
                    if let Some(tx) = ready.take() {
                        let _ = tx.try_send(Err(e.clone()));
                    }
                    reporter.push(CaptureEvent::Error {
                        message: e.to_string(),
                    });
                }
                reporter.push(CaptureEvent::Stopped);
            })
            .map_err(|e| AudioError::Stream(format!("spawn capture thread: {e}")))?;

        self.handle = Some(handle);
        match ready_rx.recv_timeout(START_TIMEOUT) {
            Ok(Ok(())) => Ok(()),
            Ok(Err(e)) => {
                // Thread has already returned; join so the source is reusable.
                if let Some(h) = self.handle.take() {
                    let _ = h.join();
                }
                Err(e)
            }
            Err(_) => {
                self.stop();
                Err(AudioError::Stream(
                    "loopback capture did not start in time".into(),
                ))
            }
        }
    }

    fn stop(&mut self) {
        self.stop_flag.store(true, Ordering::SeqCst);
        if let Some(h) = self.handle.take() {
            let _ = h.join();
        }
    }

    fn current_device(&self) -> String {
        lock(&self.device).clone()
    }

    fn stats(&self) -> CaptureStats {
        *lock(&self.stats)
    }
}

/// Capture thread body: COM init once, then open/pump/rebuild loop.
fn run(
    frames: Arc<FrameQueue>,
    events: Arc<EventQueue>,
    ready: &mut Option<mpsc::SyncSender<Result<(), AudioError>>>,
    stop: &AtomicBool,
    stats: &Mutex<CaptureStats>,
    device: &Mutex<String>,
) -> Result<(), AudioError> {
    // Pitfall 1: COM must be initialised on *this* thread.
    wasapi::initialize_mta()
        .ok()
        .map_err(|e| AudioError::Stream(format!("COM init failed: {e}")))?;

    let mut pipeline: Option<CapturePipeline> = None;
    let mut failures: u32 = 0;
    let mut first_attempt = true;

    loop {
        if stop.load(Ordering::SeqCst) {
            return Ok(());
        }

        let session = match Session::open(device) {
            Ok(s) => s,
            Err(e) => {
                if first_attempt {
                    // Startup failure: `start()` reports it, no background retry.
                    if let Some(tx) = ready.take() {
                        let _ = tx.try_send(Err(e.clone()));
                    }
                    return Err(e);
                }
                failures += 1;
                events.push(CaptureEvent::Warning {
                    message: format!(
                        "loopback reopen failed ({failures}/{MAX_SESSION_FAILURES}): {e}"
                    ),
                });
                if failures >= MAX_SESSION_FAILURES {
                    return Err(e);
                }
                thread::sleep(RETRY_BACKOFF);
                continue;
            }
        };

        first_attempt = false;
        if let Some(tx) = ready.take() {
            let _ = tx.try_send(Ok(()));
        }

        // Keep seq/ts monotonic across rebuilds; only the resampler is retargeted.
        match pipeline.as_mut() {
            Some(p) => p.set_input_rate(session.rate)?,
            None => pipeline = Some(CapturePipeline::new(session.rate, frames.clone())?),
        }
        let pipeline = pipeline.as_mut().expect("pipeline set above");

        match session.pump(pipeline, &events, stop) {
            Ok(SessionEnd::Stopped) => return Ok(()),
            Ok(SessionEnd::DeviceChanged) => {
                let old = lock(device).clone();
                let new = default_render_name().unwrap_or_else(|_| "unknown".to_string());
                events.push(CaptureEvent::DeviceChanged {
                    old,
                    new: new.clone(),
                });
                events.push(CaptureEvent::StreamRebuilt { device: new });
                failures = 0;
            }
            Err(e) => {
                failures += 1;
                events.push(CaptureEvent::Warning {
                    message: format!(
                        "loopback session failed ({failures}/{MAX_SESSION_FAILURES}): {e}"
                    ),
                });
                if failures >= MAX_SESSION_FAILURES {
                    return Err(e);
                }
                thread::sleep(RETRY_BACKOFF);
            }
        }

        let mut s = lock(stats);
        s.frames_emitted = pipeline.frames_emitted();
        s.frames_dropped = frames.dropped();
    }
}

/// Friendly name of the current default render device (fresh enumerator; COM is
/// already initialised on this thread).
fn default_render_name() -> Result<String, AudioError> {
    let enumerator = DeviceEnumerator::new().map_err(werr)?;
    let device = enumerator
        .get_default_device(&Direction::Render)
        .map_err(werr)?;
    Ok(device
        .get_friendlyname()
        .unwrap_or_else(|_| "unknown".to_string()))
}

/// Build an unstarted loopback source. Kept as a free function so callers do not
/// need to name the concrete type (mirrors `cpal_mic::open_default_mic`).
pub fn open_default_loopback() -> WasapiLoopback {
    WasapiLoopback::new()
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Format plumbing is device-independent and is the part most likely to
    /// regress silently: 5.1 f32 interleaved → mono at the device rate.
    #[test]
    fn decode_multichannel_mixdown() {
        let frames = 3usize;
        let channels = 6u16;
        let mut bytes = Vec::new();
        for f in 0..frames {
            for c in 0..channels as usize {
                let v = (f * 10 + c) as f32 / 100.0;
                bytes.extend_from_slice(&v.to_le_bytes());
            }
        }
        let mono = decode_interleaved_f32_le_to_mono(&bytes, channels);
        assert_eq!(mono.len(), frames);
        for (f, m) in mono.iter().enumerate() {
            let expected = (0..channels as usize)
                .map(|c| (f * 10 + c) as f32 / 100.0)
                .sum::<f32>()
                / channels as f32;
            assert!((m - expected).abs() < 1e-6);
        }
    }

    #[test]
    fn new_source_is_idle_and_reports_no_device_yet() {
        let s = open_default_loopback();
        assert_eq!(s.stats(), CaptureStats::default());
        assert_eq!(s.current_device(), "");
    }
}
