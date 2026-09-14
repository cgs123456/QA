//! Capture-source contract shared by every backend, plus the bounded queues.
//!
//! R13 (Rust side): capture must never block the audio thread. Frames leave the
//! capture context through [`FrameQueue`] — a bounded queue that drops the
//! OLDEST frame (and counts it) rather than blocking or growing. Diagnostics
//! flow the other way on an [`EventQueue`] with the same policy, so both
//! directions are bounded and neither side can stall the other.
//!
//! Why not `std::sync::mpsc`: a `SyncSender` can only reject the *newest* item
//! when full (`try_send`), and the sender cannot evict the head — "drop oldest"
//! is not expressible. The queue below is the smallest construct that satisfies
//! R13 exactly.

use std::collections::VecDeque;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Condvar, Mutex, MutexGuard};
use std::time::Duration;

use super::frame::Frame16k;

/// ≈1.9 s of 30 ms frames. Deep enough to ride out a scheduler hiccup or a
/// transient consumer stall, shallow enough that the ASR path never falls far
/// behind real time before the drop policy kicks in.
pub const FRAME_QUEUE_CAPACITY: usize = 64;
/// Diagnostics are advisory and low-rate; a shallow queue is plenty.
pub const EVENT_QUEUE_CAPACITY: usize = 32;

/// Poison-tolerant lock: a panicking producer must not wedge the consumer
/// (and vice versa). Capture runs on its own thread, so a poisoned mutex would
/// otherwise silently stop the whole pipeline.
fn lock<T>(m: &Mutex<T>) -> MutexGuard<'_, T> {
    m.lock().unwrap_or_else(|e| e.into_inner())
}

/// Diagnostic / lifecycle events flowing opposite the audio (to UI/logs).
///
/// R14: every `message` here is content-free — device names, error kinds and
/// counters only, never transcript text.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CaptureEvent {
    Started {
        device: String,
        sample_rate: u32,
        channels: u16,
    },
    DeviceChanged {
        old: String,
        new: String,
    },
    StreamRebuilt {
        device: String,
    },
    Warning {
        message: String,
    },
    Error {
        message: String,
    },
    Stopped,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum AudioError {
    NoDevice(String),
    Stream(String),
    Resample(String),
    Unsupported(String),
}

impl std::fmt::Display for AudioError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            AudioError::NoDevice(e) => write!(f, "no audio device: {e}"),
            AudioError::Stream(e) => write!(f, "stream error: {e}"),
            AudioError::Resample(e) => write!(f, "resample error: {e}"),
            AudioError::Unsupported(e) => write!(f, "unsupported: {e}"),
        }
    }
}

impl std::error::Error for AudioError {}

/// Counters a capture source exposes to the shell (R13 observability).
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct CaptureStats {
    /// Frames handed to the consumer (monotonic, wraps never).
    pub frames_emitted: u64,
    /// Frames evicted by the drop-oldest policy.
    pub frames_dropped: u64,
}

/// Bounded, single-consumer queue that never blocks the producer.
///
/// `push` evicts the head when full and counts the eviction; `pop_*` are the
/// consumer side. Both ends tolerate a poisoned mutex.
pub struct DropOldestQueue<T> {
    inner: Mutex<VecDeque<T>>,
    cap: usize,
    pushed: AtomicU64,
    dropped: AtomicU64,
    ready: Condvar,
}

impl<T> DropOldestQueue<T> {
    pub fn new(cap: usize) -> Arc<Self> {
        assert!(cap > 0, "queue capacity must be > 0");
        Arc::new(Self {
            inner: Mutex::new(VecDeque::with_capacity(cap)),
            cap,
            pushed: AtomicU64::new(0),
            dropped: AtomicU64::new(0),
            ready: Condvar::new(),
        })
    }

    /// Never blocks and never grows past `cap`: when full the OLDEST item is
    /// evicted and counted (R13).
    pub fn push(&self, item: T) {
        {
            let mut q = lock(&self.inner);
            if q.len() >= self.cap {
                q.pop_front();
                self.dropped.fetch_add(1, Ordering::Relaxed);
            }
            q.push_back(item);
        }
        self.pushed.fetch_add(1, Ordering::Relaxed);
        self.ready.notify_one();
    }

    /// Non-blocking pop.
    pub fn try_pop(&self) -> Option<T> {
        lock(&self.inner).pop_front()
    }

    /// Wait up to `timeout` for an item.
    pub fn pop_timeout(&self, timeout: Duration) -> Option<T> {
        let mut q = lock(&self.inner);
        if q.is_empty() {
            let (guard, _) = self
                .ready
                .wait_timeout(q, timeout)
                .unwrap_or_else(|e| e.into_inner());
            q = guard;
        }
        q.pop_front()
    }

    pub fn len(&self) -> usize {
        lock(&self.inner).len()
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }

    pub fn capacity(&self) -> usize {
        self.cap
    }

