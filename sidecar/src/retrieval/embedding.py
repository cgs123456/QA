"""双 embedding 基建（F5.3/F6.5）：local bge-512 / cloud text-embedding-3-large-3072。

分层（对标 asr/ 的 provider → catalog → runtime 三层，但收拢在一个模块内——
只有两个 provider，无需拆三文件）：

1. **Provider（无状态）**：`LocalEmbeddingProvider`（bge-small-zh-v1.5 ONNX，
   推理仍走 `retrieval.embedder.Embedder`）与 `OpenAIEmbeddingProvider`
   （text-embedding-3-large，3072 维）。接口统一为
   `async embed(texts) -> list[list[float]]`，L2 归一化由各家自行保证
   （OpenAI 返回即归一化；本地沿用 Embedder 的 mean-pooling 归一化）。
2. **目录（无状态）**：`EMBEDDING_CATALOG`（显示名/维度/vec 表/是否需 key，
   给设置页 + `/embedding/provider` 快照共用，内容无关 R14）。
3. **运行时态（有状态，进程级）**：`_ACTIVE`（默认 local，task15 行为不变；
   sidecar 重启即回 local，与 secrets 内存语义一致）。

关键纪律（R6/R14/R18）：

- **推理永远在事务外**：provider 只产向量；写库由调用方（compiler 短事务 /
  重建 worker 按批短事务）完成。本模块不碰 DB 写。
- **维度守卫**：vec 表与 provider 强绑定（local↔512↔vec_qa_local，
  cloud↔3072↔vec_qa_cloud），`check_blob_dim` 在每次写前断言；
  512 模型的向量写不进 cloud 表，反之亦然——大声失败，绝不静默混写。
- **成本日志只记 token 数**：OpenAI 响应自带 `usage.prompt_tokens`，记数不记文；
  文本永不进日志/异常/进度记录（R14/R18，单测以 caplog + 响应断言锁定）。
- **构造不加载权重**：`LocalEmbeddingProvider` 首次 `embed` 才建会话；
  `model_ready()` 只查文件存在性（供目录 `ready` 快照，不加载、不联网）。
- **重建节流**：`BATCH_SIZE=64` 限定单批工作量；后台 worker 逐批 await，
  每批之间让出事件循环（检索请求在批间隙仍可服务）。

密钥：cloud 复用 LLM 侧 `openai` 槽位（`POST /settings/llm-secret`），
与云端 ASR（`CLOUD_SECRET_SLOT="openai"`）同源——用户只需保存一次。
"""

import asyncio
import logging
import threading

import httpx

logger = logging.getLogger("retrieval.embedding")

PROVIDER_LOCAL = "local"
PROVIDER_CLOUD = "cloud"
DEFAULT_PROVIDER = PROVIDER_LOCAL

TABLE_LOCAL = "vec_qa_local"
TABLE_CLOUD = "vec_qa_cloud"

#: provider → vec 表（唯一映射，反向亦然）。
TABLE_FOR_PROVIDER = {
    PROVIDER_LOCAL: TABLE_LOCAL,
    PROVIDER_CLOUD: TABLE_CLOUD,
}

#: vec 表 → 维度（维度守卫的唯一真源）。
DIM_FOR_TABLE = {
    TABLE_LOCAL: 512,
    TABLE_CLOUD: 3072,
}

#: cloud 密钥槽位：复用 LLM 侧 openai（与云端 ASR 同源，用户只存一次）。
CLOUD_SECRET_SLOT = "openai"

CLOUD_MODEL = "text-embedding-3-large"
CLOUD_DIM = 3072
CLOUD_BASE_URL = "https://api.openai.com/v1"
CLOUD_TIMEOUT_S = 30.0
#: 单批文本数：限定单次请求体与单批事件循环占用。
BATCH_SIZE = 64
#: 限流退避：429/5xx/瞬断最多追加 5 次，1s 起指数退避（sleep 可注入，单测用假时钟）。
MAX_RETRIES = 5
BACKOFF_BASE_S = 1.0
_RETRYABLE_STATUS = (429, 500, 502, 503, 504)

