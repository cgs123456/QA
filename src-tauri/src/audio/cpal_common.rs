//! Shared cpal capture core: microphone and (Linux) monitor are the same
//! pipeline with a different device-selection rule.
//!
//! Threading contract (R13): the cpal data callback runs on a backend thread and
//! must never block. It therefore does exactly one thing — downmix its slice to
//! mono and push it into a bounded drop-oldest queue — while a separate pump
//! thread owns the resampler and the frame queue.
//!
//! `cpal::Stream` is `!Send`, so the stream is built, played and dropped
//! entirely on the pump thread; nothing cpal-typed ever crosses a thread
//! boundary.
//!
//! Startup failure is reported synchronously through `start()`'s return value
//! via a one-shot readiness channel, so the shell can render the degraded page
//! immediately instead of waiting for an event.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc;
use std::sync::{Arc, Mutex, MutexGuard};
use std::thread::{self, JoinHandle};
use std::time::Duration;

use cpal::traits::{DeviceTrait, HostTrait, StreamTrait};
use cpal::{FromSample, SampleFormat, SizedSample};

use super::loopback::{
    AudioError, CaptureEvent, CapturePipeline, CaptureStats, DropOldestQueue, EventQueue,
    FrameQueue, LoopbackSource,
};

/// Raw mono chunks awaiting resampling. A typical callback period is 10 ms, so
/// this is ≈0.3 s of slack for the pump thread to fall behind by.
const CHUNK_QUEUE_CAPACITY: usize = 32;
const PUMP_TICK: Duration = Duration::from_millis(100);
const STREAM_BUILD_TIMEOUT: Duration = Duration::from_secs(5);
/// How long `start()` waits for the stream to actually be running.
const START_TIMEOUT: Duration = Duration::from_secs(8);

fn lock<T>(m: &Mutex<T>) -> MutexGuard<'_, T> {
    m.lock().unwrap_or_else(|e| e.into_inner())
}

/// Which cpal input device to open.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CpalTarget {
    /// The host's default input device (microphone).
    DefaultInput,
    /// The monitor of the default output sink (Linux/PulseAudio). Prefers
    /// `<default sink>.monitor`, falls back to the first monitor source.
    DefaultSinkMonitor,
    /// A specific device, matched by exact `Device::name()`.
    Named(String),
}

/// Shared handles the capture thread needs.
struct Ctx {
    frames: Arc<FrameQueue>,
    events: Arc<EventQueue>,
    stop: Arc<AtomicBool>,
    stats: Arc<Mutex<CaptureStats>>,
    device: Arc<Mutex<String>>,
}

/// cpal-backed capture source (mic on every platform, monitor on Linux).
pub struct CpalCapture {
    target: CpalTarget,
    label: &'static str,
    handle: Option<JoinHandle<()>>,
    stop_flag: Arc<AtomicBool>,
    device: Arc<Mutex<String>>,
    stats: Arc<Mutex<CaptureStats>>,
}

impl CpalCapture {
    pub fn new(target: CpalTarget, label: &'static str) -> Self {
        Self {
            target,
            label,
            handle: None,
            stop_flag: Arc::new(AtomicBool::new(false)),
            device: Arc::new(Mutex::new(String::new())),
            stats: Arc::new(Mutex::new(CaptureStats::default())),
        }
    }
}

impl Drop for CpalCapture {
    fn drop(&mut self) {
        self.stop();
    }
}

