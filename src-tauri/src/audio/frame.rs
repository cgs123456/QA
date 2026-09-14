//! 30ms / 16kHz mono frames + R10 wire encoding (pure, fully unit-tested).
//!
//! - VAD consumes [`Frame16k::pcm_i16`] (webrtc-vad legal input: 30ms @16kHz).
//! - WS uplink consumes [`Frame16k::pcm_f32`] via [`encode_ws_frame`]
//!   (header `[1B type=0x00][2B seq_le][4B ts_ms_le]` + 480×f32 LE = 1927B).

pub const SAMPLE_RATE: u32 = 16_000;
pub const FRAME_SAMPLES: usize = 480;
pub const FRAME_MS: u64 = 30;
pub const F32_BYTES: usize = FRAME_SAMPLES * 4; // 1920
pub const HEADER_BYTES: usize = 7;
pub const WIRE_BYTES: usize = HEADER_BYTES + F32_BYTES; // 1927
/// R10: the first byte is dual-purpose — binary audio vs JSON event.
pub const TYPE_AUDIO: u8 = 0x00;
pub const TYPE_EVENT: u8 = 0x01;

/// One 30ms mono frame at 16kHz, dual representation (R11: int16 for VAD,
/// float32 for WS — converted once here, never twice downstream).
#[derive(Debug, Clone, PartialEq)]
pub struct Frame16k {
    /// Per-source sequence counter (u16 on wire, wraps; see R10).
    pub seq: u32,
    /// Capture-start timestamp, ms relative to session origin.
    pub ts_ms: u64,
    pub pcm_i16: [i16; FRAME_SAMPLES],
    pub pcm_f32: [f32; FRAME_SAMPLES],
}

impl Frame16k {
    pub fn from_f32(seq: u32, ts_ms: u64, pcm: [f32; FRAME_SAMPLES]) -> Self {
        let mut pcm_i16 = [0i16; FRAME_SAMPLES];
        for (i, s) in pcm.iter().enumerate() {
            pcm_i16[i] = f32_to_i16(*s);
        }
        Self {
            seq,
            ts_ms,
            pcm_i16,
            pcm_f32: pcm,
        }
    }
}

/// float32 [-1,1] → int16 (symmetric clamp).
pub fn f32_to_i16(x: f32) -> i16 {
    (x * 32768.0).clamp(-32768.0, 32767.0) as i16
}

/// int16 → float32 (PRD §3.5: `/32768.0`).
pub fn i16_to_f32(x: i16) -> f32 {
    x as f32 / 32768.0
}

/// R10 wire encoding: `[0x00][seq u16le][ts_ms u32le][480×f32 le]` = 1927B.
pub fn encode_ws_frame(seq: u16, ts_ms: u32, pcm: &[f32; FRAME_SAMPLES]) -> [u8; WIRE_BYTES] {
    let mut out = [0u8; WIRE_BYTES];
    out[0] = TYPE_AUDIO;
    out[1..3].copy_from_slice(&seq.to_le_bytes());
    out[3..7].copy_from_slice(&ts_ms.to_le_bytes());
    for (i, s) in pcm.iter().enumerate() {
        out[HEADER_BYTES + i * 4..HEADER_BYTES + (i + 1) * 4].copy_from_slice(&s.to_le_bytes());
    }
    out
}

/// R10 event encoding: `[0x01][seq u16le][ts_ms u32le][UTF-8 JSON]`.
///
/// Same 7-byte header as an audio frame and the **same seq space** (the sidecar
/// checks continuity across both kinds). Length is variable, so the caller must
/// not assume a fixed frame size — only audio frames are 1927B.
pub fn encode_ws_event(seq: u16, ts_ms: u32, json: &[u8]) -> Vec<u8> {
    let mut out = Vec::with_capacity(HEADER_BYTES + json.len());
    out.push(TYPE_EVENT);
    out.extend_from_slice(&seq.to_le_bytes());
    out.extend_from_slice(&ts_ms.to_le_bytes());
    out.extend_from_slice(json);
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn frame_size_math() {
        assert_eq!(FRAME_SAMPLES, 480);
        assert_eq!(F32_BYTES, 1920);
        assert_eq!(HEADER_BYTES, 7);
        assert_eq!(WIRE_BYTES, 1927);
        assert_eq!(encode_ws_frame(0, 0, &[0.0; 480]).len(), 1927);
    }

    #[test]
    fn wire_header_layout() {
        let mut pcm = [0.0f32; 480];
        pcm[0] = 1.0;
        pcm[479] = -1.0;
        let wire = encode_ws_frame(0x0201, 0x04030201, &pcm);
        assert_eq!(wire[0], 0x00);
        assert_eq!(&wire[1..3], &[0x01, 0x02]);
        assert_eq!(&wire[3..7], &[0x01, 0x02, 0x03, 0x04]);
        assert_eq!(&wire[7..11], &1.0f32.to_le_bytes());
        assert_eq!(&wire[1927 - 4..], &(-1.0f32).to_le_bytes());
    }

    #[test]
    fn event_frame_has_the_same_header_and_a_json_body() {
        let json = br#"{"event":"segment_start","ts_ms":12345,"path":"loopback"}"#;
        let wire = encode_ws_event(0x0201, 0x04030201, json);
        assert_eq!(wire[0], TYPE_EVENT);
        // R10: header is little-endian for both frame kinds.
        assert_eq!(&wire[1..3], &[0x01, 0x02]);
        assert_eq!(&wire[3..7], &[0x01, 0x02, 0x03, 0x04]);
        assert_eq!(&wire[7..], json);
        assert_eq!(wire.len(), HEADER_BYTES + json.len());
        // Event frames are variable length — only audio frames are 1927B.
        assert_ne!(wire.len(), WIRE_BYTES);
    }

    #[test]
    fn int16_float32_roundtrip_snr() {
        // 440Hz sine mapped forth and back: quantization noise floor ≈ -90dB.
        let sine: Vec<f32> = (0..4800)
            .map(|n| (2.0 * std::f32::consts::PI * 440.0 * n as f32 / 16000.0).sin() * 0.8)
            .collect();
        let (mut sig, mut noise) = (0.0f64, 0.0f64);
        for s in &sine {
            let back = i16_to_f32(f32_to_i16(*s));
            sig += (*s as f64) * (*s as f64);
            let d = (*s - back) as f64;
            noise += d * d;
        }
        let snr = 10.0 * (sig / noise).log10();
        assert!(snr > 60.0, "roundtrip SNR too low: {snr}");
    }

    #[test]
    fn conversion_endpoints() {
        assert_eq!(f32_to_i16(1.0), 32767);
        assert_eq!(f32_to_i16(-1.0), -32768);
        assert_eq!(f32_to_i16(2.0), 32767); // clamped, never wraps
        assert!((i16_to_f32(32767) - 0.99997).abs() < 1e-4);
        assert_eq!(i16_to_f32(-32768), -1.0);
    }
}
