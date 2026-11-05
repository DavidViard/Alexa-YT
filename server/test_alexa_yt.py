"""Tests sans réseau : yt-dlp et YouTube sont simulés.

Lancer : python3 -m unittest -v test_alexa_yt
"""
from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import threading
import unittest
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import alexa_yt  # noqa: E402

AUDIO = bytes(range(256)) * 4000  # ~1 Mo de faux audio
SKILL_ID = "amzn1.ask.skill.test"


class FakeUpstream(BaseHTTPRequestHandler):
    """Imite googlevideo : n'accepte que des requêtes avec Range."""
    protocol_version = "HTTP/1.1"
    requests = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        rng = self.headers.get("Range")
        FakeUpstream.requests.append(rng)
        start, end = [int(x) for x in rng.split("=")[1].split("-")]
        end = min(end, len(AUDIO) - 1)
        data = AUDIO[start:end + 1]
        self.send_response(206)
        self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, len(AUDIO)))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class FakeGoogle:
    configured = connected = True

    def my_playlists(self):
        return [{"name": "Musique du Matin", "id": "PLmatin"},
                {"name": "Rock Années 80", "id": "PLrock"}]

    def playlist_items(self, pid):
        return {"PLmatin": ["vidAAAAAAA1", "vidBBBBBBB2", "vidCCCCCCC3"],
                "PLrock": ["vidRRRRRRR1"]}[pid]


