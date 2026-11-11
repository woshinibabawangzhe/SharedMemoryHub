"""Download the pinned model into the checkout models/Qwen3-Embedding-0.6B directory."""
from __future__ import annotations

import os
from pathlib import Path

HUB_ROOT = Path(__file__).resolve().parent.parent
MODEL_DIR = HUB_ROOT / "models" / "Qwen3-Embedding-0.6B"
CACHE_DIR = HUB_ROOT / "cache" / "hf"
REPO_ID = "Qwen/Qwen3-Embedding-0.6B"
REVISION = "97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3"


def main() -> None:
    import sys
    sys.path.insert(0,str(HUB_ROOT))
    from hub.paths import checked_path
    checked_path(MODEL_DIR);checked_path(CACHE_DIR)
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    os.environ["HF_HOME"] = str(CACHE_DIR)
    from huggingface_hub import snapshot_download

    # Try hf-mirror first if default huggingface.co is slow or blocked, else fallback to default
    for endpoint in ("https://hf-mirror.com", "https://huggingface.co"):
        try:
            print(f"Downloading {REPO_ID}@{REVISION[:12]} from {endpoint} -> {MODEL_DIR} ...")
            snapshot_download(
                repo_id=REPO_ID,
                revision=REVISION,
                local_dir=str(MODEL_DIR),
                cache_dir=str(CACHE_DIR),
                endpoint=endpoint,
                max_workers=4,
            )
            if (MODEL_DIR / "config.json").is_file() and (MODEL_DIR / "model.safetensors").is_file():
                print("Model download verified successfully!")
                return
        except Exception as e:
            print(f"Endpoint {endpoint} failed: {e}")

    raise RuntimeError("Failed to download Qwen/Qwen3-Embedding-0.6B from all endpoints.")


if __name__ == "__main__":
    main()
