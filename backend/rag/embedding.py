from __future__ import annotations

import logging
import re
import hashlib
from typing import Optional

import numpy as np

from backend.config import EMBEDDING_MODEL, RAG_EMBEDDING_BACKEND

logger = logging.getLogger("rag.embedding")


class EmbeddingEngine:
    """向量化引擎：封装 sentence-transformers，提供文本向量化能力"""

    def __init__(self, model_name: str = EMBEDDING_MODEL) -> None:
        self.model_name = model_name
        self._model: Optional[object] = None
        self.dimension: int = 384  # all-MiniLM-L6-v2 输出维度
        self._use_hash = RAG_EMBEDDING_BACKEND.lower() in {"hash", "fallback"}

    def _load_model(self) -> object:
        """延迟加载模型"""
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
                self._model = SentenceTransformer(self.model_name)
                self.dimension = self._model.get_sentence_embedding_dimension()
                logger.info(f"Embedding模型加载完成: {self.model_name} "
                           f"(维度={self.dimension})")
            except ImportError:
                raise ImportError(
                    "sentence-transformers 未安装，请执行: pip install sentence-transformers"
                )
        return self._model

    def encode(self, text: str | list[str]) -> np.ndarray:
        """将文本编码为向量"""
        if self._use_hash:
            return self._hash_encode(text)
        model = self._load_model()
        is_single = isinstance(text, str)
        if is_single:
            texts = [text]
        else:
            texts = text

        embeddings = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)

        if is_single:
            return embeddings[0]
        return embeddings

    def _hash_encode(self, text: str | list[str]) -> np.ndarray:
        """无模型时的确定性词袋向量，保证本地离线 RAG 仍可工作。"""
        values = [text] if isinstance(text, str) else text
        matrix = np.zeros((len(values), self.dimension), dtype=np.float32)
        for row, value in enumerate(values):
            tokens = re.findall(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]", str(value).lower())
            for token in tokens:
                digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
                index = int.from_bytes(digest[:4], "little") % self.dimension
                matrix[row, index] += 1.0
            norm = np.linalg.norm(matrix[row])
            if norm:
                matrix[row] /= norm
        return matrix[0] if isinstance(text, str) else matrix

    def encode_to_bytes(self, text: str) -> bytes:
        """将文本编码后转为字节（用于SQLite BLOB存储）"""
        vec = self.encode(text)
        return vec.astype(np.float32).tobytes()

    def decode_from_bytes(self, data: bytes) -> np.ndarray:
        """从字节解码回向量"""
        return np.frombuffer(data, dtype=np.float32)

    def batch_encode_to_bytes(self, texts: list[str]) -> list[bytes]:
        """批量编码为字节列表"""
        embeddings = self.encode(texts)
        return [e.astype(np.float32).tobytes() for e in embeddings]


# 全局单例
_engine: Optional[EmbeddingEngine] = None


def get_embedding_engine() -> EmbeddingEngine:
    """获取 EmbeddingEngine 全局单例"""
    global _engine
    if _engine is None:
        _engine = EmbeddingEngine()
    return _engine
