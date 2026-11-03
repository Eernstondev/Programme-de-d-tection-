"""Lecture de plaques d'immatriculation (optionnel : pip install "fast-alpr[onnx]")."""
from __future__ import annotations

import logging
import re

import numpy as np

log = logging.getLogger(__name__)
_CLEAN = re.compile(r"[^A-Z0-9]")


class PlateReader:
    def __init__(self, min_confidence: float):
        from fast_alpr import ALPR  # import ici : dépendance optionnelle

        self.min_confidence = min_confidence
        self.alpr = ALPR(
            detector_model="yolo-v9-t-384-license-plate-end2end",
            ocr_model="cct-xs-v2-global-model",
        )

    def read(self, frame: np.ndarray, box: tuple[int, int, int, int]) -> str | None:
        """Cherche une plaque dans la zone du véhicule et retourne le texte le plus fiable."""
        h, w = frame.shape[:2]
        x1, y1, x2, y2 = box
        mx, my = int((x2 - x1) * 0.05), int((y2 - y1) * 0.05)
        crop = frame[max(0, y1 - my):min(h, y2 + my), max(0, x1 - mx):min(w, x2 + mx)]
        if crop.size == 0:
            return None

        best, best_conf = None, 0.0
        for result in self.alpr.predict(crop):
            if result.ocr is None:
                continue
            conf = result.ocr.confidence
            conf = float(np.mean(conf)) if isinstance(conf, list) else float(conf)
            text = _CLEAN.sub("", result.ocr.text.upper())
            if len(text) >= 4 and conf >= self.min_confidence and conf > best_conf:
                best, best_conf = text, conf
        return best
