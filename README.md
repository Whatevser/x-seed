# xseed

**Cross-seed the files your torrent client isn't seeding.**

Over time a torrent disk fills up with data that no torrent in qBittorrent points at anymore: torrents you removed but whose files you kept, things copied over from another machine, *arr imports whose original torrent is gone, packs you renamed. That data could be seeding on your trackers, but nothing tells you what it is or where it came from.

xseed finds it, groups it into releases the way a tracker would (a movie folder with its extras, a season, an album, a collection), searches every tracker you have in Prowlarr for the same release, proves each hit byte-for-byte against your files, and adds it to qBittorrent pointing at the data you already have. It's like [cross-seed](https://www.cross-seed.org/), but it starts from your *disk* rather than from torrents you already seed, and it is much more forgiving about names.

Single Python file, standard library only, runs on the machine that has the disks (built for Linux Mint with qbittorrent-nox and Prowlarr). Version 0.2.

## What it does

| Step | What happens | Output |
|---|---|---|
| **1 scan** | Walks your download folders and matches every file against qBittorrent: by path, by inode (hardlinks are recognized), and by name + size for torrents whose files aren't visible from here. What's left is grouped into releases: a movie folder with its screens/nfo/featurettes, a season, a multi-season pack *and* each season inside it, a movie collection *and* each movie in it, an album with its CDs. Nothing is ever searched file by file. | `orphans.txt` |
| **2 search** | Searches Prowlarr with several name variants per release (`Title 2020`, `Title GROUP`, `Title.2020`, `TitleWords 2020`, alt titles, `Show S02`...). **Every tracker is searched on its own with its own cooldown** (30 s by default): while one tracker cools down, the others keep going with the next releases, and it catches up afterwards. A hit needs the same title, the same size (exact, within the indexer's display rounding, or within 0.1%), and no conflicting tag (group, resolution, source, codec, streaming service, edition, year, season/episode). | `candidates.txt` |
| **3 download** | Grabs the `.torrent` files (approve one by one, or all), maps the torrent's files onto yours by size and name, and SHA1-checks real pieces against your data. Wrong files are caught here even if the size matched. | `torrents/`, `downloads.txt` |
| **4 add** | Adds verified torrents to qBittorrent **stopped**. If the layout matches, it points straight at your files (files renamed inside qBittorrent if needed, nothing on disk is renamed). If the torrent is a folder but you have a lone `.mkv`, or the torrent has extras you don't have, it gets a hardlink folder on the same disk (`<disk>/hardlinked/xseed/...`) so your library stays untouched. | `added.txt` |
| **w web page** | Settings in your browser, from any device on your network: scan folders with folder suggestions as you type, qBittorrent / Prowlarr address and API key, which trackers to search, the search cooldown, and a connection test. | `xseed.json` |
| **s settings** | The same settings in the terminal. | `xseed.json` |
| **t test** | Checks the qBittorrent and Prowlarr connections and lists your trackers. | |

Nothing is ever started. You review the result in qBittorrent (filter by tag `xseed`) and press Start yourself. A torrent tagged `xseed-partial` downloads only the files you don't have (into its hardlink folder, not your library).

## Quick start

```bash
# put xseed.py in a folder, then:
python3 xseed.py          # interactive menu
```

On the first run it writes `xseed.json` next to itself. Press **w**: it prints the link to the settings page, for example `http://192.168.1.20:9898/` (this machine's own address on your network, port `web.port`). On that page:

1. **Folders to scan.** Start typing a path and the folders inside it appear underneath; click one to step into it (`/media/you/disk/Torrent/` offers `Download`, then `Movies` and `Shows`...). It also lists where qBittorrent saves your torrents, one line per disk, ready to add.
2. **qBittorrent API key.** In qBittorrent: Options, WebUI, API key, Generate (qBittorrent 5.2 or newer).
3. **Prowlarr API key.** In Prowlarr: Settings, General, Security, API Key. Then tick the trackers to search.
4. **Save changes**, then **Test connections**.

Then run **1, 2, 3, 4** from the menu. The page keeps running in the background while you use the menu; **w** again stops it.

Without the menu:

```bash
python3 xseed.py 1 2            # scan, then search
python3 xseed.py 3 4 --yes      # download + add everything without asking
python3 xseed.py w              # just the settings page, until Ctrl+C
python3 xseed.py s              # settings in the terminal
python3 xseed.py t --debug      # connection test, every HTTP call shown
```

Everything (lists, `.torrent` files, the search cache, a full log of every request and decision) goes into `xseed_data/` next to the config.

## Requirements

- Python 3.8+ (nothing to `pip install`)
- qBittorrent **5.2 or newer** with the WebUI on, and an API key (tested with 5.2.2 / WebAPI 2.15.1). xseed only uses the API key, never a username or password.
- Prowlarr with your trackers in it, and its API key.
- The script must see the files under the same paths qBittorrent uses. If it doesn't (docker, NFS), set `scan.path_mappings`.

## Searching politely

Trackers don't like being hammered. xseed:

- searches **one tracker per request** and waits `prowlarr.search_cooldown_sec` (default 30 s, editable in settings) between two searches on the *same* tracker. Different trackers run in parallel, so a slow or cooling tracker never holds up the rest.
- pauses a tracker when Prowlarr answers HTTP 429, for as long as it asks (`Retry-After`).
- stops searching a movie/season on a tracker once *that tracker* gave a good match.
- caches every search per tracker for `cache_hours` (default 7 days). Re-running step 2, or resuming after **Ctrl+C**, replays finished searches instantly and continues where it stopped.

The status line shows how many trackers are searching or cooling and an ETA based on the slowest tracker.

## How matching avoids garbage

- **Titles must really match.** Punctuation, `The`, `&`/`and`, accents, `II`/`2`, `Part One`/`Part 1` don't matter. Otherwise they must be the same: *Behind the Attraction* is not *Attraction*, *American Pie 2* is not *American Pie*, *Blood+* is not *Blood*. Indexers that answer every query with their latest uploads can't sneak anything in.
- **Tags missing on either side are fine; tags that disagree are not.** A Radarr-renamed `Movie (2011).mkv` can still match the scene release.
- **Sizes:** exact, or within the indexer's display rounding (`22.1 GiB`), or within 0.1% (an nfo you deleted). Looser sizes (up to 2%) also need the same group or two of resolution/source/codec.
- **Step 3 has the final word:** real piece hashes from the `.torrent` are checked against your files before anything is added.

## Files in `xseed_data/`

| File | What |
|---|---|
| `orphans.txt` | releases no torrent covers (FULL) or only partly covers (PART), with the seasons/movies inside them |
| `orphan_extras.txt` | releases where only nfo/srt/jpg extras are unseeded (not searched) |
| `weak_matches.txt` | files counted as seeded only by name + size (the torrent's files aren't visible from here) |
| `candidates.txt` | every search per release, and the matches with their score |
| `downloads.txt` | every `.torrent` grabbed and its verdict: exact, renamed, partial, bad pieces, mismatch |
| `added.txt` | what was added to qBittorrent, where it points, and what to do next |
| `logs/` | full log of every run |

## Settings worth knowing (`xseed.json`)

Every key has a `_..._help` line next to it in the file. The main ones:

- `scan.paths`: folders to scan (settings → 1)
- `qbittorrent.url / api_key` (settings → 2, 3)
- `prowlarr.url / api_key` (settings → 4, 5)
- `prowlarr.search_cooldown_sec`: seconds between two searches on one tracker (settings → 6)
- `prowlarr.indexer_ids`: `[-2]` = every enabled torrent tracker, or a list of Prowlarr ids (settings → 7)
- `search.max_queries_per_item` / `max_queries_per_sub_unit`: name variants tried per release / per season or movie
- `search.reject_on`: which tag conflicts reject a result
- `add.link_dir`: hardlink folder, default `{drive}/hardlinked/xseed`
- `add.tags`: tags put on everything xseed adds (default `xseed`)
- `web.port`: port of the settings page (default `9898`)

The settings page has no login: anyone on your network who can open it can change the settings. API keys are never sent to the page, only their first and last four characters.
