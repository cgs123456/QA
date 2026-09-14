//! DoD harness — record N seconds of the default render device's mix (WASAPI
//! loopback) and write the resulting int16 frame sequence to a WAV file, then
//! report the numbers the Phase 1b acceptance criteria ask for.
//!
//! Usage:
//!   cargo run --release --example record_loopback -- [seconds] [out.wav]
//! Defaults: 10 seconds, `loopback-10s.wav`.
//!
//! Play something before running it if you want to *hear* the capture; with a
//! silent desktop the engine still delivers (silent) packets, so the frame
//! plumbing, timestamps and WAV output are verified either way. The harness
//! says which of the two it observed instead of pretending they are the same.

#[cfg(target_os = "windows")]
fn main() {
    if let Err(e) = windows_impl::run() {
        eprintln!("record_loopback failed: {e}");
        std::process::exit(1);
    }
}

#[cfg(not(target_os = "windows"))]
fn main() {
    eprintln!(
        "record_loopback exercises WASAPI loopback and is Windows-only. \
         The mic/monitor paths are covered by `cargo test`."
    );
    std::process::exit(2);
}

#[cfg(target_os = "windows")]
mod windows_impl {
    use std::time::{Duration, Instant};

    use interview_copilot_lib::audio::loopback::{EventQueue, FrameQueue, LoopbackSource};
    use interview_copilot_lib::audio::{open_default_loopback, Frame16k};

    const FRAME_MS: u64 = 30;
    const FRAME_SAMPLES: usize = 480;

    pub fn run() -> Result<(), String> {
        let args: Vec<String> = std::env::args().skip(1).collect();
        let seconds: u64 = match args.first() {
            Some(s) => s
                .parse()
                .map_err(|_| format!("bad seconds argument: {s}"))?,
            None => 10,
        };
        if seconds == 0 || seconds > 3600 {
            return Err(format!("seconds out of range: {seconds}"));
        }
        let out = args
            .get(1)
            .cloned()
            .unwrap_or_else(|| format!("loopback-{seconds}s.wav"));

        let frames = FrameQueue::new(64);
        let events = EventQueue::new(32);
        let mut source = open_default_loopback();
        let started = Instant::now();
        source
            .start(frames.clone(), events.clone())
            .map_err(|e| e.to_string())?;
        println!("[ok]   capture started in {:?}", started.elapsed());

        // Report whatever the capture thread already told us.
        let mut native = None;
        while let Some(ev) = events.try_pop() {
            if let interview_copilot_lib::audio::CaptureEvent::Started {
                device,
                sample_rate,
                channels,
            } = &ev
            {
                native = Some((device.clone(), *sample_rate, *channels));
            }
            println!("[event] {ev:?}");
        }
        let (device, native_rate, native_channels) =
            native.ok_or_else(|| "capture started but no Started event arrived".to_string())?;
        println!(
            "[info] device={device:?} native={native_rate}Hz/{native_channels}ch -> 16000Hz/1ch"
        );

        let expected_frames = seconds * 1000 / FRAME_MS;
        let deadline = Instant::now() + Duration::from_secs(seconds + 5);
        let mut collected: Vec<Frame16k> = Vec::with_capacity(expected_frames as usize + 8);
        while (collected.len() as u64) < expected_frames && Instant::now() < deadline {
            if let Some(f) = frames.pop_timeout(Duration::from_millis(200)) {
                collected.push(f);
            }
            while let Some(ev) = events.try_pop() {
                println!("[event] {ev:?}");
            }
        }
        source.stop();
        let elapsed = started.elapsed();

        // --- write the int16 sequence as 16 kHz mono WAV -------------------
        let spec = hound::WavSpec {
            channels: 1,
            sample_rate: 16_000,
            bits_per_sample: 16,
            sample_format: hound::SampleFormat::Int,
        };
        let mut writer = hound::WavWriter::create(&out, spec).map_err(|e| e.to_string())?;
        for f in &collected {
            for s in f.pcm_i16 {
                writer.write_sample(s).map_err(|e| e.to_string())?;
            }
        }
        writer.finalize().map_err(|e| e.to_string())?;

        // --- invariants ---------------------------------------------------
        let int16_samples = collected.len() * FRAME_SAMPLES;
        let f32_samples: usize = collected.iter().map(|f| f.pcm_f32.len()).sum();
        let seq_ok = collected
            .iter()
            .enumerate()
            .all(|(i, f)| f.seq == collected[0].seq.wrapping_add(i as u32));
        let ts_ok = collected
            .iter()
            .enumerate()
            .all(|(i, f)| f.ts_ms == collected[0].ts_ms.wrapping_add(i as u64 * FRAME_MS));
        let peak = collected
            .iter()
            .flat_map(|f| f.pcm_i16.iter())
            .map(|s| (*s as i32).unsigned_abs())
            .max()
            .unwrap_or(0);

        let stats = source.stats();
        let captured_ms = collected.len() as u64 * FRAME_MS;
        let drift_ms = (seconds * 1000).abs_diff(captured_ms);

        println!("\n=== record_loopback report ===");
        println!("device            : {device}");
        println!("wall elapsed      : {elapsed:?} (requested {seconds}s)");
        println!(
            "frames            : {} (expected {expected_frames})",
            collected.len()
        );
        println!("int16 samples     : {int16_samples}");
        println!("float32 samples   : {f32_samples}");
        println!(
            "first ts_ms       : {}",
            collected.first().map(|f| f.ts_ms).unwrap_or(0)
        );
        println!(
            "last ts_ms        : {}",
            collected.last().map(|f| f.ts_ms).unwrap_or(0)
        );
        println!("drift vs wallclock: {drift_ms} ms");
        println!("frames dropped    : {}", stats.frames_dropped);
        println!("peak |int16|      : {peak}");

        let mut failed = Vec::new();
        if (collected.len() as u64) < expected_frames {
            failed.push(format!(
                "captured {} of {expected_frames} frames",
                collected.len()
            ));
        }
        if int16_samples != f32_samples {
            failed.push(format!(
                "int16/f32 length mismatch: {int16_samples} vs {f32_samples}"
            ));
        }
        if !seq_ok {
            failed.push("sequence numbers are not contiguous".to_string());
        }
        if !ts_ok {
            failed.push("timestamps are not sample-count derived".to_string());
        }
        if drift_ms > 50 {
            failed.push(format!(
                "timestamp drift {drift_ms} ms exceeds the 50 ms budget"
            ));
        }
        if stats.frames_dropped > 0 {
            failed.push(format!("{} frames were dropped", stats.frames_dropped));
        }

        println!("wav               : {out}");
        if failed.is_empty() {
            println!("verdict           : PASS (frame/ts/WAV invariants)");
            if peak == 0 {
                println!(
                    "note              : capture was silent — play audio and listen to {out} \
                     to confirm content, this run only proves the plumbing"
                );
            } else {
                println!(
                    "note              : non-silent capture; listen to {out} to confirm content"
                );
            }
            Ok(())
        } else {
            for f in &failed {
                eprintln!("[FAIL] {f}");
            }
            Err(format!("{} invariant(s) failed", failed.len()))
        }
    }
}
