#!/usr/bin/env python3
"""Alexa-YT : serveur de skill Alexa qui joue des playlists YouTube sur un Echo.

Un seul processus, sans dépendance externe (bibliothèque standard Python >= 3.8) :
  * POST /alexa          -> point de terminaison de la skill Alexa
  * GET  /stream/<id>    -> flux audio d'une vidéo (proxy vers YouTube via yt-dlp)
  * GET  /health         -> vérification rapide

yt-dlp est appelé en ligne de commande (binaire autonome yt-dlp_linux_aarch64).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import random
import re
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime, timezone
from difflib import SequenceMatcher
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

LOG = logging.getLogger("alexa-yt")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULTS = {
    "port": 8765,
    "bind": "127.0.0.1",
    "skill_id": "",
    "public_base_url": "",
    "secret": "",
    "ytdlp_path": "yt-dlp",
    "ytdlp_extra_args": [],
    "audio_format": "140/bestaudio[ext=m4a]/bestaudio",
    "shuffle_by_default": False,
    "playlist_refresh_minutes": 60,
    "state_file": "state.json",
    "token_file": "google_token.json",
    "google_client_id": "",
    "google_client_secret": "",
    "playlists": [],
}


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = dict(DEFAULTS)
        cfg.update(json.load(f))
    if not cfg["secret"]:
        raise SystemExit("config: 'secret' est vide (mets une longue chaîne aléatoire)")
    if not str(cfg["public_base_url"]).startswith("https://"):
        raise SystemExit("config: 'public_base_url' doit commencer par https://")
    cfg["public_base_url"] = cfg["public_base_url"].rstrip("/")
    base = os.path.dirname(os.path.abspath(path))
    if "/" in cfg["ytdlp_path"] and not os.path.isabs(cfg["ytdlp_path"]):
        cfg["ytdlp_path"] = os.path.normpath(os.path.join(base, cfg["ytdlp_path"]))
    for key in ("state_file", "token_file"):
        if not os.path.isabs(cfg[key]):
            cfg[key] = os.path.join(base, cfg[key])
    return cfg


# ---------------------------------------------------------------------------
# Texte : normalisation et recherche approximative des noms de playlist
# ---------------------------------------------------------------------------

def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = re.sub(r"\b(la|le|les|l|ma|mon|mes|playlist|liste)\b", " ", text)
    return " ".join(text.split())


def best_match(query: str, names: list) -> tuple:
    """Retourne (index, score) du nom le plus proche, ou (-1, 0)."""
    q = normalize(query)
    best, best_score = -1, 0.0
    for i, name in enumerate(names):
        n = normalize(name)
        if not n:
            continue
        score = SequenceMatcher(None, q, n).ratio()
        if q and (q in n or n in q):
            score = max(score, 0.9)
        if score > best_score:
            best, best_score = i, score
    return best, best_score


def playlist_id_from(value: str) -> str:
    m = re.search(r"[?&]list=([A-Za-z0-9_-]+)", value)
    return m.group(1) if m else value.strip()


# ---------------------------------------------------------------------------
# Compte Google (API YouTube Data v3, OAuth « appareil à saisie limitée »)
# ---------------------------------------------------------------------------

GOOGLE_SCOPE = "https://www.googleapis.com/auth/youtube.readonly"
GOOGLE_DEVICE_URL = "https://oauth2.googleapis.com/device/code"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
YT_API = "https://www.googleapis.com/youtube/v3/"


def http_json(url: str, data: dict = None, headers: dict = None) -> dict:
    body = None
    if data is not None:
        body = "&".join("%s=%s" % (k, quote(str(v), safe="")) for k, v in data.items())
        body = body.encode()
    req = urllib.request.Request(url, data=body, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8"))
        except ValueError:
            detail = {}
        err = GoogleError(detail.get("error") or "http_%d" % exc.code)
        err.detail = detail
        raise err from None


class GoogleError(RuntimeError):
    detail: dict = {}

    @property
    def code(self) -> str:
        return str(self.args[0]) if self.args else ""


class Google:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.token: dict = {}
        try:
            with open(cfg["token_file"], encoding="utf-8") as f:
                self.token = json.load(f)
        except (OSError, ValueError):
            pass

    @property
    def configured(self) -> bool:
        return bool(self.cfg["google_client_id"] and self.cfg["google_client_secret"])

    @property
    def connected(self) -> bool:
        return self.configured and bool(self.token.get("refresh_token"))

    def _save(self) -> None:
        tmp = self.cfg["token_file"] + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(self.token, f)
        os.replace(tmp, self.cfg["token_file"])

    def login(self) -> None:
        """Connexion interactive : affiche un code à saisir sur google.com/device."""
        if not self.configured:
            raise SystemExit("Renseigne google_client_id et google_client_secret dans config.json")
        dev = http_json(GOOGLE_DEVICE_URL, {"client_id": self.cfg["google_client_id"],
                                            "scope": GOOGLE_SCOPE})
        print("\n1. Ouvre %s sur ton téléphone ou ton ordinateur" % dev["verification_url"])
        print("2. Saisis le code : %s" % dev["user_code"])
        print("3. Choisis ton compte YouTube et accepte.\n")
        print("En attente de ton autorisation...", flush=True)
        interval = int(dev.get("interval", 5))
        deadline = time.time() + int(dev.get("expires_in", 1800))
        while time.time() < deadline:
            time.sleep(interval)
            try:
                tok = http_json(GOOGLE_TOKEN_URL, {
                    "client_id": self.cfg["google_client_id"],
                    "client_secret": self.cfg["google_client_secret"],
                    "device_code": dev["device_code"],
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
            except GoogleError as exc:
                if exc.code == "authorization_pending":
                    continue
                if exc.code == "slow_down":
                    interval += 5
                    continue
                raise SystemExit("Connexion refusée : %s %s" % (exc.code, exc.detail))
            tok["expires_at"] = time.time() + int(tok.get("expires_in", 3600)) - 60
            self.token = tok
            self._save()
            print("Connecté ! Jeton enregistré dans %s" % self.cfg["token_file"])
            return
        raise SystemExit("Délai dépassé, relance la commande.")

    def access_token(self) -> str:
        with self.lock:
            if self.token.get("access_token") and self.token.get("expires_at", 0) > time.time():
                return self.token["access_token"]
            if not self.token.get("refresh_token"):
                raise RuntimeError("compte Google non connecté (lance --login)")
            tok = http_json(GOOGLE_TOKEN_URL, {
                "client_id": self.cfg["google_client_id"],
                "client_secret": self.cfg["google_client_secret"],
                "refresh_token": self.token["refresh_token"],
                "grant_type": "refresh_token"})
            self.token["access_token"] = tok["access_token"]
            self.token["expires_at"] = time.time() + int(tok.get("expires_in", 3600)) - 60
            self._save()
            return self.token["access_token"]

    def api(self, endpoint: str, params: dict) -> dict:
        query = "&".join("%s=%s" % (k, quote(str(v), safe="")) for k, v in params.items())
        return http_json(YT_API + endpoint + "?" + query,
                         headers={"Authorization": "Bearer " + self.access_token()})

    def _paged(self, endpoint: str, params: dict) -> list:
        items, page = [], None
        while True:
            p = dict(params, maxResults=50)
            if page:
                p["pageToken"] = page
            data = self.api(endpoint, p)
            items += data.get("items") or []
            page = data.get("nextPageToken")
            if not page:
                return items

    def my_playlists(self) -> list:
        items = self._paged("playlists", {"part": "snippet", "mine": "true"})
        return [{"name": i["snippet"]["title"], "id": i["id"]} for i in items]

    def playlist_items(self, playlist_id: str) -> list:
        items = self._paged("playlistItems", {"part": "snippet,contentDetails,status",
                                              "playlistId": playlist_id})
        out = []
        for i in items:
            vid = (i.get("contentDetails") or {}).get("videoId")
            privacy = (i.get("status") or {}).get("privacyStatus")
            title = (i.get("snippet") or {}).get("title") or ""
            # Les vidéos privées ou supprimées ne sont pas lisibles par yt-dlp sans
            # cookies de connexion : on les ignore au lieu d'échouer en lecture.
            if not vid or privacy in ("private", "privacyStatusUnspecified") or \
                    title in ("Private video", "Deleted video"):
                LOG.debug("vidéo ignorée (%s) : %s", privacy, vid)
                continue
            out.append(vid)
        return out


# ---------------------------------------------------------------------------
# yt-dlp
# ---------------------------------------------------------------------------

class YouTube:
    def __init__(self, cfg: dict, google: Google = None):
        self.cfg = cfg
        self.google = google
        self._account_playlists: list = []
        self._tracks: dict = {}  # playlist_id -> (timestamp, [video ids])
        self._streams: dict = {}  # video_id -> (expires_at, info)
        self._lock = threading.Lock()
        self._resolving: dict = {}  # video_id -> threading.Event

    def _run(self, args: list, timeout: int = 120) -> str:
        cmd = [self.cfg["ytdlp_path"], "--no-warnings", "--ignore-config"]
        cmd += list(self.cfg["ytdlp_extra_args"]) + args
        LOG.debug("yt-dlp %s", " ".join(args))
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if out.returncode != 0:
            raise RuntimeError("yt-dlp a échoué : " + out.stderr.strip()[-500:])
        return out.stdout

    # -- playlists ---------------------------------------------------------

    def playlists(self) -> list:
        """Playlists de config.json, puis celles du compte Google (sans doublon)."""
        out = [{"name": p["name"], "id": playlist_id_from(p["id"]),
                "shuffle": p.get("shuffle"), "loop": p.get("loop")}
               for p in self.cfg["playlists"]]
        seen = {p["id"] for p in out}
        with self._lock:
            account = list(self._account_playlists)
        out += [p for p in account if p["id"] not in seen]
        return out

    def refresh_account_playlists(self) -> None:
        if self.google and self.google.connected:
            found = self.google.my_playlists()
            with self._lock:
                self._account_playlists = found
            LOG.info("compte Google : %d playlists", len(found))

    def fetch_playlist(self, playlist_id: str) -> list:
        if self.google and self.google.connected:
            ids = self.google.playlist_items(playlist_id)
        else:
            url = "https://www.youtube.com/playlist?list=" + playlist_id
            data = json.loads(self._run(["--flat-playlist", "-J", url]))
            ids = []
            for e in data.get("entries") or []:
                vid = (e or {}).get("id")
                title = (e or {}).get("title") or ""
                if vid and title not in ("[Private video]", "[Deleted video]"):
                    ids.append(vid)
        with self._lock:
            self._tracks[playlist_id] = (time.time(), ids)
        LOG.info("playlist %s : %d titres", playlist_id, len(ids))
        return ids

    def tracks(self, playlist_id: str) -> list:
        with self._lock:
            cached = self._tracks.get(playlist_id)
        if cached:
            return cached[1]
        return self.fetch_playlist(playlist_id)

    def refresh_all(self) -> None:
        try:
            self.refresh_account_playlists()
        except Exception as exc:  # noqa: BLE001
            LOG.warning("liste des playlists du compte impossible : %s", exc)
        for p in self.playlists():
            try:
                self.fetch_playlist(p["id"])
            except Exception as exc:  # noqa: BLE001
                LOG.warning("rafraîchissement de %s impossible : %s", p.get("name"), exc)

    # -- flux audio --------------------------------------------------------

    def stream_info(self, video_id: str, force: bool = False) -> dict:
        """URL googlevideo + en-têtes + taille, en cache jusqu'à expiration."""
        while True:
            with self._lock:
                cached = self._streams.get(video_id)
                if cached and not force and cached[0] > time.time():
                    return cached[1]
                event = self._resolving.get(video_id)
                if event is None:
                    event = threading.Event()
                    self._resolving[video_id] = event
                    owner = True
                else:
                    owner = False
            if not owner:
                event.wait(150)
                force = False
                continue
            try:
                info = self._resolve(video_id)
                with self._lock:
                    self._streams[video_id] = (info["expires"], info)
                return info
            finally:
                with self._lock:
                    self._resolving.pop(video_id, None)
                event.set()

    def _resolve(self, video_id: str) -> dict:
        url = "https://www.youtube.com/watch?v=" + video_id
        data = json.loads(self._run(["-f", self.cfg["audio_format"], "-j", url]))
        stream_url = data.get("url")
        if not stream_url:
            raise RuntimeError("aucun flux audio pour " + video_id)
        expire = parse_qs(urlparse(stream_url).query).get("expire", [None])[0]
        expires = int(expire) - 300 if expire and expire.isdigit() else time.time() + 3600
        ext = data.get("ext") or "m4a"
        ctype = {"m4a": "audio/mp4", "mp4": "audio/mp4", "webm": "audio/webm",
                 "mp3": "audio/mpeg"}.get(ext, "application/octet-stream")
        LOG.info("flux prêt : %s (%s)", data.get("title"), ext)
        return {
            "url": stream_url,
            "headers": data.get("http_headers") or {},
            "size": data.get("filesize") or 0,
            "content_type": ctype,
            "title": data.get("title") or "",
            "expires": expires,
        }

    def prefetch(self, video_id: str) -> None:
        def work():
            try:
                self.stream_info(video_id)
            except Exception as exc:  # noqa: BLE001
                LOG.warning("préchargement %s impossible : %s", video_id, exc)
        threading.Thread(target=work, daemon=True).start()


