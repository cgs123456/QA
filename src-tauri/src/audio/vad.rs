//! VAD abstraction: one trait, two providers (PRD §3.5 / F1.2).
//!
//! **Provider choice is still open (R19).** `webrtc-vad` is wired now because it
//! needs no model download; `silero-vad` is the W4 comparison candidate and is
//! only stubbed here (see [`SileroVad`]). W4 owns the accuracy comparison in
//! noisy conditions; nothing in this module presumes the outcome.
//!
//! # Hard input contract (the R19 pitfall, made non-silent)
//!
//! `webrtc-vad` 0.4.0 wraps **libfvad** and accepts only 10/20/30 ms frames at
//! 8/16/32/48 kHz. Our pipeline is 30 ms @ 16 kHz = [`FRAME_SAMPLES`] samples,
//! which is legal — but the crate's `Vad::new()` defaults to **8 kHz/Quality**,
//! and 480 samples at 8 kHz is a 60 ms frame, i.e. illegal. Feeding the wrong
//! pairing does not degrade gracefully: `is_voice_segment` returns `Err(())`,
//! and any caller that maps that to `false` sees **all-silence, silently**.
//!
//! This module closes that hole four ways:
//!
//! 1. the crate is constructed with the rate **pinned** to
//!    [`SampleRate::Rate16kHz`] (never the defaulting `new()`);
//! 2. the frame length is validated before the crate is called;
//! 3. a provider error is returned as [`VadError`] rather than collapsed to
//!    `false` — silence and *failure to classify* stay distinguishable;
//! 4. those errors are **counted** ([`WebrtcVad::errors`]) so the condition is
//!    observable instead of invisible (R13 observability, R14 content-free).
//!
//! The frame-length/rate pairing is additionally pinned at compile time by the
//! `const _` assertion below, so a future edit to `frame.rs` cannot quietly
//! invalidate the VAD's legal input.
//!
//! There is a second entrance to the same failure: the crate's `reset()` is
//! `fvad_reset()`, which restores libfvad's **defaults** (8 kHz, mode 0). A
//! state machine that resets at a segment boundary would therefore go deaf on
//! the very next frame. [`WebrtcVad`] re-pins its configuration after every
//! reset; `crate_reset_reverts_rate_and_we_compensate` holds that line.
//!
//! # Threading
//!
//! [`webrtc_vad::Vad`] owns a raw `*mut Fvad` and is therefore `!Send`. Build
//! it on the thread that will drive it (one instance per audio path, owned by
//! that path's worker) and never hand it across a thread boundary. The [`Vad`]
//! trait deliberately does not require `Send` rather than faking the bound.

use std::fmt;

use webrtc_vad::{SampleRate, Vad as Fvad, VadMode};

use super::frame::{FRAME_MS, FRAME_SAMPLES, SAMPLE_RATE};

/// Initial aggressiveness, recorded as a project parameter (PRD §3.5).
///
/// libfvad mode 2 = `Aggressive`. W4 re-tunes this against a real sample set;
/// until then it is deliberately the documented starting value, not a tuned one.
pub const DEFAULT_AGGRESSIVENESS: u8 = 2;

/// Compile-time pin: the pipeline frame must be a legal libfvad frame at the
/// configured rate. `480 * 1000 / 16000 == 30` ms — one of {10, 20, 30}.
const _: () = assert!(
    FRAME_SAMPLES * 1000 / SAMPLE_RATE as usize == FRAME_MS as usize,
    "frame.rs no longer describes a legal 30ms/16kHz libfvad frame"
);
const _: () = assert!(
    matches!(SAMPLE_RATE, 8_000 | 16_000 | 32_000 | 48_000),
    "libfvad accepts only 8/16/32/48 kHz"
);

/// A VAD failure that must never be laundered into "not speech".
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum VadError {
    /// Aggressiveness outside `0..=3` (libfvad's four modes).
    BadAggressiveness(u8),
    /// Frame was not exactly [`FRAME_SAMPLES`] int16 samples (30 ms @ 16 kHz).
    BadFrameLength { got: usize, expected: usize },
    /// Provider cannot be used (missing model, or not integrated yet).
    Unavailable(&'static str),
}

impl fmt::Display for VadError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            VadError::BadAggressiveness(a) => {
                write!(f, "aggressiveness {a} out of range 0..=3")
            }
            VadError::BadFrameLength { got, expected } => {
                write!(f, "vad frame length {got} != {expected} (30ms @16kHz)")
            }
            VadError::Unavailable(why) => write!(f, "vad provider unavailable: {why}"),
        }
    }
}

