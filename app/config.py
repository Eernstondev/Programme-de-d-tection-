"""Chargement et validation de la configuration (config.yaml + secrets dans .env)."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np
import yaml
from dotenv import load_dotenv

CATEGORIES = ("personne", "vehicule", "animal")
_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_NAME = re.compile(r"^[a-z0-9_-]+$")
_WINDOW = re.compile(r"^(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})$")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class TimeWindow:
    start: int  # minutes depuis minuit
    end: int

    def contains(self, minute: int) -> bool:
        if self.start <= self.end:
            return self.start <= minute < self.end
        return minute >= self.start or minute < self.end  # plage qui passe minuit (ex. 22:00-06:00)


@dataclass
class Zone:
    name: str
    categories: frozenset[str]
    polygon: np.ndarray | None = None  # points normalisés 0-1 ; None = toute l'image
    windows: tuple[TimeWindow, ...] = ()  # vide = 24 h/24
    min_frames: int = 3
    cooldown: float = 60.0

    def is_active(self, minute: int) -> bool:
        return not self.windows or any(w.contains(minute) for w in self.windows)

    def contains(self, x: float, y: float) -> bool:
        if self.polygon is None:
            return True
        return cv2.pointPolygonTest(self.polygon, (float(x), float(y)), False) >= 0


@dataclass
class Camera:
    name: str
    source: str | int
    fps: float
    zones: list[Zone]


@dataclass
class ClipSettings:
    enabled: bool = True
    pre_seconds: float = 5.0
    post_seconds: float = 10.0
    max_seconds: float = 30.0
    max_width: int = 1280


@dataclass
class TelegramSettings:
    enabled: bool = False
    token: str = ""
    chat_ids: list[str] = field(default_factory=list)


@dataclass
class SupabaseSettings:
    enabled: bool = False
    url: str = ""
    anon_key: str = ""
    email: str = ""
    password: str = ""
    bucket: str = "captures"


@dataclass
class Settings:
    model: str
    device: str | None
    conf: float
    imgsz: int
    data_dir: Path
    retention_days: int
    min_free_gb: float
    clips: ClipSettings
    telegram: TelegramSettings
    supabase: SupabaseSettings
    plates_enabled: bool
    plates_min_conf: float
    cameras: list[Camera]


def load_config(path: str | Path, env_file: str | Path | None = None) -> Settings:
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise ConfigError(f"Fichier de configuration introuvable : {path}")
    load_dotenv(Path(env_file).expanduser() if env_file else path.parent / ".env")

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"YAML invalide : {e}") from e
    if not isinstance(raw, dict):
        raise ConfigError("Le fichier de configuration doit être un objet YAML")

    missing: set[str] = set()

    def section(key: str) -> dict:
        data = raw.get(key) or {}
        if not isinstance(data, dict):
            raise ConfigError(f"'{key}' doit être un objet")
        # Les sections désactivées ne sont pas résolues : pas d'erreur si leurs variables .env manquent
        return _expand(data, missing) if data.get("enabled", True) else data

    storage = section("storage")
    data_dir = Path(str(storage.get("dir", "data"))).expanduser()
    if not data_dir.is_absolute():
        data_dir = path.parent / data_dir

    c = section("clips")
    clips = ClipSettings(
        enabled=bool(c.get("enabled", True)),
        pre_seconds=_num(c, "pre_seconds", 5, 0, "clips"),
        post_seconds=_num(c, "post_seconds", 10, 1, "clips"),
        max_seconds=_num(c, "max_seconds", 30, 5, "clips"),
        max_width=int(_num(c, "max_width", 1280, 320, "clips")),
    )

    t = section("telegram")
    telegram = TelegramSettings(enabled=bool(t.get("enabled", False)))
    if telegram.enabled:
        telegram.token = str(t.get("token", "")).strip()
        telegram.chat_ids = [str(x).strip() for x in t.get("chat_ids") or [] if str(x).strip()]
        if not telegram.token or not telegram.chat_ids:
            raise ConfigError("telegram : 'token' et 'chat_ids' sont obligatoires quand enabled: true")

    s = section("supabase")
    supabase = SupabaseSettings(enabled=bool(s.get("enabled", False)))
    if supabase.enabled:
        for key in ("url", "anon_key", "email", "password"):
            value = str(s.get(key, "")).strip()
            if not value:
                raise ConfigError(f"supabase.{key} est obligatoire quand enabled: true")
            setattr(supabase, key, value)
        supabase.url = supabase.url.rstrip("/")
        supabase.bucket = str(s.get("bucket", "captures"))

    p = section("plates")
    cameras = _parse_cameras(raw.get("cameras"), missing, path.parent)

    if missing:
        raise ConfigError("Variables manquantes dans .env : " + ", ".join(sorted(missing)))

    device = raw.get("device")
    return Settings(
        model=str(raw.get("model", "yolo11n.pt")),
        device=str(device) if device not in (None, "") else None,
        conf=_num(raw, "conf", 0.45, 0.05, "racine"),
        imgsz=int(_num(raw, "imgsz", 640, 160, "racine")),
        data_dir=data_dir.resolve(),
        retention_days=int(_num(storage, "retention_days", 14, 1, "storage")),
        min_free_gb=_num(storage, "min_free_gb", 5, 0, "storage"),
        clips=clips,
        telegram=telegram,
        supabase=supabase,
        plates_enabled=bool(p.get("enabled", False)),
        plates_min_conf=_num(p, "min_confidence", 0.6, 0, "plates"),
        cameras=cameras,
    )


def _expand(value, missing: set[str]):
    """Remplace ${VAR} par la valeur de la variable d'environnement (chargée depuis .env)."""
    if isinstance(value, str):
        def repl(m: re.Match) -> str:
            v = os.environ.get(m.group(1))
            if v is None:
                missing.add(m.group(1))
                return ""
            return v
        return _ENV_REF.sub(repl, value)
    if isinstance(value, list):
        return [_expand(v, missing) for v in value]
    if isinstance(value, dict):
        return {k: _expand(v, missing) for k, v in value.items()}
    return value


