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
