//! 注入测试台：把 WAV（或生成信号）当作一路**采集源**喂进生产管线。
//!
//! feature = `audio-testharness`，**只给 dev / example**（见 Cargo.toml）。
//!
//! # 为什么它必须是 `LoopbackSource` 而不是"直接往队列塞帧"
//!
//! 因为要验证的是**接线**，不是端点。真实链路里 `CaptureService` 拿到的只有
//! 两样东西：一个有界 [`FrameQueue`] 和一个有界 [`EventQueue`]。只要注入源实现
//! 同一个 trait，它后面的一切 —— 重采样、分帧、`seq`/`ts_ms`、VAD、端点状态机、
//! 上行队列、WS —— 就都是**生产代码**，一句都没被替换掉：
//!
//! ```text
//! SyntheticSource ──CapturePipeline──▶ FrameQueue ──▶ worker（真 VAD/真端点）──▶ uplink ──▶ sidecar
//! ```
//!
//! 所以「合成语音 → 段序列」这条结论才有意义。
//!
//! # R13 对注入源同等生效
//!
//! 它走的是**同一个** [`DropOldestQueue`]，队列满了照样丢最旧并计数。于是有一个
//! 反直觉但正确的推论：**`Pace::Fast` 会真的丢帧**（源瞬间推完 250 帧，worker 每轮
//! 只消费一帧，64 格的队列必然溢出）。这是 R13 在工作，不是 bug —— 所以
//! 「`dropped == 0`」这类断言必须配 [`Pace::Realtime`]，见 `tests/inject_e2e.rs`
//! 的说明。反过来说，`Pace::Fast` 正好是**验证丢帧路径**的工具。
//!
//! # 它谎报了什么（如实列出）
//!
//! - 设备名是注入的，不是真实设备；格式取自 WAV 头（真），设备名不是（假）。
//!   两者都经 `CaptureEvent::Started` 上报，所以诊断面板上看到的名字会写明"注入"。
//! - `Pace::Realtime` 按**墙钟自校正**推帧（对齐到 `step × 30ms`），不累积 sleep
//!   误差；但调度抖动仍然存在 —— 它是"像真的一样慢"，不是"和真设备一样准"。
//!
//! R14：本模块只处理音频样本与设备名，不碰转写文本，也不打印任何载荷。

use std::path::Path;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Arc;
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

use super::frame::{FRAME_MS, FRAME_SAMPLES};
use super::loopback::{
    AudioError, CaptureEvent, CapturePipeline, CaptureStats, EventQueue, FrameQueue, LoopbackSource,
};
use super::resample::downmix_interleaved_f32_to_mono;

/// 注入节拍。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Pace {
    /// 不等墙钟，能推多快推多快。**会触发 R13 丢帧**（见模块注释）——
    /// 适合验证"线上帧序/时间戳/传输"，不适合验证"段边界"。
    Fast,
    /// 按真实 30ms/帧推（墙钟自校正）。自动化测试里想要 `dropped == 0` 就必须用它；
    /// 人工走查（`examples/inject_wav --realtime`）也用它。
    Realtime,
}

impl Pace {
    /// 每帧的目标墙钟间隔（`Fast` 为 `None` = 不等待）。
    pub fn per_frame(self) -> Option<Duration> {
        match self {
            Pace::Fast => None,
            Pace::Realtime => Some(Duration::from_millis(FRAME_MS)),
        }
    }

    /// 人工走查用：是否按真实时间推。
    pub fn is_realtime(self) -> bool {
        self.per_frame().is_some()
    }
}

/// WAV 里读出来的单声道素材。
#[derive(Debug, Clone)]
pub struct WavMaterial {
    /// 原生采样率（交给 [`CapturePipeline`] 去重采样，不在这里动手）。
    pub input_rate: u32,
    /// 原生声道数（已在此处下混成单声道，但格式要如实上报）。
    pub channels: u16,
    /// 单声道样本（长度 = 帧数，不是交织长度）。
    pub samples: Vec<f32>,
}

