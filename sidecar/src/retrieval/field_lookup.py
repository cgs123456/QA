"""字段直查（第 1 级，确定性证据）。

两级匹配，**都在受控词表内**（fields.field_name / field_aliases.alias），
不是模糊匹配、更不是 LLM 判断：

1. **精确**：归一化后与 `field_name` 或 `alias` 相等 → `s=1.0`。
   先按原文查，再按归一化（NFKC/去空白/小写）查一轮，两路去重。
2. **包含**（task-6 发现的精确相等死区）：查询**关键词** ∈ `field_name`/`alias`
   → `s=CONTAINMENT_S=0.8`。死区实例：`有400电话吗？` 的关键词是 `电话`，
   字段名是 `客服电话` —— 用户说得比词表短，精确相等永远等不上，而这一路
   本来是最确定的证据。0.8 是**初值**，记录在案待标定（R15 未动阈值）。
   关键词来自 `query_prep`（与 FTS 路同一套切词/丢虚词），所以 `你们支持货到
   付款吗` 这种 null 题不会被 `支持` 之类的常见动词误触发：词表里没有 `支持`。

匹配方式决定 s，不决定"是否算命中"：包含匹配同样是**词表内**的确定证据，
只是比精确相等弱一档（用户说的是字段名的一部分，指代可能不唯一）。
同一字段精确与包含都命中时取 1.0。

同库同名可属不同 entity，命中全部返回（上限 limit）。

# 包含匹配的两条防线（P6 标定新增，机制②）

初版包含匹配是**无方向**的子串判定（`instr(field_name, kw)` 与 `instr(alias, kw)`），
在真实规模语料上被通用词击穿，实测两例：

- `帮我翻译一下这句话` 的关键词含单字 `话`，而 `话` ⊂ `客服电话` → 字段命中 →
  联动把 `客服电话` 所在 entity 的 QA 全拉高，一句与客服无关的话拿到 0.651。
- `保修期多久` 的关键词含 `多久`，而 `多久` ⊂ 别名 `多久发货` → 命中 `发货周期`
  → 跨 entity 污染，`发货周期` 的 QA（eval-001）被拉到 0.797 进 **direct 档**。

两条防线的依据是**方向**：包含匹配的原意（task-6）是「用户说得比**规范字段名**短」
（`有400电话吗` 的 `电话` ⊂ `客服电话`）。而**别名**是「用户会说的整句」
（`多久发货`），拿短词去子串命中长别名，方向反了 —— 别名只应支持**精确相等**
（`_exact` 已覆盖），不参与子串。故：

- `CONTAINMENT_MIN_LEN = 2`：单字关键词不作包含匹配（单字撞词表必然大面积误命中）。
- `CONTAINMENT_IN_ALIAS = False`：`kw ⊂ alias` 不再算包含命中（`kw ⊂ field_name` 保留）。

两个常量都是**标定项**（P6/R15），取值与前后对比见 `docs/eval-final.md`。
"""

import unicodedata

from retrieval.query_prep import prepare

# 包含匹配的 s（初值，待标定；精确=1.0）。
CONTAINMENT_S = 0.8
# 关键词进入包含匹配的最小长度（见模块注释「两条防线」）。
CONTAINMENT_MIN_LEN = 2
# `kw ⊂ alias` 是否算包含命中（见模块注释「两条防线」）。
CONTAINMENT_IN_ALIAS = False


def normalize_query(query: str) -> str:
    q = unicodedata.normalize("NFKC", query or "")
    return "".join(q.split()).lower().strip()


def _exact(conn, store_id: str, limit: int, raw: str) -> dict:
    """精确相等（原文 + 归一化两轮），s=1.0。

    P6.1 出处标记：`exact_kind` = "name"（问的就是这个字段名本身）/
    "alias"（问的是别名整句）。判定层的裸字段名救援只认 "name" ——
    别名语义宽于字段（如“退货政策”之于“退货期限”），不能享受同等待遇。
    """
    hits: dict = {}
    tried: set = set()
    for text in (raw, normalize_query(raw)):
        if not text or text in tried:
            continue
        tried.add(text)
        for kind, sql in (
            ("name", "SELECT f.id, f.entity, f.field_name, f.field_value FROM fields f"
                      " WHERE f.store_id=? AND f.field_name=? LIMIT ?"),
            ("alias", "SELECT f.id, f.entity, f.field_name, f.field_value FROM fields f"
                      " WHERE f.store_id=? AND EXISTS (SELECT 1"
                      " FROM field_aliases a WHERE a.field_id=f.id AND a.alias=?) LIMIT ?"),
        ):
            rows = conn.execute(sql, (store_id, text, limit)).fetchall()
            for fid, entity, name, value in rows:
                if fid not in hits:
                    hits[fid] = {
                        "field_id": fid,
                        "entity": entity,
                        "field_name": name,
                        "field_value": value,
                        "source": "field",
                        "s": 1.0,
                        "exact_kind": kind,
                    }
                elif kind == "name":
                    # 同一字段名和别名都精确命中时，出处记更强的字段名。
                    hits[fid]["exact_kind"] = "name"
    return hits


def _containment(conn, store_id: str, limit: int, keywords) -> dict:
    """关键词 ∈ field_name（可选 ∈ alias），s=CONTAINMENT_S。"""
    hits: dict = {}
    kws = [k for k in (keywords or ()) if k and len(k) >= CONTAINMENT_MIN_LEN]
    if not kws:
        return hits
    where = "instr(f.field_name, ?) > 0"
    if CONTAINMENT_IN_ALIAS:
        where += " OR instr(a.alias, ?) > 0"
    for kw in kws:
        rows = conn.execute(
            "SELECT DISTINCT f.id, f.entity, f.field_name, f.field_value FROM fields f"
            " LEFT JOIN field_aliases a ON a.field_id=f.id"
            f" WHERE f.store_id=? AND ({where})"
            " LIMIT ?",
            (store_id, kw, kw, limit) if CONTAINMENT_IN_ALIAS else (store_id, kw, limit),
        ).fetchall()
        for fid, entity, name, value in rows:
            if fid not in hits:
                hits[fid] = {
                    "field_id": fid,
                    "entity": entity,
                    "field_name": name,
                    "field_value": value,
                    "source": "field",
                    "s": CONTAINMENT_S,
                }
    return hits


def field_lookup(conn, store_id: str, query: str, limit: int = 5, prepared=None) -> list:
    """精确优先，未命中再用关键词包含补齐；同一字段取更强的 s。

    `prepared`：可选的 `query_prep.PreparedQuery`（调用方已算过就直接传，
    省一次 jieba 切词）。精确匹配始终用 `query` 原文，不受关键词化影响。
    """
    raw = (query or "").strip()
    if not raw:
        return []

    hits = _exact(conn, store_id, limit, raw)
    prep = prepared if prepared is not None else prepare(conn, raw)
    for fid, hit in _containment(conn, store_id, limit, prep.keywords).items():
        # 精确已命中则保留 1.0：包含是更弱的证据，不能把强证据降级。
        hits.setdefault(fid, hit)
    return list(hits.values())[:limit]