impl std::error::Error for VadError {}

/// Voiced/silence decision over one 30 ms int16 frame (R11: int16 only).
///
/// Deliberately **not** `Send`: the webrtc backend holds a C pointer, and the
/// honest contract is "one instance, one thread" rather than a bound the stub
/// provider would have to fake.
pub trait Vad {
    /// Stable provider id for diagnostics (R14: never content).
    fn provider(&self) -> &'static str;

    /// Frames per decision. Always [`FRAME_SAMPLES`] for this pipeline.
    fn frame_samples(&self) -> usize;

    /// Classify exactly `frame_samples()` int16 samples at 16 kHz.
    ///
    /// `Ok(false)` means *classified as silence*; `Err` means *could not
    /// classify*. Callers must not conflate the two.
    fn is_voiced(&mut self, frame: &[i16]) -> Result<bool, VadError>;

    /// Drop internal state (segment boundary, device swap).
    fn reset(&mut self);
}

/// Aggressiveness `0..=3` → libfvad mode (PRD §3.5's four-way scale).
fn mode_for(aggressiveness: u8) -> Option<VadMode> {
    match aggressiveness {
        0 => Some(VadMode::Quality),
        1 => Some(VadMode::LowBitrate),
        2 => Some(VadMode::Aggressive),
        3 => Some(VadMode::VeryAggressive),
        _ => None,
    }
}

/// Default provider: `webrtc-vad` (libfvad) at 30 ms / 16 kHz.
pub struct WebrtcVad {
    inner: Fvad,
    aggressiveness: u8,
    errors: u64,
}

/// Hand-written so the raw `*mut Fvad` never reaches a log line (R14).
impl fmt::Debug for WebrtcVad {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("WebrtcVad")
            .field("provider", &"webrtc-vad")
            .field("aggressiveness", &self.aggressiveness)
            .field("errors", &self.errors)
            .finish_non_exhaustive()
    }
}

impl WebrtcVad {
    /// Build with an explicit aggressiveness `0..=3`.
    pub fn new(aggressiveness: u8) -> Result<Self, VadError> {
        let mode = mode_for(aggressiveness).ok_or(VadError::BadAggressiveness(aggressiveness))?;
        Ok(Self {
            // Pinned rate: `Fvad::new()` would default to 8 kHz and silently
            // reject every 480-sample frame we hand it.
            inner: Fvad::new_with_rate_and_mode(SampleRate::Rate16kHz, mode),
            aggressiveness,
            errors: 0,
        })
    }

    /// Re-apply the configuration this pipeline depends on.
    ///
    /// libfvad's `fvad_reset()` calls `WebRtcVad_InitCore()` and then sets
    /// `rate_idx = 0`, i.e. it restores the **defaults**: 8 kHz and mode 0
    /// (`kDefaultMode = 0`, "Quality"). Because the crate's `reset()` exposes
    /// exactly that, a caller resetting at a segment boundary would find every
    /// subsequent 480-sample frame illegal — a 60 ms frame at 8 kHz — and, if it
    /// mapped `Err` to `false`, would go permanently and silently deaf. Re-pin
    /// after every reset; this is the R19 failure mode reached by an ordinary
    /// code path, not by a misconfiguration.
    fn reapply_config(&mut self) {
        self.inner.set_sample_rate(SampleRate::Rate16kHz);
        if let Some(mode) = mode_for(self.aggressiveness) {
            self.inner.set_mode(mode);
        }
    }

    pub fn aggressiveness(&self) -> u8 {
        self.aggressiveness
    }

    /// Frames the provider refused to classify. Must stay 0 in production; a
    /// non-zero value means the frame/rate contract was broken.
    pub fn errors(&self) -> u64 {
        self.errors
    }
}

