"""Local Qwen3-Embedding-0.6B Engine (with timeout/wedge guard) & Qdrant Manager (v0.3.0)."""
from __future__ import annotations

import concurrent.futures
import hashlib
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
import numpy as np
import torch

from .db import DEFAULT_GENERATION, HUB_ROOT

MODEL_DIR = HUB_ROOT / "models" / "Qwen3-Embedding-0.6B"
PINNED_MODEL_ID = "Qwen/Qwen3-Embedding-0.6B"
PINNED_MODEL_REVISION = "97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3"
EMBEDDING_DIM = 1024
QUERY_INSTRUCTION = "Instruct: Given a search query, retrieve relevant personal preferences, project context, or verified experiences\nQuery: "


def memory_id_to_point_uuid(memory_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_OID, f"smh:{memory_id}"))


class EmbeddingEngine:
    """
    Wraps local Qwen/Qwen3-Embedding-0.6B (1024-dim) with:
    - Non-blocking _infer_lock so a wedged C++/PyTorch inference thread never causes thread pileup.
    - Caller deadline via ThreadPoolExecutor; a C++/CUDA thread cannot be forcibly killed.
    - Automatic embedding_engine_wedged state for graceful degradation to keyword-only search (§4.4).
    """

    def __init__(self, model_dir: Path = MODEL_DIR, timeout_sec: float = 60.0, device: str = "cuda"):
        if device not in ("cuda", "cpu"):
            raise ValueError("device must be cuda or explicit cpu")
        self.device = device
        self.model_dir = Path(model_dir)
        self.timeout_sec = float(timeout_sec)
        self._infer_lock = threading.Lock()
        self._load_lock = threading.Lock()
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="qwen3_embed")
        self._tokenizer: Any = None
        self._model: Any = None
        self._st_model: Any = None
        self.loaded: bool = False
        self.load_error: Optional[str] = None
        self.wedged: bool = False
        self.wedged_since: Optional[float] = None
        self.simulate_hang_seconds: float = 0.0

    def ensure_loaded(self) -> bool:
        if self.loaded:
            return True
        with self._load_lock:
            if self.loaded:
                return True
            if not self.model_dir.is_dir() or not (self.model_dir / "config.json").is_file():
                self.load_error = f"Model directory missing or incomplete: {self.model_dir}"
                return False
            try:
                os.environ["HF_HUB_OFFLINE"] = "1"
                os.environ["TRANSFORMERS_OFFLINE"] = "1"
                from transformers import AutoModel, AutoTokenizer

                if self.device == "cuda" and not torch.cuda.is_available():
                    raise RuntimeError("CUDA_UNAVAILABLE: set SMH_DEVICE=cpu explicitly for CPU mode")
                self._tokenizer = AutoTokenizer.from_pretrained(
                    str(self.model_dir),
                    local_files_only=True,
                    padding_side="left",
                    trust_remote_code=False,
                )
                self._model = AutoModel.from_pretrained(
                    str(self.model_dir),
                    local_files_only=True,
                    trust_remote_code=False,
                    dtype=torch.float16 if self.device == "cuda" else torch.float32,
                )
                self._model.to(self.device)
                self._model.eval()
                self.loaded = True
                self.load_error = None
                return True
            except Exception as e:
                self.load_error = f"{type(e).__name__}: {e}"
                return False

    @staticmethod
    def _last_token_pool(last_hidden_states: torch.Tensor, attention_mask: torch.Tensor) -> torch.Tensor:
        left_padding = attention_mask[:, -1].sum() == attention_mask.shape[0]
        if left_padding:
            return last_hidden_states[:, -1]
        sequence_lengths = attention_mask.sum(dim=1) - 1
        batch_size = last_hidden_states.shape[0]
        return last_hidden_states[torch.arange(batch_size, device=last_hidden_states.device), sequence_lengths]

    def _raw_encode_locked(self, texts: List[str], is_query: bool) -> List[List[float]]:
        if not self._infer_lock.acquire(blocking=False):
            raise TimeoutError("EMBEDDING_LOCK_BUSY")
        try:
            if self.simulate_hang_seconds > 0:
                hang = self.simulate_hang_seconds
                time.sleep(hang)

            if not self.ensure_loaded():
                raise RuntimeError(self.load_error or "Embedding model not loaded")

            formatted = [f"{QUERY_INSTRUCTION}{t}" if is_query else t for t in texts]
            with torch.no_grad():
                batch_dict = self._tokenizer(
                    formatted,
                    padding=True,
                    truncation=True,
                    max_length=2048,
                    return_tensors="pt",
                )
                batch_dict = {k:v.to(self.device) for k,v in batch_dict.items()}
                outputs = self._model(**batch_dict)
                embeddings = self._last_token_pool(outputs.last_hidden_state, batch_dict["attention_mask"])
                embeddings = torch.nn.functional.normalize(embeddings, p=2, dim=1)
                arr = embeddings.cpu().numpy().astype(np.float32)
                return [row.tolist() for row in arr]
        finally:
            self._infer_lock.release()

    def encode(
        self,
        texts: List[str],
        is_query: bool = False,
        timeout_sec: Optional[float] = None,
    ) -> List[List[float]]:
        if not texts:
            return []
        if self.wedged:
            raise TimeoutError("EMBEDDING_ENGINE_WEDGED")
        if self._infer_lock.locked():
            raise TimeoutError("EMBEDDING_LOCK_BUSY")

        wait_sec = float(timeout_sec if timeout_sec is not None else self.timeout_sec)
        fut = self._executor.submit(self._raw_encode_locked, texts, is_query)
        try:
            return fut.result(timeout=wait_sec)
        except concurrent.futures.TimeoutError:
            self.wedged = True
            self.wedged_since = time.time()
            raise TimeoutError(f"EMBEDDING_INFERENCE_TIMEOUT ({wait_sec}s)")

    def encode_one(self, text: str, is_query: bool = False, timeout_sec: Optional[float] = None) -> List[float]:
        res = self.encode([text], is_query=is_query, timeout_sec=timeout_sec)
        return res[0]

    def cosine_similarity(self, query_text: str, doc_text: str) -> Optional[float]:
        v1 = np.array(self.encode_one(query_text, is_query=True, timeout_sec=5.0), dtype=np.float32)
        v2 = np.array(self.encode_one(doc_text, is_query=False, timeout_sec=5.0), dtype=np.float32)
        return float(np.dot(v1, v2))

    def reset_wedged(self) -> None:
        self.simulate_hang_seconds = 0.0
        self.wedged = False
        self.wedged_since = None


