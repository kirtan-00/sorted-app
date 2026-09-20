from __future__ import annotations
import os, threading
# Offline first: open_clip asks huggingface_hub for the weights and hub tries the
# network before the cache unless HF_HUB_OFFLINE is set. It must be set BEFORE
# open_clip (and so huggingface_hub) is imported, because hub copies the env var
# into a module constant at import time.
os.environ.setdefault("HF_HUB_OFFLINE", "1")
import numpy as np, torch, open_clip
from PIL import Image
from .config import CLIP_MODEL, CLIP_PRETRAINED, EMBED_BATCH

_LOCK = threading.Lock()   # guards get_embedder() and Embedder._load(): the model loads once
# One tensor op at a time. PyTorch's MPS backend is not thread-safe: two threads compiling or running
# Metal kernels at once (the index thread embedding thumbs while a request thread encodes a search
# query) race on MetalShaderLibrary's kernel table and segfault the whole server (two crash reports
# on 2026-09-19, both with two threads inside exec_unary_kernel). Encoding is serialised, so a search
# during an index waits a batch, never crashes.
_RUN_LOCK = threading.Lock()

class Embedder:
    def __init__(self, device: str | None = None):
        self.device = device or ("mps" if torch.backends.mps.is_available() else "cpu")
        self._model = self._pre = self._tok = None

    def _load(self):
        with _LOCK:
            if self._model is not None:
                return
            try:
                m, _, pre = open_clip.create_model_and_transforms(CLIP_MODEL, pretrained=CLIP_PRETRAINED)
            except Exception:
                # Not in the local cache. Flipping the env var now does nothing (hub read it
                # at import), so flip the module constant itself and try once online.
                import huggingface_hub.constants as C
                C.HF_HUB_OFFLINE = False
                os.environ["HF_HUB_OFFLINE"] = "0"
                m, _, pre = open_clip.create_model_and_transforms(CLIP_MODEL, pretrained=CLIP_PRETRAINED)
            self._model = m.eval().to(self.device); self._pre = pre
            self._tok = open_clip.get_tokenizer(CLIP_MODEL)

    @torch.no_grad()
    def encode_images(self, ims: list[Image.Image]) -> np.ndarray:
        self._load()
        out = []
        for i in range(0, len(ims), EMBED_BATCH):
            x = torch.stack([self._pre(im.convert("RGB")) for im in ims[i:i + EMBED_BATCH]])
            with _RUN_LOCK:
                f = self._model.encode_image(x.to(self.device))
                out.append((f / f.norm(dim=-1, keepdim=True)).float().cpu().numpy())
        return np.concatenate(out) if out else np.zeros((0, 512), np.float32)

    @torch.no_grad()
    def encode_text(self, texts: list[str]) -> np.ndarray:
        self._load()
        texts = [t if t.lower().startswith("a photo") else f"a photo of {t}" for t in texts]
        toks = self._tok(texts)
        with _RUN_LOCK:
            f = self._model.encode_text(toks.to(self.device))
            return (f / f.norm(dim=-1, keepdim=True)).float().cpu().numpy()

_E: Embedder | None = None
def get_embedder() -> Embedder:
    global _E
    with _LOCK:
        if _E is None:
            _E = Embedder()
    return _E
