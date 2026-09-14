"""ASR 输出文本清洗（纯函数，可单测）。

存在的理由：SenseVoice 的原始输出**不是纯文本**。它附带结构标签
（语言 / 情感 / 事件 / ITN 标记），形如：

    <|zh|><|NEUTRAL|><|Speech|><|woitn|>开放时间早上九点至下午五点

这些标签直接上屏会污染提词框，也会污染检索（标签混进 3-gram 指纹，
去重与检索都会算错）。故在 provider 出口统一清洗，而不是让每个消费方各自处理。

**实测结论（sherpa-onnx 1.13.8）**：该版本已把标签解析进 `result` 的独立字段
（`lang` / `emotion` / `event`），`result.text` 是干净的。所以本模块的正则路径
在**当前版本上是空操作**，作为**防御层**保留：上游若改回内联标签（官方文档里
就出现过内联形式），或换用别的导出，清洗仍然生效。行为由单测锁定。

清洗纪律：**只删标签，不改内容**。不做同音字纠正、不做繁简转换、不做标点补全 ——
那些会掩盖识别错误，让 WER 看起来比实际好。
"""

import re

# 结构标签。两种形态都要吃：
#   规范   `<|zh|>` / `<|NEUTRAL|>` / `<|woitn|>`
#   畸形   `<|Event_UNK>|`（官方文档里出现过，`>` 与 `|` 顺序颠倒）
# 故尾部写成 `[>|]\|*`：先吃一个 `>` 或 `|`，再吃掉紧随的若干 `|`。
# 要求以 `<|` 起头，所以正文里孤立的 `<` 或 `|` 不会被误删。
_TAG = re.compile(r"<\|[^<>]*[>|]\|*")
# 句级特殊符号（tokens.txt 里的 `<unk> 0` / `<s> 1` / `</s> 2`）。
_SPECIAL = re.compile(r"</?s>|<unk>|<pad>|<blank>|<eps>")

# CJK 范围：中日韩统一表意文字 + 扩展 A + 兼容表意 + 日文假名 + 谚文。
_CJK = "\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af"
_CJK_GAP = re.compile(rf"(?<=[{_CJK}])\s+(?=[{_CJK}])")

# BPE / SentencePiece 的续接与空格标记（paraformer 的 tokens.txt 里是 `and@@`）。
# 正常解码路径不产出它们；真出现说明解码没走完，删掉好过把 `and@@` 直接上屏。
_BPE = re.compile(r"@@|▁")

# 标签分类用的固定集合 —— 不用「全大写就是情感」这类启发式：
# `BGM` 也是全大写，但它是**事件**，会被启发式误判。
LANGUAGES = frozenset({"zh", "en", "ja", "ko", "yue"})
EMOTIONS = frozenset({
    "NEUTRAL", "HAPPY", "SAD", "ANGRY", "FEARFUL", "DISGUSTED", "SURPRISED",
    "EMO_UNKNOWN",
})


def strip_tag(value: str) -> str:
    """`'<|zh|>'` → `'zh'`；`'zh'` → `'zh'`；`'<|Event_UNK>|'` → `'Event_UNK'`。

    只剥分隔符（`<` `>` `|`），**不套用 `_TAG` 正则** —— 输入本身就是标签，
    用标签正则去替换会把整个输入吃掉（初版就踩了这个坑，返回全空）。
    """
    if not value:
        return ""
    return value.strip().strip("<>|").strip()


def clean_tags(text: str) -> str:
    """删结构标签与特殊符号，压空白。**不改变正文内容**。"""
    if not text:
        return ""
    out = _TAG.sub(" ", text)
    out = _SPECIAL.sub(" ", out)
    out = _BPE.sub("", out)
    # 先整体压空白，再吃掉「中文字与中文字之间」的空格
    #（模型偶尔逐字空格输出；中文正文里不该有空格）。
    out = re.sub(r"\s+", " ", out).strip()
    out = _CJK_GAP.sub("", out)
    return out.strip()


def clean_sense_voice(text: str) -> str:
    """SenseVoice 专用清洗（当前与 `clean_tags` 同实现）。

    单列一个函数而非直接复用：SenseVoice 的标签集可能随上游版本变化
    （如新增情感标签），届时改动只落在这里，不必动共享清洗。
    """
    return clean_tags(text)


def clean_paraformer(text: str) -> str:
    """Paraformer 清洗。它不带情感/事件标签，走同一套兜底即可。"""
    return clean_tags(text)


def extract_sense_voice_tags(text: str) -> dict:
    """从**文本**里刮出结构标签（防御路径；正常版本走 `tags_from_result`）。

    返回值只含标签本身，不含转写正文 —— 可安全进日志/事件（R14）。
    """
    found: dict = {}
    for raw in _TAG.findall(text or ""):
        inner = strip_tag(raw)
        if not inner:
            continue
        low = inner.lower()
        if low.endswith("itn"):
            found["itn"] = inner
        elif low in LANGUAGES:
            found["lang"] = inner
        elif inner in EMOTIONS:
            found["emotion"] = inner
        else:
            found["event"] = inner
    return found


def tags_from_result(result) -> dict:
    """从 sherpa-onnx 的 `result` 对象读结构标签（1.13.8 的主路径）。

    只取标签，不碰 `text` —— 返回值可安全进诊断事件（R14）。
    """
    found: dict = {}
    for key in ("lang", "emotion", "event"):
        value = strip_tag(getattr(result, key, "") or "")
        if value:
            found[key] = value
    return found


__all__ = [
    "EMOTIONS", "LANGUAGES", "clean_paraformer", "clean_sense_voice", "clean_tags",
    "extract_sense_voice_tags", "strip_tag", "tags_from_result",
]
