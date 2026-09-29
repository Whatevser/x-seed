#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
orphan_xseed.py - find files no qBittorrent torrent points at, hunt matching
torrents through Prowlarr, grab them and add them to qBittorrent on top of the
files you already have (stopped, hash check skipped, renamed onto your files).

  1  scan      folders vs qBittorrent (hardlink / inode aware), grouped into
               releases: a movie folder with its screens/nfo, an album with its
               CDs, a series pack with its season folders (also nested ones),
               a collection with each of its movies - never file by file
               -> orphans.txt, orphan_extras.txt, orphan_files.txt, orphans.json
  2  search    Prowlarr with several name variants per release AND per season
               folder / sub-release / movie of a collection; a result needs the
               same title, the size (exact, the indexer's rounding, or within
               0.1%) and no CONFLICT on group, codec, source, resolution,
               service, edition, year or season/episode
               -> candidates.txt, candidates.json, near_misses.txt
  3  download  .torrent files (approve each / all), verify file tree against
               your files and spot-check real piece hashes
               -> torrents/*.torrent, downloads.txt, downloads.json
  4  add       verified torrents to qBittorrent, stopped. Single-file torrents
               and folders that match yours point straight at your files
               (renamed if needed); a folder torrent over a single file of
               yours, or one with extras you don't have, sits in a hardlink
               folder on the same disk (<disk>/hardlinked/xseed)
               -> added.txt, added.json
  5  near      near misses from step 2 - same title, but a label differs
               (mislabeled upload) or the size is off by extras: try them one
               by one / all / by number - download, check pieces, add the ones
               that really are your data

Python 3.8+, standard library only. Every HTTP call and decision is written to
<work_dir>/logs/*.log; run with --debug to also see it live.

Usage:  ./orphan_xseed.py                  interactive menu
        ./orphan_xseed.py 1 2              run steps 1 then 2
        ./orphan_xseed.py 3 --yes          download everything without asking
        ./orphan_xseed.py t                test qBittorrent + Prowlarr connections
        -c /path/to/orphan_xseed.json      config (default: next to the script)
"""
import argparse
import base64
import copy
import datetime as dt
import difflib
import hashlib
import http.cookiejar
import io
import json
import os
import re
import shutil
import socket
import ssl
import sys
import threading
import time
import traceback
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

VERSION = "2.1.0"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG_NAME = "orphan_xseed.json"

DEFAULT_CONFIG = {
    "_help": "orphan_xseed.py config. Keys starting with '_' are comments and are ignored. "
             "All paths are as seen from the machine that runs the script.",
    "work_dir": "./orphan_xseed_data",
    "_work_dir_help": "Where lists, .torrent files, cache and logs go. Relative = relative to this config file.",
    "debug": False,
    "qbittorrent": {
        "url": "http://192.168.10.2:8080/",
        "username": "Roman",
        "password": "",
        "api_key": "",
        "_api_key_help": "qBittorrent 5.2+ only (Options > WebUI > API key, starts with qbt_). If set it is used "
                         "instead of username/password. Leave empty to log in normally.",
        "timeout_sec": 180,
        "verify_ssl": False,
    },
    "prowlarr": {
        "url": "http://192.168.10.2:9696/",
        "username": "roman",
        "password": "",
        "api_key": "",
        "_api_key_help": "Leave empty: the script logs in with username/password and reads the key from "
                         "/initialize.json. Or paste it from Prowlarr > Settings > General > API Key.",
        "indexer_ids": [-2],
        "_indexer_ids_help": "-2 = all torrent indexers. Or list Prowlarr indexer ids, e.g. [1, 4, 7] "
                             "(menu option t prints them).",
        "categories": [],
        "_categories_help": "Newznab category ids to restrict searches, e.g. [2000, 5000]. Empty = all.",
        "search_limit": 100,
        "search_timeout_sec": 180,
        "delay_between_searches_sec": 2,
        "cache_hours": 24,
        "_cache_hours_help": "Identical searches are answered from search_cache.json for this long, so "
                             "re-running step 2 after tweaking matching settings does not hammer indexers. 0 = off.",
    },
    "scan": {
        "paths": [
            "/media/roman/hdd16/Torrent/Download/",
            "/media/roman/DiskQ/Torrent/Download/",
            "/media/roman/DiskM/Torrent/Download/",
            "/media/roman/DiskL/Torrent/Download/",
        ],
        "path_mappings": [],
        "_path_mappings_help": "Only needed if qBittorrent sees the files under different paths than this machine "
                               "(docker, NFS...). Example: [{\"qbit\": \"/downloads\", \"local\": "
                               "\"/media/roman/hdd16/Torrent/Download\"}]",
        "ignore_extensions": [".!qb", ".part", ".parts", ".tmp", ".crdownload"],
        "ignore_names": ["Thumbs.db", ".DS_Store", "desktop.ini"],
        "ignore_dirs": [".Trash-1000", "lost+found", "@eaDir", ".stfolder", ".stversions", "$RECYCLE.BIN"],
        "container_dir_names": [],
        "_container_dir_names_help": "Folder names that only hold other releases (e.g. \"radarr\", \"tv-sonarr\"). "
                                     "Folders used as torrent save paths / category paths are detected automatically.",
        "auto_container_names": True,
        "_auto_container_names_help": "Treat folders named like categories (Movies, Shows, TV, Music Videos, radarr, "
                                      "sonarr...) and folders that only hold release folders as containers.",
        "min_orphan_content_mb": 20,
        "_min_orphan_content_mb_help": "A release counts as orphan only if at least this much real content "
                                       "(video/audio/other, not nfo/srt/jpg/samples) is unseeded. Releases with only "
                                       "extras unseeded go to orphan_extras.txt.",
        "min_file_size_mb": 0,
        "name_size_fallback": True,
        "_name_size_fallback_help": "If a torrent's file can't be found on disk from here, a local file with the same "
                                    "file name + size counts as belonging to that torrent (listed in weak_matches.txt).",
        "follow_symlinks": False,
    },
    "search": {
        "variants": ["title_year", "title_group", "alt", "dotted", "no_spaces", "title", "clean", "raw",
                     "title_year_res"],
        "_variants_help": "Query builders for a release, tried in this order until max_queries_per_item. title_year: "
                          "'Title 2020' / 'Show S01'; title_group: 'Title TiZU' (release group - same group is what "
                          "cross-seeding needs); alt: '&'<->'and', no apostrophes, no leading 'The', each side of 'aka'; "
                          "dotted: 'Title.2020'; no_spaces: 'TitleWords 2020'; title: title alone; clean: full name "
                          "minus HDR/DV/codec/audio/source junk; raw: the name with separators as spaces; "
                          "title_year_res: + 1080p.",
        "max_queries_per_item": 5,
        "_max_queries_per_item_help": "Each query hits every indexer in Prowlarr - mind tracker API limits. A release "
                                      "stops searching once it has a match scoring stop_when_score_at_least.",
        "sub_unit_variants": ["title_year", "title_group"],
        "max_queries_per_sub_unit": 2,
        "_sub_unit_help": "Season folders / sub-releases inside a release that are still unmatched get their own "
                          "queries ('Merlin S01', 'Merlin S01 TiZU').",
        "max_items": 0,
        "_max_items_help": "Search at most this many orphan releases per run, biggest first. 0 = all.",
        "reject_on": ["season_episode", "resolution", "source", "codec", "group", "service", "edition", "year",
                      "media"],
        "_reject_on_help": "A result is dropped when one of these CONFLICTS between your release and the result "
                           "(a tag missing on either side never rejects). Remove an entry to relax that rule.",
        "fuzzy_size_threshold": 0.02,
        "_fuzzy_size_threshold_help": "'near' size window (+-2%). Near-size results additionally need a similar title "
                                      "and matching group or 2 of res/source/codec. Exact sizes and sizes within the "
                                      "indexer's display rounding ('29.05 GB') always count as exact.",
        "close_size_threshold": 0.001,
        "_close_size_threshold_help": "Within 0.1% (e.g. the torrent has an nfo you deleted) counts as 'close': "
                                      "accepted on title + no conflicting tags, for *arr-renamed files that have no "
                                      "tags left to compare. Step 3's piece check has the final word.",
        "min_title_similarity": 0.6,
        "_min_title_similarity_help": "Titles must be the SAME (punctuation, articles, 'II' vs '2' ignored) or differ "
                                      "only by spelling / a subtitle with the same year. Only a byte-exact size may "
                                      "get away with a title that is merely this similar (0-1).",
        "near_miss_max_bigger_pct": 20,
        "near_miss_max_smaller_pct": 5,
        "near_misses_per_target": 3,
        "_near_miss_help": "Near misses (option 5) always need the same title, a compatible year and the same "
                           "season/episode. Then either the size fits but a label differs (720p vs 1080p, group...), "
                           "or nothing conflicts and the torrent is up to 20% bigger (featurettes, extras, episodes "
                           "you don't have) or 5% smaller than yours. At most this many per movie/season, best first.",
        "min_score": 55,
        "stop_when_score_at_least": 85,
        "max_candidates_per_item": 8,
        "skip_items_regex": [],
        "show_rejected_per_item": 3,
    },
    "download": {
        "min_score": 0,
        "delay_between_downloads_sec": 2,
        "verify_pieces": 4,
        "_verify_pieces_help": "How many pieces to SHA1-check against your files after downloading a .torrent. "
                               "0 = off. Only pieces fully covered by matched files are checked.",
        "partial_min_ratio": 0.98,
        "_partial_min_ratio_help": "A torrent where only this share of its bytes exist locally is 'partial'. "
                                   "Below this it is a 'mismatch' and never added.",
    },
    "add": {
        "allowed_statuses": ["exact", "renamed", "partial"],
        "require_piece_check": True,
        "_require_piece_check_help": "Only add torrents whose spot-checked pieces all matched.",
        "skip_checking": True,
        "start_stopped": True,
        "category": "",
        "tags": ["orphan-xseed"],
        "tag_with_status": True,
        "_tag_with_status_help": "Adds oxs-exact / oxs-renamed / oxs-partial tag so you can filter in qBittorrent.",
        "link_dir": "{drive}/hardlinked/xseed",
        "_link_dir_help": "Hardlink folder for torrents whose layout doesn't fit your files (see link_dir_mode). "
                          "{drive} = mount point of the disk your file is on, e.g. /media/roman/DiskM -> "
                          "/media/roman/DiskM/hardlinked/xseed. Must be on the same disk (hardlinks).",
        "link_dir_mode": "auto",
        "_link_dir_mode_help": "auto: use the hardlink folder when the torrent is a folder but you have a single file, "
                               "or the torrent has files you don't have (nfo/screens/sample get downloaded there, not "
                               "into your library). Single-file torrents and folders that match yours point at your "
                               "files directly. all: every folder torrent that isn't an exact match. off: never.",
        "link_dir_download_missing": True,
        "_link_dir_download_missing_help": "Files you don't have stay wanted, so Start downloads them into the "
                                           "hardlink folder. false = set them to 'do not download'.",
        "temp_hardlinks": True,
        "_temp_hardlinks_help": "For 'renamed' torrents: briefly hardlink your files under the torrent's names so "
                                "qBittorrent accepts skip_checking (100%, no 'missing files'), then remove the links and "
                                "rename inside qBittorrent onto your names. No data is copied or moved.",
        "missing_files_priority_zero": True,
        "rename_missing_into_local_folder": True,
    },
}

# --------------------------------------------------------------------------- output


class Log:
    CODES = {"red": "31", "green": "32", "yellow": "33", "blue": "34", "magenta": "35", "cyan": "36",
             "dim": "2", "bold": "1", "white": "37"}

    def __init__(self):
        self.fh = None
        self.path = None
        self.debug_console = False
        self.lock = threading.RLock()
        self.tty = sys.stdout.isatty()
        self.color = self.tty and os.environ.get("NO_COLOR") is None
        self.progress = None

    def open_file(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.fh = open(path, "a", encoding="utf-8", errors="replace")
        self.path = path

    def c(self, color, s):
        if not self.color or not color:
            return s
        return "".join("\x1b[%sm" % self.CODES[x] for x in color.split("+")) + s + "\x1b[0m"

    def _clear(self):
        if self.progress is not None and self.tty:
            sys.stdout.write("\r\x1b[2K")

    def _redraw(self):
        if self.progress is not None and self.tty:
            sys.stdout.write(self.progress.line())
            sys.stdout.flush()

    def _write(self, level, msg, color=None, console=True):
        msg = str(msg)
        with self.lock:
            if self.fh:
                ts = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                for line in msg.splitlines() or [""]:
                    self.fh.write("%s %-5s %s\n" % (ts, level, line))
                self.fh.flush()
            if console and (level != "DEBUG" or self.debug_console):
                self._clear()
                prefix = {"WARN": "! ", "ERROR": "!! ", "DEBUG": "  . "}.get(level, "")
                print(self.c(color, prefix + msg), flush=True)
                self._redraw()

    def debug(self, m):
        self._write("DEBUG", m, "dim")

    def info(self, m, color=None):
        self._write("INFO", m, color)

    def ok(self, m):
        self._write("OK", m, "green")

    def warn(self, m):
        self._write("WARN", m, "yellow")

    def error(self, m):
        self._write("ERROR", m, "red+bold")

    def file(self, m):
        self._write("DEBUG", m, None, console=False)

    def draw_progress(self):
        with self.lock:
            if self.progress is not None and self.tty:
                sys.stdout.write("\r\x1b[2K" + self.progress.line())
                sys.stdout.flush()


LOG = Log()


class Progress:
    """Live one-line progress (redrawn in place on a terminal, periodic lines otherwise)."""

    def __init__(self, label, total=None, unit="", plain_every=10.0):
        self.label, self.total, self.unit = label, total, unit
        self.done = 0
        self.info = ""
        self.t0 = time.time()
        self.last_draw = 0.0
        self.last_plain = self.t0
        self.plain_every = plain_every
        LOG.progress = self
        LOG.draw_progress()

    def update(self, n=1, info=None):
        self.done += n
        if info is not None:
            self.info = info
        self._tick()

    def set_info(self, info):
        self.info = info
        self._tick()

    def _tick(self):
        now = time.time()
        if LOG.tty:
            if now - self.last_draw >= 0.08:
                self.last_draw = now
                LOG.draw_progress()
        elif now - self.last_plain >= self.plain_every:
            self.last_plain = now
            LOG._write("INFO", self.line(plain=True), None)

    def line(self, plain=False):
        el = max(time.time() - self.t0, 1e-6)
        rate = self.done / el
        if self.total:
            pct = 100.0 * self.done / self.total
            eta = (self.total - self.done) / rate if rate > 0 else 0
            s = "%s %d/%d %5.1f%% | %.1f %s/s | ETA %s" % (self.label, self.done, self.total, pct, rate,
                                                        self.unit or "it", fmt_dur(eta))
        else:
            s = "%s %d %s | %.0f/s | %s" % (self.label, self.done, self.unit, rate, fmt_dur(el))
        if self.info:
            s += " | " + self.info
        width = shutil.get_terminal_size((120, 20)).columns - 1
        if len(s) > width:
            s = s[:max(10, width - 1)] + "~"
        return s if plain else LOG.c("cyan", s)

    def close(self, final=None):
        with LOG.lock:
            LOG._clear()
            LOG.progress = None
            if LOG.tty:
                sys.stdout.flush()
        if final:
            LOG.info(final)


def fmt_size(n):
    if n is None:
        return "?"
    n = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or unit == "TiB":
            return ("%.0f %s" % (n, unit)) if unit == "B" else ("%.2f %s" % (n, unit))
        n /= 1024.0
    return "%.2f TiB" % n


def fmt_pct(ratio):
    s = "%.2f%%" % (100.0 * ratio)
    if ratio < 1 and s.startswith("100.00"):
        s = "%.4f%%" % (100.0 * ratio)
    return s


def fmt_dur(sec):
    sec = int(max(sec, 0))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return "%d:%02d:%02d" % (h, m, s) if h else "%02d:%02d" % (m, s)


def now_iso():
    return dt.datetime.now().replace(microsecond=0).isoformat(sep=" ")


def short_id(s):
    return hashlib.sha1(s.encode("utf-8", "surrogateescape")).hexdigest()[:10]


def safe_filename(s, maxlen=150):
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", s).strip(" .")
    return (s[:maxlen] or "unnamed")


class Fatal(Exception):
    pass


class NetError(Exception):
    pass


# --------------------------------------------------------------------------- config / files


def deep_merge(base, override):
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path):
    if not os.path.exists(path):
        write_json(path, DEFAULT_CONFIG)
        print("Created default config: %s  (edit it if needed)" % path)
    with open(path, "r", encoding="utf-8") as f:
        try:
            user = json.load(f)
        except json.JSONDecodeError as e:
            raise Fatal("config %s is not valid JSON: %s" % (path, e))
    cfg = deep_merge(DEFAULT_CONFIG, user)
    wd = os.path.expanduser(cfg["work_dir"])
    if not os.path.isabs(wd):
        wd = os.path.join(os.path.dirname(os.path.abspath(path)), wd)
    cfg["_work_dir"] = os.path.normpath(wd)
    cfg["_config_path"] = os.path.abspath(path)
    return cfg


def write_json(path, data):
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", errors="surrogateescape") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def read_json(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8", errors="surrogateescape") as f:
        return json.load(f)


def write_text(path, lines):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", errors="surrogateescape") as f:
        for line in lines:
            f.write(line + "\n")
    os.replace(tmp, path)


class Paths:
    def __init__(self, cfg):
        w = cfg["_work_dir"]
        self.work = w
        self.orphans_json = os.path.join(w, "orphans.json")
        self.orphans_txt = os.path.join(w, "orphans.txt")
        self.orphan_files_txt = os.path.join(w, "orphan_files.txt")
        self.extras_txt = os.path.join(w, "orphan_extras.txt")
        self.weak_txt = os.path.join(w, "weak_matches.txt")
        self.cand_json = os.path.join(w, "candidates.json")
        self.cand_txt = os.path.join(w, "candidates.txt")
        self.dl_json = os.path.join(w, "downloads.json")
        self.near_json = os.path.join(w, "near_misses.json")
        self.near_txt = os.path.join(w, "near_misses.txt")
        self.dl_txt = os.path.join(w, "downloads.txt")
        self.added_json = os.path.join(w, "added.json")
        self.added_txt = os.path.join(w, "added.txt")
        self.torrents = os.path.join(w, "torrents")
        self.cache = os.path.join(w, "search_cache.json")
        self.logs = os.path.join(w, "logs")


class PathMap:
    """Translate between qBittorrent's paths and local paths (prefix mapping)."""

    def __init__(self, mappings):
        self.maps = []
        for m in mappings or []:
            q, l = norm(m.get("qbit", "")), norm(m.get("local", ""))
            if q and l:
                self.maps.append((q, l))

    @staticmethod
    def _swap(p, a, b):
        if p == a:
            return b
        if p.startswith(a.rstrip("/") + "/"):
            return b.rstrip("/") + p[len(a.rstrip("/")):]
        return None

    def to_local(self, p):
        if not p:
            return p
        p = norm(p)
        for q, l in sorted(self.maps, key=lambda x: -len(x[0])):
            r = self._swap(p, q, l)
            if r is not None:
                return r
        return p

    def to_qbit(self, p):
        if not p:
            return p
        p = norm(p)
        for q, l in sorted(self.maps, key=lambda x: -len(x[1])):
            r = self._swap(p, l, q)
            if r is not None:
                return r
        return p


def norm(p):
    if not p:
        return ""
    p = p.replace("\\", "/") if re.match(r"^[A-Za-z]:\\", p) else p
    return os.path.normpath(p)


# --------------------------------------------------------------------------- HTTP (stdlib)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Resp:
    def __init__(self, status, headers, body, url, elapsed):
        self.status, self.headers, self.body, self.url, self.elapsed = status, headers, body, url, elapsed

    @property
    def text(self):
        return self.body.decode("utf-8", "replace")

    def json(self):
        return json.loads(self.body.decode("utf-8", "replace"))

    def header(self, name, default=""):
        if self.headers is None:
            return default
        return self.headers.get(name, default) or default


def redact(url):
    return re.sub(r"(apikey=)[^&]+", r"\1***", url, flags=re.I)


def multipart(fields, files):
    boundary = "----oxs" + uuid.uuid4().hex
    buf = io.BytesIO()
    for k, v in fields.items():
        for vv in (v if isinstance(v, (list, tuple)) else [v]):
            buf.write(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n" % (boundary, k)).encode())
            buf.write(str(vv).encode("utf-8", "surrogateescape"))
            buf.write(b"\r\n")
    for field, filename, content, ctype in files:
        buf.write(("--%s\r\nContent-Disposition: form-data; name=\"%s\"; filename=\"%s\"\r\nContent-Type: %s\r\n\r\n"
                   % (boundary, field, filename, ctype)).encode())
        buf.write(content)
        buf.write(b"\r\n")
    buf.write(("--%s--\r\n" % boundary).encode())
    return buf.getvalue(), "multipart/form-data; boundary=%s" % boundary


class Http:
    def __init__(self, name, base_url, timeout=60, verify_ssl=True):
        self.name = name
        self.base = base_url.rstrip("/") + "/"
        self.timeout = timeout
        self.jar = http.cookiejar.CookieJar()
        handlers = [urllib.request.HTTPCookieProcessor(self.jar), _NoRedirect()]
        if not verify_ssl:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            handlers.append(urllib.request.HTTPSHandler(context=ctx))
        self.opener = urllib.request.build_opener(*handlers)
        self.headers = {"User-Agent": "orphan_xseed/%s" % VERSION, "Accept": "*/*"}

    def full_url(self, path):
        if path.startswith(("http://", "https://")):
            return path
        return urllib.parse.urljoin(self.base, path.lstrip("/"))

    def is_own(self, url):
        a, b = urllib.parse.urlsplit(url), urllib.parse.urlsplit(self.base)
        return (a.scheme, a.netloc) == (b.scheme, b.netloc)

    def request(self, method, path, params=None, data=None, files=None, headers=None, follow=True,
                timeout=None, max_hops=6):
        url = self.full_url(path)
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params, doseq=True)
        body = None
        base_hdrs = dict(self.headers)
        base_hdrs.update(headers or {})
        ctype = None
        if files:
            body, ctype = multipart(data or {}, files)
        elif data is not None:
            body = urllib.parse.urlencode(data, doseq=True).encode()
            ctype = "application/x-www-form-urlencoded"
        for _hop in range(max_hops):
            hdrs = dict(base_hdrs) if self.is_own(url) else {"User-Agent": base_hdrs["User-Agent"]}
            if ctype and body is not None:
                hdrs["Content-Type"] = ctype
            req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
            t0 = time.time()
            try:
                r = self.opener.open(req, timeout=timeout or self.timeout)
                status, rh, content = r.status, r.headers, r.read()
            except urllib.error.HTTPError as e:
                status, rh = e.code, e.headers
                try:
                    content = e.read() or b""
                except Exception:
                    content = b""
            except (urllib.error.URLError, socket.timeout, ConnectionError, OSError) as e:
                reason = getattr(e, "reason", e)
                LOG.debug("[%s] %s %s -> FAILED %s (%.0fms)" % (self.name, method, redact(url), reason,
                                                               (time.time() - t0) * 1000))
                raise NetError("%s: %s %s failed: %s" % (self.name, method, redact(url), reason))
            el = time.time() - t0
            LOG.debug("[%s] %s %s -> %s %s %.0fms" % (self.name, method, redact(url), status,
                                                      fmt_size(len(content)), el * 1000))
            resp = Resp(status, rh, content, url, el)
            if follow and status in (301, 302, 303, 307, 308):
                loc = resp.header("Location")
                if loc and not loc.lower().startswith("magnet:"):
                    url = urllib.parse.urljoin(url, loc)
                    if status in (301, 302, 303):
                        method, body, ctype = "GET", None, None
                    continue
            return resp
        raise NetError("%s: too many redirects for %s" % (self.name, redact(url)))


