# Spelunkr Tuner

Watch [Spelunkr](https://spelunkr.tv) channels in IPTV apps or Plex Live TV.

Spelunkr channels are schedules over Internet Archive videos. This app works
out what is on each channel right now and streams it from archive.org,
starting at the right point, as an ordinary live TV channel. It serves an
M3U playlist, an XMLTV guide with channel logos, and an HDHomeRun-compatible
tuner API. Video is remuxed rather than re-encoded, so it runs comfortably on
a small box like an ODROID or Raspberry Pi.

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
| `base_url`    | auto-detected  | Address players use to reach the tuner, e.g. `http://192.168.1.10:5004` |
| `mode`        | `copy`         | `copy` remuxes only; `transcode` normalises to 720p H.264 (CPU heavy) |
| `audio_codec` | `ac3`          | Audio codec: `ac3` (works without Plex's AAC codec) or `aac` |
| `tuner_count` | `4`            | How many channels can stream at once                           |
| `guide_hours` | `48`           | How far ahead the guide goes                                    |

## Watch in an IPTV app (recommended)

Any IPTV player that takes an M3U playlist works, for example iPlayTV or UHF
on Apple TV. Add this playlist URL:

```
http://<host>:5004/playlist.m3u
```

The playlist points to the guide and channel logos, so most apps pick them up
automatically. If yours asks for an EPG/XMLTV URL separately, use
`http://<host>:5004/guide.xml`.

## Watch in Plex Live TV

Requires Plex Pass on the server's owner account.

1. In Plex, go to **Settings → Live TV & DVR → Set up Plex DVR**.
2. If the Spelunkr tuner isn't found, choose **Don't see your HDHomeRun
   device?** and enter `<host>:5004`.
3. When asked for a guide, choose **Have an XMLTV guide on your server?** and
   enter `http://<host>:5004/guide.xml`.

Since app version 2026.12, Plex apps make the server re-encode all Live TV
video, so the Plex server needs to be able to transcode H.264 in real time.
On low-power ARM servers (tested: Plex on an ODROID-N2 under Home Assistant)
Plex picks MJPEG instead and Apple devices show a black picture with sound.

## Run with Docker instead

```bash
BASE_URL=http://<your-ip>:5004 docker compose up -d --build
```

## Endpoints

- `/` status page: what is on every channel now
- `/playlist.m3u`: M3U playlist for IPTV players
- `/guide.xml`: XMLTV guide
- `/logo/<channel>.png`: channel logo
- `/auto/v<channel>`: the MPEG-TS stream for a channel
- `/discover.json`, `/lineup.json`, `/lineup_status.json`: HDHomeRun API
