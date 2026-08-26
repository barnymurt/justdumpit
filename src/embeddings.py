from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import numpy as np


DEFAULT_MODEL_NAME = "all-MiniLM-L6-v2"

_cache: dict[str, "Embedder"] = {}


class Embedder:
    def __init__(self, model_name: str = DEFAULT_MODEL_NAME, cache_dir: Optional[Path] = None):
        self.model_name = model_name
        self.cache_dir = cache_dir
        self._model = None
        self.dim: Optional[int] = None

    def _load(self) -> None:
        if self._model is not None:
            return
        from sentence_transformers import SentenceTransformer
        kwargs = {}
        if self.cache_dir is not None:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            kwargs["cache_folder"] = str(self.cache_dir)
        self._model = SentenceTransformer(self.model_name, **kwargs)
        getter = getattr(self._model, "get_embedding_dimension", None) or getattr(
            self._model, "get_sentence_embedding_dimension", None
        )
        if getter is not None:
            self.dim = getter()

    def encode(self, texts: list[str], batch_size: int = 32) -> np.ndarray:
        self._load()
        vectors = self._model.encode(
            texts,
            batch_size=batch_size,
            show_progress_bar=False,
            convert_to_numpy=True,
            normalize_embeddings=True,
        )
        return vectors.astype(np.float32)

    def encode_one(self, text: str) -> np.ndarray:
        return self.encode([text])[0]


def get_embedder(model_name: str = DEFAULT_MODEL_NAME) -> Embedder:
    if model_name not in _cache:
        cache_dir = None
        env = os.getenv("SENTENCE_TRANSFORMERS_HOME")
        if env:
            cache_dir = Path(env)
        elif os.getenv("HF_HOME"):
            cache_dir = Path(os.getenv("HF_HOME"))
        _cache[model_name] = Embedder(model_name, cache_dir=cache_dir)
    return _cache[model_name]


def embed_texts(texts: list[str], model_name: str = DEFAULT_MODEL_NAME) -> np.ndarray:
    return get_embedder(model_name).encode(texts)


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    return a @ b.T


def encode_blob(arr: np.ndarray) -> bytes:
    return arr.astype(np.float32).tobytes()


def decode_blob(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)