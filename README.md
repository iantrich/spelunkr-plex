# Spelunkr for Plex

Watch [Spelunkr](https://spelunkr.tv) channels in Plex Live TV.

Spelunkr channels are schedules over Internet Archive videos. This app
pretends to be an HDHomeRun tuner: when Plex tunes a channel, it works out
what is on right now and streams it from archive.org, starting at the right
point. Video is remuxed rather than re-encoded, so it runs comfortably on a
small box like an ODROID or Raspberry Pi.

Unofficial and not affiliated with Spelunkr. All video comes from the
Internet Archive.

## Install as a Home Assistant app

1. In Home Assistant, go to **Settings → Apps → App store**, open the
   **⋮** menu, choose **Repositories**, and add this repository's URL.
2. Install **Spelunkr Tuner** and start it.
3. Open the web UI to check it. It lists every channel and what is on now.

Options:

| Option        | Default        | Meaning                                                        |
|---------------|----------------|----------------------------------------------------------------|
| `base_url`    | auto-detected  | Address Plex uses to reach the tuner, e.g. `http://192.168.1.10:5004` |
| `mode`        | `copy`         | `copy` remuxes only; `transcode` normalises to 720p H.264 (CPU heavy) |
| `tuner_count` | `4`            | How many channels can stream at once                           |
| `guide_hours` | `48`           | How far ahead the guide goes                                    |

## Add it to Plex

Requires Plex Pass on the server's owner account.

1. In Plex, go to **Settings → Live TV & DVR → Set up Plex DVR**.
2. If the Spelunkr tuner isn't found, choose **Don't see your HDHomeRun
   device?** and enter `<host>:5004`.
3. When asked for a guide, choose **Have an XMLTV guide on your server?** and
   enter `http://<host>:5004/guide.xml`.

## Run with Docker instead

```bash
BASE_URL=http://<your-ip>:5004 docker compose up -d --build
```

## Endpoints

- `/` status page: what is on every channel now
- `/discover.json`, `/lineup.json`, `/lineup_status.json`: HDHomeRun API
- `/guide.xml`: XMLTV guide
- `/playlist.m3u`: M3U playlist for other IPTV players
- `/auto/v<channel>`: the MPEG-TS stream for a channel
