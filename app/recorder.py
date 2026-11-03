"""Enregistrement de clips : garde les N dernières secondes en mémoire et écrit avant + après l'événement."""
from __future__ import annotations

import logging
import shutil
import subprocess
import threading
from collections import deque
from collections.abc import Callable
from pathlib import Path

import cv2
import numpy as np

from .config import ClipSettings

log = logging.getLogger(__name__)
FFMPEG = shutil.which("ffmpeg")


class ClipRecorder:
    def __init__(self, cfg: ClipSettings, default_fps: float,
                 path_factory: Callable[[], Path],
                 on_done: Callable[[Path | None, list[str]], None]):
        self.cfg = cfg
        self.default_fps = default_fps
        self.path_factory = path_factory
        self.on_done = on_done
        self.buffer: deque[tuple[float, np.ndarray]] = deque()
        self.codecs = ["avc1", "mp4v"]  # H.264 si disponible (lisible partout), sinon MPEG-4
        self.writer: cv2.VideoWriter | None = None
        self.path: Path | None = None
        self.codec = ""
        self.size: tuple[int, int] | None = None
        self.started_at = 0.0
        self.stop_at = 0.0
        self.event_ids: list[str] = []

    def trigger(self, ts: float, event_id: str) -> None:
        if self.writer is None and not self.event_ids:
            self.started_at = ts
            self.stop_at = ts + self.cfg.post_seconds
        else:  # événement pendant un enregistrement : on prolonge, sans dépasser max_seconds
            self.stop_at = min(max(self.stop_at, ts + self.cfg.post_seconds),
                               self.started_at + self.cfg.max_seconds)
        self.event_ids.append(event_id)

    def push(self, frame: np.ndarray, ts: float) -> None:
        frame = self._fit(frame)
        if self.writer is None:
            self.buffer.append((ts, frame))
            while ts - self.buffer[0][0] > self.cfg.pre_seconds:
                self.buffer.popleft()
            if not self.event_ids:
                return
            if not self._open():
                ids, self.event_ids = self.event_ids, []
                self.on_done(None, ids)
                return
        else:
            self._write(frame)
        if ts >= self.stop_at:
            self._finish(background=True)

    def close(self) -> None:
        """À appeler à l'arrêt : termine le clip en cours."""
        if self.writer is not None:
            self._finish(background=False)
        elif self.event_ids:
            ids, self.event_ids = self.event_ids, []
            self.on_done(None, ids)

    def _fit(self, frame: np.ndarray) -> np.ndarray:
        h, w = frame.shape[:2]
        if self.size is not None:
            return frame if (w, h) == self.size else cv2.resize(frame, self.size)
        tw = min(w, self.cfg.max_width)
        th = round(h * tw / w)
        tw, th = tw - tw % 2, th - th % 2  # H.264 exige des dimensions paires
        return frame if (tw, th) == (w, h) else cv2.resize(frame, (tw, th))

    def _write(self, frame: np.ndarray) -> None:
        if frame.shape[1::-1] != self.size:
            frame = cv2.resize(frame, self.size)
        self.writer.write(frame)

    def _estimate_fps(self) -> float:
        if len(self.buffer) >= 2:
            span = self.buffer[-1][0] - self.buffer[0][0]
            if span > 0:
                return min(30.0, max(1.0, (len(self.buffer) - 1) / span))
        return self.default_fps

    def _open(self) -> bool:
        h, w = self.buffer[-1][1].shape[:2]
        self.size = (w, h)
        fps = self._estimate_fps()
        self.path = self.path_factory()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for codec in self.codecs:
            writer = cv2.VideoWriter(str(self.path), cv2.VideoWriter_fourcc(*codec), fps, self.size)
            if writer.isOpened():
                self.writer, self.codec = writer, codec
                self.codecs = [codec]  # on retient le codec qui marche sur cette machine
                break
            writer.release()
        else:
            log.error("Impossible de créer le clip vidéo %s", self.path.name)
            self.size = None
            return False
        for _, f in self.buffer:
            self._write(f)
        self.buffer.clear()
        return True

    def _finish(self, background: bool) -> None:
        writer, path, codec, ids = self.writer, self.path, self.codec, self.event_ids
        self.writer, self.path, self.size, self.event_ids = None, None, None, []
        writer.release()
        if background:
            threading.Thread(target=self._finalize, args=(path, codec, ids), daemon=True).start()
        else:
            self._finalize(path, codec, ids)

    def _finalize(self, path: Path, codec: str, ids: list[str]) -> None:
        try:
            if codec != "avc1":
                _to_h264(path)
            ok = path.is_file() and path.stat().st_size > 0
            self.on_done(path if ok else None, ids)
        except Exception:
            log.exception("Erreur lors de la finalisation du clip %s", path.name)


def _to_h264(path: Path) -> None:
    """Convertit en H.264 (lecture directe sur téléphone/Telegram) si ffmpeg est installé."""
    if not FFMPEG:
        return
    tmp = path.with_name(path.stem + ".tmp.mp4")
    cmd = [FFMPEG, "-y", "-loglevel", "error", "-i", str(path), "-c:v", "libx264", "-preset", "veryfast",
           "-crf", "26", "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-an", str(tmp)]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=180)
    except subprocess.TimeoutExpired:
        result = None
    if result is not None and result.returncode == 0:
        tmp.replace(path)
    else:
        tmp.unlink(missing_ok=True)
        log.warning("Conversion H.264 échouée pour %s, clip conservé en MPEG-4", path.name)
