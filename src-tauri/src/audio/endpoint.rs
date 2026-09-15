//! 段端点状态机（M2-7 生产落地）。
//!
//! **规格冻结**，逐字取自 `sidecar/src/audio_eval/endpoint.py` + `docs/audio-regression.md` §1：
//!
//! - 起始 = 最近 5 帧中 ≥3 帧 voiced；
//! - 结束 = 连续 17 帧静音 hangover（500/30 = 16.67 → 17）；
//! - 最短段：`offset - onset + 1 >= 9` 保留，否则丢弃（hangover 静音不计入，
//!   因为 `offset` 恒为**最后一个 voiced 帧**）；
//! - 最长段：500 帧（15s）强制切段，封口帧 = `onset + 499`，后续帧从 idle 重走
//!   起始规则，新段 onset 以封口 +1 为下限钳位（不回看已消费帧，否则两段交叠）；
//! - `vad_state` 心跳每 1s（Rust 侧遥测；测量 runner 不涉及）。
//!
//! 边界语义（与参考实现逐条对齐，单测锁定）：
//!
//! - onset 取触发窗口内**第一个 voiced 帧**（不是窗口末帧 —— 否则系统性 +120ms 偏置）；
//! - offset 取**最后一个 voiced 帧**（hangover 本身不计入段内，段长也不含它）；
//! - 丢弃的短段**不推进 floor**（参考实现 `_close` 未保留时返回原 `floor`）。
//!
//! # 参考实现是批处理，生产要上线上行：多出来的一个概念
//!
//! `endpoint.py::detect_segments` 吃整段决策、吐段列表 —— 它可以在**段结束时**
//! 才决定这一段留不留。实时管线不行：`segment_start` 必须**先于**该段音频帧上线
//! （sidecar 对无段音频只计 `unsolicited_audio` 后丢弃），而"留不留"要到段尾才知道。
//!
//! 本模块因此引入一个**只加不改**的概念：**确认（commit）**。
//!
//! > 段在出现**第一个满足 `last_voiced >= onset + 8` 的 voiced 帧**时被确认。
//!
//! 该条件一旦成立，`offset >= onset + 8` 就与后续帧无关了（`offset` 只增不减），
//! 于是"保留"已成事实，可以安全上线。反之在确认前该段仍可能被判为短段丢弃，
//! 调用方必须**缓存** onset 起的帧、暂不上行（`Decisions::open` 才放行）。
//!
//! 确认点最晚出现在哪里：起始分支的 `silence_run <= 1`，所以触发后最多再静音
//! 16 帧就会被 hangover 关掉；故确认最晚发生在触发帧 + 17，缓存上限 = 5（窗口）
//! + 17 = 22 帧 ≈ 660ms。见 [`HOLD_CAPACITY`]。
//!
//! **不变量：强制切段一定落在已确认的段上**（故 `open` 与 `close` 不会同帧发生）。
//! 证明：若 `last_voiced < onset + 8` 而段活到 `onset + 499`，则
//! `onset+last_voiced+1 .. onset+499` 是 ≥492 帧连续静音，早在 17 帧时就已 hangover 关闭。
//! `forced_cut_never_lands_on_an_unconfirmed_segment` 用随机串把这条不变量钉住。

use std::fmt;

/// 帧长（与 `frame::FRAME_MS` 同源；本模块是纯状态机，不引 frame 以免循环依赖）。
pub const FRAME_MS: u64 = 30;

/// 起始判定窗口（帧）。
pub const START_WINDOW: usize = 5;
/// 起始判定阈值：窗口内至少这么多帧 voiced。
pub const START_MIN_VOICED: usize = 3;
/// 结束 hangover：连续这么多帧静音即封口（≈500ms）。
pub const END_SILENCE_FRAMES: u64 = 17;
/// 最短段跨度（帧）：`offset - onset + 1` 低于此值即丢弃。
pub const MIN_KEEP_FRAMES: u64 = 9;
/// 最长段（帧）：500 × 30ms = 15s。
pub const MAX_SEG_FRAMES: u64 = 500;
/// `vad_state` 心跳间隔。
pub const VAD_HEARTBEAT_MS: u64 = 1000;