class QdrantManager:
    """Docker REST backend only. Unreachable means keyword degradation, never local fallback."""
    def __init__(self, base_url="http://127.0.0.1:6333", timeout=3.0, collection_prefix="memories"):
        import re
        if not re.fullmatch(r"[a-zA-Z0-9_-]{3,80}", collection_prefix):
            raise ValueError("invalid_collection_prefix")
        self.base_url = base_url.rstrip("/")
        self.prefix = collection_prefix
        self.simulate_offline = False
        self._client = httpx.Client(base_url=self.base_url, timeout=timeout, trust_env=False)
        self._mutation_lock = threading.RLock()

    def close(self):
        self._client.close()

    def collection_name(self, generation=DEFAULT_GENERATION):
        import re
        # Accept the legacy generation spelling without permitting a foreign prefix.
        if generation.startswith("memories_"):
            generation = generation[len("memories_"):]
        elif generation.startswith(self.prefix + '_'):
            generation = generation[len(self.prefix) + 1:]
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}",generation):
            raise ValueError("invalid_generation")
        return f"{self.prefix}_{generation}"

    def list_generations(self):
        collections = self._request('GET', '/collections').json()['result']['collections']
        return [row['name'][len(self.prefix)+1:] for row in collections if row['name'].startswith(self.prefix+'_')]

    def _request(self, method, path, **kwargs):
        if self.simulate_offline:
            raise ConnectionError("qdrant_offline")
        r = self._client.request(method,path,**kwargs)
        r.raise_for_status()
        return r

    def is_healthy(self):
        try:
            return self._request("GET","/collections",timeout=0.5).status_code == 200
        except Exception:
            return False

    def ensure_collection(self, generation=DEFAULT_GENERATION, recreate=False):
        with self._mutation_lock:
            col = self.collection_name(generation)
            if recreate and not self.drop_collection(generation):
                raise RuntimeError("collection_drop_unknown")
            r = self._client.get(f"/collections/{col}") if not self.simulate_offline else None
            if r is not None and r.status_code == 200:
                return True
            if r is not None and r.status_code != 404:
                r.raise_for_status()
            self._request("PUT",f"/collections/{col}",json={"vectors":{"size":EMBEDDING_DIM,"distance":"Cosine"}})
            return True

    def drop_collection(self, generation_or_name):
        with self._mutation_lock:
            try:
                col = self.collection_name(generation_or_name)
                self._request("DELETE",f"/collections/{col}")
                return True
            except httpx.HTTPStatusError as e:
                return e.response.status_code == 404
            except Exception:
                return False

    def upsert_point(self,generation,memory_id,revision,vector,extra_payload=None):
        with self._mutation_lock:
            self.ensure_collection(generation)
            payload={"memory_id":memory_id,"revision":int(revision),"generation":generation}
            payload.update(extra_payload or {})
            self._request("PUT",f"/collections/{self.collection_name(generation)}/points?wait=true",
                json={"points":[{"id":memory_id_to_point_uuid(memory_id),"vector":vector,"payload":payload}]})
            return True

    def delete_point(self,generation,memory_id):
        with self._mutation_lock:
            try:
                col=self.collection_name(generation)
                r=self._client.get(f"/collections/{col}") if not self.simulate_offline else None
                if r is not None and r.status_code == 404:
                    return True
                self._request("POST",f"/collections/{col}/points/delete?wait=true",json={"points":[memory_id_to_point_uuid(memory_id)]})
                verify=self._request("POST",f"/collections/{col}/points",json={"ids":[memory_id_to_point_uuid(memory_id)],"with_payload":False,"with_vector":False})
                return verify.json().get("result") == []
            except Exception:
                return False

    def search_points(self,generation,query_vector,top_k=24):
        r=self._request("POST",f"/collections/{self.collection_name(generation)}/points/search",
            json={"vector":query_vector,"limit":int(top_k),"with_payload":True,"with_vector":False})
        return [{"point_id":p["id"],"score":p["score"],**(p.get("payload") or {})} for p in r.json().get("result",[])]

    def scroll_all_payloads(self,generation):
        out=[];offset=None
        while True:
            body={"limit":250,"with_payload":True,"with_vector":False}
            if offset is not None:
                body["offset"]=offset
            res=self._request("POST",f"/collections/{self.collection_name(generation)}/points/scroll",json=body).json()["result"]
            out.extend(p.get("payload") or {} for p in res["points"])
            offset=res.get("next_page_offset")
            if offset is None:
                return out

    def build_and_verify_generation(self,target_gen,active_rows,embedder):
        with self._mutation_lock:
            self.ensure_collection(target_gen,recreate=True)
            # Small batches bound VRAM on the 6GB device.
            for row in active_rows:
                vec=embedder.encode_one(f"{row['title']}\n{row['content']}",timeout_sec=60)
                self.upsert_point(target_gen,row["memory_id"],row["revision"],vec)
            payloads=self.scroll_all_payloads(target_gen)
            lines=sorted(f"{p['memory_id']}:{p['revision']}:{p['generation']}" for p in payloads)
            return len(payloads),hashlib.sha256("\n".join(lines).encode()).hexdigest()
