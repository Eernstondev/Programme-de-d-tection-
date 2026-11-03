# Surveillance caméras

Détection **personnes / véhicules / animaux** (YOLO11) sur caméras IP (RTSP), webcam ou vidéo :
zones, plages horaires, alertes Telegram (photo puis clip), historique SQLite, synchronisation Supabase,
lecture de plaques, nettoyage automatique et démarrage automatique.

## 1. Installation

```bash
cd ~/Projets/surveillance
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
cp .env.example .env
```

Facultatif :
- `brew install ffmpeg` : clips en H.264, lisibles directement sur téléphone.
- `pip install "fast-alpr[onnx]"` : lecture de plaques.

## 2. Telegram (gratuit)

1. Sur Telegram, ouvre **@BotFather** → `/newbot` → copie le token dans `.env` (`TELEGRAM_TOKEN=`).
2. Envoie un message quelconque à ton bot, puis ouvre dans un navigateur :
   `https://api.telegram.org/bot<TOKEN>/getUpdates` → copie `"chat":{"id": ...}` dans `TELEGRAM_CHAT_ID=`.
3. Teste : `python main.py --test-telegram`

Pour alerter un groupe (famille, gardiens), ajoute le bot au groupe. Son `chat_id` commence par `-`.

## 3. Caméras

Dans `config.yaml`, une entrée par caméra. Mets les identifiants dans `.env`, jamais dans `config.yaml`.

| Marque | URL RTSP (sous-flux, recommandé) |
|---|---|
| Hikvision | `rtsp://USER:PASS@IP:554/Streaming/Channels/102` |
| Dahua / Imou | `rtsp://USER:PASS@IP:554/cam/realmonitor?channel=1&subtype=1` |
| DVR Hikvision, canal 3 | `rtsp://USER:PASS@IP:554/Streaming/Channels/302` |

- Webcam du Mac : `source: 0`. Vidéo : `source: video.mp4`.
- Mot de passe avec `@ : # /` : encode-le dans `.env` (`@` → `%40`, `#` → `%23`, `:` → `%3A`).
- Supprime la caméra d'exemple `cour` si tu n'as pas encore de caméra IP.

## 4. Zones

```bash
python zone_editor.py --camera cour
```

Clique les coins de la zone (cour, porte…). Appuie sur **n** pour passer à la zone suivante et sur **Entrée** pour terminer, puis colle le YAML affiché dans `config.yaml`.

- Seul le **bas de l'objet** (pieds, roues) compte : une personne sur le trottoir derrière le portail ne déclenche pas d'alerte.
- `categories` : `[personne]`, `[vehicule]`, `[animal]` ou une combinaison.
- `schedule: ["22:00-06:00"]` : zone active seulement la nuit. Sans `schedule`, elle est active 24 h/24.
- `min_frames: 3` : l'objet doit rester 3 images dans la zone (filtre les fausses alertes).
- `cooldown: 60` : au moins 60 s entre deux alertes du même type dans la zone.
- Sans `zones`, toute l'image est surveillée pour les 3 catégories.

## 5. Lancement

```bash
source .venv/bin/activate
python main.py --show     # avec les vidéos à l'écran (q pour quitter)
python main.py            # sans affichage (Ctrl+C pour arrêter)
```

Les fichiers sont dans `data/` :
- `snapshots/` : photos des événements
- `clips/` : vidéos de 10 à 20 s
- `events.db` : historique
- `logs/` : journal

Le nettoyage est automatique après `retention_days` jours, ou si le disque descend sous `min_free_gb`.

## 6. Démarrage automatique

**Mac** : démarrage à l'ouverture de session, relance en cas de plantage, Mac maintenu éveillé.
```bash
bash deploy/install_macos.sh
tail -f data/logs/surveillance.log   # suivre
bash deploy/uninstall_macos.sh       # désactiver
```

> En démarrage automatique, macOS peut bloquer l'accès à la **webcam**. Les caméras IP (RTSP) ne sont pas concernées.

**Mini-PC / Jetson (Linux)** : voir les instructions en tête de `deploy/linux/surveillance.service`.

## 7. Historique en ligne (Supabase)

1. Crée un projet Supabase. Dans **SQL Editor**, exécute `supabase/schema.sql`.
2. Dans **Authentication → Users**, crée **un compte par site client** (email et mot de passe).
3. Renseigne `SUPABASE_*` dans `.env`, puis mets `supabase: enabled: true`.

Les événements sont d'abord enregistrés localement, puis envoyés dès que la connexion revient. Une coupure internet ne fait rien perdre. Le compte ne voit que ses propres événements (RLS) et le bucket `captures` est privé.

## 8. Plaques d'immatriculation

`pip install "fast-alpr[onnx]"`, puis `plates: enabled: true`.

La plaque est lue sur les véhicules qui déclenchent une alerte. Elle apparaît dans le message Telegram et dans l'historique. Pour une lecture fiable, il faut une caméra cadrée sur l'entrée, à moins de 5 m environ, en flux principal (`Channels/101`).

## Dépannage

| Problème | Solution |
|---|---|
| `Erreur de configuration : …` | Le message indique la ligne à corriger |
| Caméra « hors ligne » | Vérifie l'IP, le mot de passe et que le Mac est sur le même réseau. Teste l'URL dans VLC |
| Trop de fausses alertes | Monte `conf` à 0.55, `min_frames` à 5, et resserre les zones |
| Objets lointains ratés | `model: yolo11s.pt` |
| Lent avec plusieurs caméras | Sous-flux + `fps: 5` |