impl WavMaterial {
    /// 音频时长（ms）。
    pub fn duration_ms(&self) -> u64 {
        if self.input_rate == 0 {
            return 0;
        }
        self.samples.len() as u64 * 1000 / self.input_rate as u64
    }
}

/// 读 WAV → 单声道 f32。
///
/// 支持 16/32-bit PCM 与 32-bit float：注入素材是 SAPI 生成的 16-bit 立体声
/// （`testdata/synth_utterance_zh.json` 记了 sha256），其余格式**明确报错**而不是
/// 猜着读 —— 猜错的后果是"VAD 检不出语音"，那会浪费一整天去查 VAD。
pub fn read_wav(path: &Path) -> Result<WavMaterial, AudioError> {
    let mut reader = hound::WavReader::open(path)
        .map_err(|e| AudioError::Stream(format!("打开 WAV {} 失败: {e}", path.display())))?;
    let spec = reader.spec();
    let decoded: Result<Vec<f32>, hound::Error> = match (spec.sample_format, spec.bits_per_sample) {
        (hound::SampleFormat::Int, 16) => reader
            .samples::<i16>()
            .map(|s| s.map(|v| v as f32 / 32768.0))
            .collect(),
        (hound::SampleFormat::Int, 32) => reader
            .samples::<i32>()
            .map(|s| s.map(|v| v as f32 / 2_147_483_648.0))
            .collect(),
        (hound::SampleFormat::Float, 32) => reader.samples::<f32>().collect(),
        (fmt, bits) => {
            return Err(AudioError::Unsupported(format!(
                "{bits}-bit {fmt:?} WAV 不受支持（只支持 16/32-bit PCM 与 32-bit float）：{}",
                path.display()
            )))
        }
    };
    let interleaved = decoded
        .map_err(|e| AudioError::Stream(format!("读取 WAV {} 失败: {e}", path.display())))?;

    if spec.channels == 0 || spec.sample_rate == 0 {
        return Err(AudioError::Stream(format!(
            "WAV {} 头非法：channels={} sample_rate={}",
            path.display(),
            spec.channels,
            spec.sample_rate
        )));
    }
    let samples = downmix_interleaved_f32_to_mono(&interleaved, spec.channels);
    Ok(WavMaterial {
        input_rate: spec.sample_rate,
        channels: spec.channels,
        samples,
    })
}

/// 注入采集源。
///
/// 生命周期与真实源一致：`start` 起线程并立刻返回，`stop` 幂等并 join。
pub struct SyntheticSource {
    device: String,
    input_rate: u32,
    channels: u16,
    samples: Vec<f32>,
    pace: Pace,
    repeat: bool,
    stop: Arc<AtomicBool>,
    emitted: Arc<AtomicU64>,
    /// 来自真实 `FrameQueue::dropped()`（R13 对注入源同等生效）。
    dropped: Arc<AtomicU64>,
    queue: Option<Arc<FrameQueue>>,
    thread: Option<JoinHandle<()>>,
}

impl SyntheticSource {
    /// 从单声道素材构造（`samples` 是**单声道**，长度 = 帧数）。
    pub fn from_samples(
        device: impl Into<String>,
        input_rate: u32,
        channels: u16,
        samples: Vec<f32>,
    ) -> Self {
        Self {
            device: device.into(),
            input_rate,
            channels,
            samples,
            pace: Pace::Realtime,
            repeat: false,
            stop: Arc::new(AtomicBool::new(false)),
            emitted: Arc::new(AtomicU64::new(0)),
            dropped: Arc::new(AtomicU64::new(0)),
            queue: None,
            thread: None,
        }
    }

    /// 从 WAV 文件构造。设备名写明"注入"，避免在诊断面板上冒充真实设备。
    pub fn from_wav(path: &Path) -> Result<Self, AudioError> {
        let material = read_wav(path)?;
        let name = path
            .file_name()
            .map(|n| n.to_string_lossy().to_string())
            .unwrap_or_else(|| path.display().to_string());
        Ok(Self::from_samples(
            format!("注入 WAV ({name})"),
            material.input_rate,
            material.channels,
            material.samples,
        ))
    }

