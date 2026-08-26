from __future__ import annotations

from typing import Optional

import numpy as np

from src import db
from src.embeddings import get_embedder, decode_blob


def semantic_search(query: str, k: int = 10, video_id: Optional[str] = None) -> list[dict]:
    if not query.strip():
        return []

    embedder = get_embedder()
    query_vec = embedder.encode_one(query)

    with db.connect() as conn:
        if video_id:
            rows = conn.execute(
                "SELECT id, video_id, chunk_index, text, embedding, embedding_model FROM chunks WHERE video_id = ? AND embedding IS NOT NULL",
                (video_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, video_id, chunk_index, text, embedding, embedding_model FROM chunks WHERE embedding IS NOT NULL"
            ).fetchall()

    if not rows:
        return []

    vectors = []
    valid_rows = []
    for row in rows:
        if row['embedding'] is None:
            continue
        try:
            vec = decode_blob(row['embedding'])
            vectors.append(vec)
            valid_rows.append(row)
        except Exception:
            continue

    if not vectors:
        return []

    matrix = np.stack(vectors)
    scores = matrix @ query_vec

    top_indices = np.argsort(-scores)[:k]

    results = []
    for idx in top_indices:
        row = valid_rows[idx]
        results.append({
            'chunk_id': row['id'],
            'video_id': row['video_id'],
            'chunk_index': row['chunk_index'],
            'text': row['text'],
            'score': float(scores[idx]),
            'embedding_model': row['embedding_model'],
        })
    return results


def search_videos(query: str, k: int = 10) -> list[dict]:
    """Returns distinct videos ranked by max chunk score."""
    chunk_results = semantic_search(query, k=k * 5)

    by_video: dict[str, dict] = {}
    for r in chunk_results:
        vid = r['video_id']
        if vid not in by_video or r['score'] > by_video[vid]['score']:
            by_video[vid] = r

    ranked = sorted(by_video.values(), key=lambda r: -r['score'])[:k]
    return ranked


def chunk_count() -> int:
    with db.connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM chunks WHERE embedding IS NOT NULL").fetchone()
        return row['n']