EMBEDDING_CATALOG: dict = {
    PROVIDER_LOCAL: {
        "name": PROVIDER_LOCAL,
        "display": "本地 bge-small-zh-v1.5（512 维，离线）",
        "kind": "local",
        "dim": 512,
        "table": TABLE_LOCAL,
        "needs_key": False,
        "note": "默认；零网络依赖，task15 行为不变",
    },
    PROVIDER_CLOUD: {
        "name": PROVIDER_CLOUD,
        "display": "OpenAI text-embedding-3-large（3072 维）",
        "kind": "cloud",
        "dim": CLOUD_DIM,
        "table": TABLE_CLOUD,
        "needs_key": True,
        "note": "需 API Key（复用 openai 槽位）；按 token 计费，只记数不记文",
    },
}


class EmbeddingError(Exception):
    """embedding 故障（kind 与 generation.ProviderError 同表，便于调用方统一处理）。

    kind：timeout / connection / http / protocol / auth / rate_limited /
    unavailable（缺模型/缺 key，连构造/运行的前提都不满足；settings 侧映射为 409）。
    message 永不含 key 与原文（R8/R14）。
    """

    def __init__(self, kind: str, message: str):
        super().__init__(f"[{kind}] {message}")
        self.kind = kind


def _map_httpx_error(e: Exception, where: str) -> EmbeddingError:
    if isinstance(e, httpx.TimeoutException):
        return EmbeddingError("timeout", f"{where} 超时（{CLOUD_TIMEOUT_S}s）")
    if isinstance(e, httpx.ConnectError):
        return EmbeddingError("connection", f"{where} 连接失败：{type(e).__name__}")
    return EmbeddingError("protocol", f"{where} 异常：{type(e).__name__}")


def _status_error(where: str, status: int) -> EmbeddingError:
    """HTTP 状态映射（与 generation 各云 provider 同表：401/403→auth，429→rate_limited）。"""
    if status in (401, 403):
        return EmbeddingError("auth", f"{where} 鉴权失败 HTTP {status}")
    if status == 429:
        return EmbeddingError("rate_limited", f"{where} 限流 HTTP 429")
    return EmbeddingError("http", f"{where} HTTP {status}")


def check_blob_dim(table: str, blob: bytes) -> int:
    """写前维度守卫：blob 字节数/4 必须等于表维度，否则大声失败。

    返回维度（调用方可复用，免二次计算）。未知表名同样拒绝（防 SQL 拼接注入——
    调用方插表名进 SQL 前必须先过本函数做白名单）。
    """
    try:
        want = DIM_FOR_TABLE[table]
    except KeyError:
        raise ValueError(f"未知 vec 表：{table}") from None
    got = len(blob) // 4
    if got != want:
        raise ValueError(f"维度守卫：{want} 维表 {table} 拒绝 {got} 维向量（不可混写）")
    return got


class LocalEmbeddingProvider:
    """本地 bge（512 维）。构造不加载权重；模型缺失在 embed/model_ready 处 loud。"""

    name = PROVIDER_LOCAL
    dim = 512

    def __init__(self, model_dir=None):
        self._model_dir = model_dir
        self._embedder = None

    def _dir(self):
        if self._model_dir is not None:
            return self._model_dir
        from models.registry import BGE_SMALL_ZH, default_model_dir

        return default_model_dir(BGE_SMALL_ZH)

    def model_ready(self) -> bool:
        """权重文件是否齐（只查存在性，不加载）。"""
        d = self._dir()
        return (d / "model.onnx").is_file() and (d / "tokenizer.json").is_file()

    async def embed(self, texts: list) -> list:
        if not self.model_ready():
            raise EmbeddingError("unavailable", "本地 embedding 模型缺失")
        if self._embedder is None:
            from retrieval.embedder import Embedder

            self._embedder = Embedder(self._dir())
        vecs = self._embedder.embed(list(texts or []))
        return [list(map(float, row)) for row in vecs]


