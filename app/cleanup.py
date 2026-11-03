"""Nettoyage automatique : supprime les anciennes captures et libère de la place si le disque se remplit."""
from __future__ import annotations

import logging
import shutil
import threading
import time
from datetime import date
from pathlib import Path

from .storage import EventStore

log = logging.getLogger(__name__)
MEDIA_DIRS = ("clips", "snapshots")  # les clips sont supprimés en premier en cas de manque de place


class Janitor(threading.Thread):
    INTERVAL = 1800  # toutes les 30 min

    def __init__(self, data_dir: Path, store: EventStore, retention_days: int, min_free_gb: float,
                 stop_event: threading.Event):
        super().__init__(name="nettoyage", daemon=True)
        self.data_dir, self.store = data_dir, store
        self.retention_days, self.min_free_gb = retention_days, min_free_gb
        self.stop_event = stop_event

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.run_once()
            except Exception:
                log.exception("Erreur pendant le nettoyage")
            self.stop_event.wait(self.INTERVAL)

    def run_once(self) -> None:
        cutoff = time.time() - self.retention_days * 86400
        rows = self.store.expired(cutoff)
        for row in rows:
            for rel in (row["snapshot"], row["clip"]):
                if rel:
                    (self.data_dir / rel).unlink(missing_ok=True)
        if rows:
            self.store.delete([r["id"] for r in rows])
            log.info("Nettoyage : %d événements de plus de %d jours supprimés", len(rows), self.retention_days)

        for sub in MEDIA_DIRS:  # fichiers orphelins (ex. clip d'un événement déjà supprimé)
            root = self.data_dir / sub
            if root.is_dir():
                for f in root.rglob("*"):
                    if f.is_file() and f.stat().st_mtime < cutoff:
                        f.unlink(missing_ok=True)
                _remove_empty_dirs(root)

        self._free_space()

    def _free_space(self) -> None:
        need = self.min_free_gb * 1024 ** 3
        today = date.today().isoformat()
        for sub in MEDIA_DIRS:
            root = self.data_dir / sub
            days = sorted(d for d in root.iterdir() if d.is_dir() and d.name < today) if root.is_dir() else []
            for day in days:
                if shutil.disk_usage(self.data_dir).free >= need:
                    return
                shutil.rmtree(day, ignore_errors=True)
                log.warning("Disque presque plein : %s/%s supprimé", sub, day.name)


def _remove_empty_dirs(root: Path) -> None:
    for d in sorted((p for p in root.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        try:
            d.rmdir()  # ne supprime que si vide
        except OSError:
            pass