    /// 生成信号：`freq_hz` 正弦 + 首尾静音，用于"另一路"的注入素材。
    ///
    /// 用正弦而不是类语音信号是刻意的：这里的用途是**证明两路互不污染**
    /// （内容不同即可），不是证明 VAD 能检出语音。
    pub fn tone(freq_hz: f64, seconds: f64, lead_silence_ms: u64, tail_silence_ms: u64) -> Self {
        let rate = 16_000u32;
        let lead = (rate as u64 * lead_silence_ms / 1000) as usize;
        let tail = (rate as u64 * tail_silence_ms / 1000) as usize;
        let body = (rate as f64 * seconds) as usize;
        let mut samples = Vec::with_capacity(lead + body + tail);
        samples.extend(std::iter::repeat_n(0.0f32, lead));
        for i in 0..body {
            let t = i as f64 / rate as f64;
            let v = (2.0 * std::f64::consts::PI * freq_hz * t).sin() * 0.4;
            samples.push(v as f32);
        }
        samples.extend(std::iter::repeat_n(0.0f32, tail));
        Self::from_samples("注入信号 (tone)", rate, 1, samples)
    }

    pub fn with_pace(mut self, pace: Pace) -> Self {
        self.pace = pace;
        self
    }

    /// 播完是否从头再来（等 sidecar 起来时用）。
    pub fn with_repeat(mut self, repeat: bool) -> Self {
        self.repeat = repeat;
        self
    }

    /// 素材帧数（原生率下，仅供参考；上线帧数由 `CapturePipeline` 决定）。
    pub fn sample_count(&self) -> usize {
        self.samples.len()
    }
}

impl std::fmt::Debug for SyntheticSource {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("SyntheticSource")
            .field("device", &self.device)
            .field("input_rate", &self.input_rate)
            .field("channels", &self.channels)
            .field("samples", &self.samples.len())
            .field("pace", &self.pace)
            .finish_non_exhaustive()
    }
}

/// 每步推进的原生样本数（≈30ms）。
///
/// 非整数倍率（22050Hz → 661.5）向下取整，靠 `Pace::Realtime` 的**墙钟自校正**
/// 保证节拍不漂：步长只影响单步粒度，不影响每秒帧数。
fn chunk_samples(input_rate: u32) -> usize {
    ((input_rate as u64 * FRAME_MS / 1000).max(1)) as usize
}

impl LoopbackSource for SyntheticSource {
    fn start(
        &mut self,
        frames: Arc<FrameQueue>,
        events: Arc<EventQueue>,
    ) -> Result<(), AudioError> {
        // 格式取自素材（真），设备名标明注入（不冒充真实设备）。
        events.push(CaptureEvent::Started {
            device: self.device.clone(),
            sample_rate: self.input_rate,
            channels: self.channels,
        });

        let mut pipeline = CapturePipeline::new(self.input_rate, frames.clone())?;
        let samples = std::mem::take(&mut self.samples);
        let pace = self.pace;
        let repeat = self.repeat;
        let chunk = chunk_samples(self.input_rate);
        let stop = self.stop.clone();
        let emitted = self.emitted.clone();
        let dropped = self.dropped.clone();
        // 线程与 `self` 共用**同一个**队列实例：`stats()` 读的就是它。
        let queue = frames;
        self.queue = Some(queue.clone());

        self.thread = Some(thread::spawn(move || {
            let started = Instant::now();
            let mut cursor = 0usize;
            let mut step: u64 = 0;
            while !stop.load(Ordering::SeqCst) {
                if cursor >= samples.len() {
                    if !repeat {
                        break;
                    }
                    cursor = 0;
                }
                let end = (cursor + chunk).min(samples.len());
                if pipeline.push(&samples[cursor..end]).is_ok() {
                    emitted.store(pipeline.frames_emitted(), Ordering::Relaxed);
                    dropped.store(queue.dropped(), Ordering::Relaxed);
                }
                cursor = end;
                step += 1;
                if stop.load(Ordering::SeqCst) {
                    break;
                }
                if let Some(per) = pace.per_frame() {
                    // 对齐到 `step × per`，不累积 sleep 误差；落后就直接追
                    // （不补睡，否则"越慢越补"会变成雪崩）。
                    let target = started + per * step as u32;
                    let now = Instant::now();
                    if target > now {
                        thread::sleep(target - now);
                    }
                }
            }
        }));
        Ok(())
    }