# --------------------------------------------------------------------------- qBittorrent


def parse_version(s):
    nums = re.findall(r"\d+", s or "")
    return tuple(int(x) for x in nums[:4]) if nums else (0,)


class QBit:
    def __init__(self, cfg):
        c = cfg["qbittorrent"]
        self.c = c
        self.url = c["url"].rstrip("/")
        self.http = Http("qbit", self.url, timeout=c.get("timeout_sec", 180), verify_ssl=c.get("verify_ssl", False))
        # qBittorrent CSRF protection: Referer/Origin must match the Host we talk to
        self.http.headers["Referer"] = self.url + "/"
        self.api_key = (c.get("api_key") or "").strip()
        self.app_version = "?"
        self.webapi = (0,)
        self.logged_in = False

    def login(self):
        LOG.info("qBittorrent: connecting to %s" % self.url)
        if self.api_key:
            LOG.debug("qBittorrent: using API key (Authorization: Bearer ...)")
            self.http.headers["Authorization"] = "Bearer " + self.api_key
        else:
            r = self.http.request("POST", "api/v2/auth/login",
                                  data={"username": self.c["username"], "password": self.c["password"]})
            body = r.text.strip()
            LOG.debug("qBittorrent login response: HTTP %s body=%r set-cookie=%r"
                      % (r.status, body[:200], r.header("Set-Cookie")[:120]))
            if r.status == 403:
                raise Fatal("qBittorrent refused login with 403: this IP is banned after too many failed logins. "
                            "Wait (default 60 min) or restart qbittorrent-nox, then fix username/password.")
            if r.status == 401 or body == "Fails.":
                raise Fatal("qBittorrent rejected username/password (HTTP %s %r). Usernames are case-sensitive: "
                            "check qbittorrent.username in the config ('%s')." % (r.status, body, self.c["username"]))
            if r.status not in (200, 204):
                raise Fatal("qBittorrent login: unexpected HTTP %s %r" % (r.status, body[:300]))
            names = [ck.name for ck in self.http.jar]
            if not names:
                # cookie jar didn't take it (odd host names) - set header by hand
                m = re.match(r"\s*([^=;\s]+)=([^;]*)", r.header("Set-Cookie"))
                if m:
                    self.http.headers["Cookie"] = "%s=%s" % (m.group(1), m.group(2))
                    names = [m.group(1)]
            LOG.debug("qBittorrent session cookie(s): %s" % (names or "none (auth bypass for this IP?)"))
        r = self._call("GET", "app/version", relogin=False)
        if r.status in (401, 403):
            raise Fatal("qBittorrent: not authorized after login (HTTP %s %r). Wrong API key? Or WebUI CSRF/host "
                        "header validation rejects %s." % (r.status, r.text[:200], self.url))
        self.app_version = r.text.strip()
        r2 = self._call("GET", "app/webapiVersion", relogin=False)
        self.webapi = parse_version(r2.text.strip())
        self.logged_in = True
        LOG.ok("qBittorrent %s (WebAPI %s) - logged in" % (self.app_version, ".".join(map(str, self.webapi))))

    def _call(self, method, ep, relogin=True, **kw):
        r = self.http.request(method, "api/v2/" + ep, **kw)
        if r.status == 403 and relogin and not self.api_key and self.logged_in:
            LOG.warn("qBittorrent answered 403 on %s - session expired? logging in again" % ep)
            self.http.jar.clear()
            self.http.headers.pop("Cookie", None)
            self.login()
            r = self.http.request(method, "api/v2/" + ep, **kw)
        return r

    def _json(self, method, ep, **kw):
        r = self._call(method, ep, **kw)
        if r.status != 200:
            raise Fatal("qBittorrent %s %s -> HTTP %s %r" % (method, ep, r.status, r.text[:300]))
        return r.json()

    def torrents(self, include_files=False, hashes=None):
        params = {}
        if include_files:
            params["includeFiles"] = "true"
        if hashes:
            params["hashes"] = "|".join(hashes)
        return self._json("GET", "torrents/info", params=params)

    def files(self, h):
        return self._json("GET", "torrents/files", params={"hash": h})

    def categories(self):
        try:
            return self._json("GET", "torrents/categories")
        except Fatal as e:
            LOG.warn("could not read categories: %s" % e)
            return {}

    def preferences(self):
        try:
            return self._json("GET", "app/preferences")
        except Fatal as e:
            LOG.warn("could not read preferences: %s" % e)
            return {}

    def add(self, torrent_bytes, filename, fields):
        return self._call("POST", "torrents/add", data=fields,
                          files=[("torrents", filename, torrent_bytes, "application/x-bittorrent")])

    def rename_file(self, h, old, new):
        return self._call("POST", "torrents/renameFile", data={"hash": h, "oldPath": old, "newPath": new})

    def file_prio(self, h, ids, prio):
        return self._call("POST", "torrents/filePrio",
                          data={"hash": h, "id": "|".join(str(i) for i in ids), "priority": str(prio)})

    def all_hashes(self):
        hs = set()
        for t in self.torrents():
            for k in ("hash", "infohash_v1", "infohash_v2"):
                v = (t.get(k) or "").lower()
                if v:
                    hs.add(v[:40] if k == "infohash_v2" else v)
        return hs


# --------------------------------------------------------------------------- Prowlarr


class Prowlarr:
    def __init__(self, cfg, paths):
        c = cfg["prowlarr"]
        self.c = c
        self.url = c["url"].rstrip("/")
        self.http = Http("prowlarr", self.url, timeout=c.get("search_timeout_sec", 180))
        self.api_key = (c.get("api_key") or "").strip()
        self.cache_path = paths.cache
        self.cache = None
        self.version = "?"
        self.last_live_search = 0.0

    def connect(self):
        LOG.info("Prowlarr: connecting to %s" % self.url)
        if not self.api_key:
            self.api_key = self._fetch_api_key()
        self.http.headers["X-Api-Key"] = self.api_key
        r = self.http.request("GET", "api/v1/system/status")
        if r.status == 401:
            raise Fatal("Prowlarr rejected the API key (HTTP 401). Fix prowlarr.api_key or clear it to auto-fetch.")
        if r.status != 200:
            raise Fatal("Prowlarr /api/v1/system/status -> HTTP %s %r" % (r.status, r.text[:300]))
        self.version = r.json().get("version", "?")
        LOG.ok("Prowlarr %s - API key OK (%s...)" % (self.version, self.api_key[:6]))

    def _key_from(self, r):
        if r.status != 200:
            return None
        try:
            k = r.json().get("apiKey")
        except ValueError:
            m = re.search(r"apiKey['\"]?\s*[:=]\s*['\"]([A-Za-z0-9]{20,})", r.text)
            k = m.group(1) if m else None
        return k or None

    def _fetch_api_key(self):
        LOG.info("Prowlarr: no api_key in config - fetching it via login")
        r = self.http.request("GET", "initialize.json", follow=False)
        k = self._key_from(r)
        if k:
            LOG.debug("Prowlarr: /initialize.json readable without login (auth disabled for local addresses)")
            return k
        LOG.debug("Prowlarr: /initialize.json without login -> HTTP %s, trying forms login" % r.status)
        r = self.http.request("POST", "login", params={"returnUrl": "/"},
                              data={"username": self.c["username"], "password": self.c["password"],
                                    "rememberMe": "on"}, follow=False)
        loc = r.header("Location")
        LOG.debug("Prowlarr login -> HTTP %s Location=%r cookies=%s" % (r.status, loc, [c.name for c in self.http.jar]))
        if "loginFailed" in loc:
            raise Fatal("Prowlarr login failed: wrong username/password for '%s'." % self.c["username"])
        k = self._key_from(self.http.request("GET", "initialize.json", follow=False))
        if k:
            return k
        basic = "Basic " + base64.b64encode(("%s:%s" % (self.c["username"], self.c["password"])).encode()).decode()
        r = self.http.request("GET", "initialize.json", headers={"Authorization": basic}, follow=False)
        k = self._key_from(r)
        if k:
            LOG.debug("Prowlarr: got key with Basic auth")
            return k
        k = self._key_from(self.http.request("GET", "initialize.js", follow=False))
        if k:
            return k
        raise Fatal("Could not get the Prowlarr API key automatically. Paste it into prowlarr.api_key "
                    "(Prowlarr > Settings > General > Security > API Key).")

    def indexers(self):
        r = self.http.request("GET", "api/v1/indexer")
        return r.json() if r.status == 200 else []

    # ---- search with cache
    def _load_cache(self):
        if self.cache is None:
            try:
                self.cache = read_json(self.cache_path, {}) or {}
            except (ValueError, OSError):
                self.cache = {}

    def search(self, query):
        """returns (results, from_cache, seconds)"""
        c = self.c
        key = json.dumps([query.lower(), c.get("indexer_ids") or [], c.get("categories") or [],
                          c.get("search_limit", 100)])
        ttl = float(c.get("cache_hours", 0)) * 3600
        if ttl > 0:
            self._load_cache()
            hit = self.cache.get(key)
            if hit and time.time() - hit["ts"] < ttl:
                return hit["results"], True, 0.0
        delay = float(c.get("delay_between_searches_sec", 0))
        wait = self.last_live_search + delay - time.time()
        if wait > 0:
            time.sleep(wait)
        params = [("query", query), ("type", "search"), ("limit", c.get("search_limit", 100)), ("offset", 0)]
        for i in c.get("indexer_ids") or []:
            params.append(("indexerIds", i))
        for cat in c.get("categories") or []:
            params.append(("categories", cat))
        t0 = time.time()
        try:
            r = self.http.request("GET", "api/v1/search", params=params)
        finally:
            self.last_live_search = time.time()
        el = time.time() - t0
        if r.status != 200:
            LOG.warn("Prowlarr search %r -> HTTP %s %s" % (query, r.status, r.text[:300]))
            return [], False, el
        try:
            res = r.json()
        except ValueError:
            LOG.warn("Prowlarr search %r returned non-JSON: %r" % (query, r.text[:200]))
            return [], False, el
        slim = []
        for x in res:
            proto = x.get("protocol")
            if isinstance(proto, str) and proto.lower() != "torrent":
                continue
            if isinstance(proto, int) and proto != 2:
                continue
            slim.append({k: x.get(k) for k in ("guid", "indexerId", "indexer", "title", "size", "files",
                                                "downloadUrl", "magnetUrl", "infoHash", "infoUrl", "seeders",
                                                "publishDate", "categories")})
        if ttl > 0:
            self.cache[key] = {"ts": time.time(), "query": query, "results": slim}
            try:
                write_json(self.cache_path, self.cache)
            except OSError as e:
                LOG.warn("cannot write search cache: %s" % e)
        return slim, False, el

    def download(self, url):
        """-> ('torrent', bytes) | ('magnet', uri) | ('error', message) | ('ratelimit', seconds)"""
        for _ in range(6):
            r = self.http.request("GET", url, follow=False, timeout=120)
            if r.status in (301, 302, 303, 307, 308):
                loc = r.header("Location")
                if loc.lower().startswith("magnet:"):
                    return "magnet", loc
                if not loc:
                    return "error", "redirect without Location"
                url = urllib.parse.urljoin(url, loc)
                LOG.debug("download redirected to %s" % redact(url))
                continue
            if r.status == 429:
                ra = r.header("Retry-After")
                return "ratelimit", int(ra) if ra.isdigit() else 60
            if r.status != 200:
                return "error", "HTTP %s %s" % (r.status, re.sub(r"\s+", " ", r.text)[:300])
            if r.body[:7] == b"magnet:":
                return "magnet", r.text.strip()
            if r.body[:1] == b"d" and b"4:info" in r.body:
                return "torrent", r.body
            return "error", "not a torrent file (%s): %r" % (r.header("Content-Type"), r.body[:150])
        return "error", "too many redirects"


# --------------------------------------------------------------------------- bencode / torrent


def bdecode(data):
    """Decode bencode; returns (value, (start, end) of the top-level 'info' value)."""
    span = [None]

    def dec(i, depth):
        c = data[i:i + 1]
        if c == b"i":
            j = data.index(b"e", i)
            return int(data[i + 1:j]), j + 1
        if c == b"l":
            i += 1
            out = []
            while data[i:i + 1] != b"e":
                v, i = dec(i, depth + 1)
                out.append(v)
            return out, i + 1
        if c == b"d":
            i += 1
            out = {}
            while data[i:i + 1] != b"e":
                k, i = dec(i, depth + 1)
                start = i
                v, i = dec(i, depth + 1)
                if depth == 0 and k == b"info":
                    span[0] = (start, i)
                out[k] = v
            return out, i + 1
        if c.isdigit():
            j = data.index(b":", i)
            n = int(data[i:j])
            return data[j + 1:j + 1 + n], j + 1 + n
        raise ValueError("bad bencode at byte %d" % i)

    v, _ = dec(0, 0)
    return v, span[0]


def _s(b):
    if isinstance(b, bytes):
        return b.decode("utf-8", "replace")
    return str(b)


class TFile:
    __slots__ = ("path", "size", "pad", "idx")

    def __init__(self, path, size, pad, idx):
        self.path, self.size, self.pad, self.idx = path, size, pad, idx


class Torrent:
    def __init__(self, data):
        meta, span = bdecode(data)
        if not isinstance(meta, dict) or b"info" not in meta or span is None:
            raise ValueError("no info dictionary")
        info = meta[b"info"]
        raw = data[span[0]:span[1]]
        self.name = _s(info.get(b"name.utf-8") or info.get(b"name") or b"unnamed").replace("/", "_")
        if self.name in ("", ".", ".."):
            self.name = "unnamed"
        self.v1 = b"pieces" in info
        self.v2 = b"file tree" in info
        self.hash = hashlib.sha1(raw).hexdigest() if self.v1 else hashlib.sha256(raw).hexdigest()[:40]
        self.piece_length = int(info.get(b"piece length", 0))
        self.pieces = info.get(b"pieces", b"")
        self.private = info.get(b"private") == 1
        ann = meta.get(b"announce")
        if not ann and meta.get(b"announce-list"):
            try:
                ann = meta[b"announce-list"][0][0]
            except (IndexError, TypeError):
                ann = None
        self.tracker = urllib.parse.urlsplit(_s(ann)).hostname if ann else ""
        self.all_files = []  # including pad files, in piece order
        if b"files" in info:
            self.multi = True
            for f in info[b"files"]:
                parts = f.get(b"path.utf-8") or f.get(b"path") or []
                parts = [_s(p).replace("/", "_") for p in parts]
                parts = [p for p in parts if p not in ("", ".", "..")] or ["unnamed"]
                pad = "p" in _s(f.get(b"attr", b""))  # BEP 47 padding file (qBittorrent hides these)
                self.all_files.append(TFile("/".join([self.name] + parts), int(f[b"length"]), pad, -1))
        elif b"length" in info:
            self.multi = False
            self.all_files.append(TFile(self.name, int(info[b"length"]), False, -1))
        elif self.v2:
            out = []

            def walk(node, prefix):
                for k in sorted(node.keys()):
                    v = node[k]
                    if k == b"":
                        out.append((prefix, int(v.get(b"length", 0))))
                    else:
                        walk(v, prefix + [_s(k)])
            walk(info[b"file tree"], [])
            self.multi = not (len(out) == 1 and len(out[0][0]) == 1 and out[0][0][0] == self.name)
            for parts, size in out:
                p = "/".join(parts) if not self.multi else "/".join([self.name] + parts)
                self.all_files.append(TFile(p, size, False, -1))
        else:
            raise ValueError("torrent has no files")
        idx = 0
        for f in self.all_files:
            if not f.pad:
                f.idx = idx
                idx += 1
        self.files = [f for f in self.all_files if not f.pad]
        self.size = sum(f.size for f in self.files)


# --------------------------------------------------------------------------- name parsing / tags / variants

KNOWN_EXT = set(".mkv .mp4 .avi .ts .m2ts .m4v .mov .wmv .flv .webm .mpg .mpeg .vob .iso .img .srt .ass .ssa .sub "
                ".idx .sup .nfo .txt .jpg .jpeg .png .mp3 .flac .m4a .m4b .aac .ogg .opus .wav .ape .wv .dsf .epub "
                ".pdf .mobi .azw3 .djvu .cbz .cbr .zip .rar .7z .exe .torrent .log .cue".split())
VIDEO_EXT = set(".mkv .mp4 .avi .ts .m2ts .mts .m4v .mov .wmv .flv .webm .mpg .mpeg .vob .iso .img .divx .ogm "
                ".rmvb .3gp .evo".split())
AUDIO_EXT = set(".flac .mp3 .m4a .aac .ogg .opus .wav .ape .wv .dsf .dff .alac .m4b .wma .mka .tak .aiff .aif "
                ".dts .ac3 .thd .eac3".split())
EXTRA_EXT = set(".nfo .txt .jpg .jpeg .png .gif .bmp .webp .tif .tiff .ico .srt .ass .ssa .sub .idx .sup .vtt .smi "
                ".log .cue .m3u .m3u8 .pls .sfv .md5 .sha1 .sha256 .ffp .st5 .accurip .url .lnk .torrent .db .ini "
                ".xml .json .html .htm .nzb .par2 .srr .srs .diz".split())
CONTENT_KINDS = ("video", "audio", "other")

# "strong" junk never appears in real titles -> marks the end of the title
STRONG_JUNK = [
    r"(?:2160|1080|720|576|540|480|4320)[pix]", r"[48]k", r"uhd", r"fhd", r"qhd",
    r"hdr(?:10(?:\+|plus|p)?)?", r"dovi", r"hlg", r"sdr", r"(?:8|10|12)-?bits?",
    r"web-?dl", r"web-?rip", r"webhd", r"blu-?ray", r"bluray", r"bdrip", r"brrip", r"bdremux", r"remux",
    r"bdmv", r"hdtv", r"pdtv", r"sdtv", r"dvd-?rip", r"dvdr", r"dvd9", r"dvd5", r"hdrip", r"bd25", r"bd50",
    r"hddvd", r"amzn", r"nf", r"dsnp", r"atvp", r"hmax", r"hulu", r"pcok", r"pmtp", r"crav", r"itunes",
    r"ddp?(?:[257][. ]?[01])?", r"dd\+", r"e-?ac-?3", r"ac-?3", r"aac(?:[257][. ]?[01])?", r"dts(?:-?hd)?",
    r"dts-?x", r"dts-?ma", r"true-?hd", r"atmos", r"flac(?:[257][. ]?[01])?", r"opus", r"l?pcm",
    r"[2-7]\.[01]", r"x-?26[45]", r"h-?26[45]", r"hevc", r"avc", r"av1", r"vc-?1", r"xvid", r"divx",
    r"mpeg-?2", r"vp9", r"mkv", r"mp4", r"avi",
]
# "weak" junk are also ordinary words -> only removed around the title, never used to cut it
WEAK_JUNK = [
    r"web", r"dv", r"dolby", r"vision", r"ma", r"bd", r"dvd", r"stan", r"proper", r"repack\d?", r"rerip",
    r"internal", r"limited", r"extended", r"unrated", r"remastered", r"imax", r"hybrid", r"multi", r"dual",
    r"dubbed", r"subbed", r"uncut", r"criterion", r"sbs", r"hsbs", r"ou", r"hou", r"3d", r"rus", r"ukr", r"eng",
    r"sub", r"subs", r"esub", r"msubs", r"complete", r"integrale", r"series", r"fra", r"french", r"vostfr",
    r"truefrench",
    r"german", r"ita", r"spa", r"jpn", r"kor",
]
STRONG_RE = re.compile(r"^(?:%s)$" % "|".join(STRONG_JUNK), re.I)
WEAK_RE = re.compile(r"^(?:%s)$" % "|".join(WEAK_JUNK), re.I)
JUNK_RE = re.compile(r"^(?:%s)$" % "|".join(STRONG_JUNK + WEAK_JUNK), re.I)
YEAR_RE = re.compile(r"^(?:19|20)\d\d$")
SE_RE = re.compile(r"^s(\d{1,2})(?:[ ._-]?e(\d{1,4})(?:[-e]+(\d{1,4}))?)?$", re.I)
SRANGE_RE = re.compile(r"^s\d{1,2}-s?\d{1,2}$", re.I)
XE_RE = re.compile(r"^(\d{1,2})x(\d{2,3})$", re.I)
RES_RE = re.compile(r"^(?:2160|1080|720|576|480|4320)[pi]$", re.I)

GENERIC_DIR_RE = re.compile(
    r"^(?:(?:cd|disc|disk|dvd|part|vol(?:ume)?)[ ._-]?\d{1,2}|screens?|screenshots?|samples?|subs?|subtitles?|"
    r"extras?|featurettes?|bonus(?:[ ._-]features)?|special[ ._-]features|proofs?|covers?|scans?|artworks?|art|"
    r"images?|bdmv|certificate|video_ts|audio_ts|stream|playlist|clipinf|backup|meta|jar|bdjo|auxdata|nfo|info|"
    r"misc|other|behind[ ._-]the[ ._-]scenes|deleted[ ._-]scenes|interviews?|trailers?|shorts?)$", re.I)
SEASON_DIR_RE = re.compile(r"^(?:season|series|saison|staffel|temporada|сезон)[ ._-]*(\d{1,2})$|^s(\d{1,2})$", re.I)
CONTAINER_NAMES = set(x.lower() for x in [
    "movies", "movie", "films", "film", "kino", "shows", "show", "tv", "tv shows", "tvshows", "tv-shows", "series",
    "serials", "television", "music", "music videos", "musicvideos", "concerts", "anime", "books", "ebooks",
    "audiobooks", "comics", "games", "software", "apps", "documentaries", "documentary", "docs", "xxx", "kids",
    "cartoons", "radarr", "sonarr", "lidarr", "readarr", "whisparr", "mylar", "tv-sonarr", "movies-radarr",
    "sonarr-anime", "anime-sonarr", "completed", "incomplete", "downloads", "download", "torrents", "seeding",
    "cross-seed", "cross-seeds", "crossseed", "xseed", "links", "uncategorized", "sport", "sports", "4k", "uhd",
    "remux", "remuxes", "1080p", "2160p", "720p", "webdl", "web-dl", "bluray", "misc", "other", "temp", "tmp"])