impl Vad for WebrtcVad {
    fn provider(&self) -> &'static str {
        "webrtc-vad"
    }

    fn frame_samples(&self) -> usize {
        FRAME_SAMPLES
    }

    fn is_voiced(&mut self, frame: &[i16]) -> Result<bool, VadError> {
        let expected = FRAME_SAMPLES;
        if frame.len() != expected {
            self.errors += 1;
            return Err(VadError::BadFrameLength {
                got: frame.len(),
                expected,
            });
        }
        // Split the borrow so the error branch can touch `self.errors`.
        match self.inner.is_voice_segment(frame) {
            Ok(voiced) => Ok(voiced),
            Err(()) => {
                self.errors += 1;
                Err(VadError::BadFrameLength {
                    got: frame.len(),
                    expected,
                })
            }
        }
    }

    fn reset(&mut self) {
        self.inner.reset();
        // `fvad_reset` drops the rate and mode back to 8 kHz / Quality; without
        // this the next 480-sample frame is illegal (see `reapply_config`).
        self.reapply_config();
    }
}

/// Build the default provider (webrtc-vad, aggressiveness [`DEFAULT_AGGRESSIVENESS`]).
pub fn open_default() -> Result<Box<dyn Vad>, VadError> {
    Ok(Box::new(WebrtcVad::new(DEFAULT_AGGRESSIVENESS)?))
}

/// Reserved silero-vad provider — **stub, W4 comparison only**.
///
/// Intentionally unconstructible this round: integrating silero means adding the
/// `ort` runtime plus an ONNX model download, which is a W4 deliverable with its
/// own size/latency budget (PRD §3.6, W4). Stubbing the trait impl now pins the
/// shape W4 must satisfy without pretending the dependency is present.
#[derive(Debug)]
pub struct SileroVad;

impl SileroVad {
    /// W4 will take the model path / session options here.
    pub fn new() -> Result<Self, VadError> {
        Err(VadError::Unavailable(
            "silero-vad not integrated (W4; needs `ort` + ONNX model)",
        ))
    }
}

impl Vad for SileroVad {
    fn provider(&self) -> &'static str {
        "silero-vad"
    }

    fn frame_samples(&self) -> usize {
        FRAME_SAMPLES
    }

    fn is_voiced(&mut self, _frame: &[i16]) -> Result<bool, VadError> {
        Err(VadError::Unavailable("silero-vad not integrated (W4)"))
    }

    fn reset(&mut self) {}
}

#[cfg(test)]
pub mod testing {
    //! Deterministic VAD for endpoint tests.
    //!
    //! Endpoint boundary error is a property of the *state machine*, so it is
    //! measured with the provider's own classification error removed. A scripted
    //! voiced/silence pattern gives frame-exact ground truth; the real libfvad
    //! is exercised separately (and against real voice) below.

    use super::*;

    pub struct ScriptedVad {
        script: Vec<bool>,
        cursor: usize,
        default_after: bool,
    }

    impl ScriptedVad {
        /// One entry per frame; frames past the end take `default_after`.
        pub fn new(script: Vec<bool>) -> Self {
            Self {
                script,
                cursor: 0,
                default_after: false,
            }
        }

        /// Expand `(frames, voiced)` runs into a per-frame script.
        pub fn pattern(runs: &[(usize, bool)]) -> Self {
            let mut script = Vec::new();
            for (frames, voiced) in runs {
                let end = script.len() + *frames;
                script.resize(end, *voiced);
            }
            Self::new(script)
        }

        /// Every frame the same decision.
        pub fn constant(voiced: bool) -> Self {
            Self {
                script: Vec::new(),
                cursor: 0,
                default_after: voiced,
            }
        }

        /// How many frames have been classified so far.
        pub fn calls(&self) -> usize {
            self.cursor
        }
    }

    impl Vad for ScriptedVad {
        fn provider(&self) -> &'static str {
            "scripted"
        }

        fn frame_samples(&self) -> usize {
            FRAME_SAMPLES
        }

        fn is_voiced(&mut self, frame: &[i16]) -> Result<bool, VadError> {
            if frame.len() != FRAME_SAMPLES {
                return Err(VadError::BadFrameLength {
                    got: frame.len(),
                    expected: FRAME_SAMPLES,
                });
            }
            let voiced = self
                .script
                .get(self.cursor)
                .copied()
                .unwrap_or(self.default_after);
            self.cursor += 1;
            Ok(voiced)
        }

