//! Device-free DoD proxy for Phase 1b's audio DoD.
//!
//! The real DoD is "record 10 s of system playback and listen to the WAV", which
//! needs a machine with a render device and a human ear. What *can* be locked
//! down in CI is everything around the ear: the int16 sequence the capture
//! pipeline emits must be a faithful, correctly-sized WAV, the float32 sequence
//! must have the same length, and the content must still be the tone we fed in.
//!
//! `examples/record_loopback.rs` covers the human half on a real device.

use interview_copilot_lib::audio::frame::{f32_to_i16, FRAME_SAMPLES, SAMPLE_RATE};
use interview_copilot_lib::audio::loopback::{CapturePipeline, FrameQueue};

/// Goertzel magnitude at `freq` — enough to prove "the 440 Hz tone survived".
fn goertzel(samples: &[f32], rate: u32, freq: f32) -> f64 {
    let n = samples.len() as f64;
    let k = (n * freq as f64 / rate as f64).round();
    let w = 2.0 * std::f64::consts::PI * k / n;
    let coeff = 2.0 * w.cos();
    let (mut s1, mut s2) = (0.0f64, 0.0f64);
    for s in samples {
        let s0 = *s as f64 + coeff * s1 - s2;
        s2 = s1;
        s1 = s0;
    }
    (s1 * s1 + s2 * s2 - coeff * s1 * s2).max(0.0).sqrt()
}

#[test]
fn captured_frames_round_trip_through_wav_with_content_intact() {
    const SECONDS: usize = 5;
    const TONE_HZ: f32 = 440.0;
    const NATIVE_RATE: u32 = 48_000;

    // 1) Synthesise what a 48 kHz stereo device would hand us, downmixed to mono.
    let total_in = NATIVE_RATE as usize * SECONDS;
    let captured: Vec<f32> = (0..total_in)
        .map(|i| (2.0 * std::f32::consts::PI * TONE_HZ * i as f32 / NATIVE_RATE as f32).sin() * 0.8)
        .collect();

    // 2) Push it through the real capture pipeline in realistic 100 ms chunks.
    let queue = FrameQueue::new(1024);
    let mut pipeline = CapturePipeline::new(NATIVE_RATE, queue.clone()).unwrap();
    for chunk in captured.chunks(NATIVE_RATE as usize / 10) {
        pipeline.push(chunk).unwrap();
    }

    let mut frames = Vec::new();
    while let Some(f) = queue.try_pop() {
        frames.push(f);
    }
    assert!(!frames.is_empty(), "pipeline produced no frames");

    // 3) Sequence/dual-representation invariants (R10 seq, R11 int16 + float32).
    for (i, f) in frames.iter().enumerate() {
        assert_eq!(f.seq as usize, i, "sequence numbers must be contiguous");
        assert_eq!(
            f.ts_ms,
            i as u64 * 30,
            "timestamps must be sample-count derived"
        );
        assert_eq!(f.pcm_i16.len(), FRAME_SAMPLES);
        assert_eq!(f.pcm_f32.len(), FRAME_SAMPLES);
        for (a, b) in f.pcm_i16.iter().zip(f.pcm_f32.iter()) {
            assert_eq!(
                *a,
                f32_to_i16(*b),
                "int16 must be the quantisation of float32"
            );
        }
    }

    // 4) Write the int16 sequence as 16 kHz mono WAV, exactly as the shell will.
    let path = std::env::temp_dir().join("interview-copilot-audio-roundtrip.wav");
    let spec = hound::WavSpec {
        channels: 1,
        sample_rate: SAMPLE_RATE,
        bits_per_sample: 16,
        sample_format: hound::SampleFormat::Int,
    };
    let mut writer = hound::WavWriter::create(&path, spec).unwrap();
    for f in &frames {
        for s in f.pcm_i16 {
            writer.write_sample(s).unwrap();
        }
    }
    writer.finalize().unwrap();

    // 5) Read it back: length and content must both survive.
    let mut reader = hound::WavReader::open(&path).unwrap();
    assert_eq!(reader.spec().sample_rate, SAMPLE_RATE);
    assert_eq!(reader.spec().channels, 1);
    let decoded: Vec<i16> = reader.samples::<i16>().map(|s| s.unwrap()).collect();

    let expected_samples = frames.len() * FRAME_SAMPLES;
    assert_eq!(
        decoded.len(),
        expected_samples,
        "WAV length must equal the frame sequence"
    );
    let flat: Vec<i16> = frames
        .iter()
        .flat_map(|f| f.pcm_i16.iter().copied())
        .collect();
    assert_eq!(
        decoded, flat,
        "WAV samples must be bit-identical to the frames"
    );

    // float32 and int16 sequences must have identical length (DoD).
    let f32_samples: usize = frames.iter().map(|f| f.pcm_f32.len()).sum();
    assert_eq!(f32_samples, decoded.len());

    // 6) Content check: the 440 Hz tone must dominate its own octave/neighbours.
    // Skip the resampler's constant startup delay (half an FFT block, ≤480 frames)
    // so the transient does not pollute the measurement.
    let skip = FRAME_SAMPLES;
    let mono: Vec<f32> = frames
        .iter()
        .skip(1)
        .flat_map(|f| f.pcm_f32.iter().copied())
        .collect();
    assert!(mono.len() > skip, "not enough audio to measure");
    let target = goertzel(&mono, SAMPLE_RATE, TONE_HZ);
    for rival in [110.0, 220.0, 880.0, 1760.0] {
        let other = goertzel(&mono, SAMPLE_RATE, rival);
        assert!(
            target > other * 8.0,
            "440 Hz should dominate {rival} Hz ({target:.1} vs {other:.1})"
        );
    }

    // 7) Sample conservation over the whole run (the drift guarantee, end to end).
    let produced = expected_samples;
    let consumed_equiv = total_in * SAMPLE_RATE as usize / NATIVE_RATE as usize;
    assert!(
        produced.abs_diff(consumed_equiv) <= FRAME_SAMPLES,
        "sample conservation broken: produced {produced}, expected ~{consumed_equiv}"
    );

    let _ = std::fs::remove_file(&path);
}
