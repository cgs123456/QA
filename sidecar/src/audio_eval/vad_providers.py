"""VAD 决策适配器（task19 测量用）：两种 provider，统一输出逐帧 bool。

- `WebrtcAdapter(mode=2)` —— `webrtcvad` wheel（libfvad 算法，mode 映射 0..=3
  与 Rust `webrtc-vad` crate 同源）。**诚实注记**：Python wheel 与 Rust crate
  是同一算法的不同二进制，runner 数字用于方法验证 + 相对对比；绝对值须在
  Rust 侧同一样本复核（步骤见 audio-regression.md），不得直接当生产结论。
- `SileroAdapter()` —— `silero-vad` PyPI 包自带的 ONNX（onnxruntime CPU），
  阈值默认 0.5（上游默认）。silero 要 512 点窗、管线帧是 480 点：每帧决策用
  最近 512 点（32 点交叠），hop 保持 480（帧边界与 webrtc 完全对齐，
  边界误差才可比）。
- 两适配器都只吃 **16k 单声道 int16**（R11：生产管线 VAD 输入即此；runner 拒收
  非 16k/mono/16-bit wav，不做重采样——重采样会污染 VAD 对比）。
- 懒 import：缺 `webrtcvad`/`onnxruntime`/`silero_vad` 时构造即抛
  `VadUnavailable`（runner 转 PENDING 行，不崩全表）。

帧长恒 30ms = 480 点 = 960 字节 int16 LE。
"""

FRAME_SAMPLES = 480
FRAME_BYTES = FRAME_SAMPLES * 2
SAMPLE_RATE = 16000


class VadUnavailable(Exception):
    pass


class WebrtcAdapter:
    """libfvad（经 webrtcvad wheel），aggressiveness 初值 2（与生产默认同值）."""

    name = "webrtc-vad"

    def __init__(self, mode: int = 2):
        try:
            import webrtcvad
        except ImportError as e:
            raise VadUnavailable("缺 webrtcvad（pip install webrtcvad-wheels）") from e
        if mode not in (0, 1, 2, 3):
            raise ValueError(f"aggressiveness 非法：{mode}")
        self._vad = webrtcvad.Vad(mode)
        self.mode = mode

    def decide(self, frames_i16: list) -> list:
        """每帧 bytes(960B) → bool 列表。长度非法直接抛（测量代码不吞数据问题）."""
        out = []
        for f in frames_i16:
            if len(f) != FRAME_BYTES:
                raise ValueError(f"VAD 帧长须 {FRAME_BYTES}B，实得 {len(f)}B")
            out.append(bool(self._vad.is_speech(f, SAMPLE_RATE)))
        return out


class SileroAdapter:
    """silero-vad ONNX（经 PyPI silero-vad 包自带权重 + onnxruntime CPU）."""

    name = "silero-vad"
    WINDOW = 512  # 上游推荐窗
    THRESHOLD = 0.5  # 上游默认阈值（调参是 W4/标定的事，本轮不动）

    def __init__(self, threshold: float = THRESHOLD):
        try:
            import onnxruntime  # noqa: F401
        except ImportError as e:
            raise VadUnavailable("缺 onnxruntime") from e
        try:
            from silero_vad import load_silero_vad
        except ImportError as e:
            raise VadUnavailable("缺 silero-vad（pip install silero-vad）") from e
        self._model = load_silero_vad(onnx=True)
        self.threshold = threshold
        self._reset_state()

    def _reset_state(self):
        self._model.reset_states()
        self._ring = bytearray()  # 最近待消费的 int16 字节流

    def reset(self):
        """样本间必须调：LSTM 状态不清会把上一样本的尾音带进来."""
        self._reset_state()

    def decide(self, frames_i16: list) -> list:
        """hop=480 对齐 webrtc，每帧看最近 512 点（含 32 点交叠）。"""
        import struct

        import numpy as np
        import torch

        out = []
        for f in frames_i16:
            if len(f) != FRAME_BYTES:
                raise ValueError(f"VAD 帧长须 {FRAME_BYTES}B，实得 {len(f)}B")
            self._ring += f
            # need 512 点 = 1024B；ring 最多攒 1024+960B，有界。
            window = bytes(self._ring[-1024:])
            if len(window) < 1024:
                window = b"\x00" * (1024 - len(window)) + window  # 段首零填充
            pcm = np.frombuffer(window, dtype="<i2").astype("float32") / 32768.0
            # OnnxWrapper 只吃 torch.Tensor；经 tolist() 构造，绕开
            # torch.from_numpy（本机 torch/numpy 互操作有 warning，见 benchmark）。
            chunk = torch.tensor(pcm.tolist(), dtype=torch.float32)
            prob = float(self._model(chunk, SAMPLE_RATE).item())
            out.append(prob >= self.threshold)
            # 只保留后 32 点供下一帧交叠，其余丢弃（有界）。
            self._ring = bytearray(self._ring[-64:])
        return out