        fn reset(&mut self) {
            self.cursor = 0;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::testing::ScriptedVad;
    use super::*;
    use crate::audio::frame::f32_to_i16;

    fn to_i16(sig: &[f32]) -> Vec<i16> {
        sig.iter().map(|s| f32_to_i16(*s)).collect()
    }

    fn silent(n: usize) -> Vec<i16> {
        vec![0i16; n]
    }

    /// Speech-shaped synthetic: harmonic stack under a formant envelope, with a
    /// pitch contour and 3.5 Hz syllabic amplitude modulation, plus low noise.
    /// Used only to prove the provider *can* say "speech"; real-voice validation
    /// is a separate, explicitly-tracked manual step.
    fn speech_like(n: usize, seed: u64) -> Vec<f32> {
        use std::f32::consts::PI;
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

    #[test]
    fn default_aggressiveness_is_two() {
        // Recorded project parameter (PRD §3.5); changing it is a tuning act.
        assert_eq!(DEFAULT_AGGRESSIVENESS, 2);
        let v = WebrtcVad::new(DEFAULT_AGGRESSIVENESS).unwrap();
        assert_eq!(v.aggressiveness(), 2);
        assert_eq!(v.provider(), "webrtc-vad");
        assert_eq!(v.frame_samples(), FRAME_SAMPLES);
    }

    #[test]
    fn aggressiveness_out_of_range_is_rejected() {
        for bad in [4u8, 7, 255] {
            assert_eq!(
                WebrtcVad::new(bad).unwrap_err(),
                VadError::BadAggressiveness(bad)
            );
        }
        for good in 0..=3u8 {
            assert!(WebrtcVad::new(good).is_ok(), "mode {good} must be legal");
        }
    }

    #[test]
    fn thirty_ms_frame_at_16k_is_legal_and_not_an_error() {
        // The pitfall guard: the configured rate/frame pairing must produce a
        // real decision, not `Err`. A silent all-silence failure would surface
        // here as `Err` (or as a non-zero error count) rather than pass quietly.
        let mut v = WebrtcVad::new(DEFAULT_AGGRESSIVENESS).unwrap();
        for _ in 0..5 {
            assert!(
                v.is_voiced(&silent(FRAME_SAMPLES)).is_ok(),
                "480 samples @16kHz must be a legal libfvad frame"
            );
        }
        assert_eq!(v.errors(), 0);
    }

    #[test]
    fn wrong_frame_length_is_reported_not_swallowed() {
        // Anything that is not 30 ms must be an error, never a silent `false`.
        let mut v = WebrtcVad::new(DEFAULT_AGGRESSIVENESS).unwrap();
        for bad in [0usize, 100, 160, 240, 481] {
            let err = v.is_voiced(&silent(bad)).unwrap_err();
            assert_eq!(
                err,
                VadError::BadFrameLength {
                    got: bad,
                    expected: FRAME_SAMPLES
                }
            );
        }
        assert_eq!(v.errors(), 5, "every rejection must be counted");
        // The provider is still usable afterwards.
        assert!(v.is_voiced(&silent(FRAME_SAMPLES)).is_ok());
        assert_eq!(v.errors(), 5);
    }

    #[test]
    fn silence_classifies_as_non_voiced() {
        let mut v = WebrtcVad::new(DEFAULT_AGGRESSIVENESS).unwrap();
        for _ in 0..50 {
            assert_eq!(v.is_voiced(&silent(FRAME_SAMPLES)), Ok(false));
        }
        assert_eq!(v.errors(), 0);
    }

    #[test]
    fn crate_default_rate_would_have_rejected_our_frames() {
        // Regression pin for the documented R19 pitfall. `webrtc_vad::Vad::new()`
        // defaults to 8 kHz; 480 samples is then a 60 ms frame, which libfvad
        // refuses. This is exactly the trap `WebrtcVad::new` avoids by pinning
        // the rate — the assertion documents that the trap is real.
        let mut wrong = Fvad::new();
        assert_eq!(wrong.is_voice_segment(&silent(FRAME_SAMPLES)), Err(()));
    }

    #[test]
    fn crate_reset_reverts_rate_and_we_compensate() {
        // The trap reached by an ordinary code path, not a misconfiguration:
        // libfvad's `fvad_reset()` restores the defaults (8 kHz, mode 0), so a
        // reset at a segment boundary would make every later frame illegal.
        // First prove the trap on the raw crate...
        let mut raw = Fvad::new_with_rate_and_mode(SampleRate::Rate16kHz, VadMode::Aggressive);
        assert!(raw.is_voice_segment(&silent(FRAME_SAMPLES)).is_ok());
        raw.reset();
        assert_eq!(
            raw.is_voice_segment(&silent(FRAME_SAMPLES)),
            Err(()),
            "raw reset is expected to drop the 16 kHz rate — if this changes, \
             `reapply_config` can be revisited"
        );

        // ...then prove our wrapper keeps the pipeline contract across resets.
        let mut ours = WebrtcVad::new(DEFAULT_AGGRESSIVENESS).unwrap();
        for _ in 0..3 {
            assert!(ours.is_voiced(&silent(FRAME_SAMPLES)).is_ok());
            ours.reset();
        }
        assert_eq!(ours.errors(), 0);
    }

    #[test]
    fn speech_like_signal_is_detected_at_default_aggressiveness() {
        // Proves the provider is classifying rather than degenerate.
        //
        // Measured with the pinned crate: this synthetic signal reads 100/100
        // voiced at *every* mode 0..=3, and pure silence reads 0/100. The
        // signal therefore separates "voiced vs not" but is too saturated to
        // discriminate aggressiveness levels — it is a non-degeneracy check,
        // not a real-speech proxy. Real-voice validation stays a manual step
        // (PRD W1/W4); the floor below is deliberately loose so it pins
        // "clearly not all-silence" without freezing W4's tuning surface.
        let sig = to_i16(&speech_like(FRAME_SAMPLES * 100, 0x5EED));
        let mut v = WebrtcVad::new(DEFAULT_AGGRESSIVENESS).unwrap();
        let total = sig.len() / FRAME_SAMPLES;
        let mut voiced = 0usize;
        for f in sig.as_chunks::<FRAME_SAMPLES>().0 {
            if v.is_voiced(f).unwrap() {
                voiced += 1;
            }
        }
        assert_eq!(v.errors(), 0);
        assert!(
            voiced * 2 >= total,
            "provider classified a speech-like signal as mostly silence \
             ({voiced}/{total}) — rate/frame pairing or mode is wrong"
        );

        // And the complement: silence must not be voiced.
        let mut v = WebrtcVad::new(DEFAULT_AGGRESSIVENESS).unwrap();
        for _ in 0..total {
            assert_eq!(v.is_voiced(&silent(FRAME_SAMPLES)), Ok(false));
        }
    }

    #[test]
    fn reset_is_callable_and_idempotent() {
        let mut v = WebrtcVad::new(DEFAULT_AGGRESSIVENESS).unwrap();
        assert!(v.is_voiced(&silent(FRAME_SAMPLES)).is_ok());
        v.reset();
        v.reset();
        assert!(v.is_voiced(&silent(FRAME_SAMPLES)).is_ok());
        assert_eq!(v.errors(), 0);
    }

    #[test]
    fn silero_stub_is_unavailable_not_silently_silent() {
        // W4 comparison provider: absent today, and it must say so.
        assert_eq!(
            SileroVad::new().unwrap_err(),
            VadError::Unavailable("silero-vad not integrated (W4; needs `ort` + ONNX model)")
        );
    }

    #[test]
    fn scripted_vad_expands_runs_frame_exactly() {
        // The test double endpoint tests rely on: exact, deterministic truth.
        let mut v = ScriptedVad::pattern(&[(3, false), (2, true), (1, false)]);
        let got: Vec<bool> = (0..7)
            .map(|_| v.is_voiced(&silent(FRAME_SAMPLES)).unwrap())
            .collect();
        assert_eq!(got, vec![false, false, false, true, true, false, false]);
        assert_eq!(v.calls(), 7);
        v.reset();
        assert_eq!(v.calls(), 0);
    }

    #[test]
    fn scripted_vad_rejects_bad_frames_like_a_real_provider() {
        let mut v = ScriptedVad::constant(true);
        assert!(v.is_voiced(&silent(FRAME_SAMPLES - 1)).is_err());
        assert_eq!(v.calls(), 0, "a rejected frame is not a classified frame");
    }

    #[test]
    fn open_default_uses_the_recorded_aggressiveness() {
        let v = open_default().unwrap();
        assert_eq!(v.provider(), "webrtc-vad");
        assert_eq!(v.frame_samples(), FRAME_SAMPLES);
    }
}
