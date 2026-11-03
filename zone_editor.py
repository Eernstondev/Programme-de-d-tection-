"""Dessine les zones de détection à la souris sur une image de la caméra et affiche le YAML à coller.

    python zone_editor.py --camera cour           # caméra déclarée dans config.yaml
    python zone_editor.py --source video.mp4      # ou n'importe quelle source

Clic gauche : ajouter un point   |  clic droit ou u : annuler le dernier point
n : zone suivante  |  c : tout effacer  |  Entrée ou s : terminer  |  q / Échap : quitter sans rien
"""
from __future__ import annotations

import argparse
import os
import sys
import time

os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from app.config import ConfigError, load_config  # noqa: E402

WINDOW = "Zones - clic gauche: point | n: zone suivante | u: annuler | Entree: terminer"
COLORS = [(40, 40, 230), (0, 200, 0), (255, 140, 0), (200, 0, 200), (0, 200, 200)]


def grab_frame(source) -> np.ndarray:
    cap = cv2.VideoCapture(source)
    try:
        deadline = time.monotonic() + 15
        frame = None
        while time.monotonic() < deadline:  # les premières images RTSP sont souvent grises
            ok, f = cap.read()
            if ok:
                frame = f
                if isinstance(source, str) and os.path.isfile(source):
                    break
                if f.std() > 5:
                    break
        if frame is None:
            raise SystemExit("Impossible de lire une image de la caméra (vérifie la source et le réseau)")
        return frame
    finally:
        cap.release()


def main() -> None:
    parser = argparse.ArgumentParser(description="Éditeur de zones")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--camera", help="Nom de la caméra dans config.yaml")
    group.add_argument("--source", help="URL RTSP, index webcam ou fichier vidéo")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    if args.camera:
        try:
            settings = load_config(args.config)
        except ConfigError as e:
            sys.exit(f"Erreur de configuration : {e}")
        cam = next((c for c in settings.cameras if c.name == args.camera), None)
        if cam is None:
            sys.exit(f"Caméra « {args.camera} » absente de {args.config}")
        source = cam.source
    else:
        source = int(args.source) if args.source.isdigit() else args.source

    frame = grab_frame(source)
    h, w = frame.shape[:2]
    scale = min(1.0, 1280 / w)
    base = cv2.resize(frame, (int(w * scale), int(h * scale))) if scale < 1 else frame
    dh, dw = base.shape[:2]
    zones: list[list[tuple[float, float]]] = [[]]

    def on_mouse(event, x, y, *_):
        if event == cv2.EVENT_LBUTTONDOWN:
            zones[-1].append((round(x / dw, 4), round(y / dh, 4)))
        elif event == cv2.EVENT_RBUTTONDOWN and zones[-1]:
            zones[-1].pop()

    cv2.namedWindow(WINDOW)
    cv2.setMouseCallback(WINDOW, on_mouse)
    done = False
    while True:
        img = base.copy()
        for i, z in enumerate(zones):
            color = COLORS[i % len(COLORS)]
            pts = np.array([(int(x * dw), int(y * dh)) for x, y in z], dtype=np.int32)
            for p in pts:
                cv2.circle(img, tuple(int(v) for v in p), 4, color, -1)
            if len(pts) >= 2:
                cv2.polylines(img, [pts], len(pts) >= 3, color, 2)
            if len(pts):
                cv2.putText(img, f"zone{i + 1}", tuple(int(v) for v in pts[0]), cv2.FONT_HERSHEY_SIMPLEX,
                            0.6, color, 2)
        cv2.imshow(WINDOW, img)
        key = cv2.waitKey(30) & 0xFF
        if key in (13, 10, ord("s")):
            done = True
            break
        if key in (27, ord("q")):
            break
        if key == ord("n") and len(zones[-1]) >= 3:
            zones.append([])
        elif key == ord("u") and zones[-1]:
            zones[-1].pop()
        elif key == ord("c"):
            zones[:] = [[]]
    cv2.destroyAllWindows()
    cv2.waitKey(1)

    valid = [z for z in zones if len(z) >= 3]
    if not done or not valid:
        print("Aucune zone enregistrée.")
        return
    print("\n# À coller dans config.yaml, sous la caméra concernée (renomme les zones) :")
    print("    zones:")
    for i, z in enumerate(valid, 1):
        print(f"      - name: zone{i}")
        print("        polygon: [" + ", ".join(f"[{x}, {y}]" for x, y in z) + "]")
        print("        categories: [personne, vehicule, animal]")


if __name__ == "__main__":
    main()
