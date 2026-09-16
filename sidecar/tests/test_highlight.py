"""高亮单测（F2.3 `simple_highlight` 集成）。

- 列锁定（R4 高发区）：结构断言（xinfo 列序）+ 行为断言（甲/乙特征字只落在自家列）。
- 中文（jieba 词级）/ simple（字级）/ 拼音三路 marks 正确且去标后与原文逐字一致。
- 自定义标记串、非法列、空表达式；怪查询不炸检索。
- 端到端：search 端点 fts_hits 带片段；answer sources 带片段（经 fuse 只透传载荷）。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from knowledge.compiler import compile_store
from knowledge.stores import create_store
from retrieval import fts5_search as F
from retrieval.fts5_search import (
    COL_ANSWER,
    COL_QUESTION,
    fts5_search,
    simple_highlight,
)

EVAL_DIR = Path(__file__).resolve().parent / "eval"
TEST_TOKEN = "test-token-" + "x" * 32


def _h():
    return {"Authorization": f"Bearer {TEST_TOKEN}"}


def _seed4(db):
    store = create_store(db, "高亮库")
    qa = [
        {"standard_question": "发货周期是多久？", "official_answer": "三天内发出",
         "usage_status": "fixed"},
        {"standard_question": "退货期限是几天？", "official_answer": "七天无理由退货",
         "usage_status": "fixed"},
        {"standard_question": "客服电话是多少？", "official_answer": "400-123-4567",
         "usage_status": "fixed"},
        {"standard_question": "公司成立时间是哪一年？", "official_answer": "公司成立于2015年",
         "usage_status": "fixed"},
    ]
    compile_store(db, store["id"], qa, [])
    return store["id"]


def _strip(hl: str) -> str:
    return hl.replace("<mark>", "").replace("</mark>", "")


def test_column_constants():
    assert (COL_QUESTION, COL_ANSWER) == (1, 2)


def test_column_lock_structural(db):
    """ft_qa 前三列必须恒为 id/standard_question/official_answer（R4）。"""
    names = [r[1] for r in db.execute("PRAGMA table_xinfo(ft_qa)").fetchall()
             if not r[6]]
    assert names[:3] == ["id", "standard_question", "official_answer"]


def test_column_lock_behavioral(db):
    """香蕉只出现在问题列、橙子只出现在答案列：标错列即红。

    用显式 OR 表达式直调（不经过分词器），断言只锁定“列索引→字段”映射本身；
    分词到表达式的链路另由端到端用例覆盖。
    """
    sid = create_store(db, "列锁库")["id"]
    compile_store(db, sid, [
        {"standard_question": "香蕉好吃", "official_answer": "橙子好吃",
         "usage_status": "fixed"},
        {"standard_question": "其它问题一", "official_answer": "其它答案一",
         "usage_status": "fixed"},
        {"standard_question": "其它问题二", "official_answer": "其它答案二",
         "usage_status": "fixed"},
        {"standard_question": "其它问题三", "official_answer": "其它答案三",
         "usage_status": "fixed"},
    ], [])
    rows = db.execute(
        "SELECT id FROM qa_pairs WHERE store_id=? AND standard_question='香蕉好吃'",
        (sid,)).fetchall()
    assert len(rows) == 1
    qid = rows[0][0]
    expr = '"香蕉" OR "橙子"'
    hlq = simple_highlight(db, sid, qid, COL_QUESTION, expr)
    hla = simple_highlight(db, sid, qid, COL_ANSWER, expr)
    assert "<mark>香蕉</mark>" in hlq and "橙子" not in hlq
    assert "<mark>橙子</mark>" in hla and "香蕉" not in hla
    # 同一行经 fts5_search 全链路也自带一致的标。
    hits = [h for h in fts5_search(db, sid, "香蕉橙子") if h["qa_id"] == qid]
    if hits:
        assert "香蕉" in hits[0].get("hl_question", "")


def test_jieba_marks_chinese(db):
    sid = _seed4(db)
    hits = [h for h in fts5_search(db, sid, "发货周期多久") if h["route"] == "jieba"]
    assert hits
    for h in hits:
        assert "<mark>" in h["hl_question"]
        assert _strip(h["hl_question"]) == h["standard_question"]
        assert _strip(h["hl_answer"]) == h["official_answer"]


def test_simple_marks_and_fidelity(db):
    sid = _seed4(db)
    hits = [h for h in fts5_search(db, sid, "发货周期多久") if h["route"] == "simple"]
    assert hits
    for h in hits:
        assert "<mark>" in h["hl_question"] or "<mark>" in h["hl_answer"]
        assert _strip(h["hl_question"]) == h["standard_question"]
        assert _strip(h["hl_answer"]) == h["official_answer"]


def test_pinyin_marks(db):
    sid = _seed4(db)
    hits = fts5_search(db, sid, "fahuo")
    assert hits
    assert any("<mark>" in h["hl_question"] for h in hits)
    for h in hits:
        assert _strip(h["hl_question"]) == h["standard_question"]
        assert _strip(h["hl_answer"]) == h["official_answer"]


def test_custom_markers(db):
    sid = _seed4(db)
    hits = fts5_search(db, sid, "发货周期多久")
    h = hits[0]
    custom = simple_highlight(db, sid, h["qa_id"], COL_QUESTION, '"发货"', "[", "]")
    assert custom is not None and custom.startswith("[")
    assert "]" in custom and "<mark>" not in custom
    assert custom.replace("[", "").replace("]", "") == h["standard_question"]


def test_bad_column_and_empty_expr(db):
    sid = _seed4(db)
    h = fts5_search(db, sid, "发货周期多久")[0]
    for bad in (0, 3, 99, -1):
        try:
            simple_highlight(db, sid, h["qa_id"], bad, '"发货"')
        except ValueError:
            pass
        else:
            raise AssertionError(f"列 {bad} 未拒绝")
    assert simple_highlight(db, sid, h["qa_id"], COL_QUESTION, "") is None
    assert simple_highlight(db, sid, h["qa_id"], COL_QUESTION, "   ") is None


def test_weird_query_never_breaks_search(db):
    sid = _seed4(db)
    assert fts5_search(db, sid, "") == []
    assert fts5_search(db, sid, "   ") == []
    # 语法垃圾：MATCH 兜底吞掉，高亮无行可标，整体为空而非抛错。
    assert fts5_search(db, sid, '" OR "') == []


def test_search_endpoint_returns_snippets(api_client):
    # 种子 4 篇 QA：2 篇会触发小语料 idf 塌缩，全被 -0.5 门限滤掉
    # （fts5_search.py 已文档化），4 篇是门限下的最小可命中规模。
    client, _ = api_client
    r = client.post(
        "/knowledge/compile",
        json={"store_name": "片段库", "format": "markdown", "content":
              "# 公司信息\n\n## 客服电话是多少？\n\n客服电话是400-123-4567。\n\n"
              "## 发货周期\n\n标准发货周期为三天内发出。\n\n"
              "## 退货期限是几天？\n\n七天内无理由退货。\n\n"
              "## 公司成立时间\n\n公司成立于2015年。\n\n"
              "## 优惠券能叠加吗？\n\n每单限用一张。\n\n"
              "## 发票怎么开？\n\n备注抬头开电子票。\n"},
        headers=_h(),
    )
    assert r.status_code == 200, r.text
    sid = r.json()["store_id"]
    r = client.get("/knowledge/search", params={"q": "客服电话", "store_id": sid},
                   headers=_h())
    assert r.status_code == 200
    hits = r.json()["fts_hits"]
    assert hits
    marked = [h for h in hits if "<mark>" in h.get("hl_question", "")]
    assert marked, "端点片段缺失标记"
    for h in marked:
        assert h["hl_question"].replace("<mark>", "").replace("</mark>", "") == \
            h["standard_question"]


class _FakeLLM:
    def generate(self, system, question, contexts):
        async def _gen():
            yield "好"

        return _gen()


@pytest.mark.anyio
async def test_answer_sources_carry_highlights(db, monkeypatch, maybe_s):
    """answer sources 透传片段（fuse 只管分数不管展示，router 在此补回）。

    用打桩分数把判定钉在 maybe_single（s 由 maybe_s 按当前常量反解），fts 桩自带 hl ——
    测的是透传逻辑本身；真机分数漂移不影响本用例。
    """
    import generation.router as router_mod
    from generation.router import answer_stream

    def _hit(qid, text, s, route, hlq=None, hla=None):
        hit = {"qa_id": qid, "standard_question": "标准答案甲。",
               "official_answer": text, "category": "E", "route": route, "s": s}
        if hlq is not None:
            hit["hl_question"] = hlq
        if hla is not None:
            hit["hl_answer"] = hla
        return hit

    monkeypatch.setattr(router_mod, "field_lookup", lambda *a, **k: [])
    monkeypatch.setattr(
        router_mod, "fts5_search",
        lambda *a, **k: [_hit("a", "标准答案甲。", maybe_s, "jieba",
                              "<mark>标准</mark>答案甲。", None),
                         _hit("a", "标准答案甲。", maybe_s, "simple", None, None)],
    )
    monkeypatch.setattr(
        router_mod, "vector_search",
        lambda *a, **k: [dict(_hit("a", "标准答案甲。", maybe_s, "vec"))],
    )

    def _zeros(texts):
        return [[0.0] * 512 for _ in texts]

    events = [e async for e in answer_stream(
        db, "s", "测试问题", _FakeLLM(), _zeros)]
    kinds = [e["type"] for e in events]
    assert kinds == ["decision", "sources", "chunk", "done"]
    done = events[-1]["result"]
    assert done["type"] == "llm"
    src = done["sources"][0]
    # jieba 行的标透传；simple 行无标不编造键。
    assert src["hl_question"] == "<mark>标准</mark>答案甲。"
    assert "hl_answer" not in src
    assert src["hl_question"].replace("<mark>", "").replace("</mark>", "") == \
        src["payload"]["standard_question"]
