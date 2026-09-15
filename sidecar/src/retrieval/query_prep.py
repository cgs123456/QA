"""查询预处理管线（D6 第一步：修**匹配**根因，不动任何阈值）。

# 要解决什么

D6 前基线 Top-3 = 0.490，25 个 miss 全部是**空池拒答**。根因不是阈值，是匹配：

- `fts5_search` 用扩展的 `jieba_query(?)` 作 MATCH 表达式，而它的语义是 **AND**
  ——查询分词后**每个词都必须命中**，虚词也算：
  `jieba_query("你们多久能发货啊？")` → `"你们" AND "多久" AND "能" AND "发货" AND "啊" AND "？"`。
  只要 `你们`/`能`/`？` 不在库里，整路归零。自然长问句因此成片落空。

# 怎么做（三步，各自独立可测）

1. **切词**：用**索引同款分词器**取词 —— 解析 `jieba_query(?)` 的输出
   （`"w1" AND "w2"` → `[w1, w2]`）。刻意**不**引入 python 版 `jieba` 包：
   索引是 libsimple 里的 cppjieba 切的，两个分词器一旦对同一句给出不同切分，
   "查询词 ∈ 索引词"这个前提就不成立，而且会多一个需要同步的词表依赖。
   `jieba_query` 已经把 cppjieba + 本项目词表（`vendor/dict/`）装在扩展里，
   复用它就是**用 jieba 做关键词提取**，且保证与索引一致。
2. **丢虚词**：用随包发布的 `vendor/dict/stop_words.utf8`（引擎自己也在用这份，
   不新增词表文件）。剩下的就是"保留专有名词/领域词"的那部分。
3. **构造 OR 表达式**：`"发货" OR "多久"`，取代扩展的 AND。FTS5 的 bm25 会按
   命中词数排序，所以"命中更多关键词的文档"自然排前面 —— 这正是我们要的排序，
   而不是把阈值放松。

# 为什么保留原文

字段直查的**精确**匹配与向量路都吃原文（`raw`）。关键词视图只服务 FTS 路：
`发货周期是多久？` 这种原文与字段名精确相等的题，一旦被关键词化就再也等不上了。

# 超短查询的并集补召回（③）

FTS5 的 idf = `ln((N-n+0.5)/(n+0.5))`，N 小时塌缩。当查询只切出 ≤2 个关键词时，
OR 表达式的候选池很窄，一条弱命中就决定排序。此时额外给一个**汉字级 OR**
表达式（`"能" OR "开" OR "发" OR "票" OR "吗"`），由 `fts5_search` 并进 simple 池：
jieba 把 `能开发票吗` 切成 `开发票`（库里是 `发票`）时，词组路必然落空，
但字符路能兜住。这不是改引擎、也不是调门限，是在**查询侧**多给一次召回机会。

# 边界

- 非中文查询（拼音 `fahuo`、纯符号）：切不出"词"，`keyword_expr` 为 `None`，
  调用方退回扩展原查询 —— 拼音容错本来就是扩展的 `fahuo` → `( f+a+h+u+o* OR ... )`
  表达式在做，不能被关键词化破坏。
- 空查询：返回空，不构造表达式。
"""

import functools
import re
import unicodedata
from dataclasses import dataclass

# 关键词数 ≤ 此值的查询额外启用汉字级 OR 并集（见模块注释 ③）。
SHORT_QUERY_KEYWORDS = 2

# `jieba_query` 的 AND 输出形如 `"发货" AND "周期"`；pinyin 输出是
# `( f+a+h+u+o* OR ... )`，没有引号词，故本正则天然把它排除。
_QUOTED = re.compile(r'"((?:[^"]|"")*)"')

_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_ALNUM_RUN = re.compile(r"[0-9A-Za-z]+")


@functools.lru_cache(maxsize=1)
def load_stopwords() -> frozenset:
    """随包发布的停用词表（与引擎共用一份，不新增词表文件）。

    路径经 `database.connection.dict_dir()` 解析（env 覆盖 / 打包态 / 开发态），
    与 `REQUIRED_DICT_FILES` 里那个 `stop_words.utf8` 是同一个文件。
    """
    from database.connection import dict_dir

    path = dict_dir() / "stop_words.utf8"
    words = set()
    with open(path, encoding="utf-8") as f:
        for line in f:
            w = line.strip()
            if w:
                words.add(w)
    return frozenset(words)