def _num(data: dict, key: str, default: float, minimum: float, where: str) -> float:
    value = data.get(key, default)
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{where}.{key} doit être un nombre") from None
    if value < minimum:
        raise ConfigError(f"{where}.{key} doit être ≥ {minimum}")
    return value


def _resolve_source(source: str, base: Path, where: str) -> str:
    """Fichier vidéo / périphérique : chemin relatif résolu depuis le dossier de config.yaml."""
    if "://" in source:
        return source
    candidate = Path(source).expanduser()
    if not candidate.is_absolute():
        candidate = base / candidate
    if not candidate.exists():
        raise ConfigError(f"{where}.source : fichier introuvable ({candidate})")
    return str(candidate)


def _parse_cameras(items, missing: set[str], base: Path) -> list[Camera]:
    if not items or not isinstance(items, list):
        raise ConfigError("Ajoute au moins une caméra dans 'cameras'")
    cameras, names = [], set()
    for i, raw in enumerate(items):
        where = f"cameras[{i}]"
        if not isinstance(raw, dict):
            raise ConfigError(f"{where} doit être un objet")
        cam = _expand(raw, missing)
        name = str(cam.get("name", "")).strip()
        if not _NAME.match(name):
            raise ConfigError(f"{where}.name : minuscules, chiffres, - et _ uniquement (ex. 'cour')")
        if name in names:
            raise ConfigError(f"Nom de caméra en double : {name}")
        names.add(name)

        source = cam.get("source")
        if source is None or str(source).strip() == "":
            raise ConfigError(f"{where}.source est obligatoire")
        source = str(source).strip()
        source = int(source) if source.isdigit() else _resolve_source(source, base, where)

        zones_raw = cam.get("zones") or []
        if not isinstance(zones_raw, list):
            raise ConfigError(f"{where}.zones doit être une liste")
        zones = [_parse_zone(z, f"{where}.zones[{j}]") for j, z in enumerate(zones_raw)]
        if not zones:
            zones = [Zone(name="tout", categories=frozenset(CATEGORIES))]
        zone_names = [z.name for z in zones]
        if len(set(zone_names)) != len(zone_names):
            raise ConfigError(f"{where} : noms de zones en double")

        cameras.append(Camera(name=name, source=source, fps=_num(cam, "fps", 10, 0.5, where), zones=zones))
    return cameras


def _parse_zone(z, where: str) -> Zone:
    if not isinstance(z, dict):
        raise ConfigError(f"{where} doit être un objet")
    name = str(z.get("name", "")).strip()
    if not _NAME.match(name):
        raise ConfigError(f"{where}.name : minuscules, chiffres, - et _ uniquement")

    cats = z.get("categories", list(CATEGORIES))
    if not isinstance(cats, list) or not cats or not set(cats) <= set(CATEGORIES):
        raise ConfigError(f"{where}.categories : liste parmi {', '.join(CATEGORIES)}")

    polygon = None
    if z.get("polygon") is not None:
        try:
            polygon = np.asarray(z["polygon"], dtype=np.float32)
        except (TypeError, ValueError):
            raise ConfigError(f"{where}.polygon invalide") from None
        if polygon.ndim != 2 or polygon.shape[1] != 2 or len(polygon) < 3:
            raise ConfigError(f"{where}.polygon : au moins 3 points [x, y]")
        if (polygon < 0).any() or (polygon > 1).any():
            raise ConfigError(f"{where}.polygon : coordonnées entre 0 et 1 (génère-les avec zone_editor.py)")

    schedule = z.get("schedule") or []
    if not isinstance(schedule, list):
        raise ConfigError(f"{where}.schedule doit être une liste, ex. [\"22:00-06:00\"]")

    return Zone(
        name=name,
        categories=frozenset(cats),
        polygon=polygon,
        windows=tuple(_parse_window(w, where) for w in schedule),
        min_frames=int(_num(z, "min_frames", 3, 1, where)),
        cooldown=_num(z, "cooldown", 60, 0, where),
    )


def _parse_window(value, where: str) -> TimeWindow:
    m = _WINDOW.match(str(value).strip())
    if not m:
        raise ConfigError(f"{where}.schedule : format attendu \"HH:MM-HH:MM\", reçu {value!r}")
    h1, m1, h2, m2 = map(int, m.groups())
    start, end = h1 * 60 + m1, h2 * 60 + m2
    if m1 > 59 or m2 > 59 or start > 1440 or end > 1440 or start == end:
        raise ConfigError(f"{where}.schedule : plage horaire invalide {value!r}")
    return TimeWindow(start, end)
