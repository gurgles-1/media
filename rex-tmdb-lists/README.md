# rex-tmdb-lists

Pull movies and TV shows from TMDB custom lists into [rex](https://rex.debridmediamanager.com/), and resolve playback through configured Stremio-compatible stream addons.

TMDB supplies catalog metadata; it does not supply playable video. A Stremio stream addon must return direct HTTP(S) media links for each item. This project never downloads media.

## Requirements

- Python 3.10+
- `ffprobe` (from FFmpeg)
- A rex nightly dated 2026-10-25 or newer
- A TMDB v4 API Read Access Token
- One or more Stremio-compatible stream addon manifest URLs

Public lists can be read without a user session, but this app uses the v4 bearer token for consistent API access. Private lists are only available to their owner. See [TMDB list details](https://developer.themoviedb.org/v4/reference/list-details) and [TMDB authentication](https://developer.themoviedb.org/v4/docs/authentication-application).

## Setup

1. Copy `config.example.json` to `config.json` and fill in your TMDB token, list IDs, rex URL, and stream addon manifest URLs.
2. Run `python rex_tmdb_lists.py`.

The addon registers the source and performs a sync at startup, then repeats every six hours by default. It listens on `127.0.0.1:9983`; set `listen` and `relay_url` if rex runs on another host or in Docker. Keep the listener on a trusted private network because anyone who can reach it can request media through the relay.

TMDB list IDs are the numeric IDs from a `themoviedb.org/list/<id>` URL. Lists may mix movies and TV. TV entries expand into their seasons and episodes, using the TMDB episode numbering. Items without an IMDb ID or a playable stream are skipped. Set an `item_limit` to avoid an unexpectedly large first sync.

## Config

See `config.example.json`. Keep `config.json` private; it contains the TMDB token and may contain stream addon URLs with provider credentials. Never commit it.

## Run as a service

Use your operating system's service manager to run `python /path/to/rex_tmdb_lists.py` from this directory. The process writes its probe cache to `cache/`.

## Notes

- Each item is resolved and probed before it is sent to rex, as rex requires `media_info`.
- Stream links are re-requested when the title is played. The service redirects rex to the chosen current link and relays responses only when a stream addon specifies proxy headers.
- TMDB posters are public image URLs. TMDB attribution is required by its API terms; this project is an independent integration and is not endorsed by TMDB.
- A full snapshot is sent only after all configured lists were read successfully. This avoids removing existing rex items after an API or network failure.
- This first version handles movie and TV lists. Unsupported list entries are ignored.

This product uses the TMDB API but is not endorsed or certified by TMDB. See [TMDB's API terms](https://www.themoviedb.org/api-terms-of-use).

## License

MIT
