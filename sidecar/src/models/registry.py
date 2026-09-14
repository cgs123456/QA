"""模型清单：来源 + SHA256 + 维度（supply-chain 可审计）。

bge-small-zh-v1.5（ONNX fp32，onnx-community 转换，base BAAI）：
- model.onnx / model.onnx_data 的 sha256 取自 HF LFS oid（权威）。
- tokenizer.json / tokenizer_config.json 为非 LFS 小文件，上游无 SHA；
  值为首次下载后实测 pin（self-attested，落盘时记录），downloader 照常校验。
- 镜像顺序恒为 ModelScope → hf-mirror → HF（缺失的镜像不列入，不调序）。

faster-whisper-base（CTranslate2 int8，Systran 转换，ASR）：
- `model.bin`（145,217,532 B）的 sha256 取自 HF LFS oid（权威）。
- config.json / tokenizer.json / vocabulary.txt 为非 LFS 小文件，上游无 SHA；
  值为 2026-09-14 首下实测 pin（尺寸 + 内容合法性均已核）。
- 三源实测均可达，同样按 ModelScope → hf-mirror → HF 列出。
"""

BGE_SMALL_ZH = "bge-small-zh-v1.5"
FASTER_WHISPER_BASE = "faster-whisper-base"

_MS = "https://modelscope.cn/models/onnx-community/bge-small-zh-v1.5-ONNX/resolve/master"
_HFM = "https://hf-mirror.com/onnx-community/bge-small-zh-v1.5-ONNX/resolve/main"
_HF = "https://huggingface.co/onnx-community/bge-small-zh-v1.5-ONNX/resolve/main"


def _urls(remote: str) -> list:
    return [f"{_MS}/{remote}", f"{_HFM}/{remote}", f"{_HF}/{remote}"]


# faster-whisper base：镜像顺序同样为 ModelScope → hf-mirror → HF。
# 2026-09-14 实测三源对 config.json 均可用（ModelScope 200 / hf-mirror 200），
# 故按既有纪律全列、不调序。
_FW_REPO = "Systran/faster-whisper-base"
_FW_MS = f"https://modelscope.cn/models/{_FW_REPO}/resolve/master"
_FW_HFM = f"https://hf-mirror.com/{_FW_REPO}/resolve/main"
_FW_HF = f"https://huggingface.co/{_FW_REPO}/resolve/main"


def _fw_urls(remote: str) -> list:
    return [f"{_FW_MS}/{remote}", f"{_FW_HFM}/{remote}", f"{_FW_HF}/{remote}"]


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
    },
    FASTER_WHISPER_BASE: {
        "display": "faster-whisper base (CTranslate2 int8, Systran 转换)",
        "base_model": "openai/whisper-base",
        "files": {
            "model.bin": {
                "remote": "model.bin",
                "size": 145217532,
                "sha256": "d01c3014881c9c6f3133c182f3d2887eb6ca1c789a7538c5c007196857a0a6a9",
                "urls": _fw_urls("model.bin"),
            },
            "config.json": {
                "remote": "config.json",
                "size": 2309,
                # 非 LFS、无上游 SHA：2026-09-14 经 hf-mirror 首下实测 pin
                #（尺寸 2309 + JSON 合法）。
                "sha256": "56a6d8110d311f19c8f0471e562832c7527f146b567275bfca59fcf7c184da9a",
                "urls": _fw_urls("config.json"),
            },
            "tokenizer.json": {
                "remote": "tokenizer.json",
                "size": 2203239,
                # 非 LFS：2026-09-14 首下实测 pin（尺寸 2203239 + JSON 合法）。
                "sha256": "fb7b63191e9bb045082c79fd742a3106a12c99513ab30df4a0d47fa6cb6fd0ab",
                "urls": _fw_urls("tokenizer.json"),
            },
            "vocabulary.txt": {
                "remote": "vocabulary.txt",
                "size": 459861,
                # 非 LFS：2026-09-14 首下实测 pin（尺寸 459861）。
                "sha256": "34ce3fe1c5041027b3f8d42912270993f986dbc4bb34cf27f951e34a1e453913",
                "urls": _fw_urls("vocabulary.txt"),
            },
        },
    },
}


def default_model_dir(name: str):
    """本地存放目录：开发态 BASE/models/<name>；打包态用户数据目录
    （与 DB 同理：可写数据绝不进 bundle）。"""
    import sys as _sys

    from database.connection import _user_data_dir, resolve_base

    if getattr(_sys, "frozen", False):
        return _user_data_dir() / "models" / name
    return resolve_base() / "models" / name
