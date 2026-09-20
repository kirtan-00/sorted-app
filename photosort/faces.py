from __future__ import annotations
from dataclasses import dataclass
import cv2, numpy as np
cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_ERROR)   # silence "Targets are not supported by the new graph engine"
from PIL import Image
from .config import YUNET_PATH, SFACE_PATH, FACE_SCORE_MIN
from .features import to_gray, eye_sharpness

@dataclass(eq=False)   # ndarray fields make the generated __eq__ raise
class Face:
    x: int; y: int; w: int; h: int
    score: float
    landmarks: np.ndarray
    embed: np.ndarray
    eye_sharp: float

class FaceEngine:
    def __init__(self, yunet=YUNET_PATH, sface=SFACE_PATH, score_min=FACE_SCORE_MIN):
        if not yunet.exists() or not sface.exists():
            raise FileNotFoundError("face models missing; run scripts/fetch_models.sh")
        self.det = cv2.FaceDetectorYN.create(str(yunet), "", (320, 320), score_min, 0.3, 5000)
        self.rec = cv2.FaceRecognizerSF.create(str(sface), "")

    def detect(self, im: Image.Image) -> list[Face]:
        bgr = cv2.cvtColor(np.asarray(im.convert("RGB")), cv2.COLOR_RGB2BGR)
        h, w = bgr.shape[:2]
        self.det.setInputSize((w, h))
        _, dets = self.det.detect(bgr)
        if dets is None or len(dets) == 0:
            return []
        gray = to_gray(im)
        out: list[Face] = []
        for d in dets:
            x, y, bw, bh = [int(v) for v in d[:4]]
            lm = d[4:14].reshape(5, 2).astype(float)
            crop = self.rec.alignCrop(bgr, d)
            feat = self.rec.feature(crop).flatten().astype(np.float32)
            feat /= (np.linalg.norm(feat) + 1e-9)
            out.append(Face(x, y, bw, bh, float(d[14]), lm, feat, eye_sharpness(gray, lm)))
        return out
