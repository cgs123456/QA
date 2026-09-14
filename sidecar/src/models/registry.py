"""模型清单：来源 + SHA256 + 维度（supply-chain 可审计）。

bge-small-zh-v1.5（ONNX fp32，onnx-community 转换，base BAAI）：
- model.onnx / model.onnx_data 的 sha256 取自 HF LFS oid（权威）。
- tokenizer.json / tokenizer_config.json 为非 LFS 小文件，上游无 SHA；
  值为首次下载后实测 pin（self-attested，落盘时记录），downloader 照常校验。
- 镜像顺序恒为 ModelScope → hf-mirror → HF（缺失的镜像不列入，不调序）。
"""

BGE_SMALL_ZH = "bge-small-zh-v1.5"

_MS = "https://modelscope.cn/models/onnx-community/bge-small-zh-v1.5-ONNX/resolve/master"
_HFM = "https://hf-mirror.com/onnx-community/bge-small-zh-v1.5-ONNX/resolve/main"
_HF = "https://huggingface.co/onnx-community/bge-small-zh-v1.5-ONNX/resolve/main"


def _urls(remote: str) -> list:
    return [f"{_MS}/{remote}", f"{_HFM}/{remote}", f"{_HF}/{remote}"]


MODEL_REGISTRY = {
    BGE_SMALL_ZH: {
        "display": "bge-small-zh-v1.5 (ONNX fp32, onnx-community)",
        "base_model": "BAAI/bge-small-zh-v1.5",
        "dim": 512,
        "files": {
            "model.onnx": {
                "remote": "onnx/model.onnx",
                "size": 41689,
                "sha256": "69b353bb2aa2d09ab606ddbbc35437b03c843615a6bff28216a37fee7309c2aa",
                "urls": _urls("onnx/model.onnx"),
            },
            "model.onnx_data": {
                "remote": "onnx/model.onnx_data",
                "size": 94765056,
                "sha256": "e72da961b03613124aa11317470c995ca197651a9d7f6be2b0e90aad92f71df0",
                "urls": _urls("onnx/model.onnx_data"),
            },
            "tokenizer.json": {
                "remote": "tokenizer.json",
                "size": 362603,
                # 非 LFS、无上游 SHA：2026-09-14 首下实测 pin
                #（尺寸 362603 + JSON 合法；经 hf-mirror 落盘，ModelScope 首试超时自动降级）。
                "sha256": "3d09c84ebd10306706a79a8276b3ab736a40d8ec03251c7639f4e52c3a1a4f8e",
                "urls": _urls("tokenizer.json"),
            },
            "tokenizer_config.json": {
                "remote": "tokenizer_config.json",
                "size": 414,
                # 非 LFS、无上游 SHA：2026-09-14 首下实测 pin（尺寸 414 + JSON 合法，经 ModelScope）。
                "sha256": "7e3bd6113f18c20975eaa8e8cc03c95b727fd83d6357f8e171e22b3736bf706d",
                "urls": _urls("tokenizer_config.json"),
            },
        },
    }
}


def default_model_dir(name: str):
    """本地存放目录：BASE/models/<name>（与 vendor 同级，gitignored）。"""
    from database.connection import resolve_base

    return resolve_base() / "models" / name
