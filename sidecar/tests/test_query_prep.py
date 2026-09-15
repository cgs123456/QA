"""查询预处理单测：切词 → 丢虚词 → OR 表达式 → 短查询并集表达式。

纯函数部分不碰 DB（关键词提取/表达式构造各自可单测）；涉及分词的两条走 `db`
夹具（真 libsimple 扩展 + 真词表），因为「与索引同款分词器」正是本模块的核心
承诺 —— 用假分词器测等于没测。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from knowledge.compiler import compile_store
from knowledge.field_extractor import extract, load_vocab
from knowledge.parsers.markdown_parser import parse_markdown
from knowledge.stores import create_store
from knowledge.validator import validate_field_items, validate_qa_items
from retrieval.query_prep import (
    SHORT_QUERY_KEYWORDS,
    char_tokens,
    extract_keywords,
    load_stopwords,
    or_expression,
    parse_and_terms,
    prepare,
    tokenize,
)

SAMPLE_MD = """# 公司信息

## 发货周期是多久？

标准发货周期为三天内发出。

## 公司成立时间

公司成立于2015年，总部位于北京。
"""


def _seed(db):
    store = create_store(db, "预处理库")
    parsed = parse_markdown(SAMPLE_MD, "sample.md")
    qa, _ = validate_qa_items(parsed["qa_items"])
    fields_raw, _ = validate_field_items(parsed["field_items"])
    fields, miss = extract(fields_raw, load_vocab())
    compile_store(db, store["id"], qa, fields, vocab_miss=miss)
    return store["id"]


# ------------------------------------------------------------------ 纯函数


def test_parse_and_terms_reads_the_extension_and_output():
    assert parse_and_terms('"发货" AND "周期" AND "多久"') == ("发货", "周期", "多久")


def test_parse_and_terms_rejects_pinyin_expressions():
    """拼音表达式没有引号词 —— 必须返回空，让调用方退回扩展原查询。"""
    assert parse_and_terms("( f+a+h+u+o* OR fa+hu+o* OR fa+huo* OR fahuo* )") == ()
    assert parse_and_terms("") == ()
    assert parse_and_terms(None) == ()


def test_or_expression_is_or_not_and():
    expr = or_expression(("发货", "多久"))
    assert expr == '"发货" OR "多久"'
    assert " AND " not in expr


def test_or_expression_escapes_embedded_quotes():
    """FTS5 字符串字面量里 `"` 要写成 `""`，否则表达式语法错 → 整路静默归零。"""
    assert or_expression(('a"b',)) == '"a""b"'


def test_or_expression_of_nothing_is_empty():
    assert or_expression(()) == ""
    assert or_expression(["", "  "]) == ""


def test_extract_keywords_drops_function_words_and_punctuation():
    stops = frozenset({"你们", "能", "啊", "？", "怎么", "是"})
    assert extract_keywords(("你们", "多久", "能", "发货", "啊", "？"), stops) == ("多久", "发货")
    assert extract_keywords(("怎么", "修改", "收货", "地址"), stops) == ("修改", "收货", "地址")


def test_extract_keywords_deduplicates_and_keeps_domain_terms():
    stops = frozenset({"了"})
    out = extract_keywords(("发货", "发货", "了", "周期"), stops)
    assert out == ("发货", "周期")


def test_char_tokens_keeps_single_han_and_alnum_runs():
    """`400` 必须保住：答案里的 `400-123-4567` 是字符级索引的连续串。"""
    assert char_tokens("有400电话吗？") == ("有", "400", "电", "话", "吗")
    assert char_tokens("发货快不快") == ("发", "货", "快", "不")  # 去重


def test_char_tokens_drops_punctuation_and_whitespace():
    assert char_tokens("  发货，周期？  ") == ("发", "货", "周", "期")


def test_short_query_threshold_is_two():
    """≤2 关键词走并集补召回；这个边界是行为契约，写死防漂移。"""
    assert SHORT_QUERY_KEYWORDS == 2


def test_stopwords_come_from_the_shipped_dict():
    stops = load_stopwords()
    assert len(stops) > 1000
    assert "的" in stops and "你们" in stops and "怎么" in stops
    # 领域词绝不能被当虚词丢掉。
    assert "发货" not in stops and "退货" not in stops and "客服" not in stops


# ------------------------------------------------------------------ 走真扩展


def test_tokenize_uses_the_index_tokenizer(db):
    """切词必须与索引用同一个分词器（libsimple 的 cppjieba）。"""
    assert tokenize(db, "你们多久能发货啊？") == ("你们", "多久", "能", "发货", "啊", "？")
    assert tokenize(db, "发货周期多久") == ("发货", "周期", "多久")
    assert tokenize(db, "   ") == ()


def test_prepare_keeps_raw_and_builds_the_or_view(db):
    prep = prepare(db, "你们多久能发货啊？")
    assert prep.raw == "你们多久能发货啊？"  # 字段直查/向量路仍吃原文
    assert "你们" not in prep.keywords and "啊" not in prep.keywords
    assert "发货" in prep.keywords
    assert prep.keyword_expr is not None and " AND " not in prep.keyword_expr


def test_prepare_pinyin_has_no_keyword_view(db):
    """拼音查询没有关键词视图 → 调用方退回扩展原查询（保住拼音容错）。"""
    prep = prepare(db, "fahuo")
    assert prep.keywords == ()
    assert prep.keyword_expr is None
    assert prep.char_expr is None
    assert prep.raw == "fahuo"


def test_prepare_empty_query(db):
    for q in ("", "   ", None):
        prep = prepare(db, q)
        assert prep.keyword_expr is None and prep.char_expr is None and prep.keywords == ()


def test_short_query_gets_a_char_level_expression(db):
    """`能开发票吗` 被切成 `开发票`（库里是 `发票`）→ 词组路落空，字符路兜住。"""
    prep = prepare(db, "能开发票吗")
    assert prep.is_short
    assert prep.char_expr is not None
    assert "开" in prep.char_expr and "票" in prep.char_expr


def test_long_query_has_no_char_level_expression(db):
    prep = prepare(db, "我下单之后要等几天才发货？")
    assert not prep.is_short
    assert prep.char_expr is None


def test_keyword_or_matches_where_and_does_not(db):
    """本模块存在的理由：同一个查询，AND 空池、OR 命中。"""
    sid = _seed(db)
    q = "你们多久能发货啊？"
    and_expr = db.execute("SELECT jieba_query(?)", (q,)).fetchone()[0]
    or_expr = prepare(db, q).keyword_expr

    def count(expr):
        return db.execute(
            "SELECT COUNT(*) FROM ft_qa JOIN qa_pairs p ON p.rowid = ft_qa.rowid"
            " WHERE ft_qa MATCH ? AND p.store_id = ?",
            (expr, sid),
        ).fetchone()[0]

    assert count(and_expr) == 0, "前提失效：AND 表达式居然命中了，本测试不再说明问题"
    assert count(or_expr) > 0, "OR 表达式必须命中"


def test_prepare_is_deterministic(db):
    """同一查询两次预处理结果必须一致（跨路共用同一个对象的前提）。"""
    a = prepare(db, "客服电话给我一下")
    b = prepare(db, "客服电话给我一下")
    assert a == b
