"""回归指标纯计算（task19）：CER/WER、边界匹配、三张表的行聚合。

- 中文按字算 CER（去空白后逐字 Levenshtein）；英文按词算 WER（小写分词）。
  不引入 jiwer 等依赖：两函数 + 单测即自足，口径写死在报告里。
- 边界匹配：期望段与检出段按最大重叠贪心配对（重叠>0 才算配上）；
  配上 → onset/offset 误差；配不上 → missed（期望落空）/ false（误触发）。
- R14：只处理数字与布尔；transcript 文本是参数，中转不落地。
"""

import re

_WS = re.compile(r"\s+")


def levenshtein(a: list, b: list) -> int:
    """标准 DP（O(lenA*lenB)；转写短句量级，无需优化）。"""
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        cur = [i]
        for j, y in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (x != y)))
        prev = cur
    return prev[-1]


def tokenize_for_wer(text: str, lang: str) -> list:
    """zh → 去空白逐字；其他 → 小写分词。口径固定，报告引用本函数名."""
    if lang == "zh":
        return [c for c in _WS.sub("", text or "")]
    return (text or "").lower().split()


def error_rate(ref: str, hyp: str, lang: str) -> dict:
    """→ {"errors", "total", "rate"}；空 ref 且空 hyp = 0 错（全对），空 ref 非空 hyp 全错."""
    r = tokenize_for_wer(ref, lang)
    h = tokenize_for_wer(hyp, lang)
    if not r:
        return {"errors": len(h), "total": 0, "rate": 0.0 if not h else 1.0}
    e = levenshtein(r, h)
    return {"errors": e, "total": len(r), "rate": e / len(r)}


def _overlap(a: tuple, b: tuple) -> int:
    return max(0, min(a[1], b[1]) - max(a[0], b[0]))


def match_segments(expected: list, detected: list) -> dict:
    """期望段(ms闭区间) vs 检出段 → 配对 + 误差 + missed/false。

    贪心：按期望段顺序，每段取剩余检出中重叠最大者（>0）。返回：
    {"pairs": [(exp_idx, det_idx, onset_err_ms, offset_err_ms)], "missed": [...], "false": [...]}
    误差符号：正 = 检出晚于/长于期望（onset 正偏 = 切晚了/漏头，offset 正偏 = 拖尾）。
    """
    pairs: list = []
    used = set()
    missed: list = []
    for ei, exp in enumerate(expected):
        best, best_ov = -1, 0
        for di, det in enumerate(detected):
            if di in used:
                continue
            ov = _overlap((exp[0], exp[1]), (det[0], det[1]))
            if ov > best_ov:
                best, best_ov = di, ov
        if best >= 0:
            used.add(best)
            det = detected[best]
            pairs.append((ei, best, det[0] - exp[0], det[1] - exp[1]))
        else:
            missed.append(ei)
    false = [di for di in range(len(detected)) if di not in used]
    return {"pairs": pairs, "missed": missed, "false": false}


def summarize_boundary(match: dict) -> dict:
    """配对集合 → 平均绝对误差（ms）+ 计数。无配对时误差记 None（不是 0——0 会伪装成完美）."""
    pairs = match["pairs"]
    if not pairs:
        return {"n_matched": 0, "mean_abs_onset_ms": None,
                "mean_abs_offset_ms": None,
                "n_missed": len(match["missed"]), "n_false": len(match["false"])}
    on = sum(abs(p[2]) for p in pairs) / len(pairs)
    off = sum(abs(p[3]) for p in pairs) / len(pairs)
    return {"n_matched": len(pairs), "mean_abs_onset_ms": on,
            "mean_abs_offset_ms": off,
            "n_missed": len(match["missed"]), "n_false": len(match["false"])}