/// 确认前必须缓存的帧数上限：起始窗口 5 + 触发后最多 16 帧静音 + 1 帧确认。
///
/// 触发帧本身已含在窗口内，故实际是 `START_WINDOW + (END_SILENCE_FRAMES - 1)`。
pub const HOLD_CAPACITY: usize = START_WINDOW + (END_SILENCE_FRAMES as usize - 1);

/// 一个保留段，帧闭区间 `[onset, offset]`（含端点）。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct Segment {
    /// 段首帧（窗口内第一个 voiced 帧）。
    pub onset: u64,
    /// 段尾帧（最后一个 voiced 帧，不含 hangover）。
    pub offset: u64,
}

impl Segment {
    /// 上线用的起始时刻：`onset × 30ms`。
    pub fn start_ms(&self) -> u64 {
        self.onset * FRAME_MS
    }

    /// 上线用的结束时刻：`(offset + 1) × 30ms`（闭区间右端之后一帧的边界）。
    ///
    /// 与 sidecar 的 `duration_ms = ts_end - ts_start` 对齐：区间 `[0,99]`
    /// 给出 3000ms，正是 100 帧 × 30ms。
    pub fn end_ms(&self) -> u64 {
        (self.offset + 1) * FRAME_MS
    }

    /// 段长（ms）。
    pub fn span_ms(&self) -> u64 {
        self.end_ms() - self.start_ms()
    }

    /// 段跨帧数（含端点）。
    pub fn frames(&self) -> u64 {
        self.offset - self.onset + 1
    }
}

/// 一次封口。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Close {
    pub segment: Segment,
    /// 该段是否曾被确认（`false` = 短段被丢弃，线上从未出现过 `segment_start`）。
    pub committed: bool,
}

/// 一帧产生的全部决策（最多三项，值类型、零分配）。
///
/// 字段独立：`hole` 只在丢帧后出现；`open` / `close` 同帧出现的组合被不变量排除
/// （见模块注释），但仍如实表达，不做静默合并。
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct Decisions {
    /// 因丢帧被迫封口的段（音频不连续，hangover 语义失效）。
    pub hole: Option<Close>,
    /// 本帧确认的段（`Some(onset)`）：onset 起的缓存帧可以上线了。
    pub open: Option<u64>,
    /// 本帧关闭的段。
    pub close: Option<Close>,
}

impl Decisions {
    /// 本帧什么都没发生（帧属于 idle，或属于已确认段的中间）。
    pub fn is_empty(&self) -> bool {
        self.hole.is_none() && self.open.is_none() && self.close.is_none()
    }
}

/// 诊断计数（R14：全部内容无关）。
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, serde::Serialize)]
pub struct EndpointStats {
    /// 喂进来的帧数（不含丢失的）。
    pub frames: u64,
    pub voiced_frames: u64,
    /// 保留并上线的段数。
    pub segments_kept: u64,
    /// 判为短段丢弃的候选段数。
    pub segments_dropped: u64,
    /// 触发 500 帧强制切段的次数。
    pub forced_cuts: u64,
    /// 帧序号跳变次数。
    pub holes: u64,
    /// 因跳变丢失的帧数。
    pub frames_lost: u64,
}

/// 端点状态机。逐帧喂 `(index, voiced)`，读回 [`Decisions`]。
pub struct EndpointState {
    /// 下一帧应有的索引。
    next_index: u64,
    in_seg: bool,
    seg_start: u64,
    last_voiced: u64,
    silence_run: u64,
    /// 新段 onset 下限 = 上一段封口 + 1（防切段后回看交叠）。
    floor: u64,
    /// 本段是否已确认（见模块注释）。
    committed: bool,
    /// 最近 [`START_WINDOW`] 帧的原始决策环形缓冲。
    window: [bool; START_WINDOW],
    /// 窗口内**最低有效帧号**（丢帧后窗口不得回看空洞之前）。
    valid_from: u64,
    stats: EndpointStats,
}