    fn stop(&mut self) {
        self.stop.store(true, Ordering::SeqCst);
        if let Some(t) = self.thread.take() {
            let _ = t.join();
        }
    }

    fn current_device(&self) -> String {
        self.device.clone()
    }

    fn stats(&self) -> CaptureStats {
        // `errors` 恒为 0：VAD 不在源这一侧，源没有可报的分类错误。
        // 谎报一个非 0 值比报 0 更糟 —— 诊断面板要能区分"源出错"与"worker 出错"。
        CaptureStats {
            frames_emitted: self.emitted.load(Ordering::Relaxed),
            frames_dropped: match &self.queue {
                Some(q) => q.dropped(),
                None => self.dropped.load(Ordering::Relaxed),
            },
            errors: 0,
        }
    }
}

/// 一路注入源的样本总数 → 期望的 16k 帧数（`floor` 语义，与 `CapturePipeline` 一致）。
///
/// 只给测试/示例做"我该期待多少帧"的算术，不参与生产路径。
pub fn expected_frames(samples: usize, input_rate: u32) -> u64 {
    if input_rate == 0 {
        return 0;
    }
    samples as u64 * super::frame::SAMPLE_RATE as u64 / input_rate as u64 / FRAME_SAMPLES as u64
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::audio::loopback::{EVENT_QUEUE_CAPACITY, FRAME_QUEUE_CAPACITY};

    fn queues() -> (Arc<FrameQueue>, Arc<EventQueue>) {
        (
            FrameQueue::new(FRAME_QUEUE_CAPACITY),
            EventQueue::new(EVENT_QUEUE_CAPACITY),
        )
    }

    #[test]
    fn pace_fast_does_not_wait_and_realtime_waits_one_frame() {
        assert_eq!(Pace::Fast.per_frame(), None);
        assert!(!Pace::Fast.is_realtime());
        assert_eq!(
            Pace::Realtime.per_frame(),
            Some(Duration::from_millis(FRAME_MS))
        );
        assert!(Pace::Realtime.is_realtime());
    }

    #[test]
    fn chunk_is_about_thirty_ms_at_any_rate() {
        // 22050 是非整数倍（661.5）—— 取整后步长只差半样本，节拍由墙钟自校正。
        assert_eq!(chunk_samples(16_000), 480);
        assert_eq!(chunk_samples(22_050), 661);
        assert_eq!(chunk_samples(48_000), 1440);
        // 极低采样率不能退化成 0（否则死循环里一步都不推进）。
        assert_eq!(chunk_samples(1), 1);
    }

    #[test]
    fn tone_material_has_the_requested_length_and_real_silence() {
        let src = SyntheticSource::tone(440.0, 0.3, 100, 200);
        // 16k：100ms + 300ms + 200ms = 600ms = 9600 样本
        assert_eq!(src.sample_count(), 9_600);
        assert_eq!(src.current_device(), "注入信号 (tone)");
        assert_eq!(src.stats().frames_emitted, 0);
        assert_eq!(src.stats().errors, 0);
    }

    #[test]
    fn start_reports_the_material_format_and_emits_frames() {
        let (frames, events) = queues();
        // 16k 单声道 3s → 正好 100 帧（480 样本/帧）。刻意**超过**队列容量，
        // 让 R13 的丢最旧在这里也真的发生一次（注入源走的就是同一个队列）。
        let mut src = SyntheticSource::from_samples("注入 (t)", 16_000, 1, vec![0.25f32; 48_000])
            .with_pace(Pace::Fast);
        src.start(frames.clone(), events.clone()).unwrap();

        // 设备/格式经生产同一条事件通道上报。
        let mut saw_started = false;
        while let Some(ev) = events.try_pop() {
            if let CaptureEvent::Started {
                device,
                sample_rate,
                channels,
            } = ev
            {
                assert_eq!(device, "注入 (t)");
                assert_eq!(sample_rate, 16_000);
                assert_eq!(channels, 1);
                saw_started = true;
            }
        }
        assert!(saw_started, "Started 事件必须上报");

        // Fast 会立刻推完并退出线程；等到帧数稳定。
        let deadline = Instant::now() + Duration::from_secs(5);
        while src.stats().frames_emitted < 100 && Instant::now() < deadline {
            thread::sleep(Duration::from_millis(5));
        }
        src.stop();
        assert_eq!(src.stats().frames_emitted, 100);
        // 没人消费 → R13 丢最旧，计数如实来自真实 FrameQueue。
        assert_eq!(frames.dropped(), 100 - FRAME_QUEUE_CAPACITY as u64);
        assert_eq!(src.stats().frames_dropped, frames.dropped());
    }

    #[test]
    fn realtime_pace_keeps_the_queue_from_overflowing() {
        // 0.3s 素材、实时节拍、边推边消费 → 不该丢帧（R13 只在真跟不上时才生效）。
        let (frames, events) = queues();
        let mut src = SyntheticSource::from_samples("注入 (rt)", 16_000, 1, vec![0.1f32; 4_800])
            .with_pace(Pace::Realtime);
        src.start(frames.clone(), events.clone()).unwrap();
        let mut got = 0u64;
        let deadline = Instant::now() + Duration::from_secs(10);
        while got < 10 && Instant::now() < deadline {
            if frames.pop_timeout(Duration::from_millis(200)).is_some() {
                got += 1;
            }
        }
        src.stop();
        assert_eq!(got, 10, "10 帧 = 300ms 素材");
        assert_eq!(frames.dropped(), 0, "实时节拍下不该丢帧");
        assert_eq!(src.stats().frames_dropped, 0);
    }

    #[test]
    fn stop_is_idempotent_and_start_after_stop_needs_a_new_source() {
        let (frames, events) = queues();
        let mut src = SyntheticSource::from_samples("注入 (s)", 16_000, 1, vec![0.0f32; 480])
            .with_pace(Pace::Fast);
        src.start(frames.clone(), events.clone()).unwrap();
        src.stop();
        src.stop(); // 幂等
        assert!(src.current_device().starts_with("注入"));
    }

    #[test]
    fn chunked_feeding_emits_the_same_frame_count_as_one_shot() {
        // 22 050Hz 走真重采样（非整数倍率），分块喂与一次喂必须产出同样多的帧：
        // 分块只该影响粒度，不该丢样本。这条挂了，注入 E2E 的帧数就没有可信基准。
        let n = 22_050 * 3; // 3s
        let samples: Vec<f32> = (0..n).map(|i| (i as f32 * 0.01).sin() * 0.3).collect();

        let one = FrameQueue::new(FRAME_QUEUE_CAPACITY);
        let mut p = CapturePipeline::new(22_050, one.clone()).unwrap();
        p.push(&samples).unwrap();
        let one_shot = p.frames_emitted();
        assert_eq!(one_shot, 100, "3s / 30ms = 100 帧");

        let (frames, events) = queues();
        let mut src =
            SyntheticSource::from_samples("注入 (chunk)", 22_050, 1, samples).with_pace(Pace::Fast);
        src.start(frames.clone(), events.clone()).unwrap();
        let deadline = Instant::now() + Duration::from_secs(10);
        while src.stats().frames_emitted < one_shot && Instant::now() < deadline {
            thread::sleep(Duration::from_millis(5));
        }
        src.stop();
        assert_eq!(
            src.stats().frames_emitted,
            one_shot,
            "分块喂与一次喂必须产出同样多的帧"
        );
    }

    #[test]
    fn the_shipped_synth_wav_feeds_without_sample_loss() {
        // 注入 E2E 的素材：分块喂必须与一次喂产出**同样多**的帧。
        // 注意这是"喂完整个素材"的帧数，不是 E2E 里观察到的帧数 ——
        // E2E 在第二段封口后就 `stop()` 了，源线程会停在素材尾部之前
        // （尾部静音没被推完），所以观察值必然更小。两者不可混为一谈。
        let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .unwrap()
            .join("testdata")
            .join("synth_utterance_zh.wav");
        if !path.is_file() {
            eprintln!("SKIP: {} not found", path.display());
            return;
        }
        let material = read_wav(&path).unwrap();
        assert_eq!(material.input_rate, 22_050);
        assert_eq!(material.channels, 1);
        assert_eq!(material.samples.len(), 165_949);
        assert_eq!(material.duration_ms(), 7_526);

        let one = FrameQueue::new(FRAME_QUEUE_CAPACITY);
        let mut p = CapturePipeline::new(material.input_rate, one.clone()).unwrap();
        p.push(&material.samples).unwrap();
        let one_shot = p.frames_emitted();
        assert_eq!(one_shot, 250, "7.526s / 30ms = 250 帧");

        let (frames, events) = queues();
        let mut src = SyntheticSource::from_samples(
            "注入 (wav)",
            material.input_rate,
            material.channels,
            material.samples.clone(),
        )
        .with_pace(Pace::Fast);
        src.start(frames.clone(), events.clone()).unwrap();
        let deadline = Instant::now() + Duration::from_secs(20);
        while src.stats().frames_emitted < one_shot && Instant::now() < deadline {
            thread::sleep(Duration::from_millis(5));
        }
        src.stop();
        assert_eq!(
            src.stats().frames_emitted,
            one_shot,
            "整个素材必须一帧不丢地喂进去（分块喂的粒度不该改变帧数）"
        );
    }

    #[test]
    fn expected_frames_matches_the_pipeline_arithmetic() {
        // 16k 单声道 1s → 33 帧（与 loopback.rs 的管线单测同口径）。
        assert_eq!(expected_frames(16_000, 16_000), 33);
        // 22050 立体声 1s → 单声道 22050 样本 → 同样 33 帧。
        assert_eq!(expected_frames(22_050, 22_050), 33);
        assert_eq!(expected_frames(0, 16_000), 0);
        assert_eq!(expected_frames(16_000, 0), 0);
    }

    #[test]
    fn unsupported_wav_format_is_refused_by_name() {
        // 24-bit PCM 不在支持范围：必须报"不受支持"而不是猜着读。
        let dir = std::env::temp_dir().join("ic-synth-test");
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("s24.wav");
        let spec = hound::WavSpec {
            channels: 1,
            sample_rate: 16_000,
            bits_per_sample: 24,
            sample_format: hound::SampleFormat::Int,
        };
        let mut w = hound::WavWriter::create(&path, spec).unwrap();
        for _ in 0..16 {
            w.write_sample(0i32).unwrap();
        }
        w.finalize().unwrap();
        let err = read_wav(&path).expect_err("24-bit must be refused");
        assert!(matches!(err, AudioError::Unsupported(_)), "{err:?}");
        assert!(err.to_string().contains("24-bit"), "{err}");
        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn wav_roundtrip_downmixes_stereo_and_keeps_the_native_rate() {
        let dir = std::env::temp_dir().join("ic-synth-test");
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("stereo.wav");
        let spec = hound::WavSpec {
            channels: 2,
            sample_rate: 22_050,
            bits_per_sample: 16,
            sample_format: hound::SampleFormat::Int,
        };
        let mut w = hound::WavWriter::create(&path, spec).unwrap();
        // 左 = +16384 (0.5)，右 = -16384 (-0.5) → 下混后 = 0（左右相消）。
        for _ in 0..220 {
            w.write_sample(16_384i16).unwrap();
            w.write_sample(-16_384i16).unwrap();
        }
        w.finalize().unwrap();

        let m = read_wav(&path).unwrap();
        assert_eq!(m.input_rate, 22_050);
        assert_eq!(m.channels, 2);
        assert_eq!(m.samples.len(), 220, "立体声 → 单声道样本数减半");
        assert!(m.samples.iter().all(|s| s.abs() < 1e-6), "左右应相消");
        assert_eq!(m.duration_ms(), 9); // 220/22050 ≈ 9.97ms
        let _ = std::fs::remove_file(&path);
    }
}
