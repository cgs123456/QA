//! Native-rate mono f32 → 16 kHz 480-frame stream (rubato `Fft`, fixed output).
//!
//! Timestamp model: the capture side stamps frame N as `t0 + N*30ms`
//! (sample-count-derived, jitter-free). The resampler's internal delay is a
//! CONSTANT offset (see [`MonoResampler16k::output_delay_frames`]) — it does
//! not affect drift, only absolute lip-sync (tens of ms, acceptable Phase 1b).
//!
//! Format handling is deliberately split from rate handling: the capture
//! backends ask the OS for the device's own sample rate and channel count and
//! only force an `f32` container, so the engine never resamples behind our
//! back. Everything below is therefore "N-channel interleaved f32 at the
//! device rate → mono f32 at the device rate", and then one rubato stage to
//! 16 kHz.

use rubato::audioadapter_buffers::direct::InterleavedSlice;
use rubato::{Fft, FixedSync, Resampler};

use super::frame::{FRAME_SAMPLES, SAMPLE_RATE};
use super::loopback::AudioError;

/// Mono resampler into fixed 480-frame outputs at 16 kHz.
///
/// Feed arbitrary native-rate chunks via [`push`](Self::push); completed
/// frames come out. All input samples are conserved (backlog buffering) —
/// no drops, no duplicates, hence no drift by construction (proven by the
/// 10-minute conservation test below).
pub struct MonoResampler16k {
    inner: Option<Fft<f32>>,
    input_rate: u32,
    backlog: Vec<f32>,
    ready: Vec<f32>,
    emitted_frames: u64,
}

impl MonoResampler16k {
    pub fn new(input_rate: u32) -> Result<Self, AudioError> {
        if input_rate == 0 || input_rate > 192_000 {
            return Err(AudioError::Resample(format!("bad input rate {input_rate}")));
        }
        let inner = if input_rate == SAMPLE_RATE {
            None // passthrough: an FFT roundtrip would only add noise and delay
        } else {
            Some(
                Fft::<f32>::new(
                    input_rate as usize,
                    SAMPLE_RATE as usize,
                    FRAME_SAMPLES,
                    1,
                    FixedSync::Output,
                )
                .map_err(|e| AudioError::Resample(format!("rubato init: {e}")))?,
            )
        };
        Ok(Self {
            inner,
            input_rate,
            backlog: Vec::new(),
            ready: Vec::new(),
            emitted_frames: 0,
        })
    }

    pub fn input_rate(&self) -> u32 {
        self.input_rate
    }

    pub fn emitted_frames(&self) -> u64 {
        self.emitted_frames
    }

    /// Resampler latency in output frames (constant; see module docs).
    pub fn output_delay_frames(&self) -> usize {
        self.inner.as_ref().map(|r| r.output_delay()).unwrap_or(0)
    }

    /// Native-rate frames the resampler wants before it can emit a frame.
    /// 0 for the 16 kHz passthrough.
    pub fn input_frames_per_frame(&self) -> usize {
        self.inner
            .as_ref()
            .map(|r| r.input_frames_next())
            .unwrap_or(FRAME_SAMPLES)
    }

    /// Feed native-rate mono samples; returns completed 480-sample frames.
    pub fn push(&mut self, mono: &[f32]) -> Result<Vec<[f32; FRAME_SAMPLES]>, AudioError> {
        match self.inner.as_mut() {
            Some(inner) => {
                self.backlog.extend_from_slice(mono);
                loop {
                    let need = inner.input_frames_next();
                    if self.backlog.len() < need {
                        break;
                    }
                    let input = InterleavedSlice::new(&self.backlog, 1, need)
                        .map_err(|e| AudioError::Resample(format!("adapter: {e:?}")))?;
                    let mut out = vec![0.0f32; FRAME_SAMPLES];
                    let mut out_ad = InterleavedSlice::new_mut(&mut out, 1, FRAME_SAMPLES)
                        .map_err(|e| AudioError::Resample(format!("adapter: {e:?}")))?;
                    let (used, written) = inner
                        .process_into_buffer(&input, &mut out_ad, None)
                        .map_err(|e| AudioError::Resample(format!("process: {e}")))?;
                    self.backlog.drain(..used);
                    if written == 0 {
                        break;
                    }
                    self.ready.extend_from_slice(&out[..written]);
                }
            }
            None => self.ready.extend_from_slice(mono),
        }

        let mut frames = Vec::new();
        while self.ready.len() >= FRAME_SAMPLES {
            let mut frame = [0.0f32; FRAME_SAMPLES];
            frame.copy_from_slice(&self.ready[..FRAME_SAMPLES]);
            self.ready.drain(..FRAME_SAMPLES);
            frames.push(frame);
            self.emitted_frames += 1;
        }
        Ok(frames)
    }
}