impl LoopbackSource for CpalCapture {
    fn start(
        &mut self,
        frames: Arc<FrameQueue>,
        events: Arc<EventQueue>,
    ) -> Result<(), AudioError> {
        if self.handle.is_some() {
            return Err(AudioError::Stream("already running".into()));
        }
        self.stop_flag.store(false, Ordering::SeqCst);
        let target = self.target.clone();
        let label = self.label;
        let ctx = Ctx {
            frames,
            events,
            stop: self.stop_flag.clone(),
            stats: self.stats.clone(),
            device: self.device.clone(),
        };
        let reporter = ctx.events.clone();
        let (ready_tx, ready_rx) = mpsc::sync_channel::<Result<(), AudioError>>(1);

        let handle = thread::Builder::new()
            .name(format!("cpal-{label}"))
            .spawn(move || {
                let mut ready = Some(ready_tx);
                if let Err(e) = run(target, label, &ctx, &mut ready) {
                    if let Some(tx) = ready.take() {
                        let _ = tx.try_send(Err(e.clone()));
                    }
                    reporter.push(CaptureEvent::Error {
                        message: format!("{label}: {e}"),
                    });
                }
                reporter.push(CaptureEvent::Stopped);
            })
            .map_err(|e| AudioError::Stream(format!("spawn capture thread: {e}")))?;

        self.handle = Some(handle);
        match ready_rx.recv_timeout(START_TIMEOUT) {
            Ok(Ok(())) => Ok(()),
            Ok(Err(e)) => {
                if let Some(h) = self.handle.take() {
                    let _ = h.join();
                }
                Err(e)
            }
            Err(_) => {
                self.stop();
                Err(AudioError::Stream(format!(
                    "{label} capture did not start in time"
                )))
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

/// Capture thread body. cpal streams are not rebuilt on device change in Phase
/// 1b (only Windows loopback watches for that); a dying stream surfaces as an
/// error event so the UI can tell the user to re-pick a device.
fn run(
    target: CpalTarget,
    label: &'static str,
    ctx: &Ctx,
    ready: &mut Option<mpsc::SyncSender<Result<(), AudioError>>>,
) -> Result<(), AudioError> {
    let session = match Session::open(&target, ctx) {
        Ok(s) => s,
        Err(e) => {
            if let Some(tx) = ready.take() {
                let _ = tx.try_send(Err(e.clone()));
            }
            return Err(e);
        }
    };
    if let Some(tx) = ready.take() {
        let _ = tx.try_send(Ok(()));
    }
    let _ = label;
    let mut session = session;
    session.pump(ctx)
}

/// One open input stream plus everything that must stay alive with it.
struct Session {
    /// Kept alive here; dropping it stops capture. `cpal::Stream` is `!Send`,
    /// which is exactly why this type never leaves the pump thread.
    _stream: cpal::Stream,
    pipeline: CapturePipeline,
    chunks: Arc<DropOldestQueue<Vec<f32>>>,
    stream_errors: Arc<EventQueue>,
}

impl Session {
    fn open(target: &CpalTarget, ctx: &Ctx) -> Result<Self, AudioError> {
        let host = cpal::default_host();
        let dev = resolve_device(&host, target)?;
        let name = device_name(&dev);
        *lock(&ctx.device) = name.clone();

        let supported = pick_config(&dev)?;
        let format = supported.sample_format();
        let config = supported.config();
        let rate = config.sample_rate;
        let channels = config.channels;

        let chunks: Arc<DropOldestQueue<Vec<f32>>> = DropOldestQueue::new(CHUNK_QUEUE_CAPACITY);
        let stream_errors = EventQueue::new(super::loopback::EVENT_QUEUE_CAPACITY);
        let stream = build_stream(
            &dev,
            format,
            config,
            channels,
            chunks.clone(),
            stream_errors.clone(),
        )?;
        stream
            .play()
            .map_err(|e| AudioError::Stream(format!("stream.play: {e}")))?;

        let pipeline = CapturePipeline::new(rate, ctx.frames.clone())?;
        ctx.events.push(CaptureEvent::Started {
            device: name,
            sample_rate: rate,
            channels,
        });

        Ok(Self {
            _stream: stream,
            pipeline,
            chunks,
            stream_errors,
        })
    }

    fn pump(&mut self, ctx: &Ctx) -> Result<(), AudioError> {
        while !ctx.stop.load(Ordering::SeqCst) {
            if let Some(chunk) = self.chunks.pop_timeout(PUMP_TICK) {
                self.pipeline.push(&chunk)?;
                let mut s = lock(&ctx.stats);
                s.frames_emitted = self.pipeline.frames_emitted();
                s.frames_dropped = self.chunks.dropped();
            }
            while let Some(ev) = self.stream_errors.try_pop() {
                ctx.events.push(ev);
            }
        }
        Ok(())
    }
}

/// cpal 0.18 exposes device metadata through `description()` rather than a bare
/// `name()`; collapse that to the friendly name the rest of the pipeline uses.
fn device_name(dev: &cpal::Device) -> String {
    dev.description()
        .map(|d| d.name().to_string())
        .unwrap_or_else(|_| "unknown".to_string())
}

/// Resolve the requested target to a concrete input device.
fn resolve_device(host: &cpal::Host, target: &CpalTarget) -> Result<cpal::Device, AudioError> {
    let no_devices = |e: cpal::Error| AudioError::NoDevice(e.to_string());
    match target {
        CpalTarget::DefaultInput => host
            .default_input_device()
            .ok_or_else(|| AudioError::NoDevice("no default input device".to_string())),
        CpalTarget::Named(want) => host
            .input_devices()
            .map_err(no_devices)?
            .find(|d| device_name(d) == want.as_str())
            .ok_or_else(|| AudioError::NoDevice(format!("no input device named {want}"))),
        CpalTarget::DefaultSinkMonitor => {
            // PulseAudio names a sink's monitor "<sink>.monitor"; prefer the one
            // belonging to the current default sink.
            let want = host
                .default_output_device()
                .map(|d| format!("{}.monitor", device_name(&d)));
            let mut fallback: Option<cpal::Device> = None;
            for dev in host.input_devices().map_err(no_devices)? {
                let name = device_name(&dev);
                if !name.to_lowercase().contains("monitor") {
                    continue;
                }
                if want.as_deref() == Some(name.as_str()) {
                    return Ok(dev);
                }
                if fallback.is_none() {
                    fallback = Some(dev);
                }
            }
            fallback.ok_or_else(|| {
                AudioError::NoDevice("no monitor source found (is PulseAudio running?)".to_string())
            })
        }
    }
}

/// Device default config, falling back to the first supported range (monitor
/// sources often advertise no default).
fn pick_config(dev: &cpal::Device) -> Result<cpal::SupportedStreamConfig, AudioError> {
    if let Ok(cfg) = dev.default_input_config() {
        return Ok(cfg);
    }
    let mut ranges = dev
        .supported_input_configs()
        .map_err(|e| AudioError::NoDevice(e.to_string()))?;
    match ranges.next() {
        Some(r) => Ok(r
            .try_with_standard_sample_rate()
            .unwrap_or_else(|| r.with_max_sample_rate())),
        None => Err(AudioError::NoDevice(
            "device reports no input configs".to_string(),
        )),
    }
}

/// Dispatch on the device's sample format. Formats we cannot express as f32
/// (DSD streams) fail loudly at build time rather than silently emitting noise.
fn build_stream(
    dev: &cpal::Device,
    format: SampleFormat,
    config: cpal::StreamConfig,
    channels: u16,
    chunks: Arc<DropOldestQueue<Vec<f32>>>,
    errors: Arc<EventQueue>,
) -> Result<cpal::Stream, AudioError> {
    macro_rules! arm {
        ($t:ty) => {
            build_stream_typed::<$t>(dev, config, channels, chunks, errors)
        };
    }
    match format {
        SampleFormat::F32 => arm!(f32),
        SampleFormat::F64 => arm!(f64),
        SampleFormat::I8 => arm!(i8),
        SampleFormat::I16 => arm!(i16),
        SampleFormat::I24 => arm!(cpal::I24),
        SampleFormat::I32 => arm!(i32),
        SampleFormat::I64 => arm!(i64),
        SampleFormat::U8 => arm!(u8),
        SampleFormat::U16 => arm!(u16),
        SampleFormat::U24 => arm!(cpal::U24),
        SampleFormat::U32 => arm!(u32),
        SampleFormat::U64 => arm!(u64),
        other => Err(AudioError::Unsupported(format!(
            "input sample format {other:?}"
        ))),
    }
}

fn build_stream_typed<T>(
    dev: &cpal::Device,
    config: cpal::StreamConfig,
    channels: u16,
    chunks: Arc<DropOldestQueue<Vec<f32>>>,
    errors: Arc<EventQueue>,
) -> Result<cpal::Stream, AudioError>
where
    T: SizedSample,
    f32: FromSample<T>,
{
    dev.build_input_stream(
        config,
        move |data: &[T], _info: &cpal::InputCallbackInfo| {
            // R13: no blocking, no waiting — a full queue evicts its oldest chunk.
            let mono = downmix_to_mono(data, channels);
            if !mono.is_empty() {
                chunks.push(mono);
            }
        },
        move |err| {
            errors.push(CaptureEvent::Error {
                message: format!("input stream error: {err}"),
            });
        },
        Some(STREAM_BUILD_TIMEOUT),
    )
    .map_err(|e| AudioError::Stream(format!("build_input_stream: {e}")))
}

/// Interleaved `T` (any channel count) → mono f32 via the channel mean.
/// A trailing partial frame is ignored rather than panicking (capture thread).
pub fn downmix_to_mono<T>(data: &[T], channels: u16) -> Vec<f32>
where
    T: SizedSample,
    f32: FromSample<T>,
{
    if channels == 0 {
        return Vec::new();
    }
    let ch = channels as usize;
    if ch == 1 {
        return data.iter().map(|s| f32::from_sample_(*s)).collect();
    }
    data.chunks_exact(ch)
        .map(|frame| frame.iter().map(|s| f32::from_sample_(*s)).sum::<f32>() / channels as f32)
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn downmix_normalises_every_sample_format() {
        // i16 extremes must land on ±1.0, not on raw integer magnitudes.
        assert_eq!(downmix_to_mono::<i16>(&[i16::MIN], 1)[0], -1.0);
        assert!((downmix_to_mono::<i16>(&[i16::MAX], 1)[0] - 1.0).abs() < 1e-4);
        // f32 passes through untouched.
        assert_eq!(downmix_to_mono::<f32>(&[0.5, -0.5], 1), vec![0.5, -0.5]);
        // u8 is centred on 128.
        assert_eq!(downmix_to_mono::<u8>(&[128], 1)[0], 0.0);
    }

    #[test]
    fn downmix_is_the_channel_mean_and_ignores_ragged_tails() {
        let stereo = [1.0f32, -1.0, 0.5, 0.5];
        assert_eq!(downmix_to_mono(&stereo, 2), vec![0.0, 0.5]);
        let six = [0.6f32, 0.0, 0.0, 0.0, 0.0, 0.0];
        assert!((downmix_to_mono(&six, 6)[0] - 0.1).abs() < 1e-6);
        // ragged tail: 3 samples as stereo → 1 frame, no panic
        assert_eq!(downmix_to_mono(&[1.0f32, 1.0, 1.0], 2).len(), 1);
        assert!(downmix_to_mono::<f32>(&[1.0], 0).is_empty());
    }

    #[test]
    fn new_source_is_idle() {
        let cap = CpalCapture::new(CpalTarget::DefaultInput, "mic");
        assert_eq!(cap.stats(), CaptureStats::default());
        assert_eq!(cap.current_device(), "");
    }
}