class OpenAIEmbeddingProvider:
    """OpenAI text-embedding-3-large（3072 维）：批量 + 限流退避 + token 计数日志。"""

    name = PROVIDER_CLOUD
    dim = CLOUD_DIM

    def __init__(self, api_key: str, model: str | None = None,
                 base_url: str | None = None,
                 client: httpx.AsyncClient | None = None,
                 sleep=None, max_retries: int = MAX_RETRIES):
        if not api_key:
            raise ValueError("cloud embedding 需要 API Key")
        self.api_key = api_key
        self.model = model or CLOUD_MODEL
        self.base_url = (base_url or CLOUD_BASE_URL).rstrip("/")
        self._client = client
        # sleep 可注入：生产用 asyncio.sleep，单测用记录型假时钟（不真等）。
        self._sleep = sleep or asyncio.sleep
        self.max_retries = max_retries
        # 累计 prompt tokens（计数，供重建进度通道与成本日志；文本永不记录）。
        self.tokens_used = 0

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}"}

    async def _post_batch(self, client: httpx.AsyncClient, batch: list) -> tuple:
        """单批请求 + 退避重试。返回 (vectors, prompt_tokens)。"""
        url = self.base_url + "/embeddings"
        payload = {"model": self.model, "input": batch}
        last_error = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = await client.post(url, json=payload, headers=self._headers())
            except Exception as e:  # noqa: BLE001 — 传输层异常统一映射后按瞬断退避
                last_error = _map_httpx_error(e, "cloud embeddings")
                if last_error.kind not in ("timeout", "connection"):
                    raise last_error
            else:
                if resp.status_code == 200:
                    try:
                        body = resp.json()
                        vecs = [d["embedding"] for d in body["data"]]
                        usage = body.get("usage") or {}
                        tokens = int(usage.get("prompt_tokens", 0) or 0)
                    except (ValueError, KeyError, IndexError, TypeError) as e:
                        raise EmbeddingError("protocol", f"cloud embeddings 解析失败：{type(e).__name__}")
                    if len(vecs) != len(batch):
                        raise EmbeddingError(
                            "protocol",
                            f"cloud embeddings 条目数不符（要 {len(batch)} 回 {len(vecs)}）")
                    for v in vecs:
                        if len(v) != self.dim:
                            raise EmbeddingError(
                                "protocol",
                                f"cloud embeddings 维度不符（要 {self.dim} 回 {len(v)}）")
                    return vecs, tokens
                if resp.status_code in _RETRYABLE_STATUS:
                    last_error = _status_error("cloud embeddings", resp.status_code)
                else:
                    raise _status_error("cloud embeddings", resp.status_code)
            if attempt < self.max_retries:
                await self._sleep(BACKOFF_BASE_S * (2 ** attempt))
        raise last_error

    async def embed(self, texts: list) -> list:
        """批量 embed（BATCH_SIZE 切批；日志只记 token 数，不记文本）。

        texts 为空返回 []（与本地 Embedder 的 (0, dim) 语义对齐的列表版）。
        """
        items = list(texts or [])
        if not items:
            return []
        client = self._client or httpx.AsyncClient(
            timeout=httpx.Timeout(CLOUD_TIMEOUT_S))
        try:
            out: list = []
            total_tokens = 0
            for i in range(0, len(items), BATCH_SIZE):
                batch = items[i:i + BATCH_SIZE]
                vecs, tokens = await self._post_batch(client, batch)
                out.extend(vecs)
                total_tokens += tokens
                self.tokens_used += tokens
                # tokens 是计数（R14/R18 内容无关）；batch 内容永不进日志。
                logger.info("embed batch model=%s n=%d prompt_tokens=%d",
                            self.model, len(batch), tokens)
            logger.info("embed total model=%s n=%d prompt_tokens=%d",
                        self.model, len(items), total_tokens)
            return out
        except EmbeddingError:
            raise
        except Exception as e:  # noqa: BLE001 — 兜底：种类名可下行，原文不可
            raise _map_httpx_error(e, "cloud embeddings")
        finally:
            if self._client is None:
                await client.aclose()

    async def check_reachable(self) -> int:
        """可达校验：单条探测（固定文本，无用户数据；token 开销可忽略）。"""
        vecs = await self.embed(["连通性探测"])
        if not vecs or len(vecs[0]) != self.dim:
            raise EmbeddingError("protocol", "cloud embeddings 可达校验无回包")
        return self.dim


