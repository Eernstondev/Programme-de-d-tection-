"""Notifications Telegram (photo immédiate, puis clip vidéo en réponse au même message)."""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
from pathlib import Path

import requests

log = logging.getLogger(__name__)
MAX_ATTEMPTS = 8  # ~3 min de tentatives : couvre les coupures internet courtes


class TelegramNotifier(threading.Thread):
    def __init__(self, token: str, chat_ids: list[str]):
        super().__init__(name="telegram", daemon=True)
        self._base = f"https://api.telegram.org/bot{token}/"
        self.chat_ids = chat_ids
        self._queue: queue.Queue = queue.Queue(maxsize=500)
        self._session = requests.Session()
        self._msg_ids: dict[str, dict[str, int]] = {}  # event_id -> {chat_id: message_id}

    # --- API publique (non bloquante) ---
    def text(self, message: str) -> None:
        self._put(("text", [], None, message))

    def photo(self, event_id: str, path: Path, caption: str) -> None:
        self._put(("photo", [event_id], path, caption))

    def video(self, event_ids: list[str], path: Path, caption: str) -> None:
        self._put(("video", event_ids, path, caption))

    def send_test(self) -> bool:
        """Envoi synchrone, pour vérifier le token et les chat_ids."""
        return all(self._call("sendMessage", {"chat_id": c, "text": "✅ Test de la surveillance : OK"})
                   for c in self.chat_ids)

    def stop(self, timeout: float = 30) -> None:
        self._queue.put(None)
        self.join(timeout)

    # --- interne ---
    def _put(self, job) -> None:
        try:
            self._queue.put_nowait(job)
        except queue.Full:
            log.warning("File Telegram pleine, notification ignorée")

    def run(self) -> None:
        while (job := self._queue.get()) is not None:
            try:
                self._process(*job)
            except Exception:
                log.exception("Erreur d'envoi Telegram")

    def _process(self, kind: str, event_ids: list[str], path: Path | None, caption: str) -> None:
        for chat in self.chat_ids:
            data = {"chat_id": chat}
            if kind == "text":
                self._call("sendMessage", {**data, "text": caption})
            elif kind == "photo":
                mid = self._call("sendPhoto", {**data, "caption": caption}, ("photo", path))
                if mid:
                    self._msg_ids.setdefault(event_ids[0], {})[chat] = mid
            else:
                reply = next((self._msg_ids[e][chat] for e in event_ids
                              if chat in self._msg_ids.get(e, {})), None)
                if reply:
                    data["reply_parameters"] = json.dumps({"message_id": reply, "allow_sending_without_reply": True})
                self._call("sendVideo", {**data, "caption": caption, "supports_streaming": "true"}, ("video", path))

        if kind == "video":
            for e in event_ids:
                self._msg_ids.pop(e, None)
        if len(self._msg_ids) > 1000:  # photos dont le clip n'est jamais arrivé
            for e in list(self._msg_ids)[:500]:
                del self._msg_ids[e]

    def _call(self, method: str, data: dict, file: tuple[str, Path] | None = None) -> int | None:
        for attempt in range(MAX_ATTEMPTS):
            wait = min(2 ** attempt, 60)
            try:
                if file:
                    field, path = file
                    with open(path, "rb") as fh:
                        r = self._session.post(self._base + method, data=data, files={field: fh}, timeout=120)
                else:
                    r = self._session.post(self._base + method, data=data, timeout=30)
                body = r.json() if "json" in r.headers.get("content-type", "") else {}
                if r.ok and body.get("ok"):
                    return body["result"]["message_id"]
                if r.status_code == 429:
                    wait = int(body.get("parameters", {}).get("retry_after", wait))
                elif 400 <= r.status_code < 500:
                    log.error("Telegram %s refusé : %s", method, body.get("description", r.status_code))
                    return None
            except requests.RequestException as e:
                # Le message d'erreur contient l'URL avec le token : on ne logue que le type
                log.warning("Telegram injoignable (%s), nouvel essai dans %ss", type(e).__name__, wait)
            except OSError as e:
                log.error("Fichier à envoyer illisible : %s", e)
                return None
            time.sleep(wait)
        log.error("Telegram %s : abandon après %d tentatives", method, MAX_ATTEMPTS)
        return None
