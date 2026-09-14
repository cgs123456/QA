"""Paraformer 本地 Provider（sherpa-onnx ONNX int8，中文）。

选型理由同 `sensevoice_provider.py`：sherpa-onnx 免 torch，体积约 18 MiB。

**为何两者都留**：SenseVoice 覆盖中英日韩粤（多语种，适合中英混说的面试场景），
Paraformer 是中文专用非自回归模型（中文场景通常更快）。
哪个更适合本产品由 `docs/benchmark.md` 的同样本横向对比决定，不靠印象拍板。

**输入格式**：与 SenseVoice 完全一致 —— 单声道 float32 + 16kHz，
由本 provider 内部交给 sherpa-onnx（它自己抽 fbank 特征）。
PRD §3.5 要求段输入格式在 Protocol 层统一，各 Provider 自行转换，故此处不做任何
WAV 头解析 —— 上游给的就是裸 PCM。

**输出清洗**：paraformer 不带情感/事件标签，但仍走同一套清洗
（`<unk>`/`<s>`/BPE 续接符 `@@` 等兜底）。理由：清洗函数若两套，
总有一天会有一路漏掉新出现的特殊符号。
"""

from .sherpa_base import SherpaOfflineProvider
from .text_clean import clean_paraformer

# 与 registry.PARAFORMER_ZH 同值；单测断言两者一致（避免字面量手滑）。
REGISTRY_KEY = "paraformer-zh"


class ParaformerProvider(SherpaOfflineProvider):
    name = "paraformer"
    registry_key = REGISTRY_KEY

    def __init__(self, model_dir=None, num_threads: int = 1, provider: str = "cpu"):
        super().__init__(model_dir=model_dir, num_threads=num_threads, provider=provider)

    def _make(self):
        import sherpa_onnx

        return sherpa_onnx.OfflineRecognizer.from_paraformer(
            paraformer=self._model_path("model.int8.onnx"),
            tokens=self._model_path("tokens.txt"),
            num_threads=self.num_threads,
            sample_rate=16000,
            feature_dim=80,
            decoding_method="greedy_search",
            debug=False,
            provider=self.provider,
        )

    def clean(self, text: str) -> str:
        return clean_paraformer(text)


__all__ = ["ParaformerProvider", "REGISTRY_KEY"]
