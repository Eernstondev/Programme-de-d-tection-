"""Règles d'alerte : un objet suivi doit rester N images dans une zone active pour déclencher."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .config import Zone


@dataclass
class Detection:
    track_id: int | None
    category: str
    label: str
    conf: float
    box: tuple[int, int, int, int]
    anchor: tuple[float, float]  # bas-centre normalisé (pieds / roues) : plus fiable que le centre pour les zones
    in_zone: bool = False


@dataclass
class Trigger:
    zone: str
    category: str
    detections: list[Detection] = field(default_factory=list)


class ZoneMonitor:
    FORGET_AFTER = 120.0  # secondes avant d'oublier un objet disparu

    def __init__(self, zones: list[Zone]):
        self.zones = zones
        self._inside: dict[tuple[int, str], int] = {}      # (track_id, zone) -> images consécutives dans la zone
        self._alerted: dict[tuple[int, str], float] = {}   # (track_id, zone) -> dernière fois vu
        self._last_alert: dict[tuple[str, str], float] = {}  # (zone, catégorie) -> dernière alerte

    def active_zones(self, now: datetime) -> set[str]:
        minute = now.hour * 60 + now.minute
        return {z.name for z in self.zones if z.is_active(minute)}

    def update(self, detections: list[Detection], now: datetime, mono: float) -> list[Trigger]:
        active = self.active_zones(now)
        seen: set[tuple[int, str]] = set()
        hits: dict[tuple[str, str], list[Detection]] = {}

        for zone in self.zones:
            if zone.name not in active:
                continue
            for d in detections:
                if d.track_id is None or d.category not in zone.categories or not zone.contains(*d.anchor):
                    continue
                d.in_zone = True
                key = (d.track_id, zone.name)
                seen.add(key)
                self._inside[key] = self._inside.get(key, 0) + 1
                if key in self._alerted:
                    self._alerted[key] = mono
                elif self._inside[key] >= zone.min_frames:
                    self._alerted[key] = mono
                    hits.setdefault((zone.name, d.category), []).append(d)

        # Compteur remis à zéro dès qu'un objet sort de la zone
        self._inside = {k: v for k, v in self._inside.items() if k in seen}
        self._alerted = {k: t for k, t in self._alerted.items() if mono - t < self.FORGET_AFTER}

        cooldowns = {z.name: z.cooldown for z in self.zones}
        triggers = []
        for (zone_name, category), items in hits.items():
            key = (zone_name, category)
            if key in self._last_alert and mono - self._last_alert[key] < cooldowns[zone_name]:
                continue
            self._last_alert[key] = mono
            triggers.append(Trigger(zone=zone_name, category=category, detections=items))
        return triggers
