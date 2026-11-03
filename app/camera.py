"""Un thread par caméra : lecture du flux, détection YOLO, zones, captures, clips et alertes."""
from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

# RTSP en TCP : évite les images grises/déchirées dues aux pertes de paquets UDP
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")

import cv2  # noqa: E402
import numpy as np  # noqa: E402
from ultralytics import YOLO  # noqa: E402

from .config import Camera, Settings  # noqa: E402
from .notifier import TelegramNotifier  # noqa: E402
from .recorder import ClipRecorder  # noqa: E402
from .storage import Event, EventStore  # noqa: E402
from .zones import Detection, Trigger, ZoneMonitor  # noqa: E402

log = logging.getLogger(__name__)

COCO_TO_CATEGORY = {
    0: "personne",
    1: "vehicule", 2: "vehicule", 3: "vehicule", 5: "vehicule", 7: "vehicule",  # vélo, voiture, moto, bus, camion
    **{i: "animal" for i in range(14, 24)},  # oiseau, chat, chien, cheval, mouton, vache…
}
CATEGORY_LABEL = {"personne": "Personne", "vehicule": "Véhicule", "animal": "Animal"}
CATEGORY_ICON = {"personne": "🚶", "vehicule": "🚗", "animal": "🐾"}
COLORS = {"personne": (0, 200, 0), "vehicule": (255, 140, 0), "animal": (0, 140, 255)}  # BGR
OFFLINE_AFTER = 60  # secondes sans image avant l'alerte "caméra hors ligne"


class Stream:
    """Lit le flux dans un thread et ne garde que la dernière image (évite la latence RTSP)."""

    def __init__(self, source: str | int, name: str):
        self.name = name
        self.source = source
        self.is_file = isinstance(source, str) and Path(source).expanduser().is_file()
        if self.is_file:
            self.source = str(Path(source).expanduser())
        self.frame: np.ndarray | None = None
        self.last_frame_at = time.monotonic()
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        self.cap = self._open()
        if self.is_file:
            if not self.cap.isOpened():
                raise RuntimeError(f"[{name}] impossible d'ouvrir la vidéo")
        else:
            threading.Thread(target=self._reader, name=f"flux-{name}", daemon=True).start()

    def _open(self) -> cv2.VideoCapture:
        if isinstance(self.source, str) and "://" in self.source:
            cap = cv2.VideoCapture(self.source, cv2.CAP_FFMPEG,
                                   [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 10000, cv2.CAP_PROP_READ_TIMEOUT_MSEC, 10000])
        else:
            cap = cv2.VideoCapture(self.source)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        return cap

    def _reader(self) -> None:
        lost = False
        while not self._stopped.is_set():
            ok, frame = self.cap.read() if self.cap.isOpened() else (False, None)
            if not ok:
                if not lost:  # l'URL n'est jamais loguée : elle contient le mot de passe
                    log.warning("[%s] flux perdu, reconnexion…", self.name)
                    lost = True
                self.cap.release()
                if self._stopped.wait(3):
                    break
                self.cap = self._open()
                continue
            if lost:
                log.info("[%s] flux rétabli", self.name)
                lost = False
            with self._lock:
                self.frame = frame
                self.last_frame_at = time.monotonic()
        self.cap.release()

    def read(self) -> tuple[bool, np.ndarray | None]:
        """(actif, image) — image=None si aucune nouvelle image ; actif=False en fin de fichier vidéo."""
        if self.is_file:
            ok, frame = self.cap.read()
            return ok, frame if ok else None
        with self._lock:
            frame, self.frame = self.frame, None
        return True, frame

    def seconds_without_frame(self) -> float:
        return time.monotonic() - self.last_frame_at

    def stop(self) -> None:
        self._stopped.set()
        if self.is_file:
            self.cap.release()