def parse_and_terms(expr: str) -> tuple:
    """从 `jieba_query()` 的输出里取出词元。

    仅接受 AND 形式（引号词序列）；pinyin 表达式返回空元组，由调用方退回原查询。
    """
    if not expr or " AND " not in expr:
        return ()
    return tuple(m.group(1).replace('""', '"') for m in _QUOTED.finditer(expr))


def tokenize(conn, query: str) -> tuple:
    """用索引同款分词器切词（不引入 python jieba）。"""
    raw = (query or "").strip()
    if not raw:
        return ()
    row = conn.execute("SELECT jieba_query(?)", (raw,)).fetchone()
    return parse_and_terms(row[0] if row else "")


def extract_keywords(tokens, stopwords=None) -> tuple:
    """丢弃虚词与单字符标点，保留其余（专有名词/领域词天然留下）。"""
    stops = load_stopwords() if stopwords is None else stopwords
    out = []
    seen = set()
    for t in tokens or ():
        t = (t or "").strip()
        if not t or t in stops or t in seen:
            continue
        # 纯标点/空白（停用词表覆盖不全时兜底）：既非汉字也非字母数字即丢弃。
        if not _CJK.search(t) and not _ALNUM_RUN.search(t):
            continue
        seen.add(t)
        out.append(t)
    return tuple(out)


def or_expression(words) -> str:
    """`("发货", "多久")` → `"发货" OR "多久"`（FTS5 字符串字面量转义）。

    空输入返回空串（调用方应据此判定"没有可用的关键词视图"）。
    """
    parts = []
    for w in words or ():
        w = (w or "").strip()
        if not w:
            continue
        parts.append('"' + w.replace('"', '""') + '"')
    return " OR ".join(parts)


def char_tokens(query: str) -> tuple:
    """汉字级词元：单个汉字 + 连续字母数字串（`400` 要保住）。

    按原文顺序产出（保持可读、可断言），标点与空白丢弃；重复项去重
    （OR 语义下重复无意义，去重让表达式更短）。
    """
    q = unicodedata.normalize("NFKC", query or "")
    out, seen = [], set()
    i = 0
    while i < len(q):
        ch = q[i]
        if _CJK.match(ch):
            tok, i = ch, i + 1
        else:
            m = _ALNUM_RUN.match(q, i)
            if m is None:
                i += 1
                continue
            tok, i = m.group(0), m.end()
        if tok not in seen:
            seen.add(tok)
            out.append(tok)
    return tuple(out)


@dataclass(frozen=True)
class PreparedQuery:
    """一次查询的两种视图：原文（字段/向量）与关键词（FTS）。

    `keyword_expr` 为 `None` 表示**没有关键词视图**（非中文/空查询），
    调用方应退回扩展原查询，而不是拿空表达式去 MATCH。
    """

    raw: str
    keywords: tuple
    keyword_expr: str | None
    char_expr: str | None

    @property
    def is_short(self) -> bool:
        """是否命中"超短查询"分支（决定是否并集补召回）。"""
        return 0 < len(self.keywords) <= SHORT_QUERY_KEYWORDS


def prepare(conn, query: str, stopwords=None) -> PreparedQuery:
    """把查询预处理成 (原文, 关键词, 词级 OR 表达式, 字符级 OR 表达式)。"""
    raw = (query or "").strip()
    if not raw:
        return PreparedQuery(raw="", keywords=(), keyword_expr=None, char_expr=None)

    keywords = extract_keywords(tokenize(conn, raw), stopwords)
    expr = or_expression(keywords) or None
    if expr is None:
        return PreparedQuery(raw=raw, keywords=(), keyword_expr=None, char_expr=None)

    short = len(keywords) <= SHORT_QUERY_KEYWORDS
    char_expr = or_expression(char_tokens(raw)) or None if short else None
    return PreparedQuery(
        raw=raw, keywords=keywords, keyword_expr=expr, char_expr=char_expr
    )