# ---------------------------------------------------------------------------
# État de lecture par appareil Echo
# ---------------------------------------------------------------------------

class State:
    def __init__(self, path: str):
        self.path = path
        self.lock = threading.Lock()
        try:
            with open(path, encoding="utf-8") as f:
                self.devices = json.load(f)
        except (OSError, ValueError):
            self.devices = {}

    def get(self, device: str) -> dict:
        with self.lock:
            return dict(self.devices.get(device) or {})

    def put(self, device: str, st: dict) -> None:
        with self.lock:
            self.devices[device] = st
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.devices, f)
            os.replace(tmp, self.path)


# ---------------------------------------------------------------------------
# Logique de la skill
# ---------------------------------------------------------------------------

MAX_CONSECUTIVE_FAILURES = 3  # évite de parcourir toute la playlist si rien ne se lit


def make_token(st: dict, pos: int) -> str:
    return "%s|%d|%s" % (st["playlist"], pos, st["order"][pos])


def parse_token(token: str) -> tuple:
    try:
        pl, pos, vid = token.split("|", 2)
        return pl, int(pos), vid
    except (ValueError, AttributeError):
        return None, -1, None


class Skill:
    def __init__(self, cfg: dict, yt: YouTube, state: State):
        self.cfg = cfg
        self.yt = yt
        self.state = state

    # -- réponses ----------------------------------------------------------

    @staticmethod
    def response(speech: str = None, end: bool = True, directives: list = None,
                 reprompt: str = None) -> dict:
        r: dict = {"shouldEndSession": end}
        if speech:
            r["outputSpeech"] = {"type": "PlainText", "text": speech}
        if reprompt:
            r["reprompt"] = {"outputSpeech": {"type": "PlainText", "text": reprompt}}
        if directives:
            r["directives"] = directives
        return {"version": "1.0", "response": r}

    def stream_url(self, video_id: str) -> str:
        sig = hmac.new(self.cfg["secret"].encode(), video_id.encode(), hashlib.sha256)
        return "%s/stream/%s?k=%s" % (self.cfg["public_base_url"], quote(video_id),
                                     sig.hexdigest()[:32])

    def play_directive(self, st: dict, pos: int, behavior: str = "REPLACE_ALL",
                       offset: int = 0, previous: str = None) -> dict:
        vid = st["order"][pos]
        stream = {"token": make_token(st, pos), "url": self.stream_url(vid),
                  "offsetInMilliseconds": int(offset)}
        if previous:
            stream["expectedPreviousToken"] = previous
        self.yt.prefetch(vid)
        return {"type": "AudioPlayer.Play", "playBehavior": behavior,
                "audioItem": {"stream": stream}}

    @staticmethod
    def stop_directive() -> dict:
        return {"type": "AudioPlayer.Stop"}

    def playlist_names(self) -> list:
        return [p["name"] for p in self.yt.playlists()]

    def names_sentence(self) -> str:
        names = self.playlist_names()
        if not names:
            return "Aucune playlist n'est configurée."
        if len(names) == 1:
            return "J'ai une playlist : " + names[0] + "."
        return "J'ai ces playlists : " + ", ".join(names[:-1]) + " et " + names[-1] + "."

    def next_pos(self, st: dict, pos: int) -> int:
        if pos + 1 < len(st["order"]):
            return pos + 1
        return 0 if st.get("loop") else -1

    # -- point d'entrée ----------------------------------------------------

    def handle(self, body: dict) -> dict:
        req = body.get("request") or {}
        rtype = req.get("type", "")
        ctx = body.get("context") or {}
        device = ((ctx.get("System") or {}).get("device") or {}).get("deviceId", "default")
        LOG.info("requête %s %s", rtype, (req.get("intent") or {}).get("name", ""))

        if rtype == "LaunchRequest":
            return self.response("Quelle playlist veux-tu écouter ? " + self.names_sentence(),
                                 end=False, reprompt="Dis par exemple : joue " +
                                 (self.playlist_names() or ["ma playlist"])[0] + ".")
        if rtype == "IntentRequest":
            return self.on_intent(req["intent"], device, ctx)
        if rtype.startswith("AudioPlayer."):
            resp = self.on_audio_event(rtype, req, device)
            resp["response"].pop("shouldEndSession", None)
            return resp
        if rtype.startswith("PlaybackController."):
            name = {"PlaybackController.NextCommandIssued": "AMAZON.NextIntent",
                    "PlaybackController.PreviousCommandIssued": "AMAZON.PreviousIntent",
                    "PlaybackController.PlayCommandIssued": "AMAZON.ResumeIntent",
                    "PlaybackController.PauseCommandIssued": "AMAZON.PauseIntent"}.get(rtype)
            if name:
                resp = self.on_intent({"name": name}, device, ctx)
                resp["response"].pop("outputSpeech", None)
                resp["response"].pop("shouldEndSession", None)
                return resp
            return self.response()
        if rtype == "System.ExceptionEncountered":
            LOG.warning("exception Alexa : %s", json.dumps(req.get("error")))
        return self.response()

    # -- intents -----------------------------------------------------------

    def on_intent(self, intent: dict, device: str, ctx: dict) -> dict:
        name = intent.get("name", "")
        st = self.state.get(device)
        audio = ctx.get("AudioPlayer") or {}
        if audio.get("token") and st:
            _, pos, _ = parse_token(audio["token"])
            if 0 <= pos < len(st.get("order", [])):
                st["pos"] = pos
                st["offset"] = audio.get("offsetInMilliseconds", st.get("offset", 0))

        if name == "PlayPlaylistIntent":
            slot = ((intent.get("slots") or {}).get("playlist") or {}).get("value") or ""
            return self.start_playlist(slot, device)
        if name == "ListPlaylistsIntent":
            return self.response(self.names_sentence() + " Laquelle veux-tu écouter ?",
                                 end=False, reprompt="Laquelle veux-tu écouter ?")
        if name in ("AMAZON.HelpIntent", "AMAZON.FallbackIntent"):
            prefix = "" if name == "AMAZON.HelpIntent" else "Je n'ai pas compris. "
            return self.response(prefix + "Dis par exemple : joue la playlist " +
                                 (self.playlist_names() or ["ma musique"])[0] + ". " +
                                 self.names_sentence(), end=False,
                                 reprompt="Quelle playlist veux-tu écouter ?")
        if name in ("AMAZON.PauseIntent", "AMAZON.StopIntent", "AMAZON.CancelIntent",
                    "AMAZON.NavigateHomeIntent"):
            if st:
                self.state.put(device, st)
            return self.response(directives=[self.stop_directive()])

        if not st or not st.get("order"):
            return self.response("Aucune lecture en cours. Demande-moi de jouer une playlist.")

        if name == "AMAZON.ResumeIntent":
            return self.response(directives=[self.play_directive(
                st, st.get("pos", 0), offset=st.get("offset", 0))])
        if name == "AMAZON.NextIntent":
            pos = self.next_pos(st, st.get("pos", 0))
            if pos < 0:
                return self.response("C'était le dernier titre de la playlist.",
                                     directives=[self.stop_directive()])
            return self.jump(st, device, pos)
        if name == "AMAZON.PreviousIntent":
            return self.jump(st, device, max(0, st.get("pos", 0) - 1))
        if name == "AMAZON.StartOverIntent":
            return self.jump(st, device, 0)
        if name == "AMAZON.RepeatIntent":
            return self.jump(st, device, st.get("pos", 0))
        if name == "AMAZON.LoopOnIntent":
            st["loop"] = True
            self.state.put(device, st)
            return self.response("La playlist sera répétée.")
        if name == "AMAZON.LoopOffIntent":
            st["loop"] = False
            self.state.put(device, st)
            return self.response("Répétition désactivée.")
        if name in ("AMAZON.ShuffleOnIntent", "AMAZON.ShuffleOffIntent"):
            return self.set_shuffle(st, device, name == "AMAZON.ShuffleOnIntent")
        return self.response("Je ne sais pas encore faire ça.")

    def start_playlist(self, query: str, device: str) -> dict:
        playlists = self.yt.playlists()
        if not playlists:
            return self.response("Aucune playlist n'est configurée sur le serveur.")
        idx, score = best_match(query, self.playlist_names())
        if idx < 0 or score < 0.5:
            return self.response("Je ne trouve pas la playlist " + query + ". " +
                                 self.names_sentence() + " Laquelle veux-tu ?",
                                 end=False, reprompt="Quelle playlist veux-tu écouter ?")
        p = playlists[idx]
        pl_id = p["id"]
        try:
            ids = self.yt.tracks(pl_id)
        except Exception as exc:  # noqa: BLE001
            LOG.error("lecture de la playlist %s impossible : %s", pl_id, exc)
            return self.response("Je n'arrive pas à lire la playlist " + p["name"] + ".")
        if not ids:
            return self.response("La playlist " + p["name"] + " est vide.")
        order = list(ids)
        shuffle = self.cfg["shuffle_by_default"] if p.get("shuffle") is None else p["shuffle"]
        shuffle = bool(shuffle)
        if shuffle:
            random.shuffle(order)
        st = {"playlist": pl_id, "name": p["name"], "order": order, "pos": 0, "failures": 0,
              "offset": 0, "shuffle": shuffle, "loop": bool(p.get("loop"))}
        self.state.put(device, st)
        if len(order) > 1:
            self.yt.prefetch(order[1])
        return self.response("Je lance " + p["name"] + ".",
                             directives=[self.play_directive(st, 0)])

    def jump(self, st: dict, device: str, pos: int) -> dict:
        st["pos"], st["offset"] = pos, 0
        self.state.put(device, st)
        return self.response(directives=[self.play_directive(st, pos)])

    def set_shuffle(self, st: dict, device: str, on: bool) -> dict:
        pos = st.get("pos", 0)
        current = st["order"][pos]
        played, rest = st["order"][:pos + 1], st["order"][pos + 1:]
        if on:
            random.shuffle(rest)
        else:
            try:
                original = self.yt.tracks(st["playlist"])
                rest = [v for v in original if v not in set(played)]
            except Exception:  # noqa: BLE001
                pass
        st["order"] = played + rest
        st["shuffle"] = on
        st["pos"] = st["order"].index(current)
        self.state.put(device, st)
        return self.response("Lecture aléatoire activée." if on else
                             "Lecture aléatoire désactivée.")

    # -- événements du lecteur audio ----------------------------------------

    def on_audio_event(self, rtype: str, req: dict, device: str) -> dict:
        st = self.state.get(device)
        token = req.get("token", "")
        pl, pos, _ = parse_token(token)
        if not st or pl != st.get("playlist") or not (0 <= pos < len(st.get("order", []))):
            return self.response()
        if rtype == "AudioPlayer.PlaybackStarted":
            st["pos"], st["offset"] = pos, req.get("offsetInMilliseconds", 0)
            st["failures"] = 0
            self.state.put(device, st)
            nxt = self.next_pos(st, pos)
            if nxt >= 0:
                self.yt.prefetch(st["order"][nxt])
        elif rtype == "AudioPlayer.PlaybackStopped":
            st["pos"], st["offset"] = pos, req.get("offsetInMilliseconds", 0)
            self.state.put(device, st)
        elif rtype == "AudioPlayer.PlaybackNearlyFinished":
            nxt = self.next_pos(st, pos)
            if nxt >= 0:
                return self.response(directives=[self.play_directive(
                    st, nxt, behavior="ENQUEUE", previous=token)])
        elif rtype == "AudioPlayer.PlaybackFailed":
            LOG.warning("échec de lecture %s : %s", token, json.dumps(req.get("error")))
            st["failures"] = st.get("failures", 0) + 1
            self.state.put(device, st)
            if st["failures"] >= MAX_CONSECUTIVE_FAILURES:
                LOG.error("%d échecs de suite, lecture arrêtée", st["failures"])
                return self.response()
            nxt = self.next_pos(st, pos)
            if nxt >= 0 and nxt != pos:
                st["pos"], st["offset"] = nxt, 0
                self.state.put(device, st)
                return self.response(directives=[self.play_directive(st, nxt)])
        return self.response()