/// Stereo interleaved f32 → mono (mid; averaging cannot clip for [-1,1] input).
pub fn downmix_stereo_f32_to_mono(interleaved: &[f32]) -> Vec<f32> {
    interleaved
        .as_chunks::<2>()
        .0
        .iter()
        .map(|pair| (pair[0] + pair[1]) * 0.5)
        .collect()
}

/// Interleaved f32 (any channel count ≥1) → mono (mean).
///
/// A trailing partial frame is ignored rather than panicking: this runs on the
/// capture thread, where a panic would silently kill audio.
pub fn downmix_interleaved_f32_to_mono(interleaved: &[f32], channels: u16) -> Vec<f32> {
    if channels == 0 {
        return Vec::new();
    }
    let ch = channels as usize;
    if ch == 1 {
        return interleaved.to_vec();
    }
    interleaved
        .chunks_exact(ch)
        .map(|frame| frame.iter().sum::<f32>() / channels as f32)
        .collect()
}

/// Raw little-endian f32 bytes (interleaved, `channels`) → mono f32.
///
/// Single pass, no intermediate full-size buffer — this is the WASAPI hot path
/// where the device hands us `[u8]`.
pub fn decode_interleaved_f32_le_to_mono(bytes: &[u8], channels: u16) -> Vec<f32> {
    if channels == 0 {
        return Vec::new();
    }
    let ch = channels as usize;
    let frame_bytes = ch * 4;
    if ch == 1 {
        return bytes
            .as_chunks::<4>()
            .0
            .iter()
            .map(|b| f32::from_le_bytes(*b))
            .collect();
    }
    bytes
        .chunks_exact(frame_bytes)
        .map(|frame| {
            let mut sum = 0.0f32;
            for s in frame.as_chunks::<4>().0 {
                sum += f32::from_le_bytes(*s);
            }
            sum / channels as f32
        })
        .collect()
}

