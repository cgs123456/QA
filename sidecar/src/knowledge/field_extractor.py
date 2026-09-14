"""字段归一化与别名展开（PRD §3.3 field_vocab.json）。

- field_name 命中任一别名组 → 规范化为该组 key；未命中保留原值并计数
  （compiler 聚合成 content-free 的 field_vocab_miss 事件）。
- 别名来源 = 词表组全体成员 + 输入自带 aliases；去重保序。
- 别名必须写入 field_aliases 展开表（否则直查退化为全表 LIKE）。
"""

import json
from pathlib import Path

DEFAULT_VOCAB_PATH = Path(__file__).resolve().parent / "field_vocab.json"


def load_vocab(path=None) -> dict:
    with open(path or DEFAULT_VOCAB_PATH, encoding="utf-8") as f:
        return json.load(f)


def normalize_field_name(name: str, vocab: dict) -> tuple:
    """返回 (canonical, matched)。匹配含 key 自身与组内任一别名（精确相等）。"""
    for canonical, members in vocab.items():
        if name == canonical or name in (members or []):
            return canonical, True
    return name, False


def build_aliases(canonical: str, vocab: dict, explicit=()) -> list:
    seen: dict = {}
    for alias in list(vocab.get(canonical, []) or []) + list(explicit or []):
        alias = str(alias).strip()
        if alias and alias not in seen:
            seen[alias] = True
    return list(seen)


def extract(field_items: list, vocab: dict) -> tuple:
    """返回 (items, vocab_miss)。items 含 entity/field_name(规范后)/
    field_value/aliases。"""
    out = []
    miss = 0
    for item in field_items or []:
        canonical, matched = normalize_field_name(item["field_name"], vocab)
        if not matched:
            miss += 1
        out.append(
            {
                "entity": item["entity"],
                "field_name": canonical,
                "field_value": item["field_value"],
                "aliases": build_aliases(
                    canonical, vocab, item.get("aliases") or []
                ),
            }
        )
    return out, miss