# ---------------------------------------------------------------------------
# Serveur HTTP
# ---------------------------------------------------------------------------

UPSTREAM_CHUNK = 4 * 1024 * 1024  # YouTube limite les grosses requêtes : on découpe
COPY_BUFFER = 64 * 1024


class Handler(BaseHTTPRequestHandler):
    server_version = "AlexaYT/1.0"
    protocol_version = "HTTP/1.1"
    app = None  # type: App

    def log_message(self, fmt, *args):  # noqa: D401
        LOG.debug("%s - %s", self.address_string(), fmt % args)

    def send_json(self, code: int, obj: dict) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json;charset=UTF-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_empty(self, code: int) -> None:
        self.send_response(code)
        self.send_header("Content-Length", "0")
        self.end_headers()

    # -- routes ------------------------------------------------------------

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/health":
            return self.send_json(200, {"ok": True})
        if path.startswith("/stream/"):
            return self.stream(head=False)
        return self.send_empty(404)

    def do_HEAD(self):
        if urlparse(self.path).path.startswith("/stream/"):
            return self.stream(head=True)
        return self.send_empty(404)

    def do_POST(self):
        if urlparse(self.path).path.rstrip("/") != "/alexa":
            return self.send_empty(404)
        try:
            body = json.loads(self.read_body() or b"{}")
        except ValueError:
            LOG.warning("corps de requête illisible")
            return self.send_empty(400)
        err = self.app.check_request(body)
        if err:
            LOG.warning("requête refusée : %s", err)
            return self.send_empty(400)
        try:
            resp = self.app.skill.handle(body)
        except Exception:  # noqa: BLE001
            LOG.exception("erreur dans la skill")
            resp = Skill.response("Désolé, une erreur est survenue sur le serveur.")
        return self.send_json(200, resp)

    def read_body(self) -> bytes:
        """Lit le corps, avec Content-Length ou en « Transfer-Encoding: chunked »."""
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            data = b""
            while True:
                size = int(self.rfile.readline().split(b";")[0].strip() or b"0", 16)
                if size == 0:
                    while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                        pass
                    return data
                data += self.rfile.read(size)
                self.rfile.readline()
        return self.rfile.read(int(self.headers.get("Content-Length") or 0))

    # -- proxy audio -------------------------------------------------------

    def stream(self, head: bool) -> None:
        parsed = urlparse(self.path)
        video_id = parsed.path[len("/stream/"):]
        key = (parse_qs(parsed.query).get("k") or [""])[0]
        expected = hmac.new(self.app.cfg["secret"].encode(), video_id.encode(),
                            hashlib.sha256).hexdigest()[:32]
        if not re.fullmatch(r"[A-Za-z0-9_-]{6,20}", video_id) or \
                not hmac.compare_digest(key, expected):
            return self.send_empty(403)
        try:
            info = self.app.yt.stream_info(video_id)
            size = info["size"] or self.upstream_size(info)
        except Exception as exc:  # noqa: BLE001
            LOG.error("flux %s indisponible : %s", video_id, exc)
            return self.send_empty(502)

        start, end = 0, size - 1
        rng = self.headers.get("Range")
        partial = False
        if rng:
            m = re.fullmatch(r"bytes=(\d*)-(\d*)", rng.strip())
            if not m or (not m.group(1) and not m.group(2)):
                return self.send_empty(416)
            if m.group(1):
                start = int(m.group(1))
                if m.group(2):
                    end = min(int(m.group(2)), size - 1)
            else:
                start = max(0, size - int(m.group(2)))
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", "bytes */%d" % size)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            partial = True

        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", info["content_type"])
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if partial:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.end_headers()
        if head:
            return
        try:
            self.copy_upstream(video_id, info, start, end)
        except (BrokenPipeError, ConnectionResetError):
            LOG.debug("client déconnecté pendant %s", video_id)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("flux %s interrompu : %s", video_id, exc)
            self.close_connection = True

    def upstream_request(self, info: dict, start: int, end: int):
        headers = dict(info["headers"])
        headers["Range"] = "bytes=%d-%d" % (start, end)
        req = urllib.request.Request(info["url"], headers=headers)
        return urllib.request.urlopen(req, timeout=30)

    def upstream_size(self, info: dict) -> int:
        with self.upstream_request(info, 0, 0) as r:
            cr = r.headers.get("Content-Range", "")
        m = re.search(r"/(\d+)$", cr)
        if not m:
            raise RuntimeError("taille du flux inconnue")
        info["size"] = int(m.group(1))
        return info["size"]

    def copy_upstream(self, video_id: str, info: dict, start: int, end: int) -> None:
        pos, retried = start, False
        while pos <= end:
            stop = min(pos + UPSTREAM_CHUNK - 1, end)
            try:
                resp = self.upstream_request(info, pos, stop)
            except urllib.error.HTTPError as exc:
                if exc.code in (403, 410) and not retried:
                    LOG.info("URL expirée pour %s, nouvelle résolution", video_id)
                    info = self.app.yt.stream_info(video_id, force=True)
                    retried = True
                    continue
                raise
            with resp:
                while True:
                    buf = resp.read(COPY_BUFFER)
                    if not buf:
                        break
                    self.wfile.write(buf)
                    pos += len(buf)
            if pos <= stop:
                raise RuntimeError("réponse YouTube incomplète")