impl Default for EndpointState {
    fn default() -> Self {
        Self::new()
    }
}

impl EndpointState {
    pub fn new() -> Self {
        Self {
            next_index: 0,
            in_seg: false,
            seg_start: 0,
            last_voiced: 0,
            silence_run: 0,
            floor: 0,
            committed: false,
            window: [false; START_WINDOW],
            valid_from: 0,
            stats: EndpointStats::default(),
        }
    }

    pub fn stats(&self) -> EndpointStats {
        self.stats
    }

    /// 当前是否处于**已确认**的段内（调用方据此决定帧是上行还是缓存）。
    pub fn in_committed_segment(&self) -> bool {
        self.in_seg && self.committed
    }

    /// 喂一帧。
    ///
    /// `index` 必须是单调的帧序号（`ts_ms / 30`）。若比上一帧大 1 以上，
    /// 差额被当作**丢帧空洞**处理（见 [`EndpointState::hole`]）。
    pub fn push(&mut self, index: u64, voiced: bool) -> Decisions {
        let mut out = Decisions::default();
        if index > self.next_index {
            out.hole = self.hole(index);
        }
        debug_assert!(
            index >= self.next_index.saturating_sub(1),
            "endpoint frames must be monotonic: got {index}, next {}",
            self.next_index
        );
        self.next_index = self.next_index.max(index + 1);
        self.stats.frames += 1;
        if voiced {
            self.stats.voiced_frames += 1;
        }
        self.window[(index % START_WINDOW as u64) as usize] = voiced;

        if !self.in_seg {
            self.try_start(index, voiced, &mut out);
            return out;
        }

        if voiced {
            self.last_voiced = index;
            self.silence_run = 0;
        } else {
            self.silence_run += 1;
        }

        // 确认：last_voiced 只增不减，一旦跨过 onset + 8，"保留"就与后续帧无关了。
        if !self.committed && self.last_voiced >= self.seg_start + MIN_KEEP_FRAMES - 1 {
            self.committed = true;
            out.open = Some(self.seg_start);
        }

        // 结束：连续 17 帧静音。先于强制切段判定（与参考实现同序）。
        if self.silence_run >= END_SILENCE_FRAMES {
            out.close = Some(self.close_segment(self.last_voiced));
            return out;
        }

        // 强制切段：按"已消费帧数"计（含段内静音间隙，不含 hangover —— hangover
        // 出现即已封口）。封口帧 = onset + 499，不是 last_voiced。
        if index - self.seg_start + 1 >= MAX_SEG_FRAMES {
            self.stats.forced_cuts += 1;
            let offset = self.seg_start + MAX_SEG_FRAMES - 1;
            // 构造上必然已确认（模块注释有证明）；防御性补一个 open，绝不静默丢帧。
            debug_assert!(
                self.committed,
                "forced cut reached an unconfirmed segment — the invariant is broken"
            );
            if !self.committed {
                self.committed = true;
                out.open = Some(self.seg_start);
            }
            out.close = Some(self.close_segment(offset));
        }
        out
    }

    /// 音频结束：参考实现在收尾时对还开着的段做一次 `_close(last_voiced)`。
    pub fn finish(&mut self) -> Decisions {
        let mut out = Decisions::default();
        if self.in_seg {
            out.close = Some(self.close_segment(self.last_voiced));
        }
        out
    }