/// Interleaved int16 (any channel count) → mono f32 (`/32768.0`, averaged).
pub fn downmix_interleaved_i16_to_mono_f32(samples: &[i16], channels: u16) -> Vec<f32> {
    if channels == 0 {
        return Vec::new();
    }
    samples
        .chunks_exact(channels as usize)
        .map(|frame| frame.iter().map(|s| *s as f32 / 32768.0).sum::<f32>() / channels as f32)
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sine_48k(freq_hz: f32, seconds: f32, amp: f32) -> Vec<f32> {
        let n = (48_000.0 * seconds) as usize;
        (0..n)
            .map(|i| (2.0 * std::f32::consts::PI * freq_hz * i as f32 / 48_000.0).sin() * amp)
            .collect()
    }

    /// Best-lag cross-correlation (integer search ±2000) then SNR at that lag.
    fn snr_at_best_lag(reference: &[f32], actual: &[f32]) -> (f64, isize) {
        let mut best_lag = 0isize;
        let mut best_corr = f64::NEG_INFINITY;
        for lag in -2000isize..=2000isize {
            let mut corr = 0.0f64;
            let mut count = 0usize;
            for (i, a) in actual.iter().enumerate() {
                let j = i as isize + lag;
                if j >= 0 && (j as usize) < reference.len() {
                    corr += *a as f64 * reference[j as usize] as f64;
                    count += 1;
                }
            }
            let norm = corr / count.max(1) as f64;
            if norm > best_corr {
                best_corr = norm;
                best_lag = lag;
            }
        }
        let (mut sig, mut noise) = (0.0f64, 0.0f64);
        for (i, a) in actual.iter().enumerate() {
            let j = i as isize + best_lag;
            if j >= 0 && (j as usize) < reference.len() {
                let r = reference[j as usize] as f64;
                sig += r * r;
                let d = *a as f64 - r;
                noise += d * d;
            }
        }
        (10.0 * (sig / noise.max(1e-30)).log10(), best_lag)
    }

    #[test]
    fn downmix_math() {
        assert_eq!(
            downmix_stereo_f32_to_mono(&[1.0, -1.0, 0.5, 0.5]),
            vec![0.0, 0.5]
        );
        let mixed = downmix_interleaved_i16_to_mono_f32(&[32767, -32768], 2);
        assert_eq!(mixed.len(), 1);
        assert!((mixed[0] - (-0.5 / 32768.0)).abs() < 1e-9);
        assert_eq!(
            downmix_interleaved_i16_to_mono_f32(&[16384, 16384, 16384], 3).len(),
            1
        );
    }

    #[test]
    fn downmix_handles_mono_multichannel_and_ragged_tails() {
        // mono is a straight copy
        assert_eq!(
            downmix_interleaved_f32_to_mono(&[0.1, 0.2, 0.3], 1),
            vec![0.1, 0.2, 0.3]
        );
        // 5.1 downmixes to the channel mean
        let six = [0.6f32, 0.0, 0.0, 0.0, 0.0, 0.0];
        assert!((downmix_interleaved_f32_to_mono(&six, 6)[0] - 0.1).abs() < 1e-6);
        // ragged tail ignored, never panics
        assert_eq!(
            downmix_interleaved_f32_to_mono(&[1.0, 1.0, 1.0], 2).len(),
            1
        );
        assert_eq!(downmix_interleaved_f32_to_mono(&[1.0], 0).len(), 0);
    }

    #[test]
    fn decode_f32_le_matches_direct_downmix() {
        let interleaved = [0.25f32, -0.25, 0.5, 0.5, -1.0, 1.0];
        let bytes: Vec<u8> = interleaved.iter().flat_map(|s| s.to_le_bytes()).collect();
        let via_bytes = decode_interleaved_f32_le_to_mono(&bytes, 2);
        assert_eq!(via_bytes, downmix_stereo_f32_to_mono(&interleaved));
        // mono path preserves every sample
        let mono_bytes: Vec<u8> = interleaved.iter().flat_map(|s| s.to_le_bytes()).collect();
        assert_eq!(
            decode_interleaved_f32_le_to_mono(&mono_bytes, 1),
            interleaved.to_vec()
        );
        // ragged tail ignored
        assert_eq!(decode_interleaved_f32_le_to_mono(&bytes[..7], 2).len(), 0);
    }

    #[test]
    fn sine_through_resampler_snr() {
        // 440Hz @48k → 16k: steady-state SNR must clear 40dB by a wide margin.
        let input = sine_48k(440.0, 5.0, 0.8);
        let mut resampler = MonoResampler16k::new(48_000).unwrap();
        let mut out = Vec::new();
        for chunk in input.chunks(4800) {
            for frame in resampler.push(chunk).unwrap() {
                out.extend_from_slice(&frame);
            }
        }
        let ideal: Vec<f32> = (0..out.len())
            .map(|i| {
                (2.0 * std::f32::consts::PI * 440.0 * i as f32 / SAMPLE_RATE as f32).sin() * 0.8
            })
            .collect();
        let (snr, lag) = snr_at_best_lag(&ideal, &out);
        assert!(snr > 40.0, "resample SNR too low: {snr:.1}dB at lag {lag}");
    }

    #[test]
    fn passthrough_16k_is_exact() {
        let mut resampler = MonoResampler16k::new(16_000).unwrap();
        let input: Vec<f32> = (0..960).map(|i| i as f32 / 960.0).collect();
        let frames = resampler.push(&input).unwrap();
        assert_eq!(frames.len(), 2);
        assert_eq!(&frames[0][..], &input[..480]);
        assert_eq!(resampler.output_delay_frames(), 0);
        assert_eq!(resampler.input_frames_per_frame(), 480);
    }

    #[test]
    fn ten_minutes_no_drift() {
        // 10 min of 48k in → sample conservation proves zero rate error.
        // `produced_samples == consumed_samples / ratio` holds exactly when the
        // input length is a whole number of resampler chunks; the resampler's
        // constant delay shifts the stream but adds no frames. ±480 samples over
        // 9.6M is ±50ppm — tighter than the DoD's 83ppm (50ms/10min).
        let mut resampler = MonoResampler16k::new(48_000).unwrap();
        let chunk_in = resampler.input_frames_per_frame();
        assert_eq!(
            chunk_in, 1440,
            "48k→16k should want 3 input frames per output frame"
        );
        let total_in = 48_000usize * 600;
        assert_eq!(
            total_in % chunk_in,
            0,
            "test needs a whole number of chunks"
        );

        let mut made = 0usize;
        let mut chunk = vec![0.0f32; 4800];
        let mut idx = 0usize;
        while idx < total_in {
            let n = (total_in - idx).min(4800);
            for (i, s) in chunk[..n].iter_mut().enumerate() {
                *s = ((idx + i) as f32 * 0.01).sin() * 0.5;
            }
            made += resampler.push(&chunk[..n]).unwrap().len();
            idx += n;
        }

        let produced_samples = made * FRAME_SAMPLES;
        let expected_samples = total_in / 3;
        assert!(
            produced_samples.abs_diff(expected_samples) <= FRAME_SAMPLES,
            "sample loss/drift: in={total_in} out_frames={made} \
             produced={produced_samples} expected={expected_samples}"
        );
        assert!(made > 19_000, "catastrophic frame loss: {made}");
        // Constant (not accumulating) latency: half the FFT block, well under
        // one 30 ms frame's worth of lip-sync error budget.
        assert!(resampler.output_delay_frames() <= FRAME_SAMPLES);
    }
}
