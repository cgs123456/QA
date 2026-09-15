"""FTS5 两级检索（第 2/3 级）。

- **jieba 路 = 关键词 OR**：表达式由 `query_prep` 构造（`"发货" OR "多久"`），
  取代扩展 `jieba_query()` 的 AND 语义 —— 后者要求查询分词后每个词都命中
  （含虚词），自然长问句成片落空，是 D6 前基线 25 个空池 miss 的根因。
- **simple 路 = 扩展 `simple_query()` 原样**：字符级 AND + 拼音容错。
  它救的是 ASR 同音字（`fahuo` → `( f+a+h+u+o* OR ... )`），关键词化会毁掉
  这个能力，所以这一路**不动**。
- **超短查询并集**：查询切出的关键词 ≤2 时，`query_prep` 额外给出汉字级 OR
  表达式，这里并进 simple 池（同 route 取 max s，见 `hybrid_rank.fuse`）。
  理由见 `query_prep` 模块注释 ③。
- 非中文查询（拼音/纯符号）没有关键词视图 → 两路都退回扩展原查询。

门限：保留 bm25 < -0.5；归一化 s = a/(a+T_fts)，a=max(0,-bm25)，T_fts=0.5
（门限处 s=0.5）。**本轮未改**（R15）。已知特性：FTS5 idf =
ln((N-n+0.5)/(n+0.5))，极小语料库下塌缩（N=2、n=1 时 idf=ln(1)=0，合法命中
也被门限滤掉）——阈值必须在真实规模语料上标定，禁止为迁就 demo 数据调门限（D6）。
OR 化后同池文档变多、idf 下降，单题绝对分会整体下移：**相对排序才是本轮收益**，
弱命中由融合分（Σw=6.0）自然压制，不需要也不允许在这里顺手改门限。

每路 top_k=5；所有 SQL 过滤 usage_status='rejected'（R5）。

MATCH 异常输入兜底：仅吞 SQLITE_ERROR(1) 的**查询语法**问题；缺表/缺分词器/缺函数
（"no such table"/"no such tokenizer"/"no such function"）、I/O(10)/损坏(11)/
忙(5)/锁(6) 一律上抛（qa.py 转 error 事件 + stderr 堆栈），绝不静默转 Fail-Closed。
"""

import sqlite3

from retrieval.query_prep import prepare

T_FTS = 0.5
BM25_CUTOFF = -0.5
TOP_K = 5

_STRUCTURAL_ERRORS = ("no such table", "no such tokenizer", "no such function")


def normalize_score(bm25: float) -> float:
    a = max(0.0, -bm25)
    return a / (a + T_FTS)


def _match_expression(conn, func: str, raw: str):
    """取扩展函数给出的 MATCH 表达式（jieba_query / simple_query）。

    扩展未加载时这里是 `no such function` —— 真故障，必须 loud。
    """
    try:
        row = conn.execute(f"SELECT {func}(?)", (raw,)).fetchone()
    except sqlite3.OperationalError as e:
        if any(s in str(e) for s in _STRUCTURAL_ERRORS):
            raise
        return None
    return row[0] if row else None


def _search_route(conn, store_id: str, expr, route: str, top_k: int) -> list:
    """按已构造好的 MATCH 表达式检索一路。`expr` 为 None/空 → 空结果。"""
    if not expr:
        return []
    try:
        rows = conn.execute(
            "SELECT q.id, q.standard_question, q.official_answer, q.category,"
            " bm25(ft_qa) AS b"
            " FROM ft_qa JOIN qa_pairs q ON q.rowid = ft_qa.rowid"
            " WHERE ft_qa MATCH ? AND q.store_id = ?"
            " AND (q.usage_status IS NULL OR q.usage_status <> 'rejected')"
            " ORDER BY b ASC LIMIT ?",
            (expr, store_id, top_k),
        ).fetchall()
    except sqlite3.OperationalError as e:
        code = getattr(e, "sqlite_errorcode", 1)
        msg = str(e)
        structural = any(s in msg for s in _STRUCTURAL_ERRORS)
        if code != 1 or structural:
            raise  # 真故障（缺表/缺扩展/I-O/损坏/忙/锁）必须 loud，不许转 Fail-Closed
        return []  # 纯 MATCH 查询语法问题兜底
    hits = []
    for qa_id, question, answer, category, bm25 in rows:
        if bm25 is None or bm25 >= BM25_CUTOFF:
            continue
        hits.append(
            {
                "qa_id": qa_id,
                "standard_question": question,
                "official_answer": answer,
                "category": category,
                "route": route,
                "bm25": bm25,
                "s": normalize_score(bm25),
            }
        )
    return hits


def fts5_search(conn, store_id: str, query: str, top_k: int = TOP_K, prepared=None) -> list:
    """关键词 OR（jieba）+ 扩展原查询（simple）+ 超短查询的汉字级并集。

    `prepared`：可选的 `query_prep.PreparedQuery`（调用方已算过就直接传，
    省一次 jieba 切词）；不传则就地预处理。
    """
    raw = (query or "").strip()
    if not raw:
        return []
    prep = prepared if prepared is not None else prepare(conn, raw)

    if prep.keyword_expr is None:
        # 非中文（拼音/纯符号）：没有关键词视图，两路都用扩展原查询。
        return _search_route(
            conn, store_id, _match_expression(conn, "jieba_query", raw), "jieba", top_k
        ) + _search_route(
            conn, store_id, _match_expression(conn, "simple_query", raw), "simple", top_k
        )

    hits = _search_route(conn, store_id, prep.keyword_expr, "jieba", top_k)
    hits += _search_route(
        conn, store_id, _match_expression(conn, "simple_query", raw), "simple", top_k
    )
    if prep.char_expr is not None:
        hits += _search_route(conn, store_id, prep.char_expr, "simple", top_k)
    return hits