def alexa(rtype, intent=None, slots=None, token=None, extra=None):
    req = {"type": rtype, "requestId": "r", "locale": "fr-FR",
           "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    if intent:
        req["intent"] = {"name": intent,
                         "slots": {k: {"name": k, "value": v} for k, v in (slots or {}).items()}}
    if token:
        req["token"] = token
    req.update(extra or {})
    return {"version": "1.0",
            "context": {"System": {"application": {"applicationId": SKILL_ID},
                                   "device": {"deviceId": "echo1"}}},
            "request": req}


class PlaylistItemsTest(unittest.TestCase):
    def test_private_and_deleted_videos_are_skipped(self):
        g = alexa_yt.Google({"google_client_id": "", "google_client_secret": "",
                             "token_file": "/nonexistent"})
        g._paged = lambda endpoint, params: [
            {"contentDetails": {"videoId": "ok1"}, "status": {"privacyStatus": "public"},
             "snippet": {"title": "Titre"}},
            {"contentDetails": {"videoId": "priv"}, "status": {"privacyStatus": "private"},
             "snippet": {"title": "Private video"}},
            {"contentDetails": {"videoId": "del"}, "status": {"privacyStatus": "public"},
             "snippet": {"title": "Deleted video"}},
            {"contentDetails": {"videoId": "ok2"}, "status": {"privacyStatus": "unlisted"},
             "snippet": {"title": "Autre"}},
        ]
        self.assertEqual(g.playlist_items("PL"), ["ok1", "ok2"])


class GateTest(unittest.TestCase):
    def test_urgent_request_preempts_background(self):
        tmp = tempfile.mkdtemp()
        fake = os.path.join(tmp, "yt-dlp")
        with open(fake, "w") as f:
            f.write("#!%s\nimport json,sys,time\n"
                    "vid=sys.argv[-1][-11:]\n"
                    "time.sleep(30 if vid.startswith('slow') else 0)\n"
                    "print(json.dumps({'url':'http://x/?expire=9999999999','ext':'m4a',"
                    "'title':vid}))\n" % sys.executable)
        os.chmod(fake, 0o755)
        yt = alexa_yt.YouTube(dict(alexa_yt.DEFAULTS, ytdlp_path=fake))
        yt.prefetch("slowBBBBBBB", urgent=False)
        import time
        time.sleep(0.5)
        start = time.time()
        info = yt.stream_info("fastAAAAAAA")
        self.assertEqual(info["title"], "fastAAAAAAA")
        self.assertLess(time.time() - start, 10)


class AlexaYTTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.up = ThreadingHTTPServer(("127.0.0.1", 0), FakeUpstream)
        threading.Thread(target=cls.up.serve_forever, daemon=True).start()
        upstream = "http://127.0.0.1:%d/videoplayback?expire=9999999999" % cls.up.server_port
        fake = os.path.join(cls.tmp, "yt-dlp")
        with open(fake, "w") as f:
            f.write("#!%s\nimport json,sys\nprint(json.dumps({'url': %r, 'ext': 'm4a', "
                    "'title': sys.argv[-1][-11:], 'http_headers': {'User-Agent': 'x'}}))\n"
                    % (sys.executable, upstream))
        os.chmod(fake, os.stat(fake).st_mode | stat.S_IEXEC)
        cfgfile = os.path.join(cls.tmp, "config.json")
        with open(cfgfile, "w") as f:
            json.dump({"skill_id": SKILL_ID, "public_base_url": "https://nas.example",
                       "secret": "s3cret", "ytdlp_path": fake, "port": 0,
                       "playlists": [{"name": "Chill", "id":
                                      "https://www.youtube.com/playlist?list=PLchill"}]}, f)
        cls.cfg = alexa_yt.load_config(cfgfile)
        cls.www = os.path.join(cls.tmp, "site")
        os.mkdir(cls.www)
        os.mkdir(os.path.join(cls.www, "sous"))
        with open(os.path.join(cls.www, "index.html"), "w") as f:
            f.write("<h1>Accueil</h1>")
        with open(os.path.join(cls.www, "sous", "index.html"), "w") as f:
            f.write("sous")
        with open(os.path.join(cls.tmp, "secret.txt"), "w") as f:
            f.write("ne pas servir")
        cls.cfg["www_dir"] = cls.www
        yt = alexa_yt.YouTube(cls.cfg, FakeGoogle())
        yt.refresh_account_playlists()
        cls.app = alexa_yt.App(cls.cfg, yt)
        alexa_yt.Handler.app = cls.app
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), alexa_yt.Handler)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.base = "http://127.0.0.1:%d" % cls.httpd.server_port

    def post(self, body):
        req = urllib.request.Request(self.base + "/alexa", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())

    def play(self, name):
        return self.post(alexa("IntentRequest", "PlayPlaylistIntent", {"playlist": name}))

    def test_match(self):
        names = ["Musique du Matin", "Rock Années 80", "Chill"]
        self.assertEqual(alexa_yt.best_match("la playlist rock années quatre-vingt", names)[0], 1)
        self.assertEqual(alexa_yt.best_match("musique du matin", names)[0], 0)
        self.assertEqual(alexa_yt.best_match("ma playlist chill", names)[0], 2)

    def test_list_includes_config_and_account(self):
        names = [p["name"] for p in self.app.yt.playlists()]
        self.assertEqual(names, ["Chill", "Musique du Matin", "Rock Années 80"])
        self.assertEqual(self.app.yt.playlists()[0]["id"], "PLchill")

    def test_play_next_and_enqueue(self):
        r = self.play("musique du matin")["response"]
        self.assertIn("Musique du Matin", r["outputSpeech"]["text"])
        d = r["directives"][0]
        self.assertEqual(d["playBehavior"], "REPLACE_ALL")
        token = d["audioItem"]["stream"]["token"]
        self.assertEqual(token, "PLmatin|0|vidAAAAAAA1")
        self.assertTrue(d["audioItem"]["stream"]["url"].startswith(
            "https://nas.example/stream/vidAAAAAAA1?k="))
        r = self.post(alexa("AudioPlayer.PlaybackNearlyFinished", token=token))["response"]
        d = r["directives"][0]
        self.assertEqual(d["playBehavior"], "ENQUEUE")
        self.assertEqual(d["audioItem"]["stream"]["expectedPreviousToken"], token)
        self.assertNotIn("shouldEndSession", r)
        r = self.post(alexa("IntentRequest", "AMAZON.NextIntent"))["response"]
        self.assertEqual(r["directives"][0]["audioItem"]["stream"]["token"],
                         "PLmatin|1|vidBBBBBBB2")
        r = self.post(alexa("IntentRequest", "AMAZON.PauseIntent"))["response"]
        self.assertEqual(r["directives"][0]["type"], "AudioPlayer.Stop")

    def test_chunked_body(self):
        import http.client
        body = json.dumps(alexa("LaunchRequest")).encode()
        conn = http.client.HTTPConnection("127.0.0.1", self.httpd.server_port)
        conn.request("POST", "/alexa", body=iter([body[:50], body[50:]]),
                     headers={"Content-Type": "application/json"}, encode_chunked=True)
        r = conn.getresponse()
        self.assertEqual(r.status, 200)
        self.assertIn("Quelle playlist", json.loads(r.read())["response"]["outputSpeech"]["text"])

    def test_stops_after_repeated_failures(self):
        tok = self.play("musique du matin")["response"]["directives"][0]["audioItem"]["stream"]["token"]
        r = self.post(alexa("AudioPlayer.PlaybackFailed", token=tok))["response"]
        tok = r["directives"][0]["audioItem"]["stream"]["token"]
        r = self.post(alexa("AudioPlayer.PlaybackFailed", token=tok))["response"]
        tok = r["directives"][0]["audioItem"]["stream"]["token"]
        r = self.post(alexa("AudioPlayer.PlaybackFailed", token=tok))["response"]
        self.assertNotIn("directives", r)

    def test_unknown_playlist_reprompts(self):
        r = self.play("jazz manouche")["response"]
        self.assertFalse(r["shouldEndSession"])
        self.assertIn("Je ne trouve pas", r["outputSpeech"]["text"])

    def test_rejects_other_skill_and_old_requests(self):
        body = alexa("LaunchRequest")
        body["context"]["System"]["application"]["applicationId"] = "autre"
        with self.assertRaises(urllib.error.HTTPError):
            self.post(body)
        body = alexa("LaunchRequest", extra={"timestamp": "2020-01-01T00:00:00Z"})
        with self.assertRaises(urllib.error.HTTPError):
            self.post(body)

    def test_stream_full_and_range(self):
        url = self.app.skill.stream_url("vidAAAAAAA1").replace("https://nas.example", self.base)
        alexa_yt.UPSTREAM_CHUNK = 300000  # force plusieurs requêtes vers YouTube
        with urllib.request.urlopen(url) as r:
            self.assertEqual(r.status, 200)
            self.assertEqual(r.headers["Content-Type"], "audio/mp4")
            self.assertEqual(r.read(), AUDIO)
        req = urllib.request.Request(url, headers={"Range": "bytes=1000-1999"})
        with urllib.request.urlopen(req) as r:
            self.assertEqual(r.status, 206)
            self.assertEqual(r.headers["Content-Range"], "bytes 1000-1999/%d" % len(AUDIO))
            self.assertEqual(r.read(), AUDIO[1000:2000])

    def test_static_site(self):
        with urllib.request.urlopen(self.base + "/") as r:
            self.assertEqual(r.read(), b"<h1>Accueil</h1>")
            self.assertTrue(r.headers["Content-Type"].startswith("text/html"))
        with urllib.request.urlopen(self.base + "/sous") as r:  # suit la redirection 301
            self.assertEqual(r.read(), b"sous")
        import http.client
        for path in ("/../secret.txt", "/%2e%2e/secret.txt", "/absent.html"):
            conn = http.client.HTTPConnection("127.0.0.1", self.httpd.server_port)
            conn.request("GET", path)
            r = conn.getresponse()
            self.assertEqual(r.status, 404, path)
            r.read()
        self.cfg["www_dir"] = ""
        try:
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(self.base + "/")
            self.assertEqual(cm.exception.code, 404)
        finally:
            self.cfg["www_dir"] = self.www

    def test_stream_requires_signature(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(self.base + "/stream/vidAAAAAAA1?k=bad")
        self.assertEqual(cm.exception.code, 403)


if __name__ == "__main__":
    unittest.main()