    /// Total items accepted (including any later evicted).
    pub fn pushed(&self) -> u64 {
        self.pushed.load(Ordering::Relaxed)
    }

    pub fn dropped(&self) -> u64 {
        self.dropped.load(Ordering::Relaxed)
    }
}

/// Frame transport between a capture source and its consumer.
pub type FrameQueue = DropOldestQueue<Frame16k>;
/// Diagnostic transport between a capture source and the shell.
pub type EventQueue = DropOldestQueue<CaptureEvent>;

/// One capture source (system loopback, monitor, or microphone).
///
/// `start` spawns the capture context (its own thread / stream) and returns
/// immediately; frames arrive on `frames`, diagnostics on `events`. `stop` is
/// idempotent and joins the capture thread. Both queues are bounded with
/// drop-oldest semantics; see [`DropOldestQueue`].
pub trait LoopbackSource: Send {
    fn start(&mut self, frames: Arc<FrameQueue>, events: Arc<EventQueue>)
        -> Result<(), AudioError>;
    fn stop(&mut self);
    fn current_device(&self) -> String;
    fn stats(&self) -> CaptureStats;
}

/// mono native-rate f32 → 16 kHz 480-sample frames → [`FrameQueue`].
///
/// Owns the resampler and the per-source `seq`/`ts` counters so that every
/// backend emits byte-identical framing:
///
/// - `seq` increments once per emitted frame (R10 shares one sequence space
///   with event frames; the caller keeps counting from here).
/// - `ts_ms` is **sample-count derived** (`frames * 30`), never wall-clock.
///   This is what turns the 10-minute drift budget into a construction
///   property: timestamps cannot drift, only the sample count can, and the
///   resampler conserves samples (see `resample.rs` tests).
pub struct CapturePipeline {
    resampler: super::resample::MonoResampler16k,
    queue: Arc<FrameQueue>,
    seq: u32,
    frames: u64,
}

impl CapturePipeline {
    pub fn new(input_rate: u32, queue: Arc<FrameQueue>) -> Result<Self, AudioError> {
        Ok(Self {
            resampler: super::resample::MonoResampler16k::new(input_rate)?,
            queue,
            seq: 0,
            frames: 0,
        })
    }

    pub fn input_rate(&self) -> u32 {
        self.resampler.input_rate()
    }

    /// Frames emitted since construction.
    pub fn frames_emitted(&self) -> u64 {
        self.frames
    }

    /// Timestamp the next emitted frame will carry (ms since capture start).
    pub fn next_ts_ms(&self) -> u64 {
        self.frames * super::frame::FRAME_MS
    }

    /// Retarget the resampler when the capture device changed its native rate.
    ///
    /// `seq`/`frames` are deliberately **not** reset: a device swap mid-session
    /// must not restart the wire sequence space (R10) or the timestamp clock.
    /// Samples still buffered inside the old resampler are dropped — at most
    /// one 30 ms frame of audio, which is the honest cost of a device swap.
    pub fn set_input_rate(&mut self, input_rate: u32) -> Result<(), AudioError> {
        if self.resampler.input_rate() == input_rate {
            return Ok(());
        }
        self.resampler = super::resample::MonoResampler16k::new(input_rate)?;
        Ok(())
    }

