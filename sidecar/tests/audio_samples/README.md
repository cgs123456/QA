# 1b 回归样本集：录制指南（用户录制）

> 状态：**6 个槽位全部待录制**（见 `manifest.json`，各槽 `status: "missing"`）。
> 在真实录音落盘之前，`scripts/audio_regression.py` 的三张实测表保持
> PENDING —— 合成信号只用于 `--self-test` 定标管线本身，**永不计入实测表**
> （1b-4 已证明合成信号在四个 aggressiveness 档全饱和，无判别力）。

## 要录什么（6 个文件，中英 × 安静/嘈杂/多人）

| 槽位 id | 语言 | 条件 | 内容要求 |
|---|---|---|---|
| `zh-quiet-1` | 中文 | 安静房间 | 2~3 句自然说话，句间停顿 ≥1s，含 1 个专有名词 |
| `zh-noisy-1` | 中文 | 嘈杂（键盘/空调/街道底噪其一） | 同上，噪声源在标注里写明 |
| `zh-multi-1` | 中文 | 多人（2 人轮流说） | 每人至少 1 段，段间停顿 ≥1s |
| `en-quiet-1` | 英文 | 安静房间 | 2~3 句自然说话，句间停顿 ≥1s |
| `en-noisy-1` | 英文 | 嘈杂 | 同上 |
| `en-multi-1` | 英文 | 多人 | 同上 |

## 录制格式（硬要求，runner 会校验并拒收不合格）

- 容器：WAV；编码：16-bit PCM；声道：单声道；采样率：**16000 Hz**
  （与管线 `Frame16k` 同源；48k 录音请用 Audacity/ffmpeg 先转：
  `ffmpeg -i in.wav -ac 1 -ar 16000 out.wav`）。
- 每段有效语音 5~15s（含首尾各 ≥0.5s 静音——端点 onset/offset 误差需要静音基线）。
- 电平：峰值 -12dBFS ~ -3dBFS；**关闭**系统降噪/AGC（否则测的是降噪算法，不是 VAD）。

## 标注格式（与音频同名 `.json`，见 `manifest.json` 的 schema 注记）

```json
{
  "transcript": "开放时间早上九点至下午五点",
  "segments": [
    {"start_ms": 600, "end_ms": 2400},
    {"start_ms": 3500, "end_ms": 5900}
  ],
  "noise": "keyboard",
  "speakers": 1
}
```

- `segments`：人耳判定的每段语音起止（ms，±100ms 内可接受——边界误差表以帧为单位，
  30ms/帧，人标抖动远小于待测误差量级才有意义；多人场景不标说话人，只标有声段）。
- `transcript`：整段逐字转写（WER 用；中英文混说原样保留）。
- 标注工具建议：Audacity 看波形打标签 → 手填 ms；不要用 ASR 输出当标注
  （自我交易，`eval-baseline.md` 同一纪律）。

## 存放与版本纪律

- 音频放本目录，文件名 = 槽位 id + `.wav`（如 `zh-quiet-1.wav`），标注同名 `.json`。
- `*.wav` **永不入库**（`.gitignore` 已加规则）；`manifest.json` 入库，
  录制落盘后把对应槽 `status` 改为 `"ready"` 并填 `sha256` + 录制说明。
- 隐私：录音含真实人声，**只放本地**，不进任何云同步目录；转写文本只进报告的
  WER 分子，不贴全文（R14）。
