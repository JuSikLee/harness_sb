"""벡터 임베딩 + 코사인 유사도 기반 의미 검색 → Claude rerank."""
import json
import os
from pathlib import Path

import anthropic
import numpy as np
from sklearn.metrics.pairwise import cosine_similarity

import loader

CACHE_DIR = Path("/home/prime/harness_sb/doc-search/.cache/vectors")
MINILM = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
VOYAGE = "voyage-multilingual-2"
TOP_K = 12
FINAL_MIN, FINAL_MAX = 3, 5

_model = None


def backend():
    return os.environ.get("EMBEDDING_BACKEND", "minilm").lower()


def threshold():
    env = os.environ.get("SIMILARITY_THRESHOLD")
    if env:
        return float(env)
    return 0.4 if backend() == "voyage" else 0.25


def embed(texts, input_type="document"):
    global _model
    if backend() == "voyage":
        import voyageai
        res = voyageai.Client().embed(texts, model=VOYAGE, input_type=input_type)
        return np.asarray(res.embeddings, dtype=np.float32)
    if _model is None:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer(MINILM)
    return np.asarray(_model.encode(texts, show_progress_bar=False), dtype=np.float32)


def build_index():
    """파일 SHA256(코퍼스 해시)이 같으면 캐시 재사용, 다르면 재임베딩."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    name = backend()
    npy, meta_path = CACHE_DIR / f"{name}.npy", CACHE_DIR / f"{name}.json"
    digest = loader.corpus_hash()
    if npy.exists() and meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if meta.get("hash") == digest:
            return np.load(npy), meta["sections"]
    sections = loader.load_all()
    vectors = embed([s["text"] for s in sections])
    np.save(npy, vectors)
    meta_path.write_text(json.dumps({"hash": digest, "sections": sections}, ensure_ascii=False), encoding="utf-8")
    return vectors, sections


def rerank(question, candidates):
    """Claude로 후보 12개 중 최종 3~5개를 고른다. 실패 시 코사인 순서 유지."""
    listing = "\n".join(f"[{i}] {c['text'][:400]}" for i, c in enumerate(candidates))
    prompt = (
        f"질문: {question}\n\n후보 섹션:\n{listing}\n\n"
        f"질문에 답하는 데 가장 관련 있는 후보 번호를 관련도 순으로 {FINAL_MIN}~{FINAL_MAX}개 골라 "
        "JSON 정수 배열만 출력하세요. 예: [2, 0, 5]"
    )
    try:
        client = anthropic.Anthropic(timeout=30)
        msg = client.messages.create(
            model=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
            max_tokens=100,
            messages=[{"role": "user", "content": prompt}],
        )
        order = json.loads(msg.content[0].text.strip())
        picked = []
        for i in order:
            if isinstance(i, int) and 0 <= i < len(candidates) and i not in picked:
                picked.append(i)
        if len(picked) >= FINAL_MIN:
            return [candidates[i] for i in picked[:FINAL_MAX]]
    except Exception:
        pass
    return candidates[:FINAL_MAX]


def search(question):
    vectors, sections = build_index()
    q = embed([question], input_type="query")
    scores = cosine_similarity(q, vectors)[0]
    order = np.argsort(-scores)[:TOP_K]
    candidates = [dict(sections[i], score=float(scores[i])) for i in order]
    thr = threshold()
    if not candidates or candidates[0]["score"] < thr:
        return {"status": "no_info", "threshold": thr, "top_score": candidates[0]["score"] if candidates else 0.0, "hits": []}
    candidates = [c for c in candidates if c["score"] >= thr]
    return {"status": "ok", "threshold": thr, "top_score": candidates[0]["score"], "hits": rerank(question, candidates)}