class CameraWorker(threading.Thread):
    def __init__(self, cam: Camera, settings: Settings, store: EventStore,
                 notifier: TelegramNotifier | None, frames: dict, stop_event: threading.Event):
        super().__init__(name=f"cam-{cam.name}", daemon=True)
        self.cam, self.s, self.store, self.notifier = cam, settings, store, notifier
        self.frames, self.stop_event = frames, stop_event
        self.finished = False  # True = arrêt normal (fin de vidéo ou arrêt demandé), sinon le superviseur relance

    def run(self) -> None:
        name = self.cam.name
        model = YOLO(self.s.model)  # un modèle par caméra : le suivi des objets reste indépendant
        stream = Stream(self.cam.source, name)
        recorder = self._make_recorder()
        plates = self._make_plate_reader()
        monitor = ZoneMonitor(self.cam.zones)
        interval = 1 / self.cam.fps
        offline = False
        log.info("[%s] caméra démarrée (%d zone(s))", name, len(self.cam.zones))
        try:
            while not self.stop_event.is_set():
                t0 = time.monotonic()
                ok, frame = stream.read()
                if not ok:
                    log.info("[%s] fin de la vidéo", name)
                    break
                if frame is None:
                    if not stream.is_file and not offline and stream.seconds_without_frame() > OFFLINE_AFTER:
                        offline = True
                        log.warning("[%s] caméra hors ligne", name)
                        self._notify_text(f"⚠️ Caméra « {name} » hors ligne")
                    self.stop_event.wait(0.02)
                    continue
                if offline:
                    offline = False
                    self._notify_text(f"✅ Caméra « {name} » de nouveau en ligne")

                now = datetime.now().astimezone()
                result = model.track(frame, persist=True, conf=self.s.conf, imgsz=self.s.imgsz,
                                     classes=list(COCO_TO_CATEGORY), device=self.s.device,
                                     tracker="bytetrack.yaml", verbose=False)[0]
                detections = _parse(result, frame.shape)
                triggers = monitor.update(detections, now, time.monotonic())
                annotated = _draw(frame, detections, self.cam, monitor.active_zones(now), now)

                for trigger in triggers:
                    self._emit(trigger, frame, annotated, now, recorder, plates)
                if recorder:
                    recorder.push(annotated, time.monotonic())
                self.frames[name] = annotated

                if not stream.is_file:
                    self.stop_event.wait(max(0.0, interval - (time.monotonic() - t0)))
            self.finished = True
        finally:
            if recorder:
                recorder.close()
            stream.stop()

    def _emit(self, trig: Trigger, frame: np.ndarray, annotated: np.ndarray, now: datetime,
              recorder: ClipRecorder | None, plates) -> None:
        cam = self.cam.name
        event_id = str(uuid.uuid4())
        labels = sorted({d.label for d in trig.detections})

        plate = None
        if plates and trig.category == "vehicule":
            for d in trig.detections:
                try:
                    plate = plates.read(frame, d.box)
                except Exception:
                    log.exception("[%s] erreur de lecture de plaque", cam)
                if plate:
                    break

        rel = Path("snapshots", f"{now:%Y-%m-%d}", f"{cam}_{now:%H%M%S}_{trig.category}_{event_id[:8]}.jpg")
        path = self.s.data_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        saved = cv2.imwrite(str(path), annotated, [cv2.IMWRITE_JPEG_QUALITY, 85])
        if not saved:
            log.error("[%s] impossible d'enregistrer la capture", cam)

        self.store.add(Event(id=event_id, ts=now, camera=cam, zone=trig.zone, category=trig.category,
                             labels=labels, count=len(trig.detections), plate=plate,
                             snapshot=rel.as_posix() if saved else None, clip_done=recorder is None))
        log.info("[%s] ALERTE %s dans « %s » : %s%s", cam, trig.category, trig.zone,
                 ", ".join(labels), f" (plaque {plate})" if plate else "")

        if self.notifier:
            caption = (f"{CATEGORY_ICON[trig.category]} {CATEGORY_LABEL[trig.category]} détecté\n"
                       f"📍 {cam} › {trig.zone}\n"
                       f"🔎 {', '.join(labels)} ({len(trig.detections)})\n"
                       + (f"🔢 Plaque : {plate}\n" if plate else "")
                       + f"🕒 {now:%d/%m/%Y %H:%M:%S}")
            if saved:
                self.notifier.photo(event_id, path, caption)
            else:
                self.notifier.text(caption)
        if recorder:
            recorder.trigger(time.monotonic(), event_id)

    def _on_clip(self, path: Path | None, event_ids: list[str]) -> None:
        self.store.set_clip(event_ids, path.relative_to(self.s.data_dir).as_posix() if path else None)
        if path and self.notifier:
            self.notifier.video(event_ids, path, f"🎥 Clip — {self.cam.name}")

    def _make_recorder(self) -> ClipRecorder | None:
        if not self.s.clips.enabled:
            return None

        def path_factory() -> Path:
            now = datetime.now()
            return self.s.data_dir / "clips" / f"{now:%Y-%m-%d}" / f"{self.cam.name}_{now:%H%M%S}.mp4"

        return ClipRecorder(self.s.clips, self.cam.fps, path_factory, self._on_clip)

    def _make_plate_reader(self):
        if not self.s.plates_enabled:
            return None
        try:
            from .plates import PlateReader
            return PlateReader(self.s.plates_min_conf)
        except ImportError:
            log.error('Lecture de plaques désactivée : installe-la avec  pip install "fast-alpr[onnx]"')
        except Exception:
            log.exception("Lecture de plaques désactivée (erreur de chargement)")
        return None

    def _notify_text(self, message: str) -> None:
        if self.notifier:
            self.notifier.text(message)