def build_embedding_provider(name: str, *, api_key: str | None = None,
                             client=None, **opts):
    """构造 embedding provider。cloud 缺 key 即抛（调用方转 409/降级）。"""
    key = (name or "").strip().lower()
    if key == PROVIDER_LOCAL:
        return LocalEmbeddingProvider(**opts)
    if key == PROVIDER_CLOUD:
        resolved = api_key
        if resolved is None:
            from core.secrets import get_llm_secret

            resolved = get_llm_secret(CLOUD_SECRET_SLOT)
        if not resolved:
            raise EmbeddingError("unavailable", "cloud embedding 缺少 API Key（openai 槽位）")
        return OpenAIEmbeddingProvider(api_key=resolved, client=client, **opts)
    raise ValueError(f"未知 embedding provider：{name}")


def model_ready(name: str) -> bool:
    """该 provider 现在能否跑（只查文件/内存 key，不加载、不联网）。

    cloud 的“可达”（live 探测）不在此列——由切换端点在创建重建任务前
    显式校验（联网动作不藏在目录快照里）。
    """
    key = (name or "").strip().lower()
    if key == PROVIDER_LOCAL:
        return LocalEmbeddingProvider().model_ready()
    if key == PROVIDER_CLOUD:
        from core.secrets import get_llm_secret

        return bool(get_llm_secret(CLOUD_SECRET_SLOT))
    return False


def catalog_entries() -> list:
    """设置页目录快照（内容无关：无 key、无向量字节）。"""
    return [
        {
            "name": name,
            "display": EMBEDDING_CATALOG[name]["display"],
            "kind": EMBEDDING_CATALOG[name]["kind"],
            "dim": EMBEDDING_CATALOG[name]["dim"],
            "table": EMBEDDING_CATALOG[name]["table"],
            "needs_key": EMBEDDING_CATALOG[name]["needs_key"],
            "note": EMBEDDING_CATALOG[name]["note"],
            "ready": model_ready(name),
        }
        for name in (PROVIDER_LOCAL, PROVIDER_CLOUD)
    ]


# ---------------- 运行时态（进程级，默认 local） ----------------
# sidecar/LLM 重启即回 local（内存语义，与 secrets/ASR 切换板一致）。

_ACTIVE = DEFAULT_PROVIDER
_ACTIVE_LOCK = threading.Lock()


def get_active() -> str:
    """当前生效的 embedding provider（检索与入库共用同一答案）。"""
    with _ACTIVE_LOCK:
        return _ACTIVE


def set_active(name: str) -> str:
    """原子切换生效 provider（重建 worker 仅在目标表全量完成后调一次）。

    未知名字就地 ValueError——切换失败必须保持原状，不能留下半切状态。
    """
    key = (name or "").strip().lower()
    if key not in (PROVIDER_LOCAL, PROVIDER_CLOUD):
        raise ValueError(f"未知 embedding provider：{name}")
    global _ACTIVE
    with _ACTIVE_LOCK:
        _ACTIVE = key
    return key


def active_table() -> str:
    """当前生效 provider 对应的 vec 表（检索/入库的唯一真源）。"""
    return TABLE_FOR_PROVIDER[get_active()]


def describe() -> dict:
    """当前态快照（内容无关，供设置页初始渲染）。"""
    active = get_active()
    return {
        "active": active,
        "dim": EMBEDDING_CATALOG[active]["dim"],
        "table": TABLE_FOR_PROVIDER[active],
        "available": catalog_entries(),
    }


__all__ = [
    "BATCH_SIZE", "CLOUD_BASE_URL", "CLOUD_DIM", "CLOUD_MODEL",
    "CLOUD_SECRET_SLOT", "CLOUD_TIMEOUT_S", "DEFAULT_PROVIDER",
    "DIM_FOR_TABLE", "EMBEDDING_CATALOG", "MAX_RETRIES",
    "PROVIDER_CLOUD", "PROVIDER_LOCAL", "TABLE_CLOUD", "TABLE_FOR_PROVIDER",
    "TABLE_LOCAL",
    "EmbeddingError", "LocalEmbeddingProvider", "OpenAIEmbeddingProvider",
    "active_table", "build_embedding_provider", "catalog_entries",
    "check_blob_dim", "describe", "get_active", "model_ready", "set_active",
]