_SOURCE = [("remux", r"\b(?:bd)?remux\b"),
           ("disc", r"\b(?:bdmv|bd25|bd50|bd66|bd100|untouched)\b|\bcomplete (?:uhd )?blu-?ray\b"),
           ("webdl", r"\bweb[ -]?dl\b|\bwebdl\b|\bweb[ -]?hd\b"),
           ("webrip", r"\bweb[ -]?rip\b"),
           ("web", r"\bweb\b"),
           ("bluray", r"\bblu[ -]?ray\b|\bbluray\b|\bbd ?rip\b|\bbr ?rip\b|\bbd\b|\buhd ?bd\b"),
           ("hdtv", r"\b(?:hdtv|pdtv|sdtv|dsr|tv ?rip|sat ?rip|dvb)\b"),
           ("dvd", r"\bdvd(?:rip|r|5|9|scr)?\b"),
           ("hdrip", r"\bhd ?rip\b")]
_CODEC = [("hevc", r"\b(?:x ?265|h ?265|hevc)\b"), ("avc", r"\b(?:x ?264|h ?264|avc)\b"), ("av1", r"\bav1\b"),
          ("xvid", r"\b(?:xvid|divx)\b"), ("vc1", r"\bvc-?1\b"), ("mpeg2", r"\bmpeg-?2\b"), ("vp9", r"\bvp9\b")]
_AUDIO = [("truehd", r"\btrue-?hd\b"), ("dts", r"\bdts(?:-?hd|-?x|-?es|-?ma)?\b"),
          ("eac3", r"\bddp|\bdd\+|\be-?ac-?3\b"), ("ac3", r"\bdd(?:\d[. ]?\d)?\b|\bac-?3\b"),
          ("flac", r"\bflac"), ("lpcm", r"\bl?pcm\b"), ("aac", r"\baac"), ("opus", r"\bopus\b"), ("mp3", r"\bmp3\b")]
_SERVICE = [("amzn", r"\bamzn\b|\bamazon\b"), ("nf", r"\bnf\b|\bnetflix\b"), ("dsnp", r"\bdsnp\b|\bdsny\b"),
            ("atvp", r"\batvp\b|\baptv\b"), ("hmax", r"\bhmax\b|\bhbom\b|\bhbo max\b"), ("hulu", r"\bhulu\b"),
            ("pcok", r"\bpcok\b"), ("pmtp", r"\bpmtp\b"), ("crav", r"\bcrav\b")]
_CUTS = [("unrated", r"\bunrated\b"), ("extended", r"\bextended\b"), ("theatrical", r"\btheatrical\b"),
         ("directors", r"\bdirector'?s\b|\bdirectors\b"), ("uncut", r"\buncut\b")]
_SOURCE = [(k, re.compile(p)) for k, p in _SOURCE]
_CODEC = [(k, re.compile(p)) for k, p in _CODEC]
_AUDIO = [(k, re.compile(p)) for k, p in _AUDIO]
_SERVICE = [(k, re.compile(p)) for k, p in _SERVICE]
_CUTS = [(k, re.compile(p)) for k, p in _CUTS]
_AUDIO_MEDIA_RE = re.compile(r"\b(?:flac|mp3|alac|lossless|320|v0|24 ?bit|16 ?bit|24-(?:44|48|88|96|176|192)|"
                             r"vinyl|discography|album|single|ost|soundtrack|cd ?rip|web flac|ogg|aiff|dsd)\b")


def _first(table, low):
    for k, rx in table:
        if rx.search(low):
            return k
    return None


def strip_ext(name):
    root, ext = os.path.splitext(name)
    return root if ext.lower() in KNOWN_EXT or ext.lower() in VIDEO_EXT or ext.lower() in AUDIO_EXT else name


def fold(s):
    s = unicodedata.normalize("NFKD", s)
    return "".join(ch for ch in s if not unicodedata.combining(ch))


def file_kind(path):
    base = os.path.basename(path).lower()
    ext = os.path.splitext(base)[1]
    if ext in VIDEO_EXT:
        if re.search(r"(?:^|[ ._\-\[(])sample(?:[ ._\-\])]|$)", base):
            return "sample"
        return "video"
    if ext in AUDIO_EXT:
        return "audio"
    if ext in EXTRA_EXT:
        return "extra"
    return "other"


def _is_tech(tok):
    return bool(JUNK_RE.match(tok) or RES_RE.match(tok) or SE_RE.match(tok) or re.match(r"^[2-7]\.[01]$", tok)
                or re.match(r"(?i)^(?:ddp?|aac|dts|flac|truehd|e?ac3|lpcm|opus|atmos)[\d.+]*$", tok))


def release_group(stem):
    """'...x264-D-Z0N3' -> 'D-Z0N3', '...AV1-TiZU' -> 'TiZU', '...x264.D-Z0N3' -> 'D-Z0N3', 'Spider-Man' -> None"""
    s = stem.strip()
    anime = None
    m = re.match(r"^\s*\[([^\]]+)\]", s)
    if m:
        anime = m.group(1).strip()
    s = re.sub(r"(?:\s*[\[{][^\]}]*[\]}])+\s*$", "", s)  # trailing [rarbg] / {tracker} tags
    s = re.sub(r"(?i)\b([hx])\.(26[45])\b", r"\1\2", s)
    s = re.sub(r"(?<=\d)\.(?=\d\b)", "#", s)
    toks = [t for t in re.split(r"[ ._]+", s) if t]
    if len(toks) < 2:
        return anime
    last = toks[-1].replace("#", ".")
    if JUNK_RE.match(last) or RES_RE.match(last) or YEAR_RE.match(last):
        return anime
    dashes = [i for i, ch in enumerate(last) if ch == "-"]
    for i in reversed(dashes):
        left, right = last[:i], last[i + 1:]
        if right and (_is_tech(left) or YEAR_RE.match(left)) and not any(_is_tech(x) for x in right.split("-")):
            return right
    prev = toks[-2].replace("#", ".")
    if _is_tech(prev) and not _is_tech(last) and not re.match(r"^\d+$", last):
        return last
    return anime


def group_key(g):
    return re.sub(r"[^a-z0-9]", "", (g or "").lower())


def group_match(a, b):
    a, b = group_key(a), group_key(b)
    if not a or not b:
        return None
    return a == b or (len(a) >= 3 and len(b) >= 3 and (a.startswith(b) or b.startswith(a)))


def parse_se(low):
    """-> (kind, season, episode, seasons) kind: episode|season|multi|None"""
    m = re.search(r"\bs(\d{1,2}) ?e(\d{1,4})\b", low)
    if m:
        return "episode", int(m.group(1)), int(m.group(2)), None
    m = re.search(r"\b(\d{1,2})x(\d{2,3})\b", low)
    if m:
        return "episode", int(m.group(1)), int(m.group(2)), None
    m = re.search(r"\bs(\d{1,2}) ?(?:-|to|~) ?s?(\d{1,2})\b", low) or \
        re.search(r"\bseasons? (\d{1,2}) ?(?:-|to|~|and|&) ?(\d{1,2})\b", low)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        if b > a:
            return "multi", None, None, tuple(range(a, b + 1))
    seas = sorted(set(int(x) for x in re.findall(r"\bs(\d{1,2})\b", low) + re.findall(r"\bseason (\d{1,2})\b", low)))
    if len(seas) == 1:
        return "season", seas[0], None, None
    if len(seas) > 1:
        return "multi", None, None, tuple(seas)
    if re.search(r"\b(?:complete series|complete season|all seasons|integrale|the complete series|full series)\b",
                 low):
        return "multi", None, None, None
    return None, None, None, None


def se_label(kind, season, episode, seasons):
    if kind == "episode":
        return "S%02dE%02d" % (season, episode)
    if kind == "season":
        return "S%02d" % season
    if kind == "multi":
        if seasons:
            ss = sorted(seasons)
            return "S%02d-S%02d" % (ss[0], ss[-1]) if ss == list(range(ss[0], ss[-1] + 1)) else \
                "+".join("S%02d" % x for x in ss)
        return "complete series"
    return None


class Parsed:
    """Title + tags of a release / file name."""

    def __init__(self, name):
        self.original = name
        stem = strip_ext(name).strip()
        self.group = release_group(stem)
        s = stem
        m = re.match(r"^\s*\[([^\]]+)\]\s*", s)  # anime style [Group] Title
        if m:
            s = s[m.end():]
        if self.group and s.rstrip().endswith(self.group):
            s = s.rstrip()[:-len(self.group)].rstrip(" ._-")
        # brackets: keep content of () (years), drop [] {} blocks
        s = re.sub(r"\[[^\]]*\]|\{[^}]*\}|【[^】]*】", " ", s)
        s = s.replace("(", " ").replace(")", " ")
        # keep 5.1 / DDP5.1 / 2.0 / H.264 together before splitting on dots
        s = re.sub(r"(?<=\d)\.(?=\d\b)", "#", s)
        s = re.sub(r"(?i)\b([hx])\.(26[45])\b", r"\1\2", s)
        s = re.sub(r"[._]+", " ", s)
        s = re.sub(r"\s+-+\s+|\s+-+$|^-+\s+", " ", s)
        s = s.replace("#", ".")
        s = re.sub(r"\s+", " ", s).strip(" -")
        self.normalized = s
        toks = s.split(" ") if s else []
        self.tokens = toks
        low = " " + " ".join(t.lower() for t in toks) + " "
        self.low = low
        self.year = self.res = None
        marker = None
        i = 0
        while i < len(toks):
            t = toks[i]
            tl = t.lower()
            if YEAR_RE.match(t) and i > 0:
                # "Blade Runner 2049 2017": the last of consecutive years is the year
                if i + 1 < len(toks) and YEAR_RE.match(toks[i + 1]):
                    i += 1
                    continue
                marker = i if marker is None else marker
                self.year = self.year or t
            elif SE_RE.match(t) or XE_RE.match(t) or SRANGE_RE.match(t):
                marker = i if marker is None else marker
            elif tl in ("season", "seasons") and i + 1 < len(toks) and re.match(r"^\d{1,2}", toks[i + 1]) and i > 0:
                marker = i if marker is None else marker
                i += 1
            elif RES_RE.match(t) or tl in ("4k", "uhd"):
                marker = i if marker is None else marker
                self.res = self.res or tl
            elif STRONG_RE.match(t) and i > 0:
                marker = i if marker is None else marker
            i += 1
        title_toks = list(toks[:marker] if marker is not None else toks)
        # "Alien EXTENDED 1080p" -> drop trailing weak junk ("The Web" stays "The Web")
        while len(title_toks) > 1 and WEAK_RE.match(title_toks[-1]):
            if len(title_toks) == 2 and title_toks[0].lower() in ("the", "a", "an"):
                break
            title_toks.pop()
        if marker is None:  # no year/res/codec at all: cut at the first weak junk word after 2 words
            for j in range(2, len(title_toks)):
                if WEAK_RE.match(title_toks[j]):
                    title_toks = title_toks[:j]
                    break
        self.title = " ".join(title_toks).strip(" -")
        # ---- tags
        r = self.res or ""
        self.res_n = 2160 if r in ("4k", "uhd") else (int(r[:-1]) if r[:-1].isdigit() else None)
        self.source = _first(_SOURCE, low)
        self.codec = _first(_CODEC, low)
        self.audio = _first(_AUDIO, low)
        self.service = _first(_SERVICE, low)
        self.cuts = set(k for k, rx in _CUTS if rx.search(low))
        self.se_kind, self.season_n, self.episode_n, self.seasons = parse_se(low)
        self.se = self.se_label = se_label(self.se_kind, self.season_n, self.episode_n, self.seasons)
        if self.se_kind == "multi":
            self.se = None  # not useful as a search word
        video = bool(self.res_n or self.codec or self.source in ("remux", "disc", "webdl", "webrip", "bluray",
                                                                 "hdtv", "dvd", "hdrip"))
        self.media = "video" if video else ("audio" if _AUDIO_MEDIA_RE.search(low) else None)

    def tags(self):
        out = []
        if self.se_label:
            out.append(self.se_label)
        for v in (("%dp" % self.res_n) if self.res_n else None, self.source, self.codec, self.audio, self.service):
            if v:
                out.append(v)
        if self.group:
            out.append("grp " + self.group)
        return " ".join(out)


def looks_release(name):
    p = Parsed(name)
    return bool(p.year or p.se_kind or p.res_n or p.source or p.codec or p.group or p.audio)


def title_tokens(title):
    t = fold(title).lower().replace("&", " and ")
    t = re.sub(r"['’`´]", "", t)
    t = re.sub(r"[^\w]+", " ", t)
    return [x for x in t.split() if x]


def alt_titles(title):
    alts = []
    for part in re.split(r"(?i)\s+(?:aka|a\s*k\s*a)\s+|\s+/\s+|\s+\|\s+", title):
        part = part.strip(" -")
        if part:
            alts.append(part)
    extra = []
    for a in [title] + alts:
        if "&" in a:
            extra.append(re.sub(r"\s*&\s*", " and ", a))
        if re.search(r"(?i)\band\b", a):
            extra.append(re.sub(r"(?i)\s+and\s+", " & ", a))
        if re.search(r"['’`´]", a):
            extra.append(re.sub(r"['’`´]", "", a))
        if re.match(r"(?i)^the\s+", a):
            extra.append(re.sub(r"(?i)^the\s+", "", a))
        if ":" in a or " - " in a:
            extra.append(re.split(r"\s*[:]\s*|\s+-\s+", a)[0])
        folded = fold(a)
        if folded != a:
            extra.append(folded)
    out = []
    for a in alts + extra:
        if a and a.lower() != title.lower() and a not in out:
            out.append(a)
    return out


def clean_query(q):
    q = re.sub(r"[^\w\s'&+.\-]", " ", q, flags=re.U)
    q = " ".join(t.lstrip("-") for t in q.split())
    return re.sub(r"\s+", " ", q).strip(" .-")


def build_queries(names, cfg, variants=None, title_override=None):
    """names: release/file names to derive queries from (first = main)."""
    variants = variants or cfg["search"]["variants"]
    out = []

    def add(q, kind):
        q = clean_query(q)
        if len(q.replace(" ", "")) >= 3 and q.lower() not in [x[0].lower() for x in out]:
            out.append((q, kind))

    parsed = [Parsed(n) for n in names if n]
    for v in variants:
        for p in parsed:
            title = title_override or p.title or p.normalized
            tail = " ".join(x for x in (p.year, p.se) if x)
            if v == "title_year":
                add("%s %s" % (title, tail), v)
            elif v == "title_group":
                if p.group:
                    add("%s %s %s" % (title, p.se or "", p.group), v)
            elif v == "title_year_res":
                if p.res:
                    add("%s %s %s" % (title, tail, p.res), v)
            elif v == "clean":
                toks = [t for t in p.tokens if not JUNK_RE.match(t) or t in p.title.split()]
                add(" ".join(toks), v)
            elif v == "alt":
                for a in alt_titles(title):
                    add("%s %s" % (a, tail), v)
            elif v == "dotted":
                dt_title = re.sub(r"['’`´]", "", re.sub(r"\s*&\s*", " and ", title))
                add(".".join(re.sub(r"[^\w\s-]+", " ", dt_title + " " + tail).split()), v)
            elif v == "no_spaces":
                ns = re.sub(r"[\W_]+", "", title)
                if ns and ns != title:
                    add("%s %s" % (ns, tail), v)
            elif v == "raw":
                add(p.normalized, v)
            elif v == "title":
                add(title + (" " + p.se if p.se else ""), v)
            else:
                LOG.warn("unknown search variant %r in config" % v)
    return out


def title_similarity(local_titles, result_title):
    rp = result_title if isinstance(result_title, Parsed) else Parsed(result_title)
    rt = title_tokens(rp.title or rp.normalized)
    best = 0.0
    if not rt:
        return 0.0
    for lt_str in local_titles:
        lt = title_tokens(lt_str)
        if not lt:
            continue
        inter = len(set(lt) & set(rt))
        dice = 2.0 * inter / (len(set(lt)) + len(set(rt)))
        j1, j2 = "".join(lt), "".join(rt)
        seq = difflib.SequenceMatcher(None, j1, j2).ratio()
        s = max(dice, seq * 0.95, 1.0 if j1 == j2 else 0.0)
        best = max(best, s)
    return round(best, 3)


_ROMAN = {"ii": "2", "iii": "3", "iv": "4", "vi": "6", "vii": "7", "viii": "8", "ix": "9"}
_NUMWORDS = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7", "eight": "8",
             "nine": "9", "ten": "10"}
_TITLE_STOP = {"the", "a", "an", "and"}
PACK_WORDS = {"collection", "complete", "trilogy", "duology", "quadrilogy", "pentalogy", "hexalogy", "saga",
              "anthology", "boxset", "box", "set", "pack", "franchise", "films", "movies", "all", "parts", "series"}


def title_key(title, pack=False):
    """'The Lord of the Rings: The Two Towers' -> ['lord','of','rings','2','towers'] - what two names must share to
    be the same title. Articles, 'and', punctuation, accents, II/III and 'Part Two' don't matter."""
    toks = title_tokens(title)
    out = []
    for i, t in enumerate(toks):
        if t in _TITLE_STOP or re.match(r"^(?:cd|disc|disk)\d{1,2}$", t):
            continue
        if pack and (t in PACK_WORDS or t in ("film", "movie") or YEAR_RE.match(t) or
                     (t.isdigit() and i + 1 < len(toks) and toks[i + 1] in ("film", "films", "movie", "movies"))):
            continue  # 'Harry Potter Complete 8-Film Collection 2001-2011' -> 'harry potter'
        if t in _ROMAN:
            t = _ROMAN[t]
        elif t in _NUMWORDS and i > 0 and toks[i - 1] in ("part", "chapter", "vol", "volume", "episode", "book"):
            t = _NUMWORDS[t]
        elif t == "volume":
            t = "vol"
        out.append(t)
    return out


def title_match(ltitles, lyear, rp, pack=False):
    """Is the result the same TITLE as ours? -> 'same' | 'close' | None.
    same : identical once punctuation/articles/numbering style are ignored ('Spider-Man: No Way Home' =
           'Spider Man No Way Home', 'American Pie II' = 'American Pie 2')
    close: a spelling difference (>= 90% alike, same numbers), or one title is the other plus a subtitle
           AND both carry the same year ('Star Wars The Empire Strikes Back 1980' vs 'The Empire Strikes Back 1980')
    'Behind the Attraction' vs 'Attraction', 'American Pie 2' vs 'American Pie' -> None"""
    rtitle = rp.title or rp.normalized
    rts = [rtitle] + alt_titles(rtitle)
    same_year = bool(lyear and rp.year and str(lyear) == str(rp.year))
    best = None
    for lt in ltitles:
        a = title_key(lt, pack)
        if not a:
            continue
        ja = "".join(a)
        for rt in rts:
            b = title_key(rt, pack)
            if not b:
                continue
            jb = "".join(b)
            if ja == jb:
                return "same"
            if best:
                continue
            nums_a, nums_b = [x for x in a if x.isdigit()], [x for x in b if x.isdigit()]
            if nums_a == nums_b and min(len(ja), len(jb)) >= 5 and \
                    difflib.SequenceMatcher(None, ja, jb).ratio() >= 0.9:
                best = "close"
            elif same_year:
                short, long_ = (a, b) if len(a) < len(b) else (b, a)
                if len(short) >= 2 and (long_[:len(short)] == short or long_[-len(short):] == short):
                    best = "close"
    return best


# --------------------------------------------------------------------------- step 1: scan


class LF:
    __slots__ = ("path", "root", "size", "dev", "ino", "nlink", "is_link", "assoc", "item")

    def __init__(self, path, root, st, is_link):
        self.path, self.root = path, root
        self.size, self.dev, self.ino, self.nlink = st.st_size, st.st_dev, st.st_ino, st.st_nlink
        self.is_link = is_link
        self.assoc = None
        self.item = None


def scan_local(cfg):
    sc = cfg["scan"]
    ign_ext = set(e.lower() for e in sc["ignore_extensions"])
    ign_names = set(sc["ignore_names"])
    ign_dirs = set(sc["ignore_dirs"])
    min_size = int(float(sc.get("min_file_size_mb", 0)) * 1024 * 1024)
    follow = bool(sc.get("follow_symlinks"))
    files, skipped = [], 0
    roots = []
    for r in sc["paths"]:
        rn = norm(os.path.expanduser(r))
        if not os.path.isdir(rn):
            LOG.warn("scan path does not exist / not mounted: %s" % rn)
            continue
        roots.append(rn)
    prog = Progress("scanning disk", unit="files")
    seen_dirs = set()
    for root in roots:
        stack = [root]
        while stack:
            d = stack.pop()
            try:
                dst = os.stat(d)
                key = (dst.st_dev, dst.st_ino)
                if key in seen_dirs:
                    continue
                seen_dirs.add(key)
                it = os.scandir(d)
            except OSError as e:
                LOG.warn("cannot read %s: %s" % (d, e))
                continue
            with it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=follow):
                            if e.name not in ign_dirs:
                                stack.append(e.path)
                            else:
                                LOG.file("ignored dir %s" % e.path)
                            continue
                        is_link = e.is_symlink()
                        if not e.is_file(follow_symlinks=True):
                            continue
                        if e.name in ign_names or os.path.splitext(e.name)[1].lower() in ign_ext:
                            skipped += 1
                            LOG.file("ignored file %s" % e.path)
                            continue
                        st = e.stat(follow_symlinks=True)
                    except OSError as ex:
                        LOG.warn("cannot stat %s: %s" % (e.path, ex))
                        continue
                    if st.st_size < min_size:
                        skipped += 1
                        continue
                    files.append(LF(norm(e.path), root, st, is_link))
                    prog.update(1, d[-70:])
    prog.close("disk: %d files (%s) under %d paths, %d ignored" %
               (len(files), fmt_size(sum(f.size for f in files)), len(roots), skipped))
    return files, roots


def fetch_qbit_torrents(qb):
    use_include = qb.webapi >= (2, 11, 8)
    LOG.info("qBittorrent: reading torrent list%s ..." % (" with files (includeFiles)" if use_include else ""))
    t0 = time.time()
    torrents = qb.torrents(include_files=use_include)
    LOG.info("qBittorrent: %d torrents (%.1fs)" % (len(torrents), time.time() - t0))
    need = [t for t in torrents if "files" not in t]
    if need:
        prog = Progress("qBittorrent file lists", total=len(need), unit="torrents")
        lock = threading.Lock()

        def job(t):
            return t, qb.files(t["hash"])

        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = [ex.submit(job, t) for t in need]
            for fut in as_completed(futs):
                try:
                    t, fl = fut.result()
                    t["files"] = fl
                    with lock:
                        prog.update(1, t.get("name", "")[:60])
                except Exception as e:  # keep going, report
                    LOG.warn("file list failed: %s" % e)
                    prog.update(1)
        prog.close()
    nfiles = sum(len(t.get("files") or []) for t in torrents)
    nometa = [t for t in torrents if not t.get("files")]
    LOG.info("qBittorrent: %d files in %d torrents (%d torrents without metadata/files)"
             % (nfiles, len(torrents), len(nometa)))
    return torrents