def _parse(result, shape) -> list[Detection]:
    boxes = result.boxes
    if boxes is None or len(boxes) == 0:
        return []
    h, w = shape[:2]
    ids = boxes.id.int().tolist() if boxes.id is not None else [None] * len(boxes)
    out = []
    for (x1, y1, x2, y2), cls, conf, tid in zip(boxes.xyxy.int().tolist(), boxes.cls.int().tolist(),
                                                boxes.conf.tolist(), ids):
        category = COCO_TO_CATEGORY.get(cls)
        if category:
            out.append(Detection(tid, category, result.names[cls], conf, (x1, y1, x2, y2),
                                 ((x1 + x2) / 2 / w, y2 / h)))
    return out


def _draw(frame: np.ndarray, detections: list[Detection], cam: Camera, active: set[str],
          now: datetime) -> np.ndarray:
    img = frame.copy()
    h, w = img.shape[:2]
    polys = [(z, (z.polygon * [w, h]).astype(np.int32)) for z in cam.zones if z.polygon is not None]

    if polys:  # zones en transparence (rouge = active, gris = hors horaire)
        overlay = img.copy()
        for z, pts in polys:
            cv2.fillPoly(overlay, [pts], (40, 40, 200) if z.name in active else (120, 120, 120))
        cv2.addWeighted(overlay, 0.15, img, 0.85, 0, dst=img)
        for z, pts in polys:
            color = (40, 40, 230) if z.name in active else (150, 150, 150)
            cv2.polylines(img, [pts], True, color, 2)
            cv2.putText(img, z.name, (int(pts[0][0]) + 4, int(pts[0][1]) + 18),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 2)

    counts = {"personne": 0, "vehicule": 0, "animal": 0}
    for d in detections:
        counts[d.category] += 1
        x1, y1, x2, y2 = d.box
        color = COLORS[d.category]
        cv2.rectangle(img, (x1, y1), (x2, y2), color, 3 if d.in_zone else 1)
        text = f"{d.label} {d.conf:.0%}" + (f" #{d.track_id}" if d.track_id is not None else "")
        cv2.putText(img, text, (x1, max(y1 - 6, 14)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2)

    header = (f"{cam.name}  {now:%d/%m/%Y %H:%M:%S}  |  Personnes {counts['personne']}  "
              f"Vehicules {counts['vehicule']}  Animaux {counts['animal']}")
    cv2.rectangle(img, (0, 0), (w, 30), (0, 0, 0), -1)
    cv2.putText(img, header, (8, 21), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1, cv2.LINE_AA)
    return img
