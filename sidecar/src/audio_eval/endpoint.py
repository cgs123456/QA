"""参考端点状态机（task19 测量用，非生产路径）。

参数逐字取自 PROGRESS「下一项」①（1b-4 定稿、endpoint.rs 待落地）：
- 起始 = 最近 5 帧中 ≥3 帧 voiced；
- 结束 = 连续 ~500ms 静音 hangover（500/30 = 16.67 → **17 帧**）；
- 最短段 250ms 丢弃（8 帧 = 240ms < 250 → 丢弃；**语音部分 ≥9 帧保留**；
  注意是**语音帧数**，hangover 静音不计入——否则 5 帧语音 + 17 帧 hangover
  会被误留）；
- 最长段 15s 强制切段（**500 帧**封顶，超了就地封段、后续语音按起始规则重开）；
- `vad_state` 心跳每 1s —— Rust 侧遥测，runner 不涉及。

输入：逐帧 bool（True = voiced），帧长恒 30ms。
输出：`[(start_frame, end_frame)]`（闭区间，含端点），按时间排序。

边界语义（裁定，单测锁定）：
- onset 取触发窗口内**第一个 voiced 帧**（不是窗口末帧——否则系统性 +120ms 偏置）；
- offset 取**最后一个 voiced 帧**（hangover 本身不计入段内，段长也不含它）；
- 强制切段的封口帧 = start+499，后续帧从 idle 重走起始规则（同一串长语音会
  产生 back-to-back 两段，这是“切段”的字面含义，不是合并；新段 onset
  以上一段封口为下限钳位，不回看已消费帧——否则两段交叠，误差表没法算）。
"""

FRAME_MS = 30

START_WINDOW = 5
START_MIN_VOICED = 3
END_SILENCE_FRAMES = 17  # ~500ms hangover
MIN_KEEP_FRAMES = 9  # 语音部分 ≥270ms 保留（250ms 规则的上取整）
MAX_SEG_FRAMES = 500  # 15s


def detect_segments(decisions: list) -> list:
    """逐帧 voiced 序列 → 段列表。纯函数，O(n)。"""
    segs: list = []
    n = len(decisions)
    i = 0
    in_seg = False
    seg_start = 0
    last_voiced = -1
    silence_run = 0
    floor = 0  # 新段 onset 下限 = 上一段封口 + 1（防切段后回看交叠）

    def _close(end_frame: int):
        # 段长只数语音帧：end_frame 恒为 last_voiced。
        if end_frame - seg_start + 1 >= MIN_KEEP_FRAMES:
            segs.append((seg_start, end_frame))
            return end_frame + 1
        return floor

    while i < n:
        v = bool(decisions[i])
        if not in_seg:
            lo = max(0, i - START_WINDOW + 1)
            window = decisions[lo:i + 1]
            if sum(1 for x in window if x) >= START_MIN_VOICED:
                # onset = 窗口内第一个 voiced 帧，但不低于上一段封口。
                for j in range(lo, i + 1):
                    if decisions[j]:
                        seg_start = max(j, floor)
                        break
                in_seg = True
                last_voiced = i if v else seg_start
                silence_run = 0 if v else 1
            i += 1
        else:
            if v:
                last_voiced = i
                silence_run = 0
            else:
                silence_run += 1
                if silence_run >= END_SILENCE_FRAMES:
                    floor = _close(last_voiced)
                    in_seg = False
                    silence_run = 0
                    i += 1
                    continue
            # 强制切段按“已消费帧数”计（含段内静音间隙，不含 hangover——
            # hangover 出现即已封口）。
            if i - seg_start + 1 >= MAX_SEG_FRAMES:
                floor = _close(seg_start + MAX_SEG_FRAMES - 1)
                in_seg = False
                silence_run = 0
                # last_voiced 下一段重判时重建；本帧已消费，不回退。
            i += 1
    if in_seg:
        # 音频结束时段还开着：封口到最后一个 voiced 帧。
        _close(last_voiced)
    return segs


def to_ms(frames_seg: tuple) -> tuple:
    """帧闭区间 → ms 闭区间（start_ms = s*30，end_ms = e*30+30）。"""
    s, e = frames_seg
    return (s * FRAME_MS, e * FRAME_MS + FRAME_MS)