def step_scan(cfg, paths, qb=None):
    LOG.info(LOG.c("bold", "\n=== 1) scan folders vs qBittorrent ==="))
    pm = PathMap(cfg["scan"]["path_mappings"])
    local, roots = scan_local(cfg)
    if not roots:
        raise Fatal("none of the scan paths exist")
    if qb is None:
        qb = QBit(cfg)
        qb.login()
    torrents = fetch_qbit_torrents(qb)

    # ---- index of every path qBittorrent points at
    path_index = {}
    tfiles = []  # (torrent, file, candidate local paths)
    containers = set(roots)

    def add_container(p):
        """p (a torrent save path etc.) and its parents up to the scan root hold releases, they aren't items"""
        p = norm(p)
        for r in roots:
            if p == r or p.startswith(r.rstrip("/") + "/"):
                while True:
                    containers.add(p)
                    if p == r:
                        break
                    p = os.path.dirname(p)
                break

    for t in torrents:
        sp = pm.to_local(t.get("save_path") or "")
        dp = pm.to_local(t.get("download_path") or "")
        cp = pm.to_local(t.get("content_path") or "")
        for p in (sp, dp):
            if p:
                add_container(p)
        tf_list = t.get("files") or []
        for f in tf_list:
            name = f.get("name") or ""
            cands = []
            # content_path = real current location (includes temp/download path while incomplete)
            if cp:
                if len(tf_list) == 1:
                    cands.append(cp)
                elif name.startswith(os.path.basename(cp) + "/"):
                    cands.append(os.path.join(os.path.dirname(cp), name))
                else:
                    cands.append(os.path.join(cp, name))
            for b in (sp, dp):
                if b:
                    cands.append(os.path.join(b, name))
            cands = list(dict.fromkeys(norm(c) for c in cands))
            for c in cands:
                path_index.setdefault(c, t)
                path_index.setdefault(c + ".!qB", t)
            tfiles.append((t, f, cands))
    prefs = qb.preferences()
    default_save = pm.to_local(prefs.get("save_path") or "")
    for p in (default_save, pm.to_local(prefs.get("temp_path") or "")):
        if p:
            add_container(p)
    for cname, cat in (qb.categories() or {}).items():
        p = cat.get("savePath") or ""
        if p and not os.path.isabs(p) and default_save:
            p = os.path.join(default_save, p)
        elif not p and default_save:
            p = os.path.join(default_save, cname)  # auto TMM default: <default save path>/<category>
        p = pm.to_local(p)
        if p:
            add_container(p)
    LOG.debug("container folders (not items): %d" % len(containers))
    for c in sorted(containers):
        LOG.file("container: %s" % c)

    # ---- sanity: can we see qBittorrent's files from here?
    sample = tfiles[:: max(1, len(tfiles) // 300)][:300]
    seen = sum(1 for _, _, cands in sample if any(os.path.exists(c) or os.path.exists(c + ".!qB") for c in cands))
    if sample:
        LOG.info("path visibility check: %d/%d sampled torrent files exist on this machine" % (seen, len(sample)))
        if seen < len(sample) * 0.5:
            LOG.warn("most torrent files are NOT visible from here -> qBittorrent probably uses different paths. "
                     "Set scan.path_mappings. Example torrent path: %s" % (sample[0][2][:1] or ["?"])[0])

    # ---- pass 1: exact path
    for lf in local:
        t = path_index.get(lf.path)
        if t is None and lf.is_link:
            t = path_index.get(norm(os.path.realpath(lf.path)))
            if t is not None:
                lf.assoc = ("symlink", t["hash"], t.get("name", ""))
                continue
        if t is not None:
            lf.assoc = ("path", t["hash"], t.get("name", ""))
    by_path = sum(1 for f in local if f.assoc)
    LOG.info("pass 1 (same path): %d files belong to torrents" % by_path)

    # ---- pass 2: same inode (hardlink / bind mount) - only stat torrent files with a size we care about
    rest = [f for f in local if not f.assoc]
    sizes = set(f.size for f in rest)
    by_ino = {}
    for f in rest:
        by_ino.setdefault((f.dev, f.ino), []).append(f)
    todo = [x for x in tfiles if int(x[1].get("size") or -1) in sizes]
    missing_on_disk = {}
    prog = Progress("pass 2 inode check", total=len(todo), unit="files")
    hard = 0
    for t, f, cands in todo:
        found = False
        for c in cands:
            for cc in (c, c + ".!qB"):
                try:
                    st = os.stat(cc)
                except OSError:
                    continue
                found = True
                hits = by_ino.get((st.st_dev, st.st_ino))
                if hits:
                    for lf in hits:
                        if not lf.assoc:
                            lf.assoc = ("hardlink", t["hash"], t.get("name", ""), cc)
                            hard += 1
                break
            if found:
                break
        if not found:
            missing_on_disk[t["hash"]] = t
        prog.update(1, (f.get("name") or "")[-60:])
    prog.close("pass 2 (same inode / hardlink): %d more files belong to torrents; %d torrents have same-size "
               "files that are not visible on disk" % (hard, len(missing_on_disk)))

    # ---- pass 3: torrents we can't see on disk: claim a local folder only if the torrent's WHOLE inner layout
    #      (relative paths without the top folder + sizes) exists there. Protects generic names like "file.mkv".
    weak = []
    if cfg["scan"].get("name_size_fallback", True) and missing_on_disk:
        free = {}
        for lf in local:
            if not lf.assoc:
                free.setdefault((os.path.basename(lf.path).lower(), lf.size), []).append(lf)
        free_by_path = {lf.path: lf for lf in local if not lf.assoc}
        for t in missing_on_disk.values():
            fl = [f for f in (t.get("files") or []) if int(f.get("size") or 0) > 0]
            if not fl:
                continue
            multi = len(t.get("files") or []) > 1 or "/" in (fl[0].get("name") or "")
            inner = []
            for f in fl:
                parts = (f.get("name") or "").split("/")
                inner.append(("/".join(parts[1:]) if multi and len(parts) > 1 else parts[-1], int(f["size"])))
            anchor = max(inner, key=lambda x: x[1])
            for lf in free.get((os.path.basename(anchor[0]).lower(), anchor[1]), []):
                if lf.assoc or not lf.path.lower().endswith("/" + anchor[0].lower()):
                    continue
                root = lf.path[:len(lf.path) - len(anchor[0]) - 1]
                group = []
                for rel, size in inner:
                    cand = free_by_path.get(os.path.join(root, rel))
                    if cand is None or cand.size != size or cand.assoc:
                        group = None
                        break
                    group.append(cand)
                if group:
                    qroot = pm.to_local(t.get("content_path") or t.get("save_path") or "")
                    for g in group:
                        g.assoc = ("name+size", t["hash"], t.get("name", ""), qroot)
                        weak.append((g, t, qroot))
                    break
        LOG.info("pass 3 (torrent not visible on disk, whole file layout found by name+size): %d files" % len(weak))

    for lf in local:
        if lf.assoc:
            LOG.file("ASSOC %-9s %s <- [%s] %s" % (lf.assoc[0], lf.path, lf.assoc[1][:8], lf.assoc[2]))
        else:
            LOG.file("ORPHAN %s (%s, nlink=%d)" % (lf.path, fmt_size(lf.size), lf.nlink))

    # ---- group into releases: a movie folder with its screens/nfo, an album with its CDs, a series pack with
    #      its season folders... Files are never searched one by one.
    releases, leftovers, verdict = build_releases(local, roots, containers, cfg)
    auto = sorted(d for d, (v, why) in verdict.items() if v and d not in containers)
    if auto:
        LOG.debug("folders treated as containers (hold releases, are not releases): %s" %
                  ", ".join(os.path.relpath(d, next((r for r in roots if d.startswith(r)), "/")) for d in auto[:40]))
    for d, (v, why) in sorted(verdict.items()):
        if v:
            LOG.file("container: %s (%s)" % (d, why))

    orphans = sorted([f for f in local if not f.assoc], key=lambda x: x.path)
    stats = {"local_files": len(local), "local_bytes": sum(f.size for f in local),
             "torrents": len(torrents), "torrent_files": len(tfiles),
             "assoc_path": by_path, "assoc_hardlink": hard, "assoc_name_size": len(weak),
             "orphan_files": len(orphans), "orphan_bytes": sum(f.size for f in orphans),
             "orphan_releases": len(releases), "orphan_release_bytes": sum(r["orphan_size"] for r in releases),
             "leftover_releases": len(leftovers)}
    write_json(paths.orphans_json, {"version": 3, "generated": now_iso(), "roots": roots, "stats": stats,
                                    "releases": releases,
                                    "leftovers": [{"path": p, "orphans": [f.path for f in o]} for p, fl, o in leftovers]})
    write_text(paths.orphan_files_txt, ["# every single file no torrent points at (debug list). The list to read is "
                                        "orphans.txt", ""] + [f.path for f in orphans])
    per_root = {}
    for r in releases:
        per_root.setdefault(r["root"], []).append(r)
    lines = ["# orphan releases - things on disk that no torrent in qBittorrent covers (FULL) or only partly covers "
             "(PART).",
             "# One line per release (movie folder incl. screens/nfo, album incl. CDs, series pack, collection...).",
             "# Indented below it: season folders (also inside packs), sub-releases and each movie of a collection -",
             "# step 2 searches the release as a whole AND each of those.",
             "# generated %s   every single file: orphan_files.txt   releases with only extras (nfo/srt/jpg) "
             "unseeded: orphan_extras.txt" % now_iso(), ""]
    for root in roots:
        rs = sorted(per_root.get(root, []), key=lambda r: r["path"].lower())
        lines.append("## %s   (%d releases, %s orphaned)" % (root, len(rs), fmt_size(sum(r["orphan_size"] for r in rs))))
        for r in rs:
            full = r["orphan_count"] == r["file_count"]
            what = "%d files" % r["file_count"] if full else "%d of %d files orphaned" % (r["orphan_count"],
                                                                                       r["file_count"])
            lines.append("%s %10s  %-12s %s%s   [%s]" % ("FULL" if full else "PART", fmt_size(r["orphan_size"]),
                                                       unit_label(r), r["path"], "/" if r["is_dir"] else "", what))
            for u in r["units"]:
                lines.append("%s %10s  %-12s %s%s" % (" " * 4, fmt_size(u["total_size"]), unit_label(u),
                                                      os.path.relpath(u["path"], r["path"]), "/" if u["is_dir"] else ""))
        lines.append("")
    write_text(paths.orphans_txt, lines)
    ex = ["# releases where only small extra files (nfo, srt, jpg, samples...) are not covered by a torrent - usually "
          "added later by you or Bazarr. Not searched.", ""]
    for p, fl, o in sorted(leftovers, key=lambda x: x[0]):
        ex.append("%s  (%d of %d files, %s)" % (p, len(o), len(fl), fmt_size(sum(f.size for f in o))))
        for f in o[:20]:
            ex.append("      %s" % os.path.relpath(f.path, p) if f.path != p else "      (the file itself)")
        if len(o) > 20:
            ex.append("      ... %d more" % (len(o) - 20))
    write_text(paths.extras_txt, ex)
    write_text(paths.weak_txt, ["# local files counted as seeded only because a torrent that is NOT visible on disk "
                                "from here has exactly the same file layout (names + sizes)", ""] +
               ["%s\t%s\t[%s] %s\t(qbit path: %s)" % (lf.path, fmt_size(lf.size), t["hash"][:8], t.get("name", ""), q)
                for lf, t, q in weak])
    LOG.ok("orphans: %d releases (%s), %d season folders / sub-releases inside them; %d releases with only "
           "extras unseeded (not searched)" % (len(releases), fmt_size(stats["orphan_release_bytes"]),
                                                sum(len(r["units"]) for r in releases), len(leftovers)))
    for root in roots:
        rs = per_root.get(root, [])
        LOG.info("   %-45s %4d releases  %s" % (root, len(rs), fmt_size(sum(r["orphan_size"] for r in rs))))
    LOG.info("-> %s   (the list)\n-> %s\n-> %s\n-> %s" % (paths.orphans_txt, paths.extras_txt, paths.orphan_files_txt,
                                                         paths.orphans_json))
    if weak:
        LOG.info("-> %s (%d name+size matches, check them)" % (paths.weak_txt, len(weak)))
    return releases


MB = 1024 * 1024


def unit_label(u):
    if u.get("collection"):
        return "collection"
    if u.get("kind") == "work":
        return "movie" if u.get("media") == "video" else "item"
    se = u.get("se") or [None]
    lab = se_label(*se) if se[0] else None
    if u.get("media") == "audio":
        return "audio"
    if u.get("media") == "video":
        return ("TV " + lab) if lab else "video"
    return lab or "other"


def build_releases(local, roots, containers, cfg):
    sc = cfg["scan"]
    cdn = set(x.lower() for x in sc.get("container_dir_names") or [])
    if sc.get("auto_container_names", True):
        cdn |= CONTAINER_NAMES
    child_dirs, direct = {}, {}
    for lf in local:
        d = os.path.dirname(lf.path)
        direct.setdefault(d, []).append(lf)
        while d != lf.root and d.startswith(lf.root.rstrip("/") + "/"):
            parent = os.path.dirname(d)
            child_dirs.setdefault(parent, set()).add(d)
            d = parent
    verdict = {}

    def is_container(d):
        if d in verdict:
            return verdict[d][0]
        name = os.path.basename(d)
        if d in containers and not looks_release(name):
            r = (True, "qBittorrent save/category path")
        elif name.lower() in cdn:
            r = (True, "category folder name")
        elif SEASON_DIR_RE.match(name) or GENERIC_DIR_RE.match(name) or looks_release(name):
            r = (False, "")
        else:
            # holds releases of DIFFERENT things -> container. Seasons of one show ('Show/Season 1', 'Show/Show.S02...')
            # and a franchise ('Harry Potter/Harry.Potter.and.the...') are one release with parts, searched both ways.
            keys = set()
            for c in child_dirs.get(d, ()):
                cn = os.path.basename(c)
                if looks_release(cn):
                    k = work_key(cn)
                    if k:
                        keys.add(k[:2])
            direct_content = any(file_kind(f.path) in ("video", "audio") for f in direct.get(d, []))
            own = "".join(title_key(Parsed(name).title or name, pack=True))
            franchise = bool(own) and bool(keys) and all(k[1].startswith(own) for k in keys)
            r = (len(keys) >= 2 and not direct_content and not franchise,
                 "holds %d different releases, no media of its own" % len(keys))
        verdict[d] = r
        return r[0]

    for lf in local:
        parts = os.path.relpath(lf.path, lf.root).split(os.sep)
        cur = lf.root
        for i, part in enumerate(parts):
            cand = os.path.join(cur, part)
            if i == len(parts) - 1 or not is_container(cand):
                lf.item = cand
                break
            cur = cand
    groups = {}
    for lf in local:
        groups.setdefault(lf.item, []).append(lf)
    min_content = float(sc.get("min_orphan_content_mb", 20)) * MB
    releases, leftovers = [], []
    for rpath, fl in groups.items():
        orph = [f for f in fl if not f.assoc]
        if not orph:
            continue
        if sum(f.size for f in orph if file_kind(f.path) in CONTENT_KINDS) < min_content:
            leftovers.append((rpath, fl, orph))
            continue
        releases.append(make_release(rpath, fl, min_content))
    releases.sort(key=lambda r: r["path"])
    return releases, leftovers, verdict


def work_key(name):
    """Identity of a movie-like item. Same title + year + resolution + edition = the same work; seasons / episodes of
    one show share one key. None for 'Season 1', 'Extras', 'CD1'..."""
    if SEASON_DIR_RE.match(name) or GENERIC_DIR_RE.match(name):
        return None
    p = Parsed(name)
    k = "".join(title_key(p.title or p.normalized))
    if not k:
        return None
    if p.se_kind:
        return ("show", k)
    return ("movie", k, p.year, p.res_n, tuple(sorted(p.cuts)))


TRACK_RE = re.compile(r"^\d{1,3}(?:\s*[-._)\]]\s*|\s+)")
MAX_WORKS = 30


def works_in(direct_files, subdirs, min_content):
    """Two or more DIFFERENT movies directly in one folder - as loose files ('American Pie/American.Pie.1999...mkv',
    'American.Pie.2.2001...mkv') or one folder each ('Harry Potter/Harry Potter and the Chamber of Secrets/') ->
    [(path, is_dir, files)], else []. Not movies: featurettes (< 1/4 of the biggest), episodes (S01E01, or 'Show - 01'
    / 'Show - 02' numbered without years), concert tracks ('01 - Song.mkv'), folders with > MAX_WORKS videos."""
    cands = []
    for f in direct_files:
        if file_kind(f.path) != "video":
            continue
        name = os.path.basename(f.path)
        p = Parsed(name)
        if p.se_kind or (TRACK_RE.match(name) and not (p.year or p.res_n)):
            continue
        k = work_key(name)
        if k:
            cands.append((k, f.path, False, [f], f.size))
    for dpath, sf in subdirs:
        name = os.path.basename(dpath)
        if SEASON_DIR_RE.match(name) or GENERIC_DIR_RE.match(name) or Parsed(name).se_kind:
            continue
        vids = [f for f in sf if file_kind(f.path) == "video"]
        vbytes = sum(f.size for f in vids)
        content = sum(f.size for f in sf if file_kind(f.path) in CONTENT_KINDS)
        if not vids or vbytes < 0.5 * content or any(Parsed(os.path.basename(f.path)).se_kind for f in vids):
            continue  # music, or a show
        mk = work_key(os.path.basename(max(vids, key=lambda f: f.size).path))
        k = work_key(name) or mk
        if k and mk and mk[0] == "movie" and k[0] == "movie":  # 'Harry Potter and the...' (no year) + the file's year
            k = (k[0], k[1], k[2] or mk[2], k[3] or mk[3], k[4] or mk[4])
        if k:
            cands.append((k, dpath, True, sf, vbytes))
    if len(cands) < 2:
        return []
    big = max(c[4] for c in cands)
    cands = [c for c in cands if c[4] >= max(min_content, 0.25 * big)]
    # 'Show - 01', 'Show - 02' (no years) are episodes; 'American Pie 1999', 'American Pie 2 2001' are movies
    by_base = {}
    for c in cands:
        by_base.setdefault(re.sub(r"\d+$", "", c[0][1]), []).append(c)
    drop = set()
    for grp in by_base.values():
        if len(set(c[0] for c in grp)) >= 2:
            years = [c[0][2] for c in grp if c[0][0] == "movie"]
            if len(years) < len(grp) or None in years or len(set(years)) < len(set(c[0][1] for c in grp)):
                drop.update(id(c) for c in grp if re.search(r"\d$", c[0][1]) or len(grp) > 2)
    cands = [c for c in cands if id(c) not in drop]
    keys = set(c[0] for c in cands)
    if len(keys) < 2 or len(cands) > MAX_WORKS:
        return []
    return [(c[1], c[2], c[3]) for c in cands]


def _make_unit(upath, kind, files, root, release_path, ancestors=()):
    kinds = [(f, file_kind(f.path)) for f in files]
    content = [f for f, k in kinds if k in CONTENT_KINDS]
    video = sum(f.size for f, k in kinds if k == "video")
    audio = sum(f.size for f, k in kinds if k == "audio")
    main = max(content, key=lambda f: f.size) if content else None
    eps = []
    for f, k in kinds:
        if k == "video":
            p = Parsed(os.path.basename(f.path))
            if p.se_kind == "episode":
                eps.append({"path": f.path, "size": f.size, "season": p.season_n, "episode": p.episode_n,
                            "orphan": not f.assoc})
    name = os.path.basename(upath)
    p = Parsed(name)
    if kind == "season":
        m = SEASON_DIR_RE.match(name)
        se = ["season", int(m.group(1) or m.group(2)) if m else p.season_n, None, None]
    elif kind == "work":
        se = [None, None, None, None]
    elif p.se_kind:
        se = [p.se_kind, p.season_n, p.episode_n, list(p.seasons) if p.seasons else None]
    else:
        seasons = sorted(set(e["season"] for e in eps))
        se = (["season", seasons[0], None, None] if len(seasons) == 1 else
              ["multi", None, None, seasons] if seasons else [None, None, None, None])
    return {
        "id": short_id(upath), "kind": kind, "path": upath, "name": name, "root": root, "release_path": release_path,
        "is_dir": os.path.isdir(upath), "ancestors": list(ancestors),
        "total_size": sum(f.size for f in files), "content_size": sum(f.size for f in content),
        "orphan_size": sum(f.size for f in files if not f.assoc),
        "orphan_content_size": sum(f.size for f in content if not f.assoc),
        "file_count": len(files), "orphan_count": sum(1 for f in files if not f.assoc),
        "main": main.path if main else None, "main_size": main.size if main else 0,
        "main_orphan": bool(main and not main.assoc),
        "media": "video" if video and video >= audio else ("audio" if audio else "other"),
        "se": se, "_eps": eps,
    }


def make_release(rpath, fl, min_content):
    """A release + everything inside it that is worth its own search, at any depth:
    - season folders, also inside a pack ('Show (2022)/Show S01-S05/Show S01/')
    - multi-season packs and sub-releases ('Show S01-S05/', an album inside a discography)
    - each movie of a collection ('American Pie/American.Pie.2.2001...mkv', 'Harry Potter/<one folder per film>/')
    The release itself is searched too (complete series / collection torrents)."""
    root = fl[0].root
    rel = _make_unit(rpath, "release", fl, root, rpath)
    rel["collection"] = False
    units = []
    if rel["is_dir"]:
        direct, under, children = {}, {rpath: list(fl)}, {}
        for f in fl:
            d = os.path.dirname(f.path)
            direct.setdefault(d, []).append(f)
            while d != rpath and d.startswith(rpath + "/"):
                under.setdefault(d, []).append(f)
                parent = os.path.dirname(d)
                children.setdefault(parent, set()).add(d)
                d = parent
        min_sub = max(min_content, 0.05 * rel["content_size"])

        def add(u):
            if u["orphan_content_size"] > 0:
                u.pop("_eps", None)
                units.append(u)

        def walk(d, anc, depth):
            subs = [(c, under[c]) for c in sorted(children.get(d, ()))]
            works = works_in(direct.get(d, []), subs, min_content)
            work_dirs = set(w[0] for w in works if w[1])
            if works and d == rpath:
                rel["collection"] = True
            for wpath, is_dir, wf in works:
                if not is_dir:
                    add(_make_unit(wpath, "work", wf, root, rpath, anc))
            for c, sf in subs:
                name = os.path.basename(c)
                if GENERIC_DIR_RE.match(name):
                    continue  # Screens/, Extras/, CD1/ belong to their parent
                p = Parsed(name)
                if c in work_dirs:
                    kind = "work"
                elif SEASON_DIR_RE.match(name) or p.se_kind == "season":
                    kind = "season"
                elif looks_release(name) or (" - " in name and any(file_kind(f.path) == "audio" for f in sf)):
                    kind = "sub"
                else:
                    kind = None
                if kind:
                    u = _make_unit(c, kind, sf, root, rpath, anc)
                    if not (kind == "sub" and u["content_size"] < min_sub):
                        add(u)
                if depth < 8:
                    walk(c, [name] + anc, depth + 1)

        walk(rpath, [rel["name"]], 0)
        units.sort(key=lambda u: u["path"])
        seasons = sorted(set(u["se"][1] for u in units if u["kind"] == "season" and u["se"][1] is not None))
        if rel["se"][0] is None and len(seasons) > 1 and not rel["collection"]:
            rel["se"] = ["multi", None, None, seasons]
    rel["episodes"] = rel.pop("_eps")
    rel["units"] = units
    rel["files"] = [{"path": f.path, "size": f.size, "orphan": not f.assoc, "kind": file_kind(f.path),
                     "dev": f.dev, "ino": f.ino, "assoc": (list(f.assoc[:3]) if f.assoc else None)}
                    for f in sorted(fl, key=lambda x: x.path)]
    return rel


# --------------------------------------------------------------------------- step 2: search


TAG_RULES = ["season_episode", "resolution", "source", "codec", "group", "service", "edition", "year", "media"]


def size_tolerance(rsize):
    """Indexers that scrape '29.05 GB' give rounded byte counts. Detect that and return the rounding slack."""
    for base in (1024, 1000):
        for exp in (4, 3, 2):
            unit = float(base ** exp)
            v = rsize / unit
            if v < 1 or v >= 10000:
                continue
            for d in (0, 1, 2):
                if abs(round(v, d) * unit - rsize) <= 2:
                    return 0.5 * (10 ** -d) * unit + 2
    return 0.0


def size_match(rsize, sizes, thr, close=0.001):
    """-> (kind, label, rel_diff): exact | rounded (within the indexer's display rounding) |
    close (within 0.1%: an nfo/screens difference) | near (within thr)"""
    if not rsize or rsize <= 0:
        return None
    tol = size_tolerance(rsize)
    best = None
    for label, lsize in sizes:
        if lsize <= 0:
            continue
        diff = rsize - lsize
        rel = diff / float(lsize)
        if diff == 0:
            return ("exact", label, 0.0)
        if tol and abs(diff) <= tol and tol / lsize <= 0.002:
            cand = ("rounded", label, rel)
        elif label in ("file", "main file only"):
            continue  # a torrent of one file is byte-identical to it: only exact / indexer-rounded sizes count
        elif abs(rel) <= close:
            cand = ("close", label, rel)
        elif abs(rel) <= thr:
            cand = ("near", label, rel)
        else:
            continue
        rank = ({"rounded": 0, "close": 1}.get(cand[0], 2), abs(rel))
        if best is None or rank < best[0]:
            best = (rank, cand)
    return best[1] if best else None


def source_compat(a, b):
    if not a or not b or a == b:
        return True
    webs = ("web", "webdl", "webrip")
    if a in webs and b in webs:
        return "web" in (a, b)
    return set((a, b)) == set(("bluray", "disc"))


def se_compat(tse, rp):
    tk, ts, te, tss = tse
    rk = rp.se_kind
    if tk == "episode":
        return rk == "episode" and rp.season_n == ts and rp.episode_n == te
    if rk is None and tk in ("season", "multi"):
        return True  # pack titles often carry no S01/S01-S05 at all - the size decides
    if tk == "season":
        return (rk == "season" and rp.season_n == ts) or (rk == "multi" and list(rp.seasons or []) == [ts])
    if tk == "multi":
        return rk == "multi" and (not tss or not rp.seasons or sorted(tss) == sorted(rp.seasons))
    return rk is None


class Target:
    """Something on disk a torrent could be for: a release, a collection, a season folder, a sub-release, one movie
    of a collection or one episode file."""

    def __init__(self, key, kind, path, unit, plist, se, sizes, media, titles=None, inherit=(), pack=False):
        self.key, self.kind, self.path, self.unit, self.se, self.sizes, self.media = \
            key, kind, path, unit, se, sizes, media
        self.pack = pack
        a = {}
        for k in ("year", "res_n", "source", "codec", "audio", "service", "group"):
            a[k] = next((getattr(p, k) for p in plist if getattr(p, k)), None)
            if a[k] is None and k != "year":  # a movie of a collection doesn't get the collection's year
                a[k] = next((getattr(p, k) for p in inherit if getattr(p, k)), None)
        a["cuts"] = next((p.cuts for p in plist if p.cuts), set())
        self.attrs = a
        if titles is None:
            titles = [p.title or p.normalized for p in plist]
        out = []
        for t in titles:
            if t and not SEASON_DIR_RE.match(t) and not GENERIC_DIR_RE.match(t):
                out.append(t)
                out.extend(alt_titles(t))
        self.titles = list(dict.fromkeys(out))
        lab = se_label(*se) if se and se[0] else None
        base = "collection" if pack else {"release": "release", "season": "season", "sub": "sub-release",
                                          "episode": "episode", "work": "movie"}.get(kind, kind)
        self.label = base + (" " + lab if lab else "")

    def tags(self):
        a = self.attrs
        out = [x for x in (("%dp" % a["res_n"]) if a["res_n"] else None, a["source"], a["codec"], a["audio"],
                           a["service"]) if x]
        if a["group"]:
            out.append("grp " + a["group"])
        return " ".join(out)


def _title_of(name):
    p = Parsed(name)
    t = p.title or p.normalized
    return None if not t or SEASON_DIR_RE.match(t) or GENERIC_DIR_RE.match(name) else t


def unit_target(rel, u):
    own = Parsed(u["name"])
    plist, titles = [own], [_title_of(u["name"])]
    pack = u["kind"] == "release" and bool(u.get("collection"))
    main = os.path.basename(u["main"]) if u.get("main") else None
    if main and u["is_dir"] and main != u["name"] and not pack:
        plist.append(Parsed(main))  # 'Your Highness (2011)/Your.Highness.2011...D-Z0N3.mkv' has the tags
        if looks_release(main):
            titles.append(_title_of(main))
    anc = [Parsed(n) for n in u.get("ancestors") or []]  # nearest first, up to the release
    if u["kind"] != "work":
        plist += anc  # season folders / sub-releases inherit the pack's tags and year
    if u["kind"] in ("season", "sub") or not any(titles):
        at = next((t for t in (_title_of(n) for n in u.get("ancestors") or []) if t), None)
        if at:
            titles.append(at)
            if u["kind"] == "sub" and titles[0]:  # 'Pink Floyd' + '1977 - Animals' -> 'Pink Floyd Animals'
                titles.append("%s %s" % (at, re.sub(r"^\s*(?:19|20)\d\d\s*", "", titles[0])))
    sizes = [("all files", u["total_size"])]
    if u["content_size"] and u["content_size"] != u["total_size"]:
        sizes.append(("without extras", u["content_size"]))
    if (u["se"][0] not in ("season", "multi") and not pack and u.get("main_size")
            and u["main_size"] >= 0.6 * u["content_size"] and u.get("main_orphan", True)
            and u["main_size"] not in [x[1] for x in sizes]):
        sizes.append(("main file only", u["main_size"]))
    return Target(u["id"], u["kind"], u["path"], u, plist, u["se"], sizes, u["media"],
                  titles=[t for t in titles if t], inherit=anc if u["kind"] == "work" else (), pack=pack)


def episode_target(rel, e):
    fname = os.path.basename(e["path"])
    plist = [Parsed(fname)]
    d = os.path.dirname(e["path"])
    while d.startswith(rel["path"]) and len(d) >= len(rel["path"]):
        plist.append(Parsed(os.path.basename(d)))
        if d == rel["path"]:
            break
        d = os.path.dirname(d)
    u = {"id": short_id(e["path"]), "kind": "episode", "path": e["path"], "name": fname, "root": rel["root"],
         "release_path": rel["path"], "is_dir": False, "total_size": e["size"], "content_size": e["size"],
         "orphan_size": e["size"] if e["orphan"] else 0, "orphan_content_size": e["size"] if e["orphan"] else 0,
         "file_count": 1, "orphan_count": 1 if e["orphan"] else 0, "main": e["path"], "main_size": e["size"],
         "media": "video", "se": ["episode", e["season"], e["episode"], None]}
    return Target(u["id"], "episode", e["path"], u, plist, u["se"], [("file", e["size"])], "video")


def tag_conflicts(t, rp, rules):
    a = t.attrs
    why = []
    if "season_episode" in rules and not se_compat(t.se, rp):
        why.append("%s vs %s" % (se_label(*t.se) or "no season/episode", rp.se_label or "no season/episode"))
    if "resolution" in rules and a["res_n"] and rp.res_n and a["res_n"] != rp.res_n:
        why.append("res %dp≠%dp" % (a["res_n"], rp.res_n))
    if "source" in rules and not source_compat(a["source"], rp.source):
        why.append("source %s≠%s" % (a["source"], rp.source))
    if "codec" in rules and a["codec"] and rp.codec and a["codec"] != rp.codec:
        why.append("codec %s≠%s" % (a["codec"], rp.codec))
    if "group" in rules and group_match(a["group"], rp.group) is False:
        why.append("group %s≠%s" % (a["group"], rp.group))
    if "service" in rules and a["service"] and rp.service and a["service"] != rp.service:
        why.append("service %s≠%s" % (a["service"], rp.service))
    if "edition" in rules and a["cuts"] and rp.cuts and not (a["cuts"] & rp.cuts):
        why.append("edition %s≠%s" % ("/".join(sorted(a["cuts"])), "/".join(sorted(rp.cuts))))
    if "year" in rules and a["year"] and rp.year and abs(int(a["year"]) - int(rp.year)) > 1:
        why.append("year %s≠%s" % (a["year"], rp.year))
    if "media" in rules and t.media in ("video", "audio") and rp.media in ("video", "audio") and t.media != rp.media:
        why.append("%s≠%s" % (t.media, rp.media))
    return why


def tag_agreements(t, rp):
    a = t.attrs
    out = []
    if group_match(a["group"], rp.group):
        out.append("group")
    if a["res_n"] and a["res_n"] == rp.res_n:
        out.append("res")
    if a["source"] and rp.source and source_compat(a["source"], rp.source):
        out.append("source")
    if a["codec"] and a["codec"] == rp.codec:
        out.append("codec")
    if a["audio"] and a["audio"] == rp.audio:
        out.append("audio")
    if a["year"] and a["year"] == rp.year:
        out.append("year")
    return out


def assess(r, rp, t, cfg):
    """-> (candidate | None, reason, size match tuple or None)"""
    s = cfg["search"]
    thr = float(s["fuzzy_size_threshold"])
    sm = size_match(r.get("size"), t.sizes, thr, float(s.get("close_size_threshold", 0.001)))
    if not sm:
        return None, "size", None
    why = tag_conflicts(t, rp, set(s.get("reject_on") or TAG_RULES))
    if why:
        return None, ", ".join(why), sm
    tm = title_match(t.titles, t.attrs["year"], rp, pack=t.pack)
    sim = title_similarity(t.titles, rp)
    if not tm and not (sm[0] == "exact" and sim >= float(s["min_title_similarity"])):
        return None, "different title: %r vs %r" % ((t.titles or ["?"])[0], rp.title or rp.normalized), sm
    agree = tag_agreements(t, rp)
    if sm[0] == "near":
        pts = (2 if "group" in agree else 0) + sum(1 for k in ("res", "source", "codec") if k in agree)
        if pts < 2:
            return None, "size only near (%+.2f%%) and too few matching tags (%s)" % (
                100 * sm[2], ",".join(agree) or "none"), sm
    tsim = {"same": 1.0, "close": 0.9}.get(tm, sim)
    score = {"exact": 50.0, "rounded": 46.0, "close": 42.0}.get(sm[0]) or \
        max(20.0, 40.0 - 20.0 * abs(sm[2]) / thr)
    score += 20.0 * tsim
    score += 15 * ("group" in agree) + 4 * sum(1 for k in ("res", "source", "codec") if k in agree)
    score += 3 * ("audio" in agree) + 2 * ("year" in agree)
    if t.attrs["audio"] and rp.audio and t.attrs["audio"] != rp.audio:
        score -= 10
    score = round(min(100.0, score), 1)
    if score < float(s["min_score"]):
        return None, "score %.1f < min_score %.1f" % (score, float(s["min_score"])), sm
    return {
        "key": "%s|%s" % (r.get("indexerId"), r.get("guid")),
        "indexer": r.get("indexer"), "indexer_id": r.get("indexerId"),
        "title": r.get("title"), "size": r.get("size"), "files": r.get("files"),
        "guid": r.get("guid"), "download_url": r.get("downloadUrl"), "magnet_url": r.get("magnetUrl"),
        "info_hash": (r.get("infoHash") or "").lower() or None, "info_url": r.get("infoUrl"),
        "seeders": r.get("seeders"), "target": t.label,
        "size_match": sm[0], "size_target": sm[1], "size_diff": round(sm[2], 6),
        "title_sim": tsim, "title_match": tm or "similar, exact size", "agree": agree, "score": score,
    }, None, sm


HARD_RULES = ("season_episode", "year", "media")


def extras_size_match(rsize, sizes, bigger, smaller):
    """torrent a bit bigger (featurettes, extras, episodes you don't have) or a bit smaller than something of yours"""
    if not rsize or rsize <= 0:
        return None
    best = None
    for label, lsize in sizes:
        if lsize > 0:
            rel = (rsize - lsize) / float(lsize)
            if -smaller <= rel <= bigger and (best is None or abs(rel) < abs(best[2])):
                best = ("bigger" if rel > 0 else "smaller", label, rel)
    return best


def near_verdict(r, rp, t, cfg, reason, sm):
    """A rejected result that is still worth a look (option 5) -> {kind, why, sm, title} or None.
    Always: the SAME title (see title_match), year within 1, same season/episode, same media.
    kind 'label': the size fits (exact / rounded / within 0.1%) but a tag rule said no - mislabeled uploads.
    kind 'size' : nothing conflicts, but the torrent is up to near_miss_max_bigger_pct bigger (featurettes, extras,
                  episodes you don't have) or near_miss_max_smaller_pct smaller than yours."""
    s = cfg["search"]
    tm = title_match(t.titles, t.attrs["year"], rp, pack=t.pack)
    if not tm:
        return None
    rules = set(s.get("reject_on") or TAG_RULES)
    if tag_conflicts(t, rp, rules & set(HARD_RULES)):
        return None
    soft = tag_conflicts(t, rp, rules - set(HARD_RULES))
    if sm and sm[0] in ("exact", "rounded", "close"):
        if len(soft) > 1 and sm[0] != "exact":
            return None  # only a byte-identical size excuses several wrong labels at once
        return {"kind": "label", "why": reason, "sm": sm, "title": tm}
    if soft:
        return None  # other size AND other label = another encode, not yours
    ex = extras_size_match(r.get("size"), t.sizes, float(s.get("near_miss_max_bigger_pct", 20)) / 100.0,
                           float(s.get("near_miss_max_smaller_pct", 5)) / 100.0)
    if not ex:
        return None
    lsize = dict(t.sizes).get(ex[1], 0)
    why = "same title, %s %s than yours (%s)" % (fmt_size(abs(r.get("size") - lsize)), ex[0], ex[1])
    why += " - extras / files you don't have?" if ex[0] == "bigger" else " - files the torrent doesn't have?"
    if sm and sm[0] == "near":
        why += " | step 2: %s" % reason
    return {"kind": "size", "why": why, "sm": ex, "title": tm}


def evaluate_result(r, targets, ep_targets, cfg, qhashes):
    """-> (best candidate, its target, near misses [(target, near verdict)], log reason)"""
    rp = Parsed(r.get("title") or "")
    ih = (r.get("infoHash") or "").lower()
    if ih and ih in qhashes:
        return None, None, [], "already in qBittorrent (%s)" % ih[:8]
    if not r.get("downloadUrl") and not r.get("magnetUrl"):
        return None, None, [], "no download link"
    tl = list(targets)
    if rp.se_kind == "episode":
        tl += ep_targets.get((rp.season_n, rp.episode_n), [])
    best, best_t, near, whys = None, None, [], []
    for t in tl:
        c, why, sm = assess(r, rp, t, cfg)
        if c:
            if best is None or c["score"] > best["score"]:
                best, best_t = c, t
            continue
        if sm:
            whys.append("%s: %s" % (t.label, why))
        nv = near_verdict(r, rp, t, cfg, why, sm)
        if nv:
            near.append((t, nv))
    if best:
        return best, best_t, [], None
    if whys:
        return None, None, near, "; ".join(whys[:3])
    sizes = ", ".join("%s %s" % (t.label, fmt_size(t.sizes[0][1])) for t in tl[:4])
    return None, None, near, "no size match (%s vs %s)" % (fmt_size(r.get("size")), sizes)


def unit_item(rel, t):
    """The dict step 3/4 work with: the unit plus its files, 'rel' relative to the unit's parent folder."""
    upath = t.path
    if not t.unit.get("is_dir"):
        files = [f for f in rel["files"] if f["path"] == upath]
    elif upath == rel["path"]:
        files = rel["files"]
    else:
        files = [f for f in rel["files"] if f["path"].startswith(upath + "/")]
    parent = os.path.dirname(upath)
    item = {k: v for k, v in t.unit.items() if k not in ("files", "units", "episodes", "_eps")}
    item["files"] = [dict(f, rel=os.path.relpath(f["path"], parent)) for f in files]
    item["release_path"] = rel["path"]
    item["label"] = t.label
    return item


def release_search_names(rel):
    names = [rel["name"]]
    if rel.get("main") and not rel.get("collection"):
        fname = os.path.basename(rel["main"])
        p = Parsed(fname)
        a, b = title_tokens(p.title), title_tokens(Parsed(rel["name"]).title)
        # only if it looks like a release name, not "video.mkv" / "01 - Track.flac"
        if a != b and (p.year or p.res or p.group or len(a) >= 3) and p.se_kind != "episode":
            names.append(fname)
    return names


def step_search(cfg, paths, prow=None, qb=None):
    LOG.info(LOG.c("bold", "\n=== 2) search Prowlarr for orphan releases ==="))
    data = read_json(paths.orphans_json)
    if not data:
        raise Fatal("no %s - run step 1 first" % paths.orphans_json)
    if data.get("version") != 3:
        raise Fatal("%s is from the old version of this script - run step 1 again" % paths.orphans_json)
    s = cfg["search"]
    skip_res = [re.compile(x, re.I) for x in s.get("skip_items_regex") or []]
    rels = [r for r in data["releases"] if not any(x.search(r["path"]) for x in skip_res)]
    rels.sort(key=lambda r: -r["orphan_content_size"])  # most valuable first
    if int(s.get("max_items") or 0) > 0:
        rels = rels[:int(s["max_items"])]
    maxq, subq = int(s["max_queries_per_item"]), int(s.get("max_queries_per_sub_unit", 2))
    stop_at = float(s["stop_when_score_at_least"])
    nsub = sum(len(r["units"]) for r in rels)
    LOG.info("%d orphan releases (+%d season folders / sub-releases), biggest first - at most %d queries per release "
             "and %d per season/sub-release; cached searches are free" % (len(rels), nsub, maxq, subq))
    if not rels:
        return []
    qhashes = set()
    try:
        if qb is None:
            qb = QBit(cfg)
            qb.login()
        qhashes = qb.all_hashes()
    except (Fatal, NetError) as e:
        LOG.warn("qBittorrent not reachable (%s) - can't pre-filter results already in qBittorrent" % e)
    if prow is None:
        prow = Prowlarr(cfg, paths)
        prow.connect()
    out = []
    prog = Progress("searching", total=len(rels), unit="releases")
    total_live = 0
    for n, rel in enumerate(rels, 1):
        targets = [unit_target(rel, rel)] + [unit_target(rel, u) for u in rel["units"]]
        ep_targets = {}
        for e in rel.get("episodes") or []:
            if e["orphan"]:
                ep_targets.setdefault((e["season"], e["episode"]), []).append(episode_target(rel, e))
        LOG.info(LOG.c("bold", "[%d/%d] %s%s") % (n, len(rels), rel["path"], "/" if rel["is_dir"] else "") +
                 "  (%s, %s, %s)" % (fmt_size(rel["orphan_size"]), unit_label(rel), targets[0].tags() or "no tags"))
        if rel["units"]:
            LOG.debug("   inside: " + " | ".join("%s %s" % (t.label, fmt_size(t.sizes[0][1])) for t in targets[1:]))
        seen, best, cands, near, qlog, done, nms = {}, {}, {}, {}, [], set(), []
        tmap = {t.key: t for t in targets}

        def run(q, kind, owner):
            done.add(q.lower())
            prog.set_info("%s: %s" % (kind, q[:60]))
            try:
                res, cached, secs = prow.search(q)
            except NetError as e:
                LOG.warn("search failed: %s" % e)
                qlog.append({"query": q, "variant": kind, "for": owner, "error": str(e)})
                return 0
            new = 0
            for r in res:
                k = "%s|%s" % (r.get("indexerId"), r.get("guid"))
                if k in seen:
                    continue
                seen[k] = r
                new += 1
                c, t, nm, why = evaluate_result(r, targets, ep_targets, cfg, qhashes)
                if c:
                    c["query"] = q
                    tmap.setdefault(t.key, t)
                    cands.setdefault(t.key, []).append(c)
                    best[t.key] = max(best.get(t.key, 0), c["score"])
                    LOG.debug("   + %5.1f %-7s %+.3f%% sim=%.2f %s [%s] %s -> %s" % (
                        c["score"], c["size_match"], 100 * c["size_diff"], c["title_sim"], ",".join(c["agree"]),
                        c["indexer"], c["title"], t.label))
                else:
                    LOG.file("   - [%s] %s (%s): %s" % (r.get("indexer"), r.get("title"), fmt_size(r.get("size")), why))
                    if nm:  # same title, and the size fits but a label differs / the size is off by extras
                        nt, nv = min(nm, key=lambda x: near_rank(x[1]))
                        tmap.setdefault(nt.key, nt)
                        near.setdefault(nt.key, []).append({"indexer": r.get("indexer"), "title": r.get("title"),
                                                            "size": r.get("size"), "why": nv["why"]})
                        LOG.debug("   ~ near miss (%s, title %s) [%s] %s -> %s: %s" % (
                            nv["kind"], nv["title"], r.get("indexer"), r.get("title"), nt.label, nv["why"]))
                        nms.append({"key": k, "indexer": r.get("indexer"), "indexer_id": r.get("indexerId"),
                                    "title": r.get("title"), "size": r.get("size"), "guid": r.get("guid"),
                                    "download_url": r.get("downloadUrl"), "magnet_url": r.get("magnetUrl"),
                                    "info_url": r.get("infoUrl"), "target": nt.label, "target_key": nt.key,
                                    "near_kind": nv["kind"], "title_match": nv["title"], "why": nv["why"],
                                    "size_match": nv["sm"][0], "size_target": nv["sm"][1],
                                    "size_diff": round(nv["sm"][2], 6),
                                    "release": rel["path"], "query": q, "item": unit_item(rel, nt)})
            qlog.append({"query": q, "variant": kind, "for": owner, "results": len(res), "new": new, "cached": cached})
            b = max(best.values() or [0])
            LOG.info("   %-12s %-50s %3d results%s  best=%s%s" % (
                kind, q[:50], len(res), " (cache)" if cached else " %.1fs" % secs, "%.1f" % b if b else "-",
                "  (hit search_limit, results may be cut off)" if len(res) >= int(cfg["prowlarr"]["search_limit"])
                else ""))
            return len(res)

        coll = bool(rel.get("collection"))
        for q, kind in build_queries(release_search_names(rel), cfg, variants=s.get("sub_unit_variants") if coll
                                     else None)[:subq if coll else maxq]:
            run(q, kind, targets[0].label)
            if best.get(targets[0].key, 0) >= stop_at:
                break
        rtitle = Parsed(rel["name"]).title
        for t in targets[1:]:
            if best.get(t.key, 0) >= stop_at:
                continue
            u = t.unit
            if u["kind"] == "work":  # one movie of a collection: searched like a release of its own
                names = [u["name"]]
                main = os.path.basename(u["main"]) if u.get("main") else None
                if u["is_dir"] and main and main != u["name"] and looks_release(main):
                    names.append(main)
                qs = build_queries(names, cfg)[:maxq]
            else:
                if SEASON_DIR_RE.match(u["name"]):
                    names = ["%s S%02d" % (rtitle, u["se"][1])]
                else:
                    names = [u["name"]]
                qs = build_queries(names, cfg, variants=s.get("sub_unit_variants"))[:subq]
            for q, kind in qs:
                if q.lower() in done:
                    continue
                run(q, kind, t.label)
                if best.get(t.key, 0) >= stop_at:
                    break
        per_t, kept = {}, []
        for m in sorted(nms, key=lambda m: near_rank(m)):
            per_t[m["target_key"]] = per_t.get(m["target_key"], 0) + 1
            if per_t[m["target_key"]] <= int(s.get("near_misses_per_target", 3)):
                kept.append(m)
        nms = kept
        units_out = []
        for key in list(dict.fromkeys(list(cands.keys()) + list(near.keys()))):
            t = tmap[key]
            cl = sorted(cands.get(key, []), key=lambda c: -c["score"])[:int(s["max_candidates_per_item"])]
            units_out.append({"item": unit_item(rel, t), "label": t.label, "tags": t.tags(), "candidates": cl,
                              "rejected_same_size": near.get(key, [])[:int(s.get("show_rejected_per_item", 3))]})
        units_out.sort(key=lambda x: (x["item"]["kind"] != "release", x["item"]["path"]))
        ncand = sum(len(u["candidates"]) for u in units_out)
        for u in units_out:
            for c in u["candidates"]:
                LOG.info(LOG.c("green", "   => %5.1f %-7s %+.3f%% %-22s [%s] %s (%s)  -> %s" % (
                    c["score"], c["size_match"], 100 * c["size_diff"], ",".join(c["agree"]) or "-", c["indexer"],
                    c["title"], fmt_size(c["size"]), u["label"])))
        if not ncand:
            closest = [(u["label"], r) for u in units_out for r in u["rejected_same_size"]][:2]  # near misses
            LOG.info(LOG.c("dim", "   => no match (%d results checked)%s" % (
                len(seen), "".join("\n      closest: [%s] %s -> %s: %s" % (r["indexer"], r["title"], lab, r["why"])
                                   for lab, r in closest))))
        out.append({"release": {k: rel[k] for k in ("id", "path", "name", "is_dir", "orphan_size", "total_size",
                                                     "file_count", "orphan_count")},
                    "label": unit_label(rel), "tags": targets[0].tags(),
                    "inside": [{"label": t.label, "path": t.path, "size": t.sizes[0][1]} for t in targets[1:]],
                    "queries": qlog, "results_seen": len(seen), "units": units_out, "near_misses": nms})
        total_live += sum(1 for q in qlog if not q.get("cached") and "error" not in q)
        prog.update(1)
        if n % 5 == 0 or n == len(rels):
            save_candidates(paths, out, partial=n != len(rels))
    prog.close()
    save_candidates(paths, out, partial=False)
    nc = sum(len(u["candidates"]) for x in out for u in x["units"])
    nr = sum(1 for x in out if any(u["candidates"] for u in x["units"]))
    nn = sum(len(x.get("near_misses") or []) for x in out)
    LOG.ok("%d candidates for %d of %d releases (%d live searches, rest from cache); %d near misses (same title, "
           "other label or size off by extras) -> option 5" % (nc, nr, len(out), total_live, nn))
    LOG.info("-> %s\n-> %s\n-> %s" % (paths.cand_txt, paths.cand_json, paths.near_txt))
    return out


SIZE_RANK = {"exact": 0, "rounded": 1, "close": 2, "near": 3, "bigger": 4, "smaller": 5}


def near_rank(m):
    """best near misses first: size fits + other label, then same title before close title, then smallest difference"""
    kind = m.get("near_kind") or m.get("kind")
    sm0 = m["sm"][0] if "sm" in m else m["size_match"]
    diff = m["sm"][2] if "sm" in m else m["size_diff"]
    return (0 if kind == "label" else 1, 0 if (m.get("title_match") or m.get("title")) == "same" else 1,
            SIZE_RANK.get(sm0, 9), abs(diff))


def target_size(item, label):
    """the local size a result was compared with ('all files', 'without extras', 'main file only', 'file')"""
    if label == "without extras":
        return item.get("content_size")
    if label in ("main file only", "file"):
        return item.get("main_size") or item.get("total_size")
    return item.get("total_size")


def near_line(m):
    if m.get("near_kind") == "size":
        return "size %s %+.1f%% vs %s" % (m["size_match"], 100 * m["size_diff"], m["size_target"])
    return "size %s %+.3f%% vs %s" % (m["size_match"], 100 * m["size_diff"], m["size_target"])


def save_near_misses(paths, out, partial):
    nml = [m for x in out for m in (x.get("near_misses") or [])]
    nml.sort(key=lambda m: (m["release"].lower(), m["item"]["path"].lower(), near_rank(m)))
    for i, m in enumerate(nml, 1):
        m["no"] = i
    write_json(paths.near_json, {"version": 3, "generated": now_iso(), "complete": not partial, "near_misses": nml})
    lines = ["# near misses: results with the SAME TITLE as something of yours (same year, same season/episode) that",
             "# step 2 still rejected because",
             "#   LABEL: the size fits, but a label differs (720p vs 1080p, other group...) - mislabeled uploads", 
             "#   SIZE : nothing conflicts, but the torrent is a bit bigger (featurettes, extras, episodes you don't",
             "#          have) or smaller than yours",
             "# Menu option 5 downloads + checks them (one by one, all at once, or by number) and adds the ones whose",
             "# pieces really are your data (missing extras get downloaded when you Start). Anything failing the",
             "# piece check is never added.",
             "# generated %s%s" % (now_iso(), ", INCOMPLETE" if partial else ""), ""]
    cur = None
    for m in nml:
        if m["release"] != cur:
            cur = m["release"]
            lines.append(cur)
        it = m["item"]
        where = "" if it["path"] == m["release"] else "  %s%s" % (os.path.relpath(it["path"], m["release"]),
                                                                   "/" if it.get("is_dir") else "")
        lines.append("  #%-4d %-5s [%s] %s  (%s)" % (m["no"], (m.get("near_kind") or "label").upper(), m["indexer"],
                                                    m["title"], fmt_size(m["size"])))
        lines.append("        aims at: %s%s  (%s: %s)   %s, title %s" % (
            m["target"], where, m["size_target"], fmt_size(target_size(it, m["size_target"])), near_line(m),
            m.get("title_match") or "?"))
        lines.append("        %s: %s" % ("rejected" if m.get("near_kind") != "size" else "why", m["why"]))
    if not nml:
        lines.append("(none)")
    write_text(paths.near_txt, lines)


def save_candidates(paths, out, partial, show=5):
    save_near_misses(paths, out, partial)
    write_json(paths.cand_json, {"version": 3, "generated": now_iso(), "complete": not partial, "releases": out})
    lines = ["# possible matches for orphan releases  (generated %s%s)" % (now_iso(), ", INCOMPLETE" if partial else ""),
             "# score = size (50 exact, 46 within the indexer's rounding, 42 within 0.1%, 20-40 near) + 20 x title sim.",
             "#         + 15 same group + 4 each same res/source/codec + 3 audio + 2 year",
             "# a result is rejected when group, codec, source, resolution, streaming service, edition, year or",
             "# season/episode CONFLICT (missing tags on either side are fine), or when the TITLE differs.",
             "# '~ near miss' = same title, but a label differs or the size is off by extras -> option 5.", ""]
    for x in out:
        r = x["release"]
        lines.append("%s%s  (%s, %s, %s)" % (r["path"], "/" if r["is_dir"] else "", fmt_size(r["orphan_size"]),
                                             x["label"], x["tags"] or "no tags"))
        for i in x.get("inside") or []:
            lines.append("   inside: %-16s %10s  %s/" % (i["label"], fmt_size(i["size"]),
                                                         os.path.relpath(i["path"], r["path"])))
        lines.append("   queries: " + " | ".join("%s[%s]" % (q["query"], q.get("results", "err")) for q in x["queries"]))
        any_c = False
        for u in x["units"]:
            it = u["item"]
            where = "" if it["path"] == r["path"] else "  %s" % os.path.relpath(it["path"], r["path"])
            if u["candidates"]:
                any_c = True
                lines.append("   [%s]%s" % (u["label"], where))
                for c in u["candidates"]:
                    lines.append("      %5.1f  %-7s %+8.3f%%  sim %.2f  %-26s %-14s %s  (%s, vs %s)" % (
                        c["score"], c["size_match"], 100 * c["size_diff"], c["title_sim"],
                        "same: " + (",".join(c["agree"]) or "-"), "[%s]" % c["indexer"], c["title"],
                        fmt_size(c["size"]), c["size_target"]))
        near = [(u, rj) for u in x["units"] for rj in u["rejected_same_size"]]
        for u, rj in near[:show]:
            it = u["item"]
            where = "" if it["path"] == r["path"] else " %s" % os.path.relpath(it["path"], r["path"])
            lines.append("      ~  near miss -> %s%s: [%s] %s (%s) - %s" % (
                u["label"], where, rj["indexer"], rj["title"], fmt_size(rj["size"]), rj["why"]))
        if len(near) > show:
            lines.append("      ~  ... %d more near misses (near_misses.txt)" % (len(near) - show))
        if not any_c:
            lines.append("   => no match (%d results checked)" % x["results_seen"])
        lines.append("")
    write_text(paths.cand_txt, lines)


# --------------------------------------------------------------------------- step 3: download + verify


def match_tree(t, item):
    """Map torrent files to local item files by size (+name for ties).
    -> dict(mapping={tidx: localfile}, matched_bytes, total_bytes)"""
    local = [f for f in item["files"] if os.path.isfile(f["path"])]
    avail = list(local)
    mapping = {}
    for tf in sorted(t.files, key=lambda f: -f.size):
        if tf.size == 0:
            continue
        cands = [lf for lf in avail if lf["size"] == tf.size]
        if not cands:
            continue
        if len(cands) > 1:
            tb = os.path.basename(tf.path).lower()
            cands.sort(key=lambda lf: (os.path.basename(lf["path"]).lower() != tb,
                                       -difflib.SequenceMatcher(None, tb, os.path.basename(lf["path"]).lower()).ratio()))
        mapping[tf.idx] = cands[0]
        avail.remove(cands[0])
    total = sum(f.size for f in t.files)
    matched = sum(f.size for f in t.files if f.idx in mapping)
    return {"mapping": mapping, "matched_bytes": matched, "total_bytes": total}


def plan_layout(t, item, mapping, cfg):
    """Where to point qBittorrent and which torrent files to rename so they land on the local files."""
    plan = {"renames": [], "missing": [], "conflicts": []}
    by_idx = {f.idx: f for f in t.files}
    if not t.multi:
        lf = mapping.get(0)
        if not lf:
            plan["conflicts"].append("single file not matched")
            return plan
        plan["savepath"] = os.path.dirname(lf["path"])
        new = os.path.basename(lf["path"])
        if new != t.files[0].path:
            plan["renames"].append((0, t.files[0].path, new))
    else:
        save = os.path.dirname(item["path"])
        plan["savepath"] = save
        local_roots = set(os.path.relpath(lf["path"], save).split(os.sep)[0] for lf in mapping.values())
        local_root = next(iter(local_roots)) if (len(local_roots) == 1 and item["is_dir"]) else None
        troot = t.name
        targets = set()
        for f in t.files:
            lf = mapping.get(f.idx)
            if lf:
                new = os.path.relpath(lf["path"], save).replace(os.sep, "/")
                if new != f.path:
                    plan["renames"].append((f.idx, f.path, new))
                targets.add(new)
        for f in t.files:
            if f.idx in mapping:
                continue
            new = None
            if cfg["add"].get("rename_missing_into_local_folder", True) and local_root and \
                    f.path.startswith(troot + "/") and local_root != troot:
                cand = local_root + f.path[len(troot):]
                if cand not in targets and not os.path.exists(os.path.join(save, cand)):
                    new = cand
            plan["missing"].append((f.idx, f.path, new, f.size))
            if new:
                plan["renames"].append((f.idx, f.path, new))
                targets.add(new)
    # safety: a rename makes qBittorrent MOVE the file at the old path if one exists there
    save = plan.get("savepath")
    keep = []
    for idx, old, new in plan["renames"]:
        oabs = os.path.join(save, old)
        lf = mapping.get(idx)
        if os.path.lexists(oabs):
            try:
                same = lf is not None and os.path.samefile(oabs, lf["path"])
            except OSError:
                same = False
            if same:
                continue  # the torrent's own name is already a hardlink of the matched file: no rename needed
            plan["conflicts"].append("torrent path %s already exists and is not the matched file - a rename "
                                     "would move it" % oabs)
        keep.append((idx, old, new))
    plan["renames"] = keep
    news = [n for _, _, n in plan["renames"]]
    if len(news) != len(set(news)):
        plan["conflicts"].append("two torrent files would be renamed to the same path")
    olds_not_renamed = set(f.path for f in by_idx.values()) - set(o for _, o, _ in plan["renames"])
    for n in news:
        if n in olds_not_renamed:
            plan["conflicts"].append("rename target %s is also an untouched torrent path" % n)
    return plan


def spot_check(t, mapping, n):
    """SHA1-check up to n pieces fully covered by matched files. -> (ok, checked, detail)"""
    if n <= 0:
        return None, 0, "disabled"
    if not t.v1 or not t.pieces or not t.piece_length:
        return None, 0, "v2-only torrent, no SHA1 pieces"
    pl = t.piece_length
    npieces = len(t.pieces) // 20
    segs = []  # (start, end, TFile)
    off = 0
    for f in t.all_files:
        segs.append((off, off + f.size, f))
        off += f.size
    total = off

    def usable(f):
        return f.pad or f.size == 0 or f.idx in mapping

    eligible = []
    j = 0
    for k in range(npieces):
        a, b = k * pl, min((k + 1) * pl, total)
        while j < len(segs) and segs[j][1] <= a:
            j += 1
        jj, ok = j, True
        while jj < len(segs) and segs[jj][0] < b:
            if segs[jj][1] > segs[jj][0] and not usable(segs[jj][2]):
                ok = False
                break
            jj += 1
        if ok:
            eligible.append(k)
    if not eligible:
        return False, 0, "no piece is fully covered by matched files"
    if len(eligible) <= n:
        chosen = eligible
    else:
        step = (len(eligible) - 1) / float(n - 1) if n > 1 else 0
        chosen = sorted(set(eligible[int(round(i * step))] for i in range(n)))
    good = 0
    bad = []
    for k in chosen:
        a, b = k * pl, min((k + 1) * pl, total)
        h = hashlib.sha1()
        for s0, s1, f in segs:
            if s1 <= a or s0 >= b or s1 == s0:
                continue
            lo, hi = max(a, s0), min(b, s1)
            if f.pad:
                h.update(b"\0" * (hi - lo))
                continue
            with open(mapping[f.idx]["path"], "rb") as fh:
                fh.seek(lo - s0)
                remaining = hi - lo
                while remaining > 0:
                    chunk = fh.read(min(remaining, 4 << 20))
                    if not chunk:
                        break
                    h.update(chunk)
                    remaining -= len(chunk)
        if h.digest() == t.pieces[k * 20:(k + 1) * 20]:
            good += 1
        else:
            bad.append(k)
    return good == len(chosen), len(chosen), ("%d of %d checked pieces differ %s" % (len(bad), len(chosen), bad[:5])
                                              if bad else "ok")


def verify_torrent(t, item, cfg, min_ratio=None):
    """min_ratio: share of the torrent's bytes that must exist locally for 'partial' (default
    download.partial_min_ratio; near misses that are bigger by extras use a lower one)"""
    m = match_tree(t, item)
    mapping = m["mapping"]
    nz = [f for f in t.files if f.size > 0]
    all_matched = all(f.idx in mapping for f in nz)
    ratio = m["matched_bytes"] / float(m["total_bytes"] or 1)
    plan = plan_layout(t, item, mapping, cfg) if mapping else {"renames": [], "missing": [], "conflicts": ["nothing matched"]}
    renamed_existing = [r for r in plan["renames"] if r[0] in mapping]
    if all_matched:
        status = "exact" if not renamed_existing else "renamed"
    elif ratio >= float(cfg["download"]["partial_min_ratio"] if min_ratio is None else min_ratio):
        status = "partial"
    else:
        status = "mismatch"
    return status, ratio, mapping, plan


class Fetcher:
    """Downloads a result's .torrent, checks it against your files (file tree + real piece hashes). Used by step 3
    and by option 5 (near misses)."""

    def __init__(self, cfg, paths, qb=None, prow=None):
        self.cfg, self.paths = cfg, paths
        self.dls = read_json(paths.dl_json, {}) or {}
        self.qhashes = set()
        try:
            if qb is None:
                qb = QBit(cfg)
                qb.login()
            self.qhashes = qb.all_hashes()
        except (Fatal, NetError) as e:
            LOG.warn("qBittorrent not reachable (%s) - can't detect torrents you already have" % e)
        if prow is None:
            prow = Prowlarr(cfg, paths)
            prow.connect()
        self.prow = prow
        os.makedirs(paths.torrents, exist_ok=True)
        self.got_hashes = {v.get("hash"): k for k, v in self.dls.items() if v.get("hash")}
        self.delay = float(cfg["download"]["delay_between_downloads_sec"])
        self.last = 0.0
        self.limited = {}  # indexer id -> retry-after seconds

    def done_before(self, key):
        prev = self.dls.get(key)
        return prev if prev and prev.get("status") not in ("error", "ratelimit", "skipped") else None

    def mark_skipped(self, c, it):
        self.dls[c["key"]] = {"status": "skipped", "item_id": it["id"], "item_path": it["path"], "title": c["title"],
                              "indexer": c["indexer"], "time": now_iso()}
        save_downloads(self.paths, self.dls)

    def fetch(self, c, it, extra=None):
        """-> the downloads.json record for this result (also saved)"""
        paths = self.paths
        rec = {"key": c["key"], "item_id": it["id"], "item_path": it["path"], "indexer": c["indexer"],
               "title": c["title"], "score": c.get("score"), "time": now_iso(), "candidate": c, "item": it}
        rec.update(extra or {})  # near misses carry their own 'partial' threshold (min_ratio) into step 4

        def store(**kw):
            rec.update(kw)
            self.dls[c["key"]] = rec
            save_downloads(paths, self.dls)
            return rec

        if c.get("indexer_id") in self.limited:
            LOG.info(LOG.c("dim", "   skipped - [%s] is rate limited for the rest of this run" % c["indexer"]))
            return store(status="ratelimit")
        wait = self.last + self.delay - time.time()
        if wait > 0:
            time.sleep(wait)
        try:
            kind, payload = self.prow.download(c.get("download_url") or c.get("magnet_url"))
        except NetError as e:
            kind, payload = "error", str(e)
        self.last = time.time()
        if kind == "ratelimit":
            LOG.warn("   [%s] rate limit / grab limit hit (retry after %ss) - skipping this indexer for the rest of "
                     "this run; run it again later to get them" % (c["indexer"], payload))
            self.limited[c.get("indexer_id")] = payload
            return store(status="ratelimit")
        if kind == "magnet":
            LOG.warn("   indexer only gives a magnet link - can't verify files offline; saved in downloads.txt")
            return store(status="magnet", magnet=payload)
        if kind == "error":
            LOG.error("   download failed: %s" % payload)
            return store(status="error", error=payload)
        try:
            t = Torrent(payload)
        except Exception as e:
            LOG.error("   bad torrent file: %s" % e)
            return store(status="error", error="bad torrent file: %s" % e)
        fname = "%s__%s__%s.torrent" % (it["id"], safe_filename(c["indexer"] or "idx", 30), safe_filename(t.name, 120))
        fpath = os.path.join(paths.torrents, fname)
        with open(fpath, "wb") as fh:
            fh.write(payload)
        rec.update(torrent_file=fpath, hash=t.hash, name=t.name, tracker=t.tracker, private=t.private,
                   files=len(t.files), size=t.size)
        LOG.info("   .torrent file: %s" % fname)
        LOG.info("   inside: %s%s  (%d file%s, %s, hash %s, tracker %s%s)" % (
            t.name, "/" if t.multi else "", len(t.files), "" if len(t.files) == 1 else "s", fmt_size(t.size),
            t.hash[:12], t.tracker or "-", ", private" if t.private else ""))
        if t.hash in self.qhashes:
            LOG.warn("   this exact torrent is already in qBittorrent")
            return store(status="already_in_qbit")
        if t.hash in self.got_hashes and self.got_hashes[t.hash] != c["key"]:
            LOG.warn("   same torrent hash already downloaded via another result")
            return store(status="duplicate")
        self.got_hashes[t.hash] = c["key"]
        return store(**self.check(t, it, (extra or {}).get("min_ratio")))

    def check(self, t, it, min_ratio=None):
        """file tree + real piece hashes vs your files -> fields for the downloads.json record"""
        status, ratio, mapping, plan = verify_torrent(t, it, self.cfg, min_ratio)
        ok, checked, detail = (None, 0, "nothing of yours in it")
        if mapping:
            try:
                ok, checked, detail = spot_check(t, mapping, int(self.cfg["download"]["verify_pieces"]))
            except OSError as e:
                ok, checked, detail = False, 0, "read error: %s" % e
        for line in mapping_lines(t, mapping):
            LOG.info(line)
        tree_status = status
        if ok is False and status != "mismatch":
            status = "bad_pieces"  # same sizes, different bytes: not your files
        col = {"exact": "green", "renamed": "green", "partial": "yellow"}.get(status, "red")
        pc = "pieces %d/%d OK" % (checked, checked) if ok else ("PIECE CHECK FAILED: %s" % detail if ok is False
                                                                else "piece check: %s" % detail)
        LOG.info(LOG.c(col, "   => %s  %s of torrent bytes found locally, %d renames, %d missing files, %s"
                       % (status.upper() if status == tree_status else "%s (file tree looked %s)" % (
                           status.upper(), tree_status), fmt_pct(ratio),
                          len([r for r in plan["renames"] if r[0] in mapping]), len(plan["missing"]), pc)))
        if status == "mismatch" and ok:
            LOG.info("   (your file%s inside it check out, but %s of the torrent is missing here - below the %s "
                     "needed for 'partial')" % ("s" if len(mapping) > 1 else "", fmt_pct(1 - ratio), fmt_pct(
                         float(self.cfg["download"]["partial_min_ratio"] if min_ratio is None else min_ratio))))
        for idx, old, new in plan["renames"][:6]:
            LOG.debug("      rename %s -> %s" % (old, new))
        for cf in plan["conflicts"]:
            LOG.warn("   conflict: %s" % cf)
        return dict(status=status, tree_status=tree_status, matched_ratio=round(ratio, 5), piece_check=ok,
                    pieces_checked=checked, piece_detail=detail, savepath=plan.get("savepath"),
                    renames=[list(r) for r in plan["renames"]], missing=[list(r) for r in plan["missing"]],
                    conflicts=plan["conflicts"], mapping={str(k): v["path"] for k, v in mapping.items()})

    def recheck(self, rec, it, min_ratio=None):
        """a .torrent downloaded earlier: check it again under the current rules (no second grab)"""
        t = Torrent(open(rec["torrent_file"], "rb").read())
        LOG.info("   using the .torrent downloaded before: %s" % os.path.basename(rec["torrent_file"]))
        LOG.info("   inside: %s%s  (%d file%s, %s, hash %s)" % (t.name, "/" if t.multi else "", len(t.files),
                                                              "" if len(t.files) == 1 else "s", fmt_size(t.size),
                                                              t.hash[:12]))
        rec = dict(rec, item=it, item_id=it["id"], item_path=it["path"], time=now_iso(), min_ratio=min_ratio)
        if t.hash in self.qhashes:
            LOG.warn("   this exact torrent is already in qBittorrent")
            rec["status"] = "already_in_qbit"
        else:
            rec.update(self.check(t, it, min_ratio))
        self.dls[rec["key"]] = rec
        save_downloads(self.paths, self.dls)
        return rec


def mapping_lines(t, mapping, maxlines=4):
    """'torrent file -> your file' lines, biggest files first"""
    out = []
    mapped = sorted([f for f in t.files if f.idx in mapping], key=lambda f: -f.size)
    if mapped:
        out.append("   torrent file -> your file:")
        for f in mapped[:maxlines]:
            out.append("      %s\n        -> %s  (%s)" % (f.path, mapping[f.idx]["path"], fmt_size(f.size)))
        if len(mapped) > maxlines:
            out.append("      ... and %d more, %s" % (len(mapped) - maxlines, fmt_size(sum(f.size for f in mapped[maxlines:]))))
    else:
        out.append("   no file of this torrent matches any of your files by size")
    miss = [f for f in t.files if f.idx not in mapping and f.size > 0]
    if miss:
        out.append("   not on your disk: %d file%s, %s: %s%s" % (
            len(miss), "" if len(miss) == 1 else "s", fmt_size(sum(f.size for f in miss)),
            ", ".join(f.path.split("/", 1)[-1] for f in miss[:4]), " ..." if len(miss) > 4 else ""))
    return out


def step_download(cfg, paths, prow=None, qb=None, yes=False):
    LOG.info(LOG.c("bold", "\n=== 3) download .torrent files + verify against your files ==="))
    data = read_json(paths.cand_json)
    if not data:
        raise Fatal("no %s - run step 2 first" % paths.cand_json)
    if not data.get("complete", True):
        LOG.warn("candidates.json is from an interrupted search - using what is there")
    if data.get("version") != 3:
        raise Fatal("%s is from the old version of this script - run steps 1 and 2 again" % paths.cand_json)
    dls_before = read_json(paths.dl_json, {}) or {}
    min_score = float(cfg["download"]["min_score"])
    queue, total = [], 0
    for x in data["releases"]:
        for u in x["units"]:
            for c in u["candidates"]:
                total += 1
                if c["score"] < min_score:
                    continue
                prev = dls_before.get(c["key"])
                if prev and prev.get("status") not in ("error", "ratelimit", "skipped"):
                    LOG.file("already processed (%s): %s" % (prev.get("status"), c["title"]))
                    continue
                queue.append((u["item"], c))
    LOG.info("%d candidates to go through (%d already done earlier, see downloads.txt)" % (len(queue),
                                                                                          total - len(queue)))
    if not queue:
        return dls_before
    fe = Fetcher(cfg, paths, qb=qb, prow=prow)
    approve_all = yes
    skip_item = None
    for n, (it, c) in enumerate(queue, 1):
        if skip_item == it["id"]:
            continue
        LOG.info(LOG.c("bold", "[%d/%d] %s%s  (%s, %s)" % (n, len(queue), it["path"], "/" if it.get("is_dir") else "",
                                                          it.get("label") or it.get("kind", ""),
                                                          fmt_size(it["total_size"]))))
        LOG.info("   %5.1f  size %s %+.3f%% vs %s | title %.2f | same: %s\n   [%s] %s  (%s)" % (
            c["score"], c["size_match"], 100 * c["size_diff"], c.get("size_target"), c["title_sim"],
            ",".join(c.get("agree") or []) or "-", c["indexer"], c["title"], fmt_size(c["size"])))
        if not approve_all:
            ans = ask("   download? [y]es [n]o [a]ll remaining [s]kip rest of this item [q]uit: ", "ynasq")
            if ans == "q":
                LOG.info("stopped by you")
                break
            if ans == "n":
                fe.mark_skipped(c, it)
                continue
            if ans == "s":
                skip_item = it["id"]
                continue
            if ans == "a":
                approve_all = True
        fe.fetch(c, it)
    save_downloads(paths, fe.dls)
    counts = {}
    for v in fe.dls.values():
        counts[v.get("status")] = counts.get(v.get("status"), 0) + 1
    LOG.ok("downloads: " + ", ".join("%s=%d" % kv for kv in sorted(counts.items())))
    LOG.info("-> %s\n-> %s\n-> %s/" % (paths.dl_txt, paths.dl_json, paths.torrents))
    return fe.dls


def save_downloads(paths, dls):
    write_json(paths.dl_json, dls)
    order = ["exact", "renamed", "partial", "bad_pieces", "mismatch", "already_in_qbit", "duplicate", "magnet",
             "error", "ratelimit", "skipped"]
    lines = ["# downloaded .torrent files and how they match your files  (%s)" % now_iso(),
             "# exact = same names+sizes, renamed = same sizes/other names (qBittorrent will be told the local names),",
             "# partial = some torrent files missing locally, mismatch = not your files", ""]
    for st in order + sorted(set(v.get("status") for v in dls.values()) - set(order)):
        recs = [v for v in dls.values() if v.get("status") == st]
        if not recs:
            continue
        lines.append("## %s (%d)" % (st, len(recs)))
        for v in sorted(recs, key=lambda r: r.get("item_path", "")):
            extra = ""
            if st in ("exact", "renamed", "partial", "mismatch", "bad_pieces"):
                extra = "  %s local, pieces: %s, %d renames, %d missing%s" % (
                    fmt_pct(v.get("matched_ratio", 0)), "OK" if v.get("piece_check") else v.get("piece_detail"),
                    len(v.get("renames") or []), len(v.get("missing") or []),
                    ", CONFLICTS: " + "; ".join(v["conflicts"]) if v.get("conflicts") else "")
            elif st == "magnet":
                extra = "  " + (v.get("magnet") or "")[:200]
            elif st == "error":
                extra = "  " + (v.get("error") or "")
            lines.append("%s\n   [%s] %s%s" % (v.get("item_path"), v.get("indexer"), v.get("title"), extra))
            if v.get("torrent_file"):
                lines.append("   " + v["torrent_file"])
        lines.append("")
    write_text(paths.dl_txt, lines)


def ask(prompt, allowed):
    while True:
        try:
            a = input(prompt).strip().lower()
        except EOFError:
            return "q"
        if a and a[0] in allowed:
            return a[0]


# --------------------------------------------------------------------------- step 4: add to qBittorrent


def mount_point(path):
    p = os.path.realpath(path)
    while not os.path.ismount(p):
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    return p


def link_base(path, item, cfg):
    """The hardlink folder on the same disk as `path`: add.link_dir with {drive} = that disk's mount point."""
    tmpl = cfg["add"].get("link_dir") or "{drive}/hardlinked/xseed"
    drive = mount_point(path)
    note = ""
    if drive == "/" and "{drive}" in tmpl:
        # disk isn't a separate mount (plain folder on the system disk): use the folder above the scan path
        root = item.get("root") or os.path.dirname(path)
        drive = os.path.dirname(os.path.normpath(root))
        note = "not a separate mount, using %s as the drive" % drive
    return norm(tmpl.replace("{drive}", drive.rstrip("/"))), drive, note


def choose_mode(t, item, status, cfg):
    """-> ('direct' | 'linkdir', reason)"""
    lm = (cfg["add"].get("link_dir_mode") or "auto").lower()
    if not t.multi:
        return "direct", "single-file torrent"
    if lm == "off":
        return "direct", "link_dir_mode is off"
    if status == "exact" and item.get("is_dir"):
        return "direct", "same folder layout as yours"
    if lm == "all":
        return "linkdir", "link_dir_mode is all"
    if not item.get("is_dir"):
        return "linkdir", "torrent is a folder, you have a single file"
    if status == "partial":
        return "linkdir", "torrent has files you don't have - they get downloaded into the hardlink folder, not your library"
    return "direct", "your folder has everything, only names differ"


def plan_linkdir(t, item, mapping, cfg):
    """Recreate the torrent's own layout under the hardlink folder, hardlinked to your files."""
    srcs = [mapping[f.idx]["path"] for f in t.files if f.idx in mapping]
    if not srcs:
        return {"fallback": "nothing matched to link"}
    base, drive, note = link_base(srcs[0], item, cfg)
    try:
        dev = os.stat(srcs[0]).st_dev
        d = base
        while not os.path.exists(d):
            d = os.path.dirname(d)
        if os.stat(d).st_dev != dev:
            return {"fallback": "hardlink folder %s is on another filesystem than %s" % (base, srcs[0])}
        if any(os.stat(x).st_dev != dev for x in srcs):
            return {"fallback": "matched files are on more than one filesystem"}
    except OSError as e:
        return {"fallback": "cannot stat: %s" % e}
    plan = {"base": base, "drive": drive, "note": note, "links": [], "reused": [], "missing": [], "conflicts": []}
    for f in t.files:
        dst = os.path.join(base, f.path)
        lf = mapping.get(f.idx)
        if lf:
            if os.path.lexists(dst):
                try:
                    same = os.path.samefile(dst, lf["path"])
                except OSError:
                    same = False
                if same:
                    plan["reused"].append(dst)  # e.g. the same release from another tracker was linked before
                else:
                    plan["conflicts"].append("%s already exists and is a different file" % dst)
            else:
                plan["links"].append((lf["path"], f.path))
        elif f.size > 0:
            plan["missing"].append((f.idx, f.path, f.size, os.path.lexists(dst)))
    return plan



def qb_sees(qb, pm, path, size):
    """Ask qBittorrent whether it can see `path` (its own view: permissions, docker mounts). True/False/None=unknown"""
    try:
        r = qb._call("GET", "app/getDirectoryContent", params={"dirPath": pm.to_qbit(os.path.dirname(path)),
                                                              "mode": "files", "withMetadata": "true"})
    except NetError:
        return None
    if r.status == 404 and "does not exist" in r.text and "Endpoint" not in r.text:
        return False
    if r.status != 200:
        return None
    try:
        entries = r.json()
    except ValueError:
        return None
    for e in entries:
        name = os.path.basename(e) if isinstance(e, str) else e.get("name")
        if name == os.path.basename(path):
            return not (isinstance(e, dict) and e.get("size") is not None and int(e["size"]) != size)
    return False


class Adder:
    """Adds one verified torrent to qBittorrent (used by step 4 and option 5)."""

    def __init__(self, cfg, paths, qb=None, approve_all=False):
        self.cfg, self.paths = cfg, paths
        self.a = cfg["add"]
        self.allowed = set(self.a["allowed_statuses"])
        self.pm = PathMap(cfg["scan"]["path_mappings"])
        self.added = read_json(paths.added_json, {}) or {}
        if qb is None:
            qb = QBit(cfg)
            qb.login()
        self.qb = qb
        self.qhashes = qb.all_hashes()
        self.approve_all = approve_all

    def why_not(self, v):
        """reason this downloads.json record can't/shouldn't be added, or None"""
        a, st = self.a, v.get("status")
        if st not in ("exact", "renamed", "partial", "mismatch", "bad_pieces"):
            return "status %s" % st
        if v.get("hash") in self.added and self.added[v["hash"]].get("ok"):
            return "added before"
        if v.get("piece_check") is False and a.get("require_piece_check", True):
            return "piece check failed (%s) - not your files" % v.get("piece_detail")
        if st == "bad_pieces":
            st = v.get("tree_status")  # require_piece_check is off
        if st not in self.allowed:
            return "status %s not in add.allowed_statuses" % st
        if v.get("hash") in self.qhashes:
            return "already in qBittorrent"
        if not os.path.exists(v.get("torrent_file") or ""):
            return "torrent file missing: %s" % v.get("torrent_file")
        return None

    def _fail(self, t, rec_or_why, v=None, item=None):
        rec = rec_or_why if isinstance(rec_or_why, dict) else {
            "ok": False, "why": rec_or_why, "time": now_iso(), "title": v.get("title"), "item_path": item["path"]}
        self.added[t.hash] = rec
        save_added(self.paths, self.added)
        return "failed"

    def add(self, v, extra_tags=(), ask_prompt="   add to qBittorrent? [y]es [n]o [a]ll remaining [q]uit: "):
        """-> 'added' | 'failed' | 'declined' | 'skipped' | 'quit'"""
        a, pm, qb, cfg = self.a, self.pm, self.qb, self.cfg
        with open(v["torrent_file"], "rb") as fh:
            raw = fh.read()
        t = Torrent(raw)
        item = v["item"]
        status, ratio, mapping, plan = verify_torrent(t, item, cfg, v.get("min_ratio"))  # files may have moved
        LOG.info("   [%s] %s  (%s%d file%s, %s)  hash %s  -> %s" % (
            v.get("indexer"), t.name, "folder, " if t.multi else "", len(t.files), "" if len(t.files) == 1 else "s",
            fmt_size(t.size), t.hash[:12], status.upper()))
        LOG.info("   .torrent file: %s" % os.path.basename(v["torrent_file"]))
        if status == "mismatch" or status not in self.allowed:
            LOG.warn("   now %s (was %s) - skipped" % (status, v["status"]))
            return "skipped"
        mode, mode_why = choose_mode(t, item, status, cfg)
        lp = None
        if mode == "linkdir":
            lp = plan_linkdir(t, item, mapping, cfg)
            if lp.get("fallback"):
                LOG.warn("   can't use a hardlink folder (%s) - pointing at your files instead" % lp["fallback"])
                mode, mode_why = "direct", "fallback, see above"
            elif lp["conflicts"]:
                for cf in lp["conflicts"]:
                    LOG.warn("   conflict: %s" % cf)
                LOG.warn("   skipped because of conflicts in the hardlink folder - check it by hand")
                self.added[t.hash] = {"ok": False, "why": "hardlink folder conflicts", "conflicts": lp["conflicts"],
                                      "time": now_iso(), "title": v.get("title"), "item_path": item["path"]}
                save_added(self.paths, self.added)
                return "failed"
        if mode == "direct" and plan["conflicts"]:
            for cf in plan["conflicts"]:
                LOG.warn("   conflict: %s" % cf)
            LOG.warn("   skipped because of conflicts - add this one by hand")
            self.added[t.hash] = {"ok": False, "why": "conflicts", "conflicts": plan["conflicts"], "time": now_iso(),
                                  "title": v.get("title"), "item_path": item["path"]}
            save_added(self.paths, self.added)
            return "failed"
        # ---- show the plan
        missing = lp["missing"] if mode == "linkdir" else [(i, o, sz, False) for i, o, nw, sz in plan["missing"]]
        save_local = lp["base"] if mode == "linkdir" else plan["savepath"]
        save_q = pm.to_qbit(save_local)
        LOG.info("   %s: %s" % ("HARDLINK FOLDER" if mode == "linkdir" else "your files", mode_why))
        LOG.info("   save path %s%s" % (save_q, "" if save_q == save_local else "  (local %s)" % save_local))
        if mode == "linkdir":
            if lp["note"]:
                LOG.info(LOG.c("dim", "   drive %s" % lp["note"]))
            for src, rel in lp["links"][:6]:
                LOG.info("   link  %s\n      <- %s" % (rel, src))
            if len(lp["links"]) > 6:
                LOG.info("   ... and %d more links" % (len(lp["links"]) - 6))
            if lp["reused"]:
                LOG.info("   %d file(s) already linked there (same release added before) - reused" % len(lp["reused"]))
        else:
            for line in rename_summary(plan["renames"]):
                LOG.info(line)
            if not plan["renames"]:
                for f in sorted([f for f in t.files if f.idx in mapping], key=lambda f: -f.size)[:3]:
                    LOG.info("   uses  %s" % mapping[f.idx]["path"])
        if missing:
            ms = sum(x[2] for x in missing)
            LOG.info(LOG.c("yellow", "   you don't have %d torrent file(s), %s: %s%s" % (
                len(missing), fmt_size(ms), ", ".join(x[1].split("/", 1)[-1] for x in missing[:4]),
                " ..." if len(missing) > 4 else "")))
        if not self.approve_all:
            ans = ask(ask_prompt, "ynaq")
            if ans == "q":
                return "quit"
            if ans == "n":
                return "declined"
            if ans == "a":
                self.approve_all = True
        # ---- add
        tags = list(a.get("tags") or [])
        if a.get("tag_with_status", True):
            tags.append("oxs-" + status)
        if mode == "linkdir":
            tags.append("oxs-linked")
        tags.extend(extra_tags)
        # skip the hash check only when every file exists where the torrent expects it; a partial torrent would sit
        # in "missing files" forever - without the skip it waits stopped and Start = check what's there + download
        # the missing bits
        skip = bool(a.get("skip_checking", True)) and not missing
        fields = {"savepath": save_q, "autoTMM": "false", "contentLayout": "Original", "useDownloadPath": "false",
                  "skip_checking": "true" if skip else "false",
                  "stopped": "true" if a.get("start_stopped", True) else "false",
                  "paused": "true" if a.get("start_stopped", True) else "false"}
        if tags:
            fields["tags"] = ",".join(tags)
        if a.get("category"):
            fields["category"] = a["category"]
        LOG.debug("   torrents/add fields: %s" % fields)
        rec = {"time": now_iso(), "title": v.get("title"), "indexer": v.get("indexer"), "item_path": item["path"],
               "status": status, "mode": mode, "savepath": save_q, "ok": False, "near_miss": bool(v.get("near_miss")),
               "torrent_file": v.get("torrent_file"), "missing": [[x[1], x[2]] for x in missing]}
        links, keep_links = None, False
        if mode == "linkdir":
            try:
                os.makedirs(lp["base"], exist_ok=True)
                links = TempLinks(lp["base"])
                for src, rel in lp["links"]:
                    links.link(src, rel)
            except OSError as e:
                LOG.error("   could not create hardlinks in %s: %s - skipped (set add.link_dir_mode to off to point "
                          "at your files instead)" % (lp["base"], e))
                if links:
                    links.cleanup()
                rec["why"] = "hardlink failed: %s" % e
                return self._fail(t, rec)
            biggest = max(((src, rel) for src, rel in lp["links"]), key=lambda x: os.path.getsize(x[0]), default=None)
            if biggest:
                seen = qb_sees(qb, pm, os.path.join(lp["base"], biggest[1]), os.path.getsize(biggest[0]))
                if seen is False:
                    LOG.error("   qBittorrent can't see %s (other user without access? docker without that folder "
                              "mounted?) - links removed, skipped" % pm.to_qbit(os.path.join(lp["base"], biggest[1])))
                    links.cleanup()
                    rec["why"] = "qBittorrent can't see the hardlink folder"
                    return self._fail(t, rec)
                LOG.debug("   qBittorrent sees the linked files: %s" % ("yes" if seen else "unknown (old qBittorrent)"))
            rec.update(link_dir=lp["base"], links=len(lp["links"]), links_reused=len(lp["reused"]))
        elif status == "renamed" and skip and a.get("temp_hardlinks", True):
            # qBittorrent/libtorrent only accept "skip hash check" if the files exist under the torrent's OWN names
            # when it is added: hardlink them there for a moment, add, remove the links, rename inside qBittorrent
            links = TempLinks(save_local)
            try:
                for idx, old, new in plan["renames"]:
                    links.link(mapping[idx]["path"], old)
                LOG.info("   %d temporary hardlink(s) under the torrent's names so qBittorrent accepts skip_checking"
                         % len(links.links))
            except OSError as e:
                LOG.warn("   could not hardlink (%s) - adding without; this torrent will show 'missing files' "
                         "until you Force recheck it" % e)
                links.cleanup()
                links = None
        info = None
        try:
            r = qb.add(raw, "%s.torrent" % t.hash, fields)
            body = r.text.strip()
            LOG.debug("   add -> HTTP %s %s" % (r.status, body[:300]))
            if r.status == 409 or body == "Fails.":
                LOG.error("   qBittorrent refused the torrent (duplicate or invalid): HTTP %s %s" % (r.status, body[:200]))
                rec["why"] = "add refused: %s %s" % (r.status, body[:200])
            elif r.status not in (200, 202):
                LOG.error("   add failed: HTTP %s %s" % (r.status, body[:300]))
                rec["why"] = "add failed: %s %s" % (r.status, body[:200])
            else:
                info = wait_added(qb, t.hash)
                if not info:
                    LOG.error("   torrent did not appear in qBittorrent within 30s")
                    rec["why"] = "not visible after add"
                else:
                    keep_links = mode == "linkdir"
                    LOG.debug("   state right after add: %s progress=%s" % (info.get("state"), info.get("progress")))
        finally:
            if links and not keep_links:
                links.cleanup()  # temp links (direct mode) always, hardlink folder only if the add failed
        if not info:
            return self._fail(t, rec)
        self.qhashes.add(t.hash)
        errs = 0
        if mode == "direct":
            qfiles = qb.files(t.hash)
            if len(qfiles) != len(t.files):
                LOG.warn("   qBittorrent reports %d files, torrent parse says %d - renames go by index anyway"
                         % (len(qfiles), len(t.files)))
            qname = {int(f.get("index", i)): f.get("name") for i, f in enumerate(qfiles)}
            errs = do_renames(qb, t.hash, plan["renames"], qname)
            if plan["missing"] and a.get("missing_files_priority_zero", True):
                ids = [idx for idx, _, _, _ in plan["missing"]]
                rr = qb.file_prio(t.hash, ids, 0)
                LOG.debug("   filePrio 0 for %s -> HTTP %s %s" % (ids, rr.status, rr.text[:100]))
        elif missing and not a.get("link_dir_download_missing", True):
            ids = [x[0] for x in missing]
            rr = qb.file_prio(t.hash, ids, 0)
            LOG.debug("   filePrio 0 for %s -> HTTP %s %s" % (ids, rr.status, rr.text[:100]))
        # verify where qBittorrent now points
        good = 0
        for f in qb.files(t.hash):
            idx = int(f.get("index", -1))
            lf = mapping.get(idx)
            if not lf:
                continue
            p = norm(os.path.join(save_local, f.get("name", "")))
            try:
                okf = os.path.samefile(p, lf["path"])
            except OSError:
                okf = False
            if okf:
                good += 1
            else:
                LOG.warn("   file #%d points at %s, expected %s" % (idx, p, lf["path"]))
        info = wait_added(qb, t.hash) or info
        state = info.get("state") or ""
        progress = float(info.get("progress") or 0)
        if state in ("missingFiles", "error"):
            nxt = "Force recheck it"
        elif progress >= 1.0:
            nxt = "ready - Start to seed"
        elif missing and mode == "direct" and a.get("missing_files_priority_zero", True):
            nxt = "Start: qBittorrent checks your data and seeds it (%d file(s) you don't have are set to " \
                  "'do not download')" % len(missing)
        elif missing:
            nxt = "Start: qBittorrent checks your data, then downloads the %d missing file(s) (%s)%s" % (
                len(missing), fmt_size(sum(x[2] for x in missing)),
                " into the hardlink folder" if mode == "linkdir" else "")
        else:
            nxt = "Force recheck it"
        rec.update(ok=errs == 0 and good == len(mapping), hash=t.hash, state=state, progress=progress,
                   files_ok=good, files_matched=len(mapping), next=nxt, needs_recheck=nxt == "Force recheck it")
        self.added[t.hash] = rec
        save_added(self.paths, self.added)
        col = ("green" if progress >= 1.0 else "yellow") if rec["ok"] else "red"
        LOG.info(LOG.c(col, "   => added (%s), %d/%d files point at your data, state=%s %.1f%% -> %s"
                       % ("hardlink folder" if mode == "linkdir" else "your files", good, len(mapping), state,
                          100 * progress, nxt)))
        return "added" if rec["ok"] else "failed"

    def summary(self):
        save_added(self.paths, self.added)
        ok = [x for x in self.added.values() if x.get("ok")]
        ready = sum(1 for x in ok if x.get("progress", 0) >= 1.0)
        linked = sum(1 for x in ok if x.get("mode") == "linkdir")
        LOG.ok("added OK: %d in added.json - %d ready to seed, %d need Start (partial: check + download the missing "
               "extras), %d in hardlink folders. All stopped; filter by tag '%s' in qBittorrent." % (
                   len(ok), ready, len(ok) - ready, linked, ",".join(self.a.get("tags") or ["-"])))
        LOG.info("-> %s\n-> %s" % (self.paths.added_txt, self.paths.added_json))


def step_add(cfg, paths, qb=None, yes=False):
    LOG.info(LOG.c("bold", "\n=== 4) add verified torrents to qBittorrent (stopped) ==="))
    dls = read_json(paths.dl_json)
    if not dls:
        raise Fatal("no %s - run step 3 first" % paths.dl_json)
    ad = Adder(cfg, paths, qb=qb, approve_all=yes)
    todo = []
    for key, v in dls.items():
        why = ad.why_not(v)
        if why is None:
            todo.append(v)
        elif not (why.startswith("status ") and "allowed" not in why) and why != "added before":
            LOG.info(LOG.c("dim", "skip  [%s] %s - %s" % (v.get("indexer"), v.get("title"), why)))
    LOG.info("%d torrents to add" % len(todo))
    for n, v in enumerate(todo, 1):
        LOG.info(LOG.c("bold", "[%d/%d] %s%s%s") % (n, len(todo), v["item"]["path"],
                                                    "/" if v["item"].get("is_dir") else "",
                                                    "   (near miss)" if v.get("near_miss") else ""))
        if ad.add(v, extra_tags=("oxs-nearmiss",) if v.get("near_miss") else ()) == "quit":
            break
    ad.summary()


# --------------------------------------------------------------------------- option 5: near misses


def parse_selection(text, maxno):
    """'3,7-9' -> [3, 7, 8, 9]"""
    out = []
    for part in re.split(r"[,\s]+", text.strip()):
        if not part:
            continue
        m = re.match(r"^(\d+)(?:-(\d+))?$", part)
        if not m:
            return None
        a, b = int(m.group(1)), int(m.group(2) or m.group(1))
        out.extend(i for i in range(min(a, b), max(a, b) + 1) if 1 <= i <= maxno)
    return sorted(set(out))


def near_aims_at(m):
    it = m["item"]
    lines = ["   aims at: %s  %s%s  (%s: %s)" % (m["target"], it["path"], "/" if it.get("is_dir") else "",
                                               m["size_target"], fmt_size(target_size(it, m["size_target"])))]
    if it.get("is_dir") and it.get("main"):
        lines.append("            biggest file: %s  (%s)" % (os.path.relpath(it["main"], it["path"]),
                                                            fmt_size(it.get("main_size"))))
    return lines


def step_nearmiss(cfg, paths, yes=False):
    LOG.info(LOG.c("bold", "\n=== 5) try near misses - same title, rejected in step 2 for a label or the size ==="))
    data = read_json(paths.near_json)
    if not data:
        raise Fatal("no %s - run step 2 first" % paths.near_json)
    if data.get("version") != 3:
        raise Fatal("%s is from the old version of this script - run steps 1 and 2 again" % paths.near_json)
    nms = data["near_misses"]
    dls = read_json(paths.dl_json, {}) or {}
    tried = {m["no"] for m in nms if (dls.get(m["key"]) or {}).get("status") not in (None, "error", "ratelimit",
                                                                                     "skipped")}
    LOG.info("%d near misses in %s (from step 2 at %s), %d already tried - list: %s" % (
        len(nms), os.path.basename(paths.near_json), data.get("generated"), len(tried), paths.near_txt))
    if not nms:
        return
    if yes:
        sel, one_by_one = [m["no"] for m in nms if m["no"] not in tried], False
    else:
        while True:
            try:
                ans = input("   [o]ne by one, [a]ll untried at once (no questions), numbers from the list "
                            "(e.g. 3,7-9), [q]uit: ").strip().lower()
            except EOFError:
                return
            if ans in ("q", ""):
                return
            if ans in ("o", "a"):
                sel = [m["no"] for m in nms if m["no"] not in tried]
                one_by_one = ans == "o"
                break
            sel = parse_selection(ans, len(nms))
            if sel:
                one_by_one = True
                break
            print("   ?")
    if not sel:
        LOG.info("nothing to try")
        return
    qb = QBit(cfg)
    qb.login()
    fe = Fetcher(cfg, paths, qb=qb)
    ad = Adder(cfg, paths, qb=qb, approve_all=not one_by_one)
    by_no = {m["no"]: m for m in nms}
    res = {}
    # a near miss that is bigger by extras / episodes you don't have is fine as 'partial' as long as the part that IS
    # on your disk passes the piece check (Start then downloads only the rest)
    min_ratio = min(float(cfg["download"]["partial_min_ratio"]),
                    1.0 / (1.0 + float(cfg["search"].get("near_miss_max_bigger_pct", 20)) / 100.0) - 0.001)
    for n, no in enumerate(sel, 1):
        m = by_no[no]
        LOG.info(LOG.c("bold", "[%d/%d] near miss #%d  [%s] %s  (%s)" % (n, len(sel), no, m["indexer"], m["title"],
                                                                      fmt_size(m["size"]))))
        for line in near_aims_at(m):
            LOG.info(line)
        LOG.info("   %s | title %s | %s: %s" % (near_line(m), m.get("title_match") or "?",
                                              "rejected in step 2" if m.get("near_kind") != "size" else "near miss",
                                              m["why"]))
        if no in tried:
            LOG.info(LOG.c("dim", "   tried before: %s" % dls[m["key"]].get("status")))
        if one_by_one:
            ans = ask("   download and check it? [y]es [n]o [a]ll remaining without questions [q]uit: ", "ynaq")
            if ans == "q":
                break
            if ans == "n":
                res["not tried"] = res.get("not tried", 0) + 1
                continue
            if ans == "a":
                one_by_one = False
                ad.approve_all = True
        c = {"key": m["key"], "indexer": m["indexer"], "indexer_id": m["indexer_id"], "title": m["title"],
             "size": m["size"], "download_url": m["download_url"], "magnet_url": m["magnet_url"], "score": None,
             "size_match": m["size_match"], "size_diff": m["size_diff"], "size_target": m["size_target"]}
        prev = fe.done_before(m["key"])
        if prev and os.path.exists(prev.get("torrent_file") or ""):
            # no second download (private trackers count every .torrent grab) - re-check the one we have
            rec = fe.recheck(dict(prev, near_miss=True, near_reason=m["why"], near_no=no), m["item"], min_ratio)
        else:
            rec = fe.fetch(c, m["item"], extra={"near_miss": True, "near_reason": m["why"], "near_no": no,
                                                "min_ratio": min_ratio})
        why = ad.why_not(rec)
        if why:
            LOG.info(LOG.c("dim", "   not added: %s" % why))
            res[rec.get("status")] = res.get(rec.get("status"), 0) + 1
            continue
        out = ad.add(rec, extra_tags=("oxs-nearmiss",))
        res["added" if out == "added" else "verified, " + out] = res.get("added" if out == "added" else
                                                                         "verified, " + out, 0) + 1
        if out == "quit":
            break
    LOG.ok("near misses: " + (", ".join("%s=%d" % kv for kv in sorted(res.items())) or "nothing done"))
    LOG.info("-> %s (status of every download)\n-> %s" % (paths.dl_txt, paths.added_txt))


def rename_summary(renames, maxlines=6):
    """'rename top folder A -> B (32 files)' instead of 32 lines when only the folder differs"""
    if not renames:
        return []
    pairs = [(o.split("/", 1), n.split("/", 1)) for _, o, n in renames]
    if all(len(o) == 2 and len(n) == 2 and o[1] == n[1] for o, n in pairs) and \
            len(set((o[0], n[0]) for o, n in pairs)) == 1:
        return ["   rename top folder  %s\n                   -> %s   (%d files, names inside unchanged)" % (
            pairs[0][0][0], pairs[0][1][0], len(pairs))]
    out = ["   rename  %s\n        -> %s" % (o, n) for _, o, n in renames[:maxlines]]
    if len(renames) > maxlines:
        out.append("   ... and %d more renames (all in the log with --debug)" % (len(renames) - maxlines))
        for _, o, n in renames[maxlines:]:
            LOG.file("   rename  %s -> %s" % (o, n))
    return out


class TempLinks:
    """Hardlinks created under a save path, removed again by cleanup() (only what we created)."""

    def __init__(self, base):
        self.base = base
        self.links = []  # (link path, source path)
        self.dirs = []   # directories we created, in creation order

    def link(self, src, rel):
        dst = os.path.join(self.base, rel)
        parts = os.path.relpath(os.path.dirname(dst), self.base).split(os.sep)
        cur = self.base
        for p in parts:
            if p in ("", "."):
                continue
            cur = os.path.join(cur, p)
            if not os.path.isdir(cur):
                os.mkdir(cur)
                self.dirs.append(cur)
        if os.path.lexists(dst):
            raise OSError("%s already exists" % dst)
        os.link(src, dst)
        self.links.append((dst, src))
        LOG.debug("   temp link %s -> %s" % (dst, src))

    def cleanup(self):
        for dst, src in reversed(self.links):
            try:
                if os.path.samefile(dst, src) and os.stat(dst).st_nlink >= 2:
                    os.unlink(dst)
                    LOG.debug("   removed temp link %s" % dst)
                else:
                    LOG.warn("   NOT removing %s: it is no longer a hardlink of %s" % (dst, src))
            except OSError as e:
                LOG.warn("   could not remove temp link %s: %s" % (dst, e))
        for d in reversed(self.dirs):
            try:
                os.rmdir(d)
            except OSError:
                pass  # not empty (qBittorrent or someone put something there) - leave it
        self.links, self.dirs = [], []


def wait_added(qb, h, timeout=30.0):
    """Wait until the torrent exists and qBittorrent finished its resume-data check."""
    t0 = time.time()
    info = None
    while time.time() - t0 < timeout:
        lst = qb.torrents(hashes=[h])
        if lst:
            info = lst[0]
            if info.get("state") not in ("checkingResumeData", "metaDL", "moving", "allocating", "unknown"):
                return info
        time.sleep(0.3)
    return info


def wait_names(qb, h, want, timeout=15.0):
    """qBittorrent applies renames asynchronously (libtorrent alert) - wait until it reports the new names."""
    t0 = time.time()
    cur = {}
    while time.time() - t0 < timeout:
        cur = {int(f.get("index", i)): f.get("name") for i, f in enumerate(qb.files(h))}
        if all(cur.get(i) == n for i, n in want.items()):
            return cur, True
        time.sleep(0.3)
    return cur, False


def do_renames(qb, h, renames, qname):
    """Rename torrent files onto local names. Two-phase when a target is still another file's current name."""
    errs = 0
    if not renames:
        return 0
    current = set(qname.values())
    first, second, want = [], [], {}
    for idx, old, new in renames:
        cur = qname.get(idx, old)
        if cur == new:
            continue
        if new in current:
            tmp = "%s.oxs-tmp-%d" % (cur, idx)
            first.append((idx, cur, tmp))
            second.append((idx, tmp, new))
            current.discard(cur)
            current.add(tmp)
        else:
            first.append((idx, cur, new))
            current.discard(cur)
            current.add(new)
    for phase in (first, second):
        if not phase:
            continue
        for idx, cur, new in phase:
            rr = qb.rename_file(h, cur, new)
            if rr.status in (200, 204):
                LOG.debug("   renamed #%d %s -> %s" % (idx, cur, new))
                want[idx] = new
            else:
                errs += 1
                LOG.error("   rename #%d failed: HTTP %s %s" % (idx, rr.status, rr.text[:200]))
        _, ok = wait_names(qb, h, want)
        if not ok:
            LOG.warn("   qBittorrent has not applied all renames yet (waited 15s)")
    return errs


def save_added(paths, added):
    write_json(paths.added_json, added)
    lines = ["# torrents added by orphan_xseed  (%s)" % now_iso(),
             "# mode 'linked' = torrent sits in a hardlink folder (add.link_dir) with your files hardlinked into it", ""]
    for h, v in sorted(added.items(), key=lambda kv: kv[1].get("time", "")):
        lines.append("%s  %-4s %-8s %-7s [%s] %s\n      %s -> %s\n      %s" % (
            v.get("time"), "OK" if v.get("ok") else "FAIL", v.get("status", ""),
            "linked" if v.get("mode") == "linkdir" else "", v.get("indexer"), v.get("title"), h[:12],
            v.get("savepath") or v.get("item_path"),
            v.get("why") or "state=%s %.1f%%, files %s/%s -> %s" % (
                v.get("state"), 100 * float(v.get("progress") or 0), v.get("files_ok"), v.get("files_matched"),
                v.get("next", ""))))
    write_text(paths.added_txt, lines)


# --------------------------------------------------------------------------- test / menu


def step_test(cfg, paths):
    LOG.info(LOG.c("bold", "\n=== t) test connections ==="))
    try:
        qb = QBit(cfg)
        qb.login()
        ts = qb.torrents()
        LOG.info("   %d torrents; includeFiles support: %s; stop/start API: %s" %
                 (len(ts), qb.webapi >= (2, 11, 8), "stopped (v5+)" if qb.webapi >= (2, 11) else "paused (v4)"))
    except (Fatal, NetError) as e:
        LOG.error(str(e))
    try:
        pr = Prowlarr(cfg, paths)
        pr.connect()
        idx = pr.indexers()
        LOG.info("   %d indexers:" % len(idx))
        for i in idx:
            LOG.info("     id %-4s %-8s %-8s %s" % (i.get("id"), i.get("protocol"),
                                                  "enabled" if i.get("enable") else "DISABLED", i.get("name")))
    except (Fatal, NetError) as e:
        LOG.error(str(e))
    for p in cfg["scan"]["paths"]:
        LOG.info("   scan path %-45s %s" % (p, "OK" if os.path.isdir(p) else LOG.c("red", "MISSING")))


def state_line(path, what):
    if not os.path.exists(path):
        return LOG.c("dim", "(not run yet)")
    ts = dt.datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y-%m-%d %H:%M")
    return LOG.c("dim", "(last: %s%s)" % (ts, ", " + what if what else ""))


def menu_status(paths):
    s1 = s2 = s3 = s4 = ""
    try:
        d = read_json(paths.orphans_json) or {}
        st = d.get("stats") or {}
        s1 = "%s orphan releases, %s" % (st.get("orphan_releases", "?"), fmt_size(st.get("orphan_release_bytes")))
    except Exception:
        pass
    try:
        d = read_json(paths.cand_json) or {}
        s2 = "%d candidates" % sum(len(u["candidates"]) for x in d.get("releases", []) for u in x["units"])
    except Exception:
        pass
    try:
        d = read_json(paths.dl_json) or {}
        s3 = "%d ok to add" % sum(1 for v in d.values() if v.get("status") in ("exact", "renamed", "partial"))
    except Exception:
        pass
    try:
        d = read_json(paths.added_json) or {}
        s4 = "%d added" % sum(1 for v in d.values() if v.get("ok"))
    except Exception:
        pass
    s5 = ""
    try:
        d = read_json(paths.near_json) or {}
        dl = read_json(paths.dl_json) or {}
        nm = d.get("near_misses") or []
        s5 = "%d near misses, %d tried" % (len(nm), sum(1 for m in nm if (dl.get(m["key"]) or {}).get("status")
                                                            not in (None, "error", "ratelimit", "skipped")))
    except Exception:
        pass
    return [state_line(paths.orphans_json, s1), state_line(paths.cand_json, s2), state_line(paths.dl_json, s3),
            state_line(paths.added_json, s4), state_line(paths.near_json, s5)]


def run_step(key, cfg, paths, yes):
    if key == "1":
        step_scan(cfg, paths)
    elif key == "2":
        step_search(cfg, paths)
    elif key == "3":
        step_download(cfg, paths, yes=yes)
    elif key == "4":
        step_add(cfg, paths, yes=yes)
    elif key == "5":
        step_nearmiss(cfg, paths, yes=yes)
    elif key == "t":
        step_test(cfg, paths)
    else:
        LOG.warn("unknown option %r" % key)


def guarded(key, cfg, paths, yes):
    t0 = time.time()
    try:
        run_step(key, cfg, paths, yes)
        LOG.info(LOG.c("dim", "(done in %s, log: %s)" % (fmt_dur(time.time() - t0), LOG.path)))
        return True
    except KeyboardInterrupt:
        if LOG.progress:
            LOG.progress.close()
        LOG.warn("interrupted (Ctrl+C) - partial results were saved where possible")
    except Fatal as e:
        if LOG.progress:
            LOG.progress.close()
        LOG.error(str(e))
    except NetError as e:
        if LOG.progress:
            LOG.progress.close()
        LOG.error("network: %s" % e)
    except Exception:
        if LOG.progress:
            LOG.progress.close()
        LOG.error("unexpected error (full traceback in log):\n" + traceback.format_exc())
    return False


def main():
    ap = argparse.ArgumentParser(description="Find orphan files vs qBittorrent and cross-seed them via Prowlarr.")
    ap.add_argument("steps", nargs="*", help="1 2 3 4 5 t - run these and exit (no menu)")
    ap.add_argument("-c", "--config", default=os.path.join(SCRIPT_DIR, DEFAULT_CONFIG_NAME))
    ap.add_argument("-y", "--yes", action="store_true", help="approve every download/add without asking")
    ap.add_argument("-d", "--debug", action="store_true", help="show debug lines (every HTTP call, every decision)")
    args = ap.parse_args()
    try:
        cfg = load_config(args.config)
    except Fatal as e:
        print("ERROR: %s" % e)
        return 2
    paths = Paths(cfg)
    os.makedirs(paths.work, exist_ok=True)
    LOG.open_file(os.path.join(paths.logs, "orphan_xseed_%s.log" % dt.datetime.now().strftime("%Y%m%d_%H%M%S")))
    LOG.debug_console = bool(args.debug or cfg.get("debug"))
    LOG.file("orphan_xseed %s, python %s, config %s" % (VERSION, sys.version.split()[0], cfg["_config_path"]))
    if args.steps:
        ok = True
        for s in args.steps:
            ok = guarded(s.lower(), cfg, paths, args.yes) and ok
        return 0 if ok else 1
    while True:
        st = menu_status(paths)
        print()
        print(LOG.c("bold", "orphan_xseed %s") % VERSION + LOG.c("dim", "   config: %s" % cfg["_config_path"]))
        print("  1) scan folders vs qBittorrent -> orphan list        %s" % st[0])
        print("  2) search Prowlarr for orphans -> possible matches    %s" % st[1])
        print("  3) download .torrent files (approve) + verify         %s" % st[2])
        print("  4) add to qBittorrent (stopped, hardlink if needed)  %s" % st[3])
        print("  5) try near misses (same title, other label / extras)   %s" % st[4])
        print("  t) test connections    d) debug output: %s    q) quit" % ("ON" if LOG.debug_console else "off"))
        print(LOG.c("dim", "  output: %s" % paths.work))
        try:
            ch = input("> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if ch in ("q", "quit", "exit"):
            return 0
        if ch == "d":
            LOG.debug_console = not LOG.debug_console
            continue
        if ch in ("1", "2", "3", "4", "5", "t"):
            guarded(ch, cfg, paths, args.yes)
        elif ch:
            print("?")


if __name__ == "__main__":
    sys.exit(main())