class App:
    def __init__(self, cfg: dict, yt: YouTube = None):
        self.cfg = cfg
        self.yt = yt or YouTube(cfg, Google(cfg))
        self.state = State(cfg["state_file"])
        self.skill = Skill(cfg, self.yt, self.state)

    def check_request(self, body: dict) -> str:
        """Vérifie que la requête vient bien de notre skill et est récente."""
        app_id = (((body.get("context") or {}).get("System") or {})
                  .get("application") or {}).get("applicationId") or \
            (((body.get("session") or {}).get("application") or {}).get("applicationId"))
        if self.cfg["skill_id"] and app_id != self.cfg["skill_id"]:
            return "skill inconnue : %s" % app_id
        ts = (body.get("request") or {}).get("timestamp")
        if not ts:
            return "horodatage absent"
        try:
            when = datetime.strptime(ts.replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S%z")
        except ValueError:
            return "horodatage invalide"
        if abs((datetime.now(timezone.utc) - when).total_seconds()) > 150:
            return "requête trop ancienne"
        return ""

    def start_refresher(self) -> None:
        def loop():
            while True:
                self.yt.refresh_all()
                time.sleep(max(5, self.cfg["playlist_refresh_minutes"]) * 60)
        threading.Thread(target=loop, daemon=True).start()

    def serve(self) -> None:
        Handler.app = self
        httpd = ThreadingHTTPServer((self.cfg["bind"], int(self.cfg["port"])), Handler)
        httpd.daemon_threads = True
        LOG.info("Alexa-YT écoute sur %s:%s", self.cfg["bind"], self.cfg["port"])
        httpd.serve_forever()


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(message)s")
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    path = args[0] if args else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "config.json")
    cfg = load_config(path)
    if "--login" in sys.argv:
        google = Google(cfg)
        google.login()
        for p in google.my_playlists():
            print("  - %s" % p["name"])
        return
    if not cfg["skill_id"]:
        LOG.warning("skill_id vide : toutes les skills Alexa sont acceptées")
    app = App(cfg)
    app.start_refresher()
    app.serve()


if __name__ == "__main__":
    main()
