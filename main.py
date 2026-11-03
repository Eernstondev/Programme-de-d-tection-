"""Surveillance caméras : détection personnes / véhicules / animaux, zones, clips et alertes Telegram.

    python main.py                    # lance la surveillance (config.yaml + .env)
    python main.py --show             # avec les vidéos annotées à l'écran (q pour quitter)
    python main.py --test-telegram    # envoie un message de test puis quitte
"""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app.config import ConfigError, Settings, load_config

log = logging.getLogger("surveillance")


def setup_logging(data_dir: Path) -> None:
    (data_dir / "logs").mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    file_handler = RotatingFileHandler(data_dir / "logs" / "surveillance.log", maxBytes=5_000_000,
                                       backupCount=3, encoding="utf-8")
    console = logging.StreamHandler()
    for h in (file_handler, console):
        h.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=[file_handler, console], force=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Surveillance caméras")
    parser.add_argument("--config", default="config.yaml", help="Fichier de configuration (défaut : config.yaml)")
    parser.add_argument("--env", default=None, help="Fichier de secrets (défaut : .env à côté de la config)")
    parser.add_argument("--show", action="store_true", help="Afficher les vidéos annotées (q pour quitter)")
    parser.add_argument("--test-telegram", action="store_true", help="Envoyer un message de test et quitter")
    args = parser.parse_args()

    try:
        settings = load_config(args.config, args.env)
    except ConfigError as e:
        print(f"Erreur de configuration : {e}", file=sys.stderr)
        return 2
    setup_logging(settings.data_dir)

    # Imports lourds (PyTorch) après la validation de la config, pour des erreurs rapides
    from app.cleanup import Janitor
    from app.notifier import TelegramNotifier
    from app.storage import EventStore

    notifier = None
    if settings.telegram.enabled:
        notifier = TelegramNotifier(settings.telegram.token, settings.telegram.chat_ids)
    if args.test_telegram:
        if not notifier:
            print("Telegram n'est pas activé dans config.yaml", file=sys.stderr)
            return 2
        ok = notifier.send_test()
        print("Message envoyé ✅" if ok else "Échec de l'envoi ❌ (voir les logs ci-dessus)")
        return 0 if ok else 1

    store = EventStore(settings.data_dir / "events.db")
    store.recover()
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())

    services: list[threading.Thread] = [Janitor(settings.data_dir, store, settings.retention_days,
                                                settings.min_free_gb, stop)]
    if settings.supabase.enabled:
        from app.cloud import SupabaseSync
        services.append(SupabaseSync(settings.supabase, store, settings.data_dir, stop))
    if notifier:
        notifier.start()
    for s in services:
        s.start()

    try:
        run_cameras(settings, store, notifier, stop, args.show)
    finally:
        stop.set()
        log.info("Arrêt en cours…")
        for s in services:
            s.join(timeout=10)
        if notifier:
            notifier.stop()
        store.close()
        log.info("Surveillance arrêtée")
    return 0


def run_cameras(settings: Settings, store, notifier, stop: threading.Event, show: bool) -> None:
    from app.camera import CameraWorker

    cameras = {c.name: c for c in settings.cameras}
    frames: dict = {}
    workers: dict[str, tuple[CameraWorker, float]] = {}  # caméra -> (thread, démarré à)
    failures: dict[str, int] = {}
    restart_at: dict[str, float] = {}

    def start(name: str) -> None:
        w = CameraWorker(cameras[name], settings, store, notifier, frames, stop)
        w.start()
        workers[name] = (w, time.monotonic())

    for name in cameras:
        start(name)
    names = ", ".join(cameras)
    log.info("Surveillance démarrée : %s", names)
    if notifier:
        notifier.text(f"✅ Surveillance démarrée ({len(cameras)} caméra(s) : {names})")

    while not stop.is_set():
        now = time.monotonic()
        # Superviseur : relance une caméra qui a planté, avec une attente croissante (5 s → 5 min)
        for name, (w, started) in list(workers.items()):
            if w.is_alive() or w.finished:
                continue
            if name not in restart_at:
                failures[name] = 0 if now - started > 600 else failures.get(name, 0) + 1
                delay = min(300, 5 * 2 ** (failures[name] - 1)) if failures[name] else 5
                log.error("[%s] arrêt anormal, relance dans %ds", name, delay)
                restart_at[name] = now + delay
            elif now >= restart_at[name]:
                del restart_at[name]
                start(name)

        if all(w.finished for w, _ in workers.values()):
            log.info("Toutes les sources sont terminées")
            break

        if show:  # l'affichage doit se faire dans le thread principal (obligatoire sur macOS)
            import cv2
            for name, frame in list(frames.items()):
                cv2.imshow(name, frame)
            if cv2.waitKey(30) & 0xFF == ord("q"):
                stop.set()
        else:
            stop.wait(1)

    stop.set()
    for w, _ in workers.values():
        w.join(timeout=15)
    if show:
        import cv2
        cv2.destroyAllWindows()
        cv2.waitKey(1)


if __name__ == "__main__":
    sys.exit(main())
