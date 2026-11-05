#!/usr/bin/env python3
"""Feed TMDB custom-list movies and episodes into rex; playback uses Stremio stream addons."""
from __future__ import annotations

import json
import mimetypes
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
TMDB = "https://api.themoviedb.org/4"
VIDEO_EXTS = {".mkv", ".mp4", ".m4v", ".ts", ".avi", ".webm", ".mov", ".m3u8"}


def request_json(url: str, headers: dict[str, str] | None = None) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "rex-tmdb-lists/0.1", **(headers or {})})
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.loads(response.read())


class Addon:
    def __init__(self, config: dict):
        self.cfg = config
        self.token = config["tmdb_token"].strip()
        self.cache = ROOT / config.get("cache_dir", "cache")
        self.cache.mkdir(parents=True, exist_ok=True)
        self.items: dict[str, dict] = {}
        self.lock = threading.RLock()

    def tmdb(self, path: str, **params) -> dict:
        params.setdefault("language", self.cfg.get("tmdb_language", "en-US"))
        query = urllib.parse.urlencode(params)
        return request_json(f"{TMDB}/{path}?{query}", {"Authorization": f"Bearer {self.token}"})

    def tmdb_v3(self, path: str, **params) -> dict:
        params.setdefault("language", self.cfg.get("tmdb_language", "en-US"))
        query = urllib.parse.urlencode(params)
        return request_json(f"https://api.themoviedb.org/3/{path}?{query}", {"Authorization": f"Bearer {self.token}"})

    def list_entries(self, list_id: int) -> list[dict]:
        first = self.tmdb(f"list/{list_id}", page=1)
        entries = list(first.get("results", []))
        for page in range(2, int(first.get("total_pages", 1)) + 1):
            entries.extend(self.tmdb(f"list/{list_id}", page=page).get("results", []))
        return entries

    def imdb_id(self, media_type: str, tmdb_id: int) -> str | None:
        try:
            value = self.tmdb_v3(f"{media_type}/{tmdb_id}/external_ids").get("imdb_id")
            return value if value and re.fullmatch(r"tt\d+", value) else None
        except Exception as exc:
            print(f"TMDB external ID lookup failed for {media_type}/{tmdb_id}: {exc}")
            return None

    def probe(self, url: str, ext: str, headers: dict[str, str] | None = None) -> dict | None:
        key = __import__("hashlib").sha256(url.encode()).hexdigest() + ".json"
        path = self.cache / key
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                path.unlink(missing_ok=True)
        args = [self.cfg.get("ffprobe", "ffprobe"), "-v", "error", "-show_format", "-show_streams", "-of", "json"]
        if headers:
            args += ["-headers", "".join(f"{k}: {v}\r\n" for k, v in headers.items())]
        args += [url]
        try:
            result = subprocess.run(args, capture_output=True, text=True, timeout=45, check=True)
            info = json.loads(result.stdout)
            if not info.get("streams"):
                return None
            if ext == ".m3u8":
                info.setdefault("format", {})["format_name"] = "hls"
            path.write_text(json.dumps(info), encoding="utf-8")
            return info
        except Exception as exc:
            print(f"ffprobe failed for {url}: {exc}")
            return None

    def streams(self, imdb: str, kind: str, season: int | None = None, episode: int | None = None) -> list[dict]:
        path = f"stream/{kind}/{imdb}"
        if season is not None:
            path += f":{season}:{episode}"
        found = []
        for manifest_url in self.cfg.get("stream_addons", []):
            base = manifest_url.rsplit("/manifest.json", 1)[0].rstrip("/")
            try:
                data = request_json(f"{base}/{path}.json")
                found.extend(data.get("streams") or [])
            except Exception as exc:
                print(f"Stream addon failed ({base}): {exc}")
        return found

    def pick_stream(self, imdb: str, kind: str, season: int | None = None, episode: int | None = None):
        for stream in self.streams(imdb, kind, season, episode):
            url = stream.get("url")
            if not url or urllib.parse.urlsplit(url).scheme not in {"http", "https"}:
                continue
            ext = Path(urllib.parse.urlsplit(url).path).suffix.lower()
            if ext not in VIDEO_EXTS:
                ext = ".m3u8" if "m3u8" in url.lower() else ".mkv"
            headers = (stream.get("behaviorHints") or {}).get("proxyHeaders", {}).get("request") or {}
            info = self.probe(url, ext, headers)
            if info:
                return url, ext, info, headers
        return None

    def make_item(self, entry: dict, list_name: str, order: int) -> list[dict]:
        media_type = entry.get("media_type")
        tmdb_id = entry.get("id")
        if media_type not in {"movie", "tv"} or not tmdb_id:
            return []
        imdb = self.imdb_id(media_type, tmdb_id)
        if not imdb:
            return []
        records = []
        if media_type == "movie":
            resolved = self.pick_stream(imdb, "movie")
            if not resolved:
                return []
            url, ext, info, headers = resolved
            record_id = f"tmdb-list-{tmdb_id}"
            path = f"tmdb-movies/{record_id}{ext}"
            display = self.display(entry)
            records.append({"id": record_id, "path": path, "kind": "movie", "ids": {"imdb": imdb}, "media_info": info, "display": display, "_play": {"url": url, "headers": headers}})
            return records

        show = self.tmdb_v3(f"tv/{tmdb_id}")
        for season_data in show.get("seasons", []):
            sn = int(season_data.get("season_number", 0))
            if sn == 0:
                continue
            try:
                season_data_full = self.tmdb_v3(f"tv/{tmdb_id}/season/{sn}")
            except Exception as exc:
                print(f"Season lookup failed for {tmdb_id}/{sn}: {exc}")
                continue
            for ep in season_data_full.get("episodes", []):
                en = int(ep.get("episode_number", 0))
                if en < 1:
                    continue
                resolved = self.pick_stream(imdb, "series", sn, en)
                if not resolved:
                    continue
                url, ext, info, headers = resolved
                record_id = f"tmdb-list-{tmdb_id}-s{sn:02d}e{en:02d}"
                ep_display = self.display({**ep, "name": ep.get("name"), "overview": ep.get("overview"), "poster_path": ep.get("still_path")})
                ep_display["show"] = {"title": show.get("name", entry.get("name", "Unknown Show")), "summary": show.get("overview", ""), "poster": self.image(show.get("poster_path")), "art": self.image(show.get("backdrop_path"))}
                ep_display["season"] = {"poster": self.image(season_data.get("poster_path"))}
                records.append({"id": record_id, "path": f"tmdb-shows/{tmdb_id}/s{sn:02d}e{en:02d}{ext}", "kind": "episode", "name": show.get("name", entry.get("name", "Unknown Show")), "season": sn, "episode": en, "ids": {"imdb": imdb}, "media_info": info, "display": ep_display, "_play": {"url": url, "headers": headers}})
        return records

    @staticmethod
    def image(path: str | None, size: str = "w500") -> str | None:
        return f"https://image.tmdb.org/t/p/{size}{path}" if path else None

    def display(self, entry: dict) -> dict:
        date = entry.get("release_date") or entry.get("first_air_date")
        out = {"title": entry.get("title") or entry.get("name") or "Untitled", "summary": entry.get("overview") or "", "thumb": self.image(entry.get("poster_path")), "art": self.image(entry.get("backdrop_path"))}
        if date:
            out["date"] = date[:10]
        return out

    def sync(self):
        print("Starting TMDB list sync")
        new_items: dict[str, dict] = {}
        try:
            for spec in self.cfg.get("lists", []):
                entries = self.list_entries(int(spec["id"]))
                if self.cfg.get("item_limit", 0):
                    entries = entries[:int(self.cfg["item_limit"])]
                for order, entry in enumerate(entries):
                    for item in self.make_item(entry, spec.get("name", "TMDB list"), order):
                        item.pop("_play", None)
                        new_items[item["id"]] = item
            self.register()
            self.post_snapshot(list(new_items.values()))
            with self.lock:
                self.items = new_items
            print(f"Sync complete: {len(new_items)} playable items")
        except Exception as exc:
            print(f"Sync failed; rex snapshot was not changed: {exc}")

    def register(self):
        source = urllib.parse.quote(self.cfg.get("source", "tmdb-lists"), safe="")
        body = {"url": self.cfg.get("relay_url", "http://127.0.0.1:9983"), "directories": [{"name": "tmdb-movies", "movies": "TMDB Lists Movies"}, {"name": "tmdb-shows", "shows": "TMDB Lists Shows"}]}
        req = urllib.request.Request(f"{self.cfg['rex_url'].rstrip('/')}/rex/sources/{source}", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="PUT")
        with urllib.request.urlopen(req, timeout=20) as response:
            response.read()

    def post_snapshot(self, items: list[dict]):
        source = urllib.parse.quote(self.cfg.get("source", "tmdb-lists"), safe="")
        url = f"{self.cfg['rex_url'].rstrip('/')}/rex/feed/{source}/snapshot"
        # Use a complete one-part snapshot. rex protects against accidental mass deletion.
        session = str(int(time.time()))
        pages = [items[i:i + 200] for i in range(0, len(items), 200)] or [[]]
        for part, page in enumerate(pages):
            payload = {"items": page, "session": session, "part": part, "complete": part == len(pages) - 1}
            req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="PUT")
            with urllib.request.urlopen(req, timeout=60) as response:
                result = response.read().decode()
                print(f"rex snapshot part {part + 1}/{len(pages)}: {result}")