    /// 起始分支（与 `endpoint.py` 的 `if not in_seg` 逐句对应）。
    fn try_start(&mut self, index: u64, voiced: bool, out: &mut Decisions) {
        let lo = index
            .saturating_sub(START_WINDOW as u64 - 1)
            .max(self.valid_from);
        let mut count = 0usize;
        let mut first_voiced: Option<u64> = None;
        for j in lo..=index {
            if self.window[(j % START_WINDOW as u64) as usize] {
                count += 1;
                if first_voiced.is_none() {
                    first_voiced = Some(j);
                }
            }
        }
        if count < START_MIN_VOICED {
            return;
        }
        let j = first_voiced.expect("count >= 3 implies at least one voiced frame");
        // onset = 窗口内第一个 voiced 帧，但不低于上一段封口。
        self.seg_start = j.max(self.floor);
        self.in_seg = true;
        self.committed = false;
        self.last_voiced = if voiced { index } else { self.seg_start };
        self.silence_run = if voiced { 0 } else { 1 };
        let _ = out;
    }

    /// 封口（与 `endpoint.py` 的 `_close` 逐句对应）。
    ///
    /// 保留 → `floor = offset + 1`；丢弃 → **floor 不变**。
    fn close_segment(&mut self, offset: u64) -> Close {
        let segment = Segment {
            onset: self.seg_start,
            offset,
        };
        let committed = self.committed;
        if offset - self.seg_start + 1 >= MIN_KEEP_FRAMES {
            self.stats.segments_kept += 1;
            self.floor = offset + 1;
        } else {
            self.stats.segments_dropped += 1;
        }
        self.in_seg = false;
        self.committed = false;
        self.silence_run = 0;
        Close { segment, committed }
    }

    /// 丢帧空洞（R13 的代价，必须显式而不是当静音处理）。
    ///
    /// - 已确认的段：立即封口（音频不连续，再等 hangover 只是自欺），
    ///   `offset = last_voiced`；返回该次封口。
    /// - 未确认的候选段：整段丢弃 —— 样本不足，**不凭空补帧**。
    /// - 无论哪种，窗口不再回看空洞之前（否则 onset 会落在丢失的音频上）。
    fn hole(&mut self, index: u64) -> Option<Close> {
        self.stats.holes += 1;
        self.stats.frames_lost += index - self.next_index;
        self.valid_from = index;
        if !self.in_seg {
            return None;
        }
        if self.committed {
            return Some(self.close_segment(self.last_voiced));
        }
        self.stats.segments_dropped += 1;
        self.in_seg = false;
        self.committed = false;
        self.silence_run = 0;
        None
    }
}

impl fmt::Debug for EndpointState {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("EndpointState")
            .field("in_seg", &self.in_seg)
            .field("seg_start", &self.seg_start)
            .field("last_voiced", &self.last_voiced)
            .field("silence_run", &self.silence_run)
            .field("floor", &self.floor)
            .field("committed", &self.committed)
            .field("stats", &self.stats)
            .finish_non_exhaustive()
    }
}

/// 批量跑一段决策序列 → 保留段列表。
///
/// 与 `endpoint.py::detect_segments` 同形；**实现走的就是流式状态机**，
/// 所以批量与实时两条路不可能各说各话。
pub fn detect_segments(decisions: &[bool]) -> Vec<Segment> {
    let mut st = EndpointState::new();
    let mut segs = Vec::new();
    for (i, v) in decisions.iter().enumerate() {
        let d = st.push(i as u64, *v);
        push_close(&mut segs, d.hole);
        push_close(&mut segs, d.close);
    }
    let d = st.finish();
    push_close(&mut segs, d.close);
    segs
}

fn push_close(segs: &mut Vec<Segment>, c: Option<Close>) {
    if let Some(c) = c {
        if c.committed {
            segs.push(c.segment);
        }
    }
}

/// `vad_state` 心跳节拍器（每 [`VAD_HEARTBEAT_MS`] 一次，按音频时间而非墙钟）。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Heartbeat {
    next_ms: u64,
}

impl Default for Heartbeat {
    fn default() -> Self {
        Self::new()
    }
}

impl Heartbeat {
    pub fn new() -> Self {
        Self {
            next_ms: VAD_HEARTBEAT_MS,
        }
    }

