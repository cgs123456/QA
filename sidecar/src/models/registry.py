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

sense-voice / paraformer-zh（sherpa-onnx ONNX int8，ASR，task17）：
- 两者的 `model.int8.onnx` sha256 均取自 HF LFS oid（权威），已实测下载后逐字节复核通过。
- `tokens.txt` 为非 LFS 小文件，上游无 SHA；值为 2026-09-14 首下实测 pin。
- **ModelScope 无对应镜像**（实测 `modelscope.cn/api/v1/models/csukuangfj/...` → 404；
  ModelScope 上的 `iic/SenseVoiceSmall`、`iic/speech_paraformer-...-pytorch` 是 PyTorch/funasr
  格式，与 ONNX 权重不通用）。按既有纪律「缺失的镜像不列入、不调序」，
  故这两条只列 hf-mirror → HF。
- 另注：本机 `huggingface.co` 直连不通（curl 000），hf-mirror 是唯一实测可用源；
  HF 条目保留是为了镜像顺序契约完整，不代表本机可用。
"""

BGE_SMALL_ZH = "bge-small-zh-v1.5"
FASTER_WHISPER_BASE = "faster-whisper-base"
SENSE_VOICE = "sense-voice"
PARAFORMER_ZH = "paraformer-zh"

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


# sherpa-onnx 系列（task17）：ModelScope 无镜像，故只列 hf-mirror → HF。
_SV_REPO = "csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"
_PF_REPO = "csukuangfj/sherpa-onnx-paraformer-zh-2023-09-14"


def _sherpa_urls(repo: str, remote: str) -> list:
    return [f"https://hf-mirror.com/{repo}/resolve/main/{remote}",
            f"https://huggingface.co/{repo}/resolve/main/{remote}"]


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
    SENSE_VOICE: {
        "display": "SenseVoiceSmall (sherpa-onnx ONNX int8, 中英日韩粤)",
        "base_model": "FunAudioLLM/SenseVoiceSmall",
        "files": {
            "model.int8.onnx": {
                "remote": "model.int8.onnx",
                "size": 239233841,
                # HF LFS oid（权威）；2026-09-14 实测下载后逐字节复核通过。
                "sha256": "c71f0ce00bec95b07744e116345e33d8cbbe08cef896382cf907bf4b51a2cd51",
                "urls": _sherpa_urls(_SV_REPO, "model.int8.onnx"),
            },
            "tokens.txt": {
                "remote": "tokens.txt",
                "size": 315894,
                # 非 LFS、无上游 SHA：2026-09-14 经 hf-mirror 首下实测 pin
                #（尺寸 315894 + 内容为 `<unk> 0` 起首的 token 表）。
                "sha256": "f449eb28dc567533d7fa59be34e2abca8784f771850c78a47fb731a31429a1dc",
                "urls": _sherpa_urls(_SV_REPO, "tokens.txt"),
            },
        },
    },
    PARAFORMER_ZH: {
        "display": "Paraformer-zh (sherpa-onnx ONNX int8, 中文)",
        "base_model": "damo/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404",
        "files": {
            "model.int8.onnx": {
                "remote": "model.int8.onnx",
                "size": 243371218,
                # HF LFS oid（权威）；2026-09-14 实测下载后逐字节复核通过。
                "sha256": "f36a0433bcf096bd6d6f11b80a3ac8bed110bdca632fe0d731df8d1a84475945",
                "urls": _sherpa_urls(_PF_REPO, "model.int8.onnx"),
            },
            "tokens.txt": {
                "remote": "tokens.txt",
                "size": 75756,
                # 非 LFS、无上游 SHA：2026-09-14 经 hf-mirror 首下实测 pin
                #（尺寸 75756 + 内容为 `<blank> 0` 起首的 token 表）。
                "sha256": "59aba8873a2ed1e122c25fee421e25f283b63290efbde85c1f01a853d83cb6e6",
                "urls": _sherpa_urls(_PF_REPO, "tokens.txt"),
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
