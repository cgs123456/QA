"""ASR 输出文本清洗单测（task17）：纯函数，无权重、无第三方依赖。

清洗纪律（见 `asr/text_clean.py`）：只删标签，不改内容。
本文件锁定该纪律 —— 任何"顺手纠正同音字/补标点"的改动都会在这里变红。
"""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from asr.text_clean import (  # noqa: E402
    clean_paraformer,
    clean_sense_voice,
    clean_tags,
    extract_sense_voice_tags,
    strip_tag,
    tags_from_result,
)


def test_sense_voice_prefix_stripped_content_kept():
    raw = "<|zh|><|NEUTRAL|><|Speech|><|woitn|>开放时间早上九点至下午五点"
    assert clean_sense_voice(raw) == "开放时间早上九点至下午五点"


def test_malformed_tag_gt_pipe_swapped_still_stripped():
    # 上游文档里出现过的畸形 `<|Event_UNK>|`（`>` 与 `|` 写反）同样要吃掉。
    assert clean_sense_voice("<|Event_UNK>|你好") == "你好"


def test_content_not_rewritten():
    # 同音字/错字原样保留 —— 清洗层掩盖识别错误会让 WER 失真。
    assert clean_sense_voice("开饭时间早上九点") == "开饭时间早上九点"
    assert clean_paraformer("开放时间早上九点") == "开放时间早上九点"


def test_empty_and_plain_passthrough():
    assert clean_tags("") == ""
    assert clean_tags("今天天气不错") == "今天天气不错"


def test_special_tokens_removed():
    assert clean_tags("你好<unk>世界") == "你好世界"
    assert clean_tags("<s>你好</s>") == "你好"


def test_bpe_continuation_removed():
    assert "@@" not in clean_tags("and@@好")
    assert "▁" not in clean_tags("▁你好")


def test_cjk_gap_spaces_collapsed_but_mixed_kept():
    # 模型偶发的逐字空格（中文之间）吃掉；中英之间的空格保留。
    assert clean_tags("开 放 时 间") == "开放时间"
    assert clean_tags("中文 English 混排") == "中文 English 混排"


def test_stray_angle_bracket_kept():
    # 正文里孤立的 `<` 不以 `<|` 起头，不会被误删。
    assert clean_tags("a < b") == "a < b"


def test_strip_tag_peels_delimiters_only():
    assert strip_tag("<|zh|>") == "zh"
    assert strip_tag("zh") == "zh"
    assert strip_tag("<|Event_UNK>|") == "Event_UNK"
    assert strip_tag("") == ""


def test_extract_tags_classified_without_text():
    found = extract_sense_voice_tags("<|zh|><|NEUTRAL|><|Speech|><|woitn|>正文")
    assert found["lang"] == "zh"
    assert found["emotion"] == "NEUTRAL"
    assert found["event"] == "Speech"
    assert found["itn"] == "woitn"
    # 返回值只含标签 —— 可安全进日志（R14），正文绝不能出现。
    assert "正文" not in str(found)


def test_extract_bgm_is_event_not_emotion():
    # 全大写的不一定是情感：BGM 是事件。启发式会误判，故用固定集合。
    assert extract_sense_voice_tags("<|BGM|>")["event"] == "BGM"


def test_tags_from_result_reads_fields_not_text():
    result = SimpleNamespace(text="干净正文", lang="<|zh|>", emotion="NEUTRAL", event="Speech")
    found = tags_from_result(result)
    assert found == {"lang": "zh", "emotion": "NEUTRAL", "event": "Speech"}


def test_tags_from_result_empty_when_no_fields():
    assert tags_from_result(SimpleNamespace(text="正文")) == {}