def main():
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    if not config.get("tmdb_token"):
        raise SystemExit("Set tmdb_token in config.json")
    addon = Addon(config)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            print("HTTP", fmt % args)

        def do_GET(self):
            if self.path == "/health":
                return self.send_json(200, {"ok": True, "items": len(addon.items)})
            if self.path == "/sync":
                threading.Thread(target=addon.sync, daemon=True).start()
                return self.send_json(202, {"sync": "started"})
            item = addon.items.get(Path(urllib.parse.urlsplit(self.path).path).name.rsplit(".", 1)[0])
            # Paths contain nested folders, so use the suffix-stripped relative path lookup.
            req_path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path).lstrip("/")
            with addon.lock:
                item = next((x for x in addon.items.values() if x["path"] == req_path), item)
            if not item:
                return self.send_json(404, {"error": "unknown media path"})
            # Resolve a fresh signed URL at play time using the stable IMDb and episode identity.
            imdb = item.get("ids", {}).get("imdb")
            if item.get("kind") == "episode":
                match = re.search(r"/s(\d+)e(\d+)\.", item["path"])
                resolved = addon.pick_stream(imdb, "series", int(match.group(1)), int(match.group(2))) if match else None
            else:
                resolved = addon.pick_stream(imdb, "movie")
            if not resolved:
                return self.send_json(502, {"error": "no playable stream"})
            url, _, _, headers = resolved
            if headers:
                return self.relay(url, headers)
            self.send_response(302)
            self.send_header("Location", url)
            self.end_headers()

        def send_json(self, code, data):
            raw = json.dumps(data).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def relay(self, url, headers):
            forwarded = dict(headers)
            if self.headers.get("Range"):
                forwarded["Range"] = self.headers["Range"]
            req = urllib.request.Request(url, headers=forwarded)
            try:
                upstream = urllib.request.urlopen(req, timeout=60)
                self.send_response(upstream.status)
                for key in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"):
                    if upstream.headers.get(key):
                        self.send_header(key, upstream.headers[key])
                self.end_headers()
                while True:
                    chunk = upstream.read(256 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            except Exception as exc:
                print(f"Relay failed: {exc}")
                if not self.wfile.closed:
                    try:
                        self.send_error(502)
                    except Exception:
                        pass

    host, port = config.get("listen", "127.0.0.1:9983").rsplit(":", 1)
    server = ThreadingHTTPServer((host, int(port)), Handler)
    threading.Thread(target=addon.sync, daemon=True).start()
    interval = max(1, int(config.get("interval_hours", 6))) * 3600
    threading.Thread(target=lambda: periodic(addon, interval), daemon=True).start()
    print(f"rex-tmdb-lists listening on {host}:{port}")
    server.serve_forever()


def periodic(addon: Addon, interval: int):
    while True:
        time.sleep(interval)
        addon.sync()


if __name__ == "__main__":
    main()
