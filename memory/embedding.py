"""
本地中文向量模型（bge-small-zh-v1.5）的 ONNX 推理封装，供 Chroma 使用。

为什么不直接用 Chroma 自带的向量函数：它内置的是英文模型 all-MiniLM-L6-v2，
实测中文句子之间几乎等于随机排序，长期记忆会挑错内容。这里换中文模型，
并用 bge 官方建议的 [CLS] 池化 + 查询前缀。

模型文件不在仓库里，用 tools/fetch_embedding_model.py 下载到 data/models 下。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import numpy as np
import onnxruntime as ort
from chromadb.api.types import Documents, EmbeddingFunction, Embeddings
from tokenizers import Tokenizer

logger = logging.getLogger(__name__)

MODEL_FILE = "model.onnx"
TOKENIZER_FILE = "tokenizer.json"
MAX_TOKENS = 512
# bge 官方给中文检索建议的查询前缀，只加在"查询"上，不加在"记忆正文"上
QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："


class OnnxChineseEmbedding(EmbeddingFunction[Documents]):
    def __init__(self, model_dir: Path | str):
        self.model_dir = Path(model_dir)
        onnx_path = self.model_dir / MODEL_FILE
        tokenizer_path = self.model_dir / TOKENIZER_FILE
        if not onnx_path.exists() or not tokenizer_path.exists():
            raise FileNotFoundError(
                f"向量模型文件缺失（{self.model_dir}）。"
                "请先在项目根目录执行：.venv\\Scripts\\python.exe tools\\fetch_embedding_model.py"
            )

        options = ort.SessionOptions()
        options.log_severity_level = 3
        self._session = ort.InferenceSession(
            str(onnx_path), providers=["CPUExecutionProvider"], sess_options=options
        )
        self._tokenizer = Tokenizer.from_file(str(tokenizer_path))
        self._tokenizer.enable_truncation(max_length=MAX_TOKENS)
        self._tokenizer.enable_padding(pad_id=0, pad_token="[PAD]")
        logger.info("中文向量模型已加载: %s", self.model_dir)

    # ---------- Chroma 要求的接口 ----------

    @staticmethod
    def name() -> str:
        return "bge-small-zh-v1.5-onnx"

    def default_space(self) -> str:
        return "cosine"

    def get_config(self) -> dict[str, Any]:
        return {"model_dir": str(self.model_dir)}

    @staticmethod
    def build_from_config(config: dict[str, Any]) -> "OnnxChineseEmbedding":
        return OnnxChineseEmbedding(config["model_dir"])

    def embed_query(self, input: Documents) -> Embeddings:
        """查询侧加上 bge 建议的前缀，提升检索命中率。"""
        texts = [QUERY_INSTRUCTION + text for text in input]
        return self.__call__(texts)

    def __call__(self, input: Documents) -> Embeddings:
        if not input:
            return []
        encoded = self._tokenizer.encode_batch(list(input))
        inputs = {
            "input_ids": np.array([e.ids for e in encoded], dtype=np.int64),
            "attention_mask": np.array([e.attention_mask for e in encoded], dtype=np.int64),
            "token_type_ids": np.zeros(
                (len(encoded), len(encoded[0].ids)), dtype=np.int64
            ),
        }
        last_hidden_state = self._session.run(None, inputs)[0]
        # bge 用 [CLS] 位（第 0 个 token）的向量作为句向量，再做 L2 归一化
        cls_vector = last_hidden_state[:, 0]
        norms = np.linalg.norm(cls_vector, axis=1, keepdims=True)
        norms[norms == 0] = 1e-12
        return [(cls_vector / norms)[index].astype(np.float32) for index in range(len(encoded))]