    /// Push mono samples at the native rate; returns how many 30 ms frames
    /// became available. Never blocks (the queue evicts instead).
    pub fn push(&mut self, mono: &[f32]) -> Result<usize, AudioError> {
        let ready = self.resampler.push(mono)?;
        let n = ready.len();
        for pcm in ready {
            let frame = Frame16k::from_f32(self.seq, self.next_ts_ms(), pcm);
            self.queue.push(frame);
            self.seq = self.seq.wrapping_add(1);
            self.frames += 1;
        }
        Ok(n)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn full_queue_evicts_oldest_and_counts() {
        let q = DropOldestQueue::new(2);
        for i in 0..5 {
            q.push(i);
        }
        assert_eq!(q.dropped(), 3);
        assert_eq!(q.pushed(), 5);
        assert_eq!(q.len(), 2);
        assert_eq!(q.try_pop(), Some(3));
        assert_eq!(q.try_pop(), Some(4));
        assert_eq!(q.try_pop(), None);
    }

    #[test]
    fn push_never_blocks_when_full() {
        // 100k pushes into a 2-slot queue must return immediately; the point of
        // R13 is that the audio thread can never be parked by the consumer.
        let q = DropOldestQueue::new(2);
        for i in 0..100_000u32 {
            q.push(i);
        }
        assert_eq!(q.dropped(), 99_998);
        assert_eq!(q.try_pop(), Some(99_998));
    }

    #[test]
    fn pop_timeout_returns_none_when_idle() {
        let q: Arc<DropOldestQueue<u8>> = DropOldestQueue::new(4);
        let t0 = std::time::Instant::now();
        assert_eq!(q.pop_timeout(Duration::from_millis(20)), None);
        assert!(t0.elapsed() >= Duration::from_millis(15));
    }

    #[test]
    fn pop_timeout_wakes_on_push() {
        let q: Arc<DropOldestQueue<u8>> = DropOldestQueue::new(4);
        let producer = q.clone();
        let t = std::thread::spawn(move || {
            std::thread::sleep(Duration::from_millis(20));
            producer.push(7);
        });
        assert_eq!(q.pop_timeout(Duration::from_secs(2)), Some(7));
        t.join().unwrap();
    }

    #[test]
    fn poisoned_queue_still_usable() {
        // A panicking consumer must not wedge the producer.
        let q: Arc<DropOldestQueue<u8>> = DropOldestQueue::new(4);
        let q2 = q.clone();
        let _ = std::thread::spawn(move || {
            let _g = lock(&q2.inner);
            panic!("simulated consumer panic");
        })
        .join();
        q.push(1);
        assert_eq!(q.try_pop(), Some(1));
    }

    #[test]
    fn pipeline_frames_are_sequenced_and_timestamped_by_sample_count() {
        let q = FrameQueue::new(FRAME_QUEUE_CAPACITY);
        let mut p = CapturePipeline::new(16_000, q.clone()).unwrap();
        // 1 s of 16 kHz mono → exactly 33 frames (990 ms), 10 ms carried over.
        let emitted = p.push(&vec![0.25f32; 16_000]).unwrap();
        assert_eq!(emitted, 33);
        assert_eq!(p.frames_emitted(), 33);
        assert_eq!(p.next_ts_ms(), 990);
        for i in 0..33u32 {
            let f = q.try_pop().unwrap();
            assert_eq!(f.seq, i);
            assert_eq!(f.ts_ms, i as u64 * 30);
            assert_eq!(f.pcm_i16[0], 8192); // 0.25 * 32768
        }
        assert!(q.is_empty());
    }

    #[test]
    fn pipeline_ten_minute_timestamp_budget() {
        // DoD: 10 min of audio, |ts drift| < 50 ms. With sample-count-derived
        // stamps the only error term is the final frame's own 30 ms span.
        // The consumer drains as we go, so this also exercises the happy path
        // of the bounded queue (no drops when the consumer keeps up). The queue
        // must hold one push's worth of frames (1 s ≈ 33 frames) or the drop
        // policy would fire legitimately.
        let q = FrameQueue::new(64);
        let mut p = CapturePipeline::new(16_000, q.clone()).unwrap();
        let chunk = vec![0.0f32; 16_000];
        let mut consumed = 0u64;
        for _ in 0..600 {
            p.push(&chunk).unwrap();
            while q.try_pop().is_some() {
                consumed += 1;
            }
        }
        let frames = p.frames_emitted();
        assert_eq!(frames, 20_000, "expected exactly 10 min of 30 ms frames");
        assert_eq!(consumed, frames);
        assert_eq!(q.dropped(), 0);
        let last_ts = (frames - 1) * 30;
        let drift_ms = 600_000u64.abs_diff(last_ts);
        assert!(drift_ms <= 30, "ts drift {drift_ms} ms exceeds budget");
    }

    #[test]
    fn pipeline_drop_policy_survives_consumer_stall() {
        let q = FrameQueue::new(4);
        let mut p = CapturePipeline::new(16_000, q.clone()).unwrap();
        // 10 s of audio with nobody consuming: bounded, and the newest survive.
        for _ in 0..10 {
            p.push(&vec![0.0f32; 16_000]).unwrap();
        }
        assert_eq!(p.frames_emitted(), 333);
        assert_eq!(q.dropped(), 333 - 4);
        assert_eq!(q.try_pop().unwrap().seq, 329);
    }

    #[test]
    fn retargeting_rate_keeps_seq_and_timestamps_monotonic() {
        // A device swap (e.g. headphones plugged in) changes the native rate
        // mid-session; the wire sequence space and the ts clock must survive it.
        let q = FrameQueue::new(64);
        let mut p = CapturePipeline::new(16_000, q.clone()).unwrap();
        p.push(&vec![0.0f32; 480]).unwrap();
        assert_eq!(p.frames_emitted(), 1);

        p.set_input_rate(48_000).unwrap();
        assert_eq!(p.input_rate(), 48_000);
        p.push(&vec![0.0f32; 1440]).unwrap();
        assert_eq!(p.frames_emitted(), 2);
        assert_eq!(p.next_ts_ms(), 60);

        let a = q.try_pop().unwrap();
        let b = q.try_pop().unwrap();
        assert_eq!((a.seq, a.ts_ms), (0, 0));
        assert_eq!((b.seq, b.ts_ms), (1, 30));
        // Same-rate retarget is a no-op, not a resampler reset.
        p.set_input_rate(48_000).unwrap();
        assert_eq!(p.frames_emitted(), 2);
    }
}
