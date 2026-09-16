"""融合单测：锚定 PRD §3.3 三条结构性案例 + 值域 + 判定边界。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from retrieval.hybrid_rank import (
    TH_DIRECT,
    TH_MAYBE,
    W_FIELD,
    W_JIEBA,
    W_SIMPLE,
    W_SUM,
    W_VEC,
    decide,
    fuse,
)


def _field(fid="f1", s=1.0, entity="公司信息"):
    return {
        "field_id": fid, "entity": entity, "field_name": "n",
        "field_value": "v", "s": s,
    }


def _qa(qid="q1", s=0.0, category="公司信息"):
    return {"qa_id": qid, "standard_question": "q", "official_answer": "a",
            "category": category, "s": s}


def test_field_only_is_half():
    (top,) = fuse(field_hits=[_field()])
    # P6 权重：W_FIELD=1.5, W_SUM=9.5 → 1.5/9.5 ≈ 0.1579
    assert abs(top["score"] - 1.5 / 9.5) < 1e-12
    assert top["type"] == "field"
    assert top["payload"]["field_value"] == "v"


def test_three_fuzzy_full_is_half():
    fused = fuse(
        jieba_hits=[_qa(s=1.0)], simple_hits=[_qa(s=1.0)], vec_hits=[_qa(s=1.0)]
    )
    assert len(fused) == 1
    # P6 权重：W_JIEBA=3.0, W_SIMPLE=1.0, W_VEC=4.0, W_SUM=9.5 → 8.0/9.5
    assert abs(fused[0]["score"] - 8.0 / 9.5) < 1e-12


def test_field_plus_threshold_fuzzy_is_three_quarters():
    # 0.5 为 FTS 双路门限 s（bm25=-0.5）；P6 权重下：
    # field(1.5*1.0) + jieba(3.0*0.5) + simple(1.0*0.5) + vec(4.0*0.5) = 1.5+1.5+0.5+2.0 = 5.5
    # 5.5/9.5 ≈ 0.5789
    fused = fuse(
        field_hits=[_field()],
        jieba_hits=[_qa(s=0.5)],
        simple_hits=[_qa(s=0.5)],
        vec_hits=[_qa(s=0.5)],
    )
    assert len(fused) == 2
    top = fused[0]
    assert top["type"] == "qa"
    assert abs(top["score"] - 5.5 / 9.5) < 1e-12
    assert fused[1]["type"] == "field"
    assert abs(fused[1]["score"] - 1.5 / 9.5) < 1e-12


def test_field_linkage_same_entity_only():
    fused = fuse(
        field_hits=[_field(entity="公司信息")],
        jieba_hits=[_qa("q1", s=0.5, category="公司信息"),
                    _qa("q2", s=0.5, category="其他实体"),
                    _qa("q3", s=0.5, category=None)],
    )
    by_key = {c["key"]: c for c in fused if c["type"] == "qa"}
    # q1: field联动(1.5*1.0) + jieba(3.0*0.5) = 3.0 → 3.0/9.5
    assert by_key["q1"]["s"]["field"] == 1.0
    assert abs(by_key["q1"]["score"] - 3.0 / 9.5) < 1e-12
    # q2/q3: 无联动，仅 jieba(3.0*0.5) = 1.5 → 1.5/9.5
    assert by_key["q2"]["s"]["field"] == 0.0
    assert abs(by_key["q2"]["score"] - 1.5 / 9.5) < 1e-12
    assert by_key["q3"]["s"]["field"] == 0.0
    assert abs(by_key["q3"]["score"] - 1.5 / 9.5) < 1e-12


def test_multi_route_merge_takes_best_per_route():
    (top,) = fuse(
        jieba_hits=[_qa(s=0.3), _qa(s=0.8)], vec_hits=[_qa(s=0.6)]
    )
    assert top["s"]["jieba"] == 0.8
    assert top["s"]["vec"] == 0.6
    # P6 权重：jieba(3.0*0.8) + vec(4.0*0.6) = 2.4 + 2.4 = 4.8 → 4.8/9.5
    expected = (3.0 * 0.8 + 4.0 * 0.6) / 9.5
    assert abs(top["score"] - expected) < 1e-12


def test_scores_always_in_range():
    fused = fuse(
        field_hits=[_field("f1", 2.0), _field("f2", -1.0), _field("f3", None)],
        jieba_hits=[_qa("q1", 99.0)],
        vec_hits=[_qa("q1", -5.0)],
    )
    assert fused
    assert all(0.0 <= c["score"] <= 1.0 for c in fused)
    by_key = {(c["type"], c["key"]): c["score"] for c in fused}
    # field f1: s=2.0→钳入1.0 → 1.5*1.0/9.5
    assert abs(by_key[("field", "f1")] - 1.5 / 9.5) < 1e-12
    # field f2: s=-1.0→钳入0.0 → 0.0
    assert by_key[("field", "f2")] == 0.0
    # field f3: s=None→0.0
    assert by_key[("field", "f3")] == 0.0


def test_exact_field_hit_is_direct_not_by_score():
    """精确字段直查（s=1.0）判 direct —— 走判定层硬规则，不靠融合分。

    P6 把 W_FIELD 从 3.0 下调到 1.5 后，纯 field 候选只有 1.5/9.5≈0.158，
    够不到 TH_MAYBE；若无此规则，29 个字段里 **21 个没有 QA 孪生**的字段
    （保修期 / 防水等级 / 包邮门槛…）会全部拒答 —— 字段直查这条路径的主体失效。
    """
    (only,) = fuse(field_hits=[_field(s=1.0)])
    assert only["score"] < TH_MAYBE  # 融合分本身确实不够
    assert decide([only])["action"] == "direct"

    # 包含匹配（s=0.8）不在此列：那是"沾边"，不是"问的就是这个字段"。
    (near,) = fuse(field_hits=[_field(s=0.8)])
    assert decide([near])["action"] == "fail_closed"

    # 精确命中但被更强的 qa 候选压过 → 以 qa 为准，不强行 direct。
    fused = fuse(field_hits=[_field(s=1.0)], jieba_hits=[_qa(s=0.6)])
    assert fused[0]["type"] == "qa"
    assert decide(fused)["action"] == "fail_closed"


def test_unavailable_route_drops_from_denominator():
    """路**不可用** → 其权重退出分母；路**召回到 0 条** → 留在分母。

    w_vec=4.0 缺席时，剩余三路天花板只有 (Σw-w_vec)/Σw = 5.5/9.5≈0.579 <
    TH_MAYBE —— 不剔除就会把"降级"变成"一律拒答"，直接违反降级不中断。

    反向也要钉住：早期版本把"召回为空"也当缺席剔除，结果 null 拒答率从
    1.000 崩到 0.154（对抗题正是靠"各路都找不到"才该被拒）。
    """
    two = fuse(jieba_hits=[_qa(s=1.0)], simple_hits=[_qa(s=1.0)])
    assert abs(two[0]["score"] - (W_JIEBA + W_SIMPLE) / W_SUM) < 1e-12
    assert decide(two)["action"] == "fail_closed"  # 0.421 < TH_MAYBE

    down = fuse(jieba_hits=[_qa(s=1.0)], simple_hits=[_qa(s=1.0)],
                unavailable=("vec",))
    assert abs(down[0]["score"]
               - (W_JIEBA + W_SIMPLE) / (W_SUM - W_VEC)) < 1e-12
    assert decide(down)["action"] == "direct"  # 0.727 ≥ TH_DIRECT

    # 召回为空 ≠ 不可用：分母必须纹丝不动。
    empty_vec = fuse(jieba_hits=[_qa(s=1.0)], simple_hits=[_qa(s=1.0)],
                     vec_hits=[])
    assert abs(empty_vec[0]["score"] - two[0]["score"]) < 1e-12


def _cand(score, key="k", ctype="qa"):
    """Minimal candidate for decide() boundary tests."""
    return {"key": key, "score": score, "type": ctype, "s": {"field": 0.0}}


def test_decide_boundaries():
    # P6 标定阈值：TH_DIRECT=0.65, TH_MAYBE=0.60, GAP=0.15
    assert decide([_cand(0.75)])["action"] == "direct"
    assert decide([_cand(0.99)])["action"] == "direct"
    assert decide([_cand(0.70)])["action"] == "direct"      # >= 0.65
    assert decide([_cand(0.649)])["action"] == "maybe_single"  # < 0.65
    assert decide([_cand(0.60)])["action"] == "maybe_single"   # >= 0.60
    assert decide([_cand(0.599)])["action"] == "fail_closed"   # < 0.60
    assert decide([])["action"] == "fail_closed"
    # gap tests: top1 in [0.60, 0.65), second can be below TH_MAYBE
    gap_exact = decide([_cand(0.62, "a"), _cand(0.47, "b")])
    assert gap_exact["action"] == "maybe_single"  # gap=0.15 含边界
    assert [c["key"] for c in gap_exact["items"]] == ["a"]
    gap_small = decide([_cand(0.62, "a"), _cand(0.48, "b")])
    assert gap_small["action"] == "maybe_multi"
    assert [c["key"] for c in gap_small["items"]] == ["a", "b"]
