"""embedder 单测（需本地模型；缺失则跳过——模型 ~95MB，gitignored）。"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from models.registry import BGE_SMALL_ZH, MODEL_REGISTRY, default_model_dir
from retrieval.embedder import Embedder

MODEL_DIR = default_model_dir(BGE_SMALL_ZH)
NEED_MODEL = pytest.mark.skipif(
    not all((MODEL_DIR / f).is_file() for f in ("model.onnx", "tokenizer.json")),
    reason="本地模型缺失（sidecar/models/bge-small-zh-v1.5/），跳过",
)


@pytest.fixture(scope="module")
def embedder():
    from retrieval.embedder import Embedder as E

    return E(MODEL_DIR)


@NEED_MODEL
def test_dim_and_l2_norm(embedder):
    import numpy as np

    vecs = embedder.embed(["你的发货周期是多久", "hello world"])
    assert vecs.shape == (2, 512)
    assert vecs.dtype == np.float32
    norms = np.linalg.norm(vecs, axis=1)
    assert abs(norms[0] - 1.0) < 1e-5
    assert abs(norms[1] - 1.0) < 1e-5


@NEED_MODEL
def test_batch_determinism(embedder):
    import numpy as np

    a = embedder.embed(["退货期限"])
    b = embedder.embed(["退货期限", "退货期限"])
    assert np.allclose(a[0], b[0], atol=1e-6)
    assert np.allclose(a[0], b[1], atol=1e-6)
    cos = float(np.dot(a[0], b[0]))
    assert cos > 0.999


@NEED_MODEL
def test_empty_input(embedder):
    vecs = embedder.embed([])
    assert vecs.shape == (0, 512)


def test_registry_dim_matches_embedder():
    assert MODEL_REGISTRY[BGE_SMALL_ZH]["dim"] == Embedder.DIM == 512
