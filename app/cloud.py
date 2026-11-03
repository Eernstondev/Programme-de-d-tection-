"""Synchronisation des événements vers Supabase (table events + bucket privé) pour le tableau de bord."""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from urllib.parse import quote

import requests

from .config import SupabaseSettings
from .storage import EventStore

log = logging.getLogger(__name__)


class SyncError(Exception):
    pass


class SupabaseSync(threading.Thread):
    INTERVAL = 15  # secondes entre deux synchronisations

    def __init__(self, cfg: SupabaseSettings, store: EventStore, data_dir: Path, stop_event: threading.Event):
        super().__init__(name="supabase", daemon=True)
        self.cfg, self.store, self.data_dir, self.stop_event = cfg, store, data_dir, stop_event
        self._session = requests.Session()
        self._session.headers["apikey"] = cfg.anon_key
        self._token: str | None = None
        self._refresh: str | None = None
        self._expires_at = 0.0
        self._user_id = ""

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self._sync_batch()
            except requests.RequestException as e:
                log.warning("Supabase injoignable (%s), nouvel essai plus tard", type(e).__name__)
            except SyncError as e:
                log.error("Synchronisation Supabase : %s", e)
            except Exception:
                log.exception("Erreur inattendue de synchronisation")
            self.stop_event.wait(self.INTERVAL)

    def _sync_batch(self) -> None:
        rows = self.store.pending_sync(20)
        if not rows:
            return
        self._auth()
        for row in rows:
            if self.stop_event.is_set():
                return
            payload = {
                "id": row["id"],
                "ts": row["ts"],
                "camera": row["camera"],
                "zone": row["zone"],
                "category": row["category"],
                "labels": json.loads(row["labels"]),
                "count": row["count"],
                "plate": row["plate"],
                "snapshot_path": self._upload(row["snapshot"], "image/jpeg"),
                "clip_path": self._upload(row["clip"], "video/mp4"),
            }
            r = self._session.post(
                f"{self.cfg.url}/rest/v1/events?on_conflict=id", json=payload, timeout=30,
                headers={**self._auth_header(), "Prefer": "resolution=merge-duplicates,return=minimal"})
            if r.ok:
                self.store.mark_synced(row["id"])
            elif self._is_permanent(r):
                log.error("Événement %s rejeté par Supabase : %s", row["id"], _err(r))
                self.store.mark_synced(row["id"], ok=False)
            else:
                raise SyncError(f"insertion refusée : {_err(r)}")

    def _upload(self, rel: str | None, content_type: str) -> str | None:
        if not rel or not (self.data_dir / rel).is_file():
            return None
        key = f"{self._user_id}/{rel}"
        with open(self.data_dir / rel, "rb") as fh:
            r = self._session.post(
                f"{self.cfg.url}/storage/v1/object/{self.cfg.bucket}/{quote(key)}", data=fh, timeout=120,
                headers={**self._auth_header(), "Content-Type": content_type, "x-upsert": "true"})
        if not r.ok:
            raise SyncError(f"envoi de {rel} refusé : {_err(r)}")
        return key

    def _auth(self) -> None:
        if self._token and time.time() < self._expires_at - 60:
            return
        url = f"{self.cfg.url}/auth/v1/token"
        if self._refresh:
            r = self._session.post(url, params={"grant_type": "refresh_token"},
                                   json={"refresh_token": self._refresh}, timeout=30)
            if r.ok:
                return self._set_session(r.json())
        r = self._session.post(url, params={"grant_type": "password"},
                               json={"email": self.cfg.email, "password": self.cfg.password}, timeout=30)
        if not r.ok:
            raise SyncError(f"connexion refusée : {_err(r)}")
        self._set_session(r.json())

    def _set_session(self, body: dict) -> None:
        self._token = body["access_token"]
        self._refresh = body.get("refresh_token")
        self._expires_at = time.time() + int(body.get("expires_in", 3600))
        self._user_id = body["user"]["id"]

    def _auth_header(self) -> dict:
        return {"Authorization": f"Bearer {self._token}"}

    def _is_permanent(self, r: requests.Response) -> bool:
        if r.status_code in (401, 403):
            self._token = None  # session expirée : reconnexion au prochain cycle
            return False
        return 400 <= r.status_code < 500 and r.status_code not in (408, 429)


def _err(r: requests.Response) -> str:
    try:
        body = r.json()
        return str(body.get("message") or body.get("msg") or body.get("error_description") or body)[:200]
    except ValueError:
        return f"HTTP {r.status_code}"
