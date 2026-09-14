"""SenseVoice 本地 Provider（sherpa-onnx ONNX int8，中英日韩粤）。

为什么用 sherpa-onnx 而不是 funasr：funasr 依赖 PyTorch，仅 torch 就是 ~2 GB，
而本项目打包态已经是 317.5 MiB —— 加 torch 会让体积翻十倍以上。
sherpa-onnx 是纯 ONNX Runtime，`sherpa-onnx` + `sherpa-onnx-core` 两个 wheel
实测合计约 18 MiB，且原生支持 SenseVoice 与 Paraformer 两者。**体积差两个数量级**，
对「本地优先、免 torch」的定位是决定性因素。

**输出必须清洗**：SenseVoice 会在正文前后附带结构标签
（`<|zh|><|NEUTRAL|><|Speech|><|woitn|>正文`）。不清洗会污染提词框，
也会污染检索（标签混进 3-gram 指纹，去重与检索都会算错）。
清洗只删标签、不改正文 —— 见 `text_clean.py` 的纪律说明。

`use_itn` 默认 **False**：ITN 会把「九点」转成「9点」并补标点，是**文本变换**。
在 100 题评测集告诉我们哪种形式更利于检索之前，先不做未经测量的变换；
需要时可经构造参数打开。
"""

from .sherpa_base import SherpaOfflineProvider
from .text_clean import (
    clean_sense_voice,
    extract_sense_voice_tags,
    tags_from_result,
)

# 与 registry.SENSE_VOICE 同值；单测断言两者一致（避免字面量手滑）。
REGISTRY_KEY = "sense-voice"


class SenseVoiceProvider(SherpaOfflineProvider):
    name = "sensevoice"
    registry_key = REGISTRY_KEY

    def __init__(self, model_dir=None, num_threads: int = 1, provider: str = "cpu",
                 language: str = "", use_itn: bool = False):
        super().__init__(model_dir=model_dir, num_threads=num_threads, provider=provider)
        # "" = 语言自动（中英混说场景不预设）。
        self.language = language
        self.use_itn = use_itn
        # 最近一次的标签快照（诊断用，内容无关：只含 lang/emotion/event，不含正文）。
        self.last_tags: dict = {}

    def _make(self):
        import sherpa_onnx

        return sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=self._model_path("model.int8.onnx"),
            tokens=self._model_path("tokens.txt"),
            num_threads=self.num_threads,
            sample_rate=16000,
            feature_dim=80,
            decoding_method="greedy_search",
            debug=False,
            provider=self.provider,
            language=self.language,
            use_itn=self.use_itn,
        )

    def clean_result(self, result) -> str:
        # 先从结构化字段取标签快照，再清洗文本：
        # 1.13.8 的标签在 `result.lang/emotion/event` 里，不在 `result.text` 里；
        # 2. 清洗是防御层，若上游改回内联标签，快照则回落到正则路径。
        self.last_tags = tags_from_result(result) or extract_sense_voice_tags(
            getattr(result, "text", "") or ""
        )
        return clean_sense_voice(getattr(result, "text", "") or "")

    def clean(self, text: str) -> str:
        return clean_sense_voice(text)

    def describe(self) -> dict:
        return {
            **super().describe(),
            "language": self.language or "auto",
            "use_itn": self.use_itn,
            "last_tags": self.last_tags,
        }


__all__ = ["REGISTRY_KEY", "SenseVoiceProvider"]
