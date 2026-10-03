from __future__ import annotations
import os, threading
# Offline first: open_clip asks huggingface_hub for the weights and hub tries the
# network before the cache unless HF_HUB_OFFLINE is set. It must be set BEFORE
# open_clip (and so huggingface_hub) is imported, because hub copies the env var
# into a module constant at import time.
os.environ.setdefault("HF_HUB_OFFLINE", "1")
import numpy as np, torch, open_clip
from PIL import Image
from .config import CLIP_MODEL, CLIP_PRETRAINED, CLIP_TEMPLATE, CLIP_TOKENIZER_DIR, EMBED_BATCH, EMBED_DIM

_LOCK = threading.Lock()   # guards get_embedder() and Embedder._load(): the model loads once
# One tensor op at a time. PyTorch's MPS backend is not thread-safe: two threads compiling or running
# Metal kernels at once (the index thread embedding thumbs while a request thread encodes a search
# query) race on MetalShaderLibrary's kernel table and segfault the whole server (two crash reports
# on 2026-09-19, both with two threads inside exec_unary_kernel). Encoding is serialised, so a search
# during an index waits a batch, never crashes.
_RUN_LOCK = threading.Lock()

def _tokenizer():
    """The encoder's tokenizer, from the folder we ship when there is one.

    open_clip would fetch SigLIP's sentencepiece vocabulary from the hub, which needs the network even
    when the file is already cached (config.TOKENIZER_DIRS says why). Loading it from models/ keeps the
    text side as offline as the rest of the app. context_length and the text cleaning rule come from
    open_clip's own model config, so a vocabulary read off disk tokenises identically to one off the hub.
    """
    if CLIP_TOKENIZER_DIR is None:
        return open_clip.get_tokenizer(CLIP_MODEL)
    from open_clip.tokenizer import HFTokenizer
    cfg = open_clip.get_model_config(CLIP_MODEL)["text_cfg"]
    return HFTokenizer(str(CLIP_TOKENIZER_DIR), context_length=cfg["context_length"],
                       **cfg.get("tokenizer_kwargs", {}))


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
            tok = _tokenizer()
            # Every field at once, and the tokenizer first: _load returns early when self._model is set, so
            # a model that loaded beside a tokenizer that did not would leave the singleton half built and
            # every later encode_text would call None for the life of the process.
            self._pre = pre; self._tok = tok; self._model = m.eval().to(self.device)

    @torch.no_grad()
    def encode_images(self, ims: list[Image.Image]) -> np.ndarray:
        self._load()
        out = []
        for i in range(0, len(ims), EMBED_BATCH):
            x = torch.stack([self._pre(im.convert("RGB")) for im in ims[i:i + EMBED_BATCH]])
            with _RUN_LOCK:
                f = self._model.encode_image(x.to(self.device))
                out.append((f / f.norm(dim=-1, keepdim=True)).float().cpu().numpy())
        return np.concatenate(out) if out else np.zeros((0, EMBED_DIM), np.float32)

    @torch.no_grad()
    def encode_text(self, texts: list[str]) -> np.ndarray:
        self._load()
        texts = [t if t.lower().startswith("a photo") else CLIP_TEMPLATE.format(t) for t in texts]
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
