"""融合单测：锚定 PRD §3.3 三条结构性案例 + 值域 + 判定边界。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from retrieval.hybrid_rank import decide, fuse


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
    assert top["score"] == 0.5  # 3.0/6.0
    assert top["type"] == "field"
    assert top["payload"]["field_value"] == "v"


def test_three_fuzzy_full_is_half():
    fused = fuse(
        jieba_hits=[_qa(s=1.0)], simple_hits=[_qa(s=1.0)], vec_hits=[_qa(s=1.0)]
    )
    assert len(fused) == 1
    assert fused[0]["score"] == 0.5  # (1+1+1)/6.0


def test_field_plus_threshold_fuzzy_is_three_quarters():
    # 0.5 为 FTS 双路门限 s（bm25=-0.5）；PRD 锚定算术 (3.0+1.5)/6.0=0.750。
    # 字段证据经 entity==category 联动落到同一 qa 候选（否则 0.75 不可达，
    # 见 hybrid_rank 模块注释）；池内同时存在 0.5 的 field 候选，top1 仍是 qa。
    # 注：向量路门限 s=0.7，若三路均取各自门限 s 则为 (3+0.5+0.5+0.7)/6≈0.783；
    # 此处按 PRD 字面锚定 0.750，不改引擎（见 PROGRESS task-6）。
    fused = fuse(
        field_hits=[_field()],
        jieba_hits=[_qa(s=0.5)],
        simple_hits=[_qa(s=0.5)],
        vec_hits=[_qa(s=0.5)],
    )
    assert len(fused) == 2
    top = fused[0]
    assert top["type"] == "qa"
    assert top["score"] == 0.75
    assert fused[1]["type"] == "field"
    assert fused[1]["score"] == 0.5


def test_field_linkage_same_entity_only():
    fused = fuse(
        field_hits=[_field(entity="公司信息")],
        jieba_hits=[_qa("q1", s=0.5, category="公司信息"),
                    _qa("q2", s=0.5, category="其他实体"),
                    _qa("q3", s=0.5, category=None)],
    )
    by_key = {c["key"]: c for c in fused if c["type"] == "qa"}
    assert by_key["q1"]["s"]["field"] == 1.0
    assert by_key["q1"]["score"] == (3.0 + 0.5) / 6.0
    assert by_key["q2"]["s"]["field"] == 0.0
    assert by_key["q3"]["s"]["field"] == 0.0


def test_multi_route_merge_takes_best_per_route():
    (top,) = fuse(
        jieba_hits=[_qa(s=0.3), _qa(s=0.8)], vec_hits=[_qa(s=0.6)]
    )
    assert top["s"]["jieba"] == 0.8
    assert top["s"]["vec"] == 0.6
    assert abs(top["score"] - (0.8 + 0.6) / 6.0) < 1e-12


def test_scores_always_in_range():
    fused = fuse(
        field_hits=[_field("f1", 2.0), _field("f2", -1.0), _field("f3", None)],
        jieba_hits=[_qa("q1", 99.0)],
        vec_hits=[_qa("q1", -5.0)],
    )
    assert fused
    assert all(0.0 <= c["score"] <= 1.0 for c in fused)
    by_key = {(c["type"], c["key"]): c["score"] for c in fused}
    assert by_key[("field", "f1")] == 0.5  # 越界钳入 [0,1]
    assert by_key[("field", "f2")] == 0.0
    assert by_key[("field", "f3")] == 0.0


def _cand(score, key="k"):
    return {"key": key, "score": score}


def test_decide_boundaries():
    assert decide([_cand(0.75)])["action"] == "direct"
    assert decide([_cand(0.99)])["action"] == "direct"
    assert decide([_cand(0.749)])["action"] == "maybe_single"
    assert decide([_cand(0.45)])["action"] == "maybe_single"
    assert decide([_cand(0.449)])["action"] == "fail_closed"
    assert decide([])["action"] == "fail_closed"
    gap_exact = decide([_cand(0.5, "a"), _cand(0.35, "b")])
    assert gap_exact["action"] == "maybe_single"  # gap=0.15 含边界
    assert [c["key"] for c in gap_exact["items"]] == ["a"]
    gap_small = decide([_cand(0.5, "a"), _cand(0.36, "b")])
    assert gap_small["action"] == "maybe_multi"
    assert [c["key"] for c in gap_small["items"]] == ["a", "b"]
