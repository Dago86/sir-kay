# Sir Kay

A local movie cleanup prototype for Jellyfin, Radarr, Seerr and qBittorrent. Search or select movies, review a per-movie plan and estimated disk space, then explicitly confirm deletion.

**Early software: deletion is permanent.** Partial failures are possible; completed actions cannot be rolled back. Use a backup and test with disposable data before using real media. The app binds only to `127.0.0.1:8793` and has no login. Do not expose it on the network or through a proxy.

## What it does

- Matches Jellyfin and Radarr by TMDB ID and verifies the exact media file path.
- For an active torrent, requires an import-history hash, exactly one torrent file, the same inode as the movie, exactly two hard links and no other torrent referencing that path. Other cases are blocked for manual review.
- Checks the configured disk mount UUID, then revalidates the signed preview after confirmation. Removes an eligible torrent and its data first, then the Jellyfin movie, Radarr entry and Seerr record. An absent torrent is skipped; a failure stops the affected movie and is reported.
- Selects up to 50 movies. Batch results report each movie; the confirmation shows logical size and estimated allocated space freed, accounting for hard links. These estimates are not a guarantee.

Only movies are supported. This prototype cannot safely delete multi-file, shared or otherwise ambiguous torrents. It does not yet have durable retry state, network authentication or automatic startup. A movie can disappear from the list after a partial deletion; inspect connected services manually when a failure is reported.

## Setup

Requires Python 3.10+, Linux `findmnt`, Jellyfin, Radarr, Seerr and qBittorrent. The paths reported by Radarr and qBittorrent must use `/data` for the same host path as `MEDIA_ROOT`. Set the environment variables in `.env.example` using a private configuration mechanism; never publish credentials. The Jellyfin token must be authorized for library deletion. Bind the server to loopback only.

```sh
python3 app.py
```

Open `http://127.0.0.1:8793` on the same computer. Set `RADARR_URL`, `RADARR_API_KEY`, `JELLYFIN_URL`, `JELLYFIN_TOKEN`, `JELLYFIN_USER_ID`, `SEERR_URL`, `SEERR_API_KEY`, `QBIT_URL`, `QBIT_USERNAME`, `QBIT_PASSWORD`, `MEDIA_ROOT` and `MEDIA_UUID` in the process environment before launching.

## Tests

```sh
python3 -m unittest -v test_integrations
```

Tests use simulated services and temporary files. Active torrent deletion has not been verified against a real torrent; no production media is deleted by the tests.
