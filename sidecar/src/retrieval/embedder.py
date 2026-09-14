"""bge-small-zh-v1.5 ONNX 本地 embedding（onnxruntime CPU，批量推理）。

- 模型：sidecar/models/bge-small-zh-v1.5/{model.onnx, *.onnx_data, tokenizer.*}
  （onnx-community 转换，base BAAI；来源/SHA 见 models/registry.py）。
- 输出：L2 归一化 float32 向量（dim=512），mean pooling（attention mask 加权）。
- 推理在任何 DB 事务之外执行（R6：调用方先批量算好，再进短事务）。
"""


class Embedder:
    """ONNX embedding 会话（单例持有；批量推理，输出 L2 归一化）。"""

    DIM = 512

    def __init__(self, model_dir, max_length: int = 512):
        import onnxruntime as ort
        from transformers import AutoTokenizer

        self.tokenizer = AutoTokenizer.from_pretrained(
            str(model_dir), trust_remote_code=False, local_files_only=True
        )
        self.session = ort.InferenceSession(
            str(model_dir / "model.onnx"), providers=["CPUExecutionProvider"]
        )
        self._input_names = [i.name for i in self.session.get_inputs()]
        self.max_length = max_length

    def embed(self, texts: list) -> "np.ndarray":
        """批量编码 → (n, 512) float32，逐行 L2 归一化。空输入返回 (0, 512)。"""
        import numpy as np

        if not texts:
            return np.zeros((0, self.DIM), dtype=np.float32)
        enc = self.tokenizer(
            list(texts),
            padding=True,
            truncation=True,
            max_length=self.max_length,
            return_tensors="np",
        )
        feed = {
            name: enc[name].astype(np.int64)
            for name in self._input_names
            if name in enc
        }
        hidden = self.session.run(None, feed)[0]
        mask = enc["attention_mask"].astype(np.float32)
        summed = (hidden * mask[..., None]).sum(axis=1)
        counts = mask.sum(axis=1, keepdims=True).clip(min=1e-9)
        pooled = summed / counts
        norms = np.linalg.norm(pooled, axis=1, keepdims=True).clip(min=1e-12)
        return (pooled / norms).astype(np.float32)