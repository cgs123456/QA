//! 合成测试信号。
//!
//! [`speech_like`] 与 `src/audio/vad.rs` 单测里的同名函数是**同一套参数**：
//! 谐波堆 + 共振峰包络 + 3.5Hz 音节调制 + 低噪声。复制一份而不是共享，
//! 是因为那边是 `#[cfg(test)]` 私有函数，而集成测试看不见 —— 但两边的形状
//! 必须一致，否则「单测说 VAD 能检出语音、集成测试却检不出」这种自相矛盾
//! 就会以最难查的形式出现。改任一处时请同步另一处。

use std::f32::consts::PI;

use interview_copilot_lib::audio::frame::{f32_to_i16, SAMPLE_RATE};

/// 类语音信号：谐波堆 + 共振峰包络 + 3.5Hz 音节调制 + 低噪声。
///
/// 只用来证明「VAD 能说这是语音」。真实人声验证是另一件单独的事。
pub fn speech_like(n: usize, seed: u64) -> Vec<f32> {
    let mut state = seed | 1;
    let mut out = Vec::with_capacity(n);
    for i in 0..n {
        let t = i as f32 / SAMPLE_RATE as f32;
        let env = 0.6 + 0.4 * (2.0 * PI * 3.5 * t).sin();
        let f0 = 120.0 + 20.0 * (2.0 * PI * 1.7 * t).sin();
        let mut s = 0.0f32;
        for h in 1..=12u32 {
            let f = f0 * h as f32;
            let w = if (300.0..=900.0).contains(&f) {
                1.0
            } else if (1100.0..=2800.0).contains(&f) {
                0.5
            } else {
                0.08
            };
            s += w * (2.0 * PI * f * t).sin();
        }
        state = state
            .wrapping_mul(6_364_136_223_846_793_005)
            .wrapping_add(1_442_695_040_888_963_407);
        let noise = ((state >> 33) as f32 / (1u64 << 31) as f32) - 1.0;
        out.push((0.25 * env * s / 6.0 + 0.01 * noise).clamp(-1.0, 1.0));
    }
    out
}

/// 数字静音。刻意是**真 0**而不是「很低的噪声」：端点判的是 VAD 结果，
/// 而 VAD 对真静音必须报 false —— 如果这里塞了噪声，测试就在赌 VAD 的阈值。
pub fn silence(n: usize) -> Vec<f32> {
    vec![0.0f32; n]
}

/// 一段「静音 + 语音 + 静音」的拼接脚本，用于按帧构造采集样本。
pub struct Script {
    pub samples: Vec<f32>,
}

impl Script {
    pub fn new() -> Self {
        Self {
            samples: Vec::new(),
        }
    }

    /// 追加 `frames` 帧类语音。
    pub fn voiced(&mut self, frames: usize, seed: u64) -> &mut Self {
        let n = frames * interview_copilot_lib::audio::frame::FRAME_SAMPLES;
        self.samples.extend(speech_like(n, seed));
        self
    }

    /// 追加 `frames` 帧静音。
    pub fn silent(&mut self, frames: usize) -> &mut Self {
        let n = frames * interview_copilot_lib::audio::frame::FRAME_SAMPLES;
        self.samples.extend(silence(n));
        self
    }

    /// 已积累的 16k 样本数换算成帧数（管线按 480 样本切帧）。
    pub fn frames(&self) -> usize {
        self.samples.len() / interview_copilot_lib::audio::frame::FRAME_SAMPLES
    }
}

impl Default for Script {
    fn default() -> Self {
        Self::new()
    }
}

/// f32 → i16（与生产同一条转换路径，便于直接喂 VAD 做前置校验）。
pub fn to_i16(sig: &[f32]) -> Vec<i16> {
    sig.iter().map(|s| f32_to_i16(*s)).collect()
}
