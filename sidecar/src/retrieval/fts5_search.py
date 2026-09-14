"""FTS5 两级检索（第 2/3 级）。

- jieba_query()：词组级精准；simple_query()：拼音容错（救 ASR 同音字）。
- 门限：保留 bm25 < -0.5；归一化 s = a/(a+T_fts)，a=max(0,-bm25)，T_fts=0.5
  （门限处 s=0.5）。初值，评测集标定（PRD §3.3）。
  已知特性：FTS5 idf = ln((N-n+0.5)/(n+0.5))，极小语料库下塌缩
  （如 N=2、n=1 时 idf=ln(1)=0，合法命中也被门限滤掉）——阈值必须在
  真实规模语料上标定，禁止为迁就 demo 数据调门限（D6）。
- 每路 top_k=5；所有 SQL 过滤 usage_status='rejected'（R5）。
- MATCH 异常输入（特殊串）兜底返回 []，不抛错。
- jieba_query/simple_query 为 AND 语义：查询分词后的每个词都必须命中
  （含虚词），自然长问句易整体落空——D6 应先做关键词提取再调 FTS；
  本任务示例题均用关键词式问法（实测结论，非引擎缺陷）。
"""

import sqlite3

T_FTS = 0.5
BM25_CUTOFF = -0.5
TOP_K = 5


def normalize_score(bm25: float) -> float:
    a = max(0.0, -bm25)
    return a / (a + T_FTS)


def _search_route(conn, store_id: str, query: str, func: str, route: str, top_k: int) -> list:
    try:
        rows = conn.execute(
            "SELECT q.id, q.standard_question, q.official_answer, q.category,"
            " bm25(ft_qa) AS b"
            " FROM ft_qa JOIN qa_pairs q ON q.rowid = ft_qa.rowid"
            f" WHERE ft_qa MATCH {func}(?) AND q.store_id = ?"
            " AND (q.usage_status IS NULL OR q.usage_status <> 'rejected')"
            " ORDER BY b ASC LIMIT ?",
            (query, store_id, top_k),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
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


def fts5_search(conn, store_id: str, query: str, top_k: int = TOP_K) -> list:
    """jieba + simple 双路结果拼接（各含 route/s 标记；融合是 D6 的事）。"""
    query = (query or "").strip()
    if not query:
        return []
    return _search_route(conn, store_id, query, "jieba_query", "jieba", top_k) + _search_route(
        conn, store_id, query, "simple_query", "simple", top_k
    )
