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
- **高亮（F2.3）**：命中附 `hl_question` / `hl_answer`（`highlight()` 全列打标，
  非截断摘要）。**按实际命中路选择高亮查询词**（坑位）：jieba 命用
  keyword_expr 标，simple 命用 simple_expr / char_expr 标——两路 token 体系
  不同（词 vs 字/拼音），混用会标错位置。每行自带其 MATCH 表达式的高亮，
  调用方原样透传即可，无需二次判断。


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

# highlight() 列索引（F2.3）：必须与 ft_qa 列序对齐（R4 高发区）。
# structurally locked by test_highlight_column_lock（xinfo 顺序 + 行为双断言）。
COL_QUESTION = 1  # standard_question
COL_ANSWER = 2  # official_answer

# 高亮标记（默认 `<mark>`；调标记串走 simple_highlight() 参数，前端按需替换）。
HL_PRE = "<mark>"
HL_POST = "</mark>"

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


def simple_highlight(conn, store_id: str, qa_id: str, col_idx: int,
                     expr: str, pre: str = HL_PRE, post: str = HL_POST):
    """单条命中的列级高亮（F2.3 命名单元）。

    对 `qa_id` 所在行，用**实际命中该行的表达式**跑 `highlight()`：
    返回带标记的整列文本（非截断；无命中标记时返回原文）。
    `col_idx` 仅接受 `COL_QUESTION` / `COL_ANSWER`（R4 列序，其它值大声拒绝）；
    `expr` 为空返回 None（调用方回落原文）。本函数只抛结构性错误——
    语法类问题由调用方按展示降级处理（见 `_attach_highlights`）。
    """
    if col_idx not in (COL_QUESTION, COL_ANSWER):
        raise ValueError(f"高亮列非法：{col_idx}（只认 1=问题/2=答案）")
    if not expr or not expr.strip():
        return None
    row = conn.execute(
        "SELECT highlight(ft_qa, ?, ?, ?) FROM ft_qa JOIN qa_pairs q"
        " ON q.rowid = ft_qa.rowid"
        " WHERE ft_qa MATCH ? AND q.id = ? AND q.store_id = ?",
        (col_idx, pre, post, expr, qa_id, store_id),
    ).fetchone()
    if row is None:
        return None
    return row[0]


def _attach_highlights(conn, store_id: str, hits: list, expr: str) -> None:
    """就地给一批同路命中打标（expr 须是产出这批命中的那个表达式）。

    高亮是展示层：单条失败只回落原文，**绝不**让检索变空（与 MATCH 语法兜底
    同方向——展示降级，不是检索降级）。
    """
    if not expr:
        return
    for h in hits:
        # 逐列独立 try：一列打标失败不影响另一列（展示降级，非检索降级）。
        for col_idx, key in ((COL_QUESTION, "hl_question"), (COL_ANSWER, "hl_answer")):
            try:
                hl = simple_highlight(conn, store_id, h["qa_id"], col_idx, expr)
            except Exception:
                continue
            if hl is not None:
                h[key] = hl


def fts5_search(conn, store_id: str, query: str, top_k: int = TOP_K, prepared=None) -> list:
    """关键词 OR（jieba）+ 扩展原查询（simple）+ 超短查询的汉字级并集。

    `prepared`：可选的 `query_prep.PreparedQuery`（调用方已算过就直接传，
    省一次 jieba 切词）；不传则就地预处理。

    每行命中自带其 MATCH 表达式的高亮（`hl_question` / `hl_answer`，
    缺席=该行打标失败，调用方回落原文）。
    """
    raw = (query or "").strip()
    if not raw:
        return []
    prep = prepared if prepared is not None else prepare(conn, raw)

    if prep.keyword_expr is None:
        # 非中文（拼音/纯符号）：没有关键词视图，两路都用扩展原查询。
        jieba_expr = _match_expression(conn, "jieba_query", raw)
        simple_expr = _match_expression(conn, "simple_query", raw)
        jieba_hits = _search_route(conn, store_id, jieba_expr, "jieba", top_k)
        _attach_highlights(conn, store_id, jieba_hits, jieba_expr)
        simple_hits = _search_route(conn, store_id, simple_expr, "simple", top_k)
        _attach_highlights(conn, store_id, simple_hits, simple_expr)
        return jieba_hits + simple_hits

    hits = _search_route(conn, store_id, prep.keyword_expr, "jieba", top_k)
    _attach_highlights(conn, store_id, hits, prep.keyword_expr)
    simple_expr = _match_expression(conn, "simple_query", raw)
    simple_hits = _search_route(conn, store_id, simple_expr, "simple", top_k)
    _attach_highlights(conn, store_id, simple_hits, simple_expr)
    if prep.char_expr is not None:
        extra = _search_route(conn, store_id, prep.char_expr, "simple", top_k)
        _attach_highlights(conn, store_id, extra, prep.char_expr)
        simple_hits += extra
    return hits + simple_hits
