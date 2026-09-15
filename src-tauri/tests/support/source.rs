//! 假采集源：一段样本 → 真实 `CapturePipeline` → `FrameQueue`。
//!
//! # 为什么不是「直接把 Frame16k 塞进队列」
//!
//! 因为那样测的只是端点。这里要测的是**接线**：样本经真重采样、真分帧、
//! 真 `seq`/`ts_ms` 生成，再被真 VAD 逐帧分类 —— 只有这条路径整条是真的，
//! 「合成语音串 → 段序列」才有意义。所以本模块只负责「按时间把样本喂进
//! `CapturePipeline::push`」，其余全是生产代码。

use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Arc;
use std::thread::{self, JoinHandle};
use std::time::Duration;

use interview_copilot_lib::audio::frame::FRAME_SAMPLES;
use interview_copilot_lib::audio::loopback::{
    AudioError, CaptureEvent, CapturePipeline, CaptureStats, EventQueue, FrameQueue, LoopbackSource,
};
use interview_copilot_lib::audio::service::SourceFactory;
use interview_copilot_lib::audio::PathLabel;

/// 真实帧率下的每帧墙钟间隔（30ms）。
pub const REAL_FRAME_PACE: Duration = Duration::from_millis(30);

/// 把一段单声道样本按给定节奏推进真实采集管线的假源。
///
/// - `input_rate`：样本的原生采样率（管线负责重采样到 16k）。
/// - `pace`：每帧之间的墙钟间隔。传 [`REAL_FRAME_PACE`] 时自检帧率会落在额定值
///   附近；调快可以让测试跑得短，但自检会如实报「高于额定」（这本身值得测）。
/// - `repeat`：播完是否从头再来（sidecar 起得慢时用来维持流不断）。
pub struct PushSource {
    device: String,
    input_rate: u32,
    samples: Vec<f32>,
    pace: Duration,
    repeat: bool,
    stop: Arc<AtomicBool>,
    emitted: Arc<AtomicU64>,
    thread: Option<JoinHandle<()>>,
}

impl PushSource {
    pub fn new(device: impl Into<String>, input_rate: u32, samples: Vec<f32>) -> Self {
        Self {
            device: device.into(),
            input_rate,
            samples,
            pace: REAL_FRAME_PACE,
            repeat: false,
            stop: Arc::new(AtomicBool::new(false)),
            emitted: Arc::new(AtomicU64::new(0)),
            thread: None,
        }
    }

    pub fn with_pace(mut self, pace: Duration) -> Self {
        self.pace = pace;
        self
    }

    pub fn with_repeat(mut self, repeat: bool) -> Self {
        self.repeat = repeat;
        self
    }
}

impl std::fmt::Debug for PushSource {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("PushSource")
            .field("device", &self.device)
            .field("input_rate", &self.input_rate)
            .field("samples", &self.samples.len())
            .field("pace", &self.pace)
            .finish_non_exhaustive()
    }
}

impl LoopbackSource for PushSource {
    fn start(
        &mut self,
        frames: Arc<FrameQueue>,
        events: Arc<EventQueue>,
    ) -> Result<(), AudioError> {
        // 设备信息走生产同一条通道（`CaptureEvent::Started`），
        // 这样诊断面板/自检里的设备名与格式不是测试编出来的。
        events.push(CaptureEvent::Started {
            device: self.device.clone(),
            sample_rate: self.input_rate,
            channels: 1,
        });

        let mut pipeline = CapturePipeline::new(self.input_rate, frames)?;
        let samples = std::mem::take(&mut self.samples);
        let pace = self.pace;
        let repeat = self.repeat;
        let stop = self.stop.clone();
        let emitted = self.emitted.clone();

        self.thread = Some(thread::spawn(move || {
            let mut cursor = 0usize;
            while !stop.load(Ordering::SeqCst) {
                if cursor >= samples.len() {
                    if !repeat {
                        break;
                    }
                    cursor = 0;
                }
                let end = (cursor + FRAME_SAMPLES).min(samples.len());
                if pipeline.push(&samples[cursor..end]).is_ok() {
                    emitted.store(pipeline.frames_emitted(), Ordering::Relaxed);
                }
                cursor = end;
                if stop.load(Ordering::SeqCst) {
                    break;
                }
                thread::sleep(pace);
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
        CaptureStats {
            frames_emitted: self.emitted.load(Ordering::Relaxed),
            frames_dropped: 0,
            // 假源没有设备可掉线；VAD 错误不属于源（在 worker 的 `vad_errors`）。
            errors: 0,
        }
    }
}

/// 给 `CaptureService::with_source_factory` 用的工厂：两路都推同一段样本。
///
/// 刻意让两路样本**不同种子**（按 path 区分），这样若哪天真把两个 worker
/// 串成一条（或共用一个 VAD 实例），断言「两路各自的段序列」就会失败，
/// 而不是碰巧都通过。
pub fn factory_for(
    samples_for: impl Fn(PathLabel) -> Vec<f32> + Send + Sync + 'static,
    input_rate: u32,
    pace: Duration,
) -> Arc<SourceFactory> {
    Arc::new(
        move |path: PathLabel| -> Result<Box<dyn LoopbackSource>, AudioError> {
            Ok(Box::new(
                PushSource::new(
                    format!("假源 ({})", path.as_str()),
                    input_rate,
                    samples_for(path),
                )
                .with_pace(pace),
            ))
        },
    )
}
