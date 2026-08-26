from typing import Optional
from src.config import MAX_CHUNK_SIZE


def chunk_transcript(text: str, max_size: int = MAX_CHUNK_SIZE) -> list[str]:
    if len(text) <= max_size:
        return [text]
    
    chunks = []
    current_chunk = []
    current_size = 0
    
    lines = text.split('\n')
    
    for line in lines:
        line_size = len(line)
        
        if current_size + line_size > max_size and current_chunk:
            chunks.append('\n'.join(current_chunk))
            current_chunk = []
            current_size = 0
        
        current_chunk.append(line)
        current_size += line_size + 1
    
    if current_chunk:
        chunks.append('\n'.join(current_chunk))
    
    return chunks


def chunk_transcript_preserve_context(
    text: str,
    max_size: int = MAX_CHUNK_SIZE,
    overlap: int = 500
) -> list[dict]:
    if len(text) <= max_size:
        return [{"text": text, "chunk_index": 0, "total_chunks": 1}]
    
    chunks = []
    lines = text.split('\n')
    current_chunk = []
    current_size = 0
    chunk_index = 0
    
    for line in lines:
        line_size = len(line)
        
        if current_size + line_size > max_size and current_chunk:
            chunk_text = '\n'.join(current_chunk)
            chunks.append({
                "text": chunk_text,
                "chunk_index": chunk_index,
                "total_chunks": chunk_index + 1
            })
            
            overlap_lines = current_chunk[-3:] if len(current_chunk) >= 3 else current_chunk
            current_chunk = overlap_lines.copy()
            current_size = sum(len(l) + 1 for l in current_chunk)
            chunk_index += 1
        
        current_chunk.append(line)
        current_size += line_size + 1
    
    if current_chunk:
        chunks.append({
            "text": '\n'.join(current_chunk),
            "chunk_index": chunk_index,
            "total_chunks": chunk_index + 1
        })
    
    return chunks