    /// 本帧是否该发心跳；该发则把节拍推到下一个整秒。
    pub fn due(&mut self, ts_ms: u64) -> bool {
        if ts_ms < self.next_ms {
            return false;
        }
        // 对齐到 ts_ms 之后的第一个整秒：掉帧/抖动不会让心跳漂移。
        self.next_ms = ts_ms - (ts_ms % VAD_HEARTBEAT_MS) + VAD_HEARTBEAT_MS;
        true
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 把 `(帧数, voiced)` 串展开成逐帧决策。
    fn pat(runs: &[(usize, bool)]) -> Vec<bool> {
        let mut out = Vec::new();
        for (n, v) in runs {
            out.resize(out.len() + n, *v);
        }
        out
    }

    fn segs(runs: &[(usize, bool)]) -> Vec<Segment> {
        detect_segments(&pat(runs))
    }

    // ---- 起始窗口 ----------------------------------------------------------

    #[test]
    fn start_needs_three_voiced_in_five() {
        // 窗口 [F,F,V,V,V] 在第 4 帧首次满足 → onset = 2（窗口内第一个 voiced）。
        let s = segs(&[(2, false), (12, true), (20, false)]);
        assert_eq!(
            s,
            vec![Segment {
                onset: 2,
                offset: 13
            }]
        );
    }

    #[test]
    fn two_voiced_per_window_never_starts() {
        // V V F F F 循环：任意 5 帧窗口最多 2 帧 voiced → 永不触发。
        let mut runs = Vec::new();
        for _ in 0..40 {
            runs.push((2, true));
            runs.push((3, false));
        }
        let d = pat(&runs);
        for w in d.windows(START_WINDOW) {
            assert!(w.iter().filter(|x| **x).count() < START_MIN_VOICED);
        }
        assert!(detect_segments(&d).is_empty());
    }

    #[test]
    fn onset_is_the_first_voiced_frame_not_the_window_end() {
        // 触发发生在第 4 帧，但 onset 必须回指第 2 帧（否则系统性 +60ms 偏置）。
        let s = segs(&[(2, false), (12, true), (20, false)]);
        assert_eq!(s[0].onset, 2);
        assert_eq!(s[0].start_ms(), 60);
    }

    #[test]
    fn onset_is_clamped_so_two_segments_never_overlap() {
        // 首段靠 500 帧强制切段封口；第二段的窗口回看到封口之前，
        // onset 必须被钳到封口 + 1 = 500，否则两段交叠、误差表没法算。
        let s = segs(&[(1000, true)]);
        assert_eq!(
            s,
            vec![
                Segment {
                    onset: 0,
                    offset: 499
                },
                Segment {
                    onset: 500,
                    offset: 999
                },
            ]
        );
        assert!(s[0].offset < s[1].onset, "segments must not overlap");
    }

    // ---- 最短段 ------------------------------------------------------------

    #[test]
    fn min_keep_boundary_nine_frames_kept_eight_dropped() {
        // 恰好 9 帧跨度（onset=2..offset=10）→ 保留。
        let kept = segs(&[(2, false), (9, true), (20, false)]);
        assert_eq!(
            kept,
            vec![Segment {
                onset: 2,
                offset: 10
            }]
        );
        assert_eq!(kept[0].frames(), MIN_KEEP_FRAMES);

        // 恰好 8 帧跨度 → 丢弃。
        let dropped = segs(&[(2, false), (8, true), (20, false)]);
        assert!(dropped.is_empty());
    }

    #[test]
    fn dropped_segment_does_not_advance_the_floor() {
        // 短段丢弃后 floor 不变（参考实现 `_close` 未保留时返回原 floor）：
        // 紧随其后的真段 onset 仍可落在短段之前的窗口里。
        let s = segs(&[(2, false), (4, true), (20, false), (20, true), (20, false)]);
        // 第一段 [2,5] 跨度 4 → 丢弃；第二段从第 26 帧起的 voiced 串触发。
        assert_eq!(s.len(), 1);
        assert!(s[0].onset >= 26, "onset {} looks too early", s[0].onset);
    }

    #[test]
    fn segment_open_at_end_of_stream_is_flushed() {
        // 参考实现收尾 `if in_seg: _close(last_voiced)`：流结束时还开着的段要封口。
        let s = segs(&[(30, true)]);
        assert_eq!(
            s,
            vec![Segment {
                onset: 0,
                offset: 29
            }]
        );
    }

    #[test]
    fn short_segment_at_end_of_stream_is_still_dropped() {
        let s = segs(&[(4, true)]);
        assert!(s.is_empty());
    }

    // ---- 结束 hangover -----------------------------------------------------

    #[test]
    fn hangover_boundary_sixteen_frames_continues_seventeen_closes() {
        // 16 帧静音（<17）不足以封口：后面的 voiced 仍属同一段。
        let one = segs(&[(9, true), (16, false), (10, true), (30, false)]);
        assert_eq!(one.len(), 1);
        assert_eq!(
            one[0],
            Segment {
                onset: 0,
                offset: 34
            }
        );

        // 17 帧静音 → 封口（段尾 offset=8，段长 9 帧刚好够保留）。
        let two = segs(&[(9, true), (17, false), (10, true), (30, false)]);
        assert_eq!(two.len(), 2);
        assert_eq!(
            two[0],
            Segment {
                onset: 0,
                offset: 8
            }
        );
        assert_eq!(
            two[1],
            Segment {
                onset: 26,
                offset: 35
            }
        );
    }

    #[test]
    fn offset_excludes_the_hangover() {
        // 段 [0,8] 后 17 帧静音：offset 必须是 8，不能把 hangover 算进段长。
        let s = segs(&[(9, true), (17, false)]);
        assert_eq!(s[0].offset, 8);
        assert_eq!(s[0].span_ms(), 9 * FRAME_MS);
        assert_eq!(s[0].end_ms(), 270);
    }

    // ---- 最长段 ------------------------------------------------------------

    #[test]
    fn forced_cut_splits_long_speech_into_back_to_back_segments() {
        // 500 帧封顶：1000 帧连续语音 → 两段，封口帧 = onset+499。
        let s = segs(&[(1000, true)]);
        assert_eq!(s.len(), 2);
        assert_eq!(s[0].frames(), MAX_SEG_FRAMES);
        assert_eq!(s[1].frames(), MAX_SEG_FRAMES);
        assert_eq!(s[0].span_ms(), 15_000);
    }

    #[test]
    fn forced_cut_uses_the_cap_frame_not_the_last_voiced_frame() {
        // 强制切段的封口帧恒为 onset+499，即使那一帧是静音（段内间隙）。
        // onset=0，第 100..498 帧静音 —— 但 17 帧 hangover 会先关掉它，
        // 所以这里用「静音不超过 16 帧」的间隙串把段撑到 500 帧。
        let mut d = vec![true; 10];
        while d.len() < 499 {
            d.extend_from_slice(&[false; 16]);
            d.push(true);
        }
        // 现在 d 长约 499，再补到 500 帧
        d.resize(500, false);
        let s = detect_segments(&d);
        assert_eq!(s.len(), 1);
        assert_eq!(s[0].onset, 0);
        assert_eq!(s[0].offset, 499, "cap frame, not last_voiced");
    }

    #[test]
    fn forced_cut_never_lands_on_an_unconfirmed_segment() {
        // 不变量（模块注释有证明）：随机串里 open 与 close 绝不同帧发生。
        let mut state = 0x1234_5678_9abc_def0u64;
        for _ in 0..200 {
            let mut d = Vec::with_capacity(1500);
            for _ in 0..1500 {
                state = state
                    .wrapping_mul(6_364_136_223_846_793_005)
                    .wrapping_add(1_442_695_040_888_963_407);
                // 偏置出长串语音（含 500 帧切段）与稀疏语音两种形态。
                d.push((state >> 60) < 9);
            }
            let mut st = EndpointState::new();
            for (i, v) in d.iter().enumerate() {
                let dec = st.push(i as u64, *v);
                assert!(
                    !(dec.open.is_some() && dec.close.is_some()),
                    "open+close in the same frame at {i}"
                );
            }
        }
    }

    // ---- 丢帧空洞 ----------------------------------------------------------

    #[test]
    fn hole_closes_a_committed_segment_immediately() {
        let mut st = EndpointState::new();
        for i in 0..20 {
            st.push(i, true);
        }
        assert!(st.in_committed_segment());
        // 第 20..39 帧丢了（R13 drop-oldest）。
        let d = st.push(40, false);
        let c = d.hole.expect("a hole must close the open segment");
        assert!(c.committed);
        assert_eq!(
            c.segment,
            Segment {
                onset: 0,
                offset: 19
            }
        );
        assert_eq!(st.stats().holes, 1);
        assert_eq!(st.stats().frames_lost, 20);
    }

    #[test]
    fn hole_drops_an_unconfirmed_candidate_instead_of_faking_frames() {
        let mut st = EndpointState::new();
        for i in 0..4 {
            st.push(i, true);
        }
        let d = st.push(10, true);
        assert!(d.hole.is_none(), "nothing committed yet, nothing to close");
        assert!(d.open.is_none());
        assert_eq!(st.stats().segments_dropped, 1);
        assert!(!st.in_seg);
    }

    #[test]
    fn hole_window_does_not_look_back_before_the_gap() {
        // 空洞前有 3 帧 voiced；空洞后只补 1 帧 —— 不得靠旧帧凑够阈值开段。
        let mut st = EndpointState::new();
        for i in 0..3 {
            st.push(i, true);
        }
        let d = st.push(50, true);
        assert!(d.open.is_none());
        assert!(!st.in_seg, "the onset window must not span the hole");
    }

    #[test]
    fn no_hole_reported_for_contiguous_frames() {
        let mut st = EndpointState::new();
        for i in 0..100 {
            assert!(st.push(i, true).hole.is_none());
        }
        assert_eq!(st.stats().holes, 0);
    }

    // ---- 心跳 --------------------------------------------------------------

    #[test]
    fn heartbeat_ticks_once_per_second_on_audio_time() {
        let mut hb = Heartbeat::new();
        let mut ticks = 0;
        for i in 0..=100u64 {
            if hb.due(i * FRAME_MS) {
                ticks += 1;
            }
        }
        // 0..3000ms 共 3 个整秒（1000/2000/3000）。
        assert_eq!(ticks, 3);
    }

    #[test]
    fn heartbeat_does_not_drift_after_a_gap() {
        let mut hb = Heartbeat::new();
        assert!(!hb.due(900));
        assert!(hb.due(1000));
        // 掉帧跳过 2000ms 整点：下一次触发应仍对齐整秒。
        assert!(hb.due(2400));
        assert!(!hb.due(2900));
        assert!(hb.due(3000));
    }

    // ---- 段几何 ------------------------------------------------------------

    #[test]
    fn segment_geometry_matches_the_wire_contract() {
        let s = Segment {
            onset: 3,
            offset: 12,
        };
        assert_eq!(s.start_ms(), 90);
        assert_eq!(s.end_ms(), 390);
        assert_eq!(s.span_ms(), 300);
        assert_eq!(s.frames(), 10);
    }

    #[test]
    fn decisions_default_is_empty() {
        assert!(Decisions::default().is_empty());
    }

    #[test]
    fn stats_account_for_every_frame() {
        let d = pat(&[(2, false), (12, true), (20, false), (3, true), (20, false)]);
        let mut st = EndpointState::new();
        for (i, v) in d.iter().enumerate() {
            st.push(i as u64, *v);
        }
        st.finish();
        let s = st.stats();
        assert_eq!(s.frames, d.len() as u64);
        assert_eq!(s.voiced_frames, d.iter().filter(|x| **x).count() as u64);
        assert_eq!(s.segments_kept, 1);
        assert_eq!(s.segments_dropped, 1);
        assert_eq!(s.holes, 0);
        assert_eq!(s.forced_cuts, 0);
    }
}
