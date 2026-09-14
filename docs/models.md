# 模型清单（来源 + SHA256 + 本地路径）

> 权重与词典体积大，一律 gitignored、本地专用；本文件 + `models/registry.py`
> 记录来源与哈希，`scripts/fetch-vendor.py` 记录 vendor 件。复现：
> `POST /model/download`（Settings 页按钮）或 `ensure_model_files`。

## bge-small-zh-v1.5（ONNX fp32，embedding，dim 512，L2 归一化）

- 转换：`onnx-community/bge-small-zh-v1.5-ONNX`（base `BAAI/bge-small-zh-v1.5`）
- 镜像顺序：ModelScope → hf-mirror → HF（实测：onnx 经 ModelScope 无降级；
  tokenizer.json 遇 ModelScope 超时自动降级 hf-mirror，顺序纪律有效）
- 本地：`sidecar/models/bge-small-zh-v1.5/`（约 95MB）

| 文件 | 尺寸 | SHA256 | 备注 |
|---|---|---|---|
| `model.onnx` | 41,689 | `69b353bb2aa2d09ab606ddbbc35437b03c843615a6bff28216a37fee7309c2aa` | HF LFS oid |
| `model.onnx_data` | 94,765,056 | `e72da961b03613124aa11317470c995ca197651a9d7f6be2b0e90aad92f71df0` | HF LFS oid |
| `tokenizer.json` | 362,603 | `3d09c84ebd10306706a79a8276b3ab736a40d8ec03251c7639f4e52c3a1a4f8e` | 非 LFS，首下实测 pin |
| `tokenizer_config.json` | 414 | `7e3bd6113f18c20975eaa8e8cc03c95b727fd83d6357f8e171e22b3736bf706d` | 非 LFS，首下实测 pin |

## libsimple（FTS5 中文分词 + 拼音，SQLite 扩展）

- 上游：`wangfenjin/simple` **v0.7.1**（MIT/GPL 双许可，按 MIT 使用）
- 变体：`libsimple-windows-x64.zip`（5,473,005 bytes，
  sha256 `7f03cc28cf307721f5621b5a52ef3bcb26c5215de012b09900492eb34d5bed0b`）
- 落盘：zip 内 `simple.dll`（1,518,592 bytes）→ `sidecar/vendor/libsimple.dll`；
  dict 五文件 → `sidecar/vendor/dict/`（idf.utf8 必需：缺失则扩展 abort 进程，
  见 `docs/compatibility.md`）
- 词典来源：CppJieba 词典（随官方包分发，见包内 `dict/README.md`）

## sqlite-vec（向量索引，MIT）

- pip 包 `sqlite-vec==0.1.9`（见 `requirements-lock.txt`），`vec0.dll` 随包，
  PyInstaller 经 `--collect-all sqlite_vec` 收包（TOCs 实证）。

## faster-whisper-base（ASR，CTranslate2 int8，MIT）

- 转换：`Systran/faster-whisper-base`（base `openai/whisper-base`）
- 镜像顺序：ModelScope → hf-mirror → HF（2026-09-14 实测三源对 `config.json` 均 200，
  故按既有纪律全列、不调序）
- 本地：`sidecar/models/faster-whisper-base/`（**实测 138.5 MiB**）
- 运行时：`faster-whisper==1.2.1` / `ctranslate2==4.8.2` / `av==18.1.0`（见 lock）；
  PyInstaller 经 `--collect-all ctranslate2` + `--collect-all av` 收包——
  ctranslate2 的原生 DLL 按名字加载、`av` 是 `faster_whisper.audio` 的顶层 import，
  两者静态分析都看不到，漏了会「装得上但跑不了」。

| 文件 | 尺寸 | SHA256 | 备注 |
|---|---|---|---|
| `model.bin` | 145,217,532 | `d01c3014881c9c6f3133c182f3d2887eb6ca1c789a7538c5c007196857a0a6a9` | HF LFS oid（权威） |
| `config.json` | 2,309 | `56a6d8110d311f19c8f0471e562832c7527f146b567275bfca59fcf7c184da9a` | 非 LFS，首下实测 pin |
| `tokenizer.json` | 2,203,239 | `fb7b63191e9bb045082c79fd742a3106a12c99513ab30df4a0d47fa6cb6fd0ab` | 非 LFS，首下实测 pin |
| `vocabulary.txt` | 459,861 | `34ce3fe1c5041027b3f8d42912270993f986dbc4bb34cf27f951e34a1e453913` | 非 LFS，首下实测 pin |

> 2026-09-14 实测：`ensure_model_files()` 经 hf-mirror 全量落盘 **17s**，
> 四个文件 **SHA256 全部通过**（registry 条目端到端验证）。
> 注：task15 原文写「~75MB」，实测 base 档为 138.5 MiB；~75MB 对应 `tiny` 档。
> 本轮按 provider 规格取 `base`，尺寸以实测为准。

## sense-voice（ASR，sherpa-onnx ONNX int8，中英日韩粤，task17）

- 转换：`csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17`
  （base `FunAudioLLM/SenseVoiceSmall`）
- 镜像顺序：hf-mirror → HF（**ModelScope 无对应镜像**：2026-09-14 实测
  `modelscope.cn/api/v1/models/csukuangfj/...` → 404；ModelScope 上的
  `iic/SenseVoiceSmall` 是 PyTorch/funasr 格式，与 ONNX 权重不通用。
  按既有纪律「缺失的镜像不列入、不调序」）
- 本地：`sidecar/models/sense-voice/`（**约 228 MiB**，两文件合计）
- 运行时：`sherpa-onnx==1.13.8`（见 lock；`sherpa-onnx` + `sherpa-onnx-core`
  两 wheel 合计约 18 MiB —— funasr 方案需 PyTorch ~2 GB，已否决）；
  PyInstaller 经 `--collect-all sherpa_onnx` 收包（原生扩展动态加载）。

| 文件 | 尺寸 | SHA256 | 备注 |
|---|---|---|---|
| `model.int8.onnx` | 239,233,841 | `c71f0ce00bec95b07744e116345e33d8cbbe08cef896382cf907bf4b51a2cd51` | HF LFS oid（权威），下载后逐字节复核通过 |
| `tokens.txt` | 315,894 | `f449eb28dc567533d7fa59be34e2abca8784f771850c78a47fb731a31429a1dc` | 非 LFS，首下实测 pin |

## paraformer-zh（ASR，sherpa-onnx ONNX int8，中文专用，task17）

- 转换：`csukuangfj/sherpa-onnx-paraformer-zh-2023-09-14`
  （base `damo/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404`）
- 镜像顺序：hf-mirror → HF（ModelScope 无对应镜像，同上）
- 本地：`sidecar/models/paraformer-zh/`（**约 232 MiB**，两文件合计）
- 运行时：同 sense-voice（同一 `sherpa-onnx`，不另增体积）。

| 文件 | 尺寸 | SHA256 | 备注 |
|---|---|---|---|
| `model.int8.onnx` | 243,371,218 | `f36a0433bcf096bd6d6f11b80a3ac8bed110bdca632fe0d731df8d1a84475945` | HF LFS oid（权威），下载后逐字节复核通过 |
| `tokens.txt` | 75,756 | `59aba8873a2ed1e122c25fee421e25f283b63290efbde85c1f01a853d83cb6e6` | 非 LFS，首下实测 pin |

