"""初始 Schema（PRD §3.3 全量，user_version=1）。

列名纪律（R4）：ft_qa 列必须与 qa_pairs 逐一对齐
（id / standard_question / official_answer），否则 FTS5 外部内容表只在
有数据时报错、空表假绿。
"""

TARGET_VERSION = 1

DDL_STATEMENTS = [
    """
    CREATE TABLE stores (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        template_id TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        is_current BOOLEAN DEFAULT FALSE
    )
    """,
    """
    CREATE TABLE qa_pairs (
        id TEXT PRIMARY KEY,
        store_id TEXT REFERENCES stores(id) ON DELETE CASCADE,
        standard_question TEXT NOT NULL,
        official_answer TEXT NOT NULL,
        category TEXT,
        usage_status TEXT CHECK(usage_status IN ('fixed', 'refresh', 'conditional', 'rejected')),
        followup_logic TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE fields (
        id TEXT PRIMARY KEY,
        store_id TEXT REFERENCES stores(id) ON DELETE CASCADE,
        entity TEXT NOT NULL,
        field_name TEXT NOT NULL,
        field_value TEXT NOT NULL,
        aliases TEXT,
        UNIQUE(store_id, entity, field_name)
    )
    """,
    """
    CREATE TABLE field_aliases (
        field_id TEXT NOT NULL REFERENCES fields(id) ON DELETE CASCADE,
        alias TEXT NOT NULL,
        PRIMARY KEY (field_id, alias)
    )
    """,
    "CREATE INDEX idx_field_aliases_alias ON field_aliases(alias)",
    """
    CREATE VIRTUAL TABLE ft_qa USING fts5(
        id UNINDEXED,
        standard_question,
        official_answer,
        content='qa_pairs',
        content_rowid='rowid',
        tokenize='simple'
    )
    """,
    """
    CREATE TRIGGER ft_qa_ai AFTER INSERT ON qa_pairs BEGIN
        INSERT INTO ft_qa(rowid, id, standard_question, official_answer)
        VALUES (new.rowid, new.id, new.standard_question, new.official_answer);
    END
    """,
    """
    CREATE TRIGGER ft_qa_ad AFTER DELETE ON qa_pairs BEGIN
        INSERT INTO ft_qa(ft_qa, rowid, id, standard_question, official_answer)
        VALUES ('delete', old.rowid, old.id, old.standard_question, old.official_answer);
    END
    """,
    """
    CREATE TRIGGER ft_qa_au AFTER UPDATE ON qa_pairs BEGIN
        INSERT INTO ft_qa(ft_qa, rowid, id, standard_question, official_answer)
        VALUES ('delete', old.rowid, old.id, old.standard_question, old.official_answer);
        INSERT INTO ft_qa(rowid, id, standard_question, official_answer)
        VALUES (new.rowid, new.id, new.standard_question, new.official_answer);
    END
    """,
    # vec 表与 embedding Provider 绑定，不可混用（PRD §3.3）：local=bge-small-zh 512 维。
    "CREATE VIRTUAL TABLE vec_qa_local USING vec0(qa_id TEXT PRIMARY KEY, embedding float[512])",
    # cloud=text-embedding-3-large 3072 维（Phase 2 启用，本任务仅建表）。
    "CREATE VIRTUAL TABLE vec_qa_cloud USING vec0(qa_id TEXT PRIMARY KEY, embedding float[3072])",
    """
    CREATE TABLE session_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_type TEXT NOT NULL,
        stage TEXT,
        error_type TEXT,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        metadata TEXT
    )
    """,
]
