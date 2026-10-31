"""Spelunkr -> Plex tuner.

Pretends to be an HDHomeRun so Plex Live TV can tune Spelunkr channels.
Spelunkr channels are schedules over archive.org files, so a "live" stream
is just: work out what is on now, then feed FFmpeg a concat list starting
at the right offset. By default FFmpeg only remuxes (-c copy), so this is
cheap enough to run on a small box such as a Home Assistant host.
"""

import json
import os
import select
import socket
import struct
import subprocess
import tempfile
import threading
import time
import urllib.request
import zlib
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote
from xml.sax.saxutils import escape

def load_options():
    """Settings come from the Home Assistant app options, else env vars."""
    options = {}
    try:
        with open("/data/options.json") as file:
            options = json.load(file)
    except (OSError, ValueError):
        pass
    return lambda key, default: str(options.get(key) or os.environ.get(key.upper()) or default)


def lan_ip():
    # No packet is sent; this just asks the OS which interface it would use.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.connect(("192.0.2.1", 9))
        return probe.getsockname()[0]


option = load_options()
PORT = int(option("port", "5004"))
BASE_URL = option("base_url", f"http://{lan_ip()}:{PORT}").rstrip("/")
# copy = remux only (cheap); transcode = normalise to 720p H.264/AAC (needs CPU)
MODE = option("mode", "copy")
AUDIO_CODEC = option("audio_codec", "ac3")  # ac3 or aac
TUNER_COUNT = int(option("tuner_count", "4"))
DEVICE_ID = option("device_id", "5E1C0A01")
REFRESH_HOURS = float(option("refresh_hours", "6"))
GUIDE_HOURS = int(option("guide_hours", "48"))
# How far ahead each FFmpeg run is scheduled; it restarts after this.
STREAM_HOURS = float(option("stream_hours", "6"))
# Restart FFmpeg if it produces nothing for this long.
STALL_SECONDS = 30

UPSTREAMS = [
    "https://data.spelunkr.tv/",
    "https://jbuopkuluyfjwgsrigry.supabase.co/storage/v1/object/public/broadcast/",
]

channels = {}  # guide number -> channel dict
channels_lock = threading.Lock()
active_streams = 0
active_lock = threading.Lock()


def log(message):
    print(message, flush=True)


# --- Spelunkr data ----------------------------------------------------------

def fetch_json(path):
    errors = []
    for base in UPSTREAMS:
        try:
            request = urllib.request.Request(
                base + path, headers={"User-Agent": "spelunkr-plex/0.1"}
            )
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except Exception as error:
            errors.append(f"{base}: {error}")
    raise RuntimeError(f"Could not fetch {path}: " + "; ".join(errors))


def load_channels():
    lineup = fetch_json("lineup.json")
    now_ms = time.time() * 1000
    loaded = {}
    manifests = {}

    for channel in lineup["channels"]:
        if channel.get("debugOnly") or channel.get("dial") is None:
            continue

        versions = channel["versions"]
        eligible = [v for v in versions if v["effectiveAtMs"] <= now_ms]
        version = max(eligible or versions, key=lambda v: v["effectiveAtMs"])

        if version["manifest"] not in manifests:
            manifests[version["manifest"]] = fetch_json(version["manifest"])

        number = str(channel["dial"])
        loaded[number] = {
            "id": channel["id"],
            "number": number,
            "name": channel["name"].title(),
            "description": channel.get("description", ""),
            "effectiveAt": version["effectiveAtMs"] / 1000,
            "startIndex": version["startIndex"],
            "items": manifests[version["manifest"]]["items"],
            "logo": render_logo(channel.get("logomark"), channel.get("color")),
        }

    return loaded


# --- Channel logos ----------------------------------------------------------
# Spelunkr logos are 32x32 grids of palette letters. The palette and colour
# maths are ported from the spelunkr.tv player so the logos match the site.

FIXED_PALETTE = {
    "k": "#0b0f26", "n": "#2a3580", "s": "#8899cc",
    "p": "#e8eaf0", "g": "#ffd740", "o": "#d2500c",
}
LOGO_SCALE = 8


def hex_to_hsv(colour):
    r, g, b = (int(colour[i:i + 2], 16) / 255 for i in (1, 3, 5))
    high, low = max(r, g, b), min(r, g, b)
    spread = high - low
    hue = 0.0
    if spread:
        if high == r:
            hue = ((g - b) / spread + (6 if g < b else 0)) / 6
        elif high == g:
            hue = ((b - r) / spread + 2) / 6
        else:
            hue = ((r - g) / spread + 4) / 6
    return hue * 360, (spread / high * 100 if high else 0), high * 100


def hsv_to_rgb(hue, sat, val):
    sat, val = sat / 100, val / 100

    def channel(n):
        k = (n + hue / 60) % 6
        return round(255 * (val - val * sat * max(0, min(k, 4 - k, 1))))

    return channel(5), channel(3), channel(1)


def hue_toward(hue, target, step):
    delta = (target - hue + 540) % 360 - 180
    sign = (delta > 0) - (delta < 0)
    return (hue + sign * min(step, abs(delta)) + 360) % 360


def logo_palette(colour):
    if not (isinstance(colour, str) and len(colour) == 7 and colour.startswith("#")):
        colour = "#666666"
    hue, sat, val = hex_to_hsv(colour.lower())
    palette = {k: tuple(int(v[i:i + 2], 16) for i in (1, 3, 5)) for k, v in FIXED_PALETTE.items()}
    palette["c"] = tuple(int(colour[i:i + 2], 16) for i in (1, 3, 5))
    palette["d"] = hsv_to_rgb(hue_toward(hue, 255, 14), min(100, sat * 1.1), val * 0.6)
    palette["l"] = hsv_to_rgb(hue_toward(hue, 50, 10), sat * 0.6, val + (100 - val) * 0.6)
    return palette


def render_logo(logomark, colour):
    """Render a pixel logomark as PNG bytes, or None if there isn't one."""
    rows = (logomark or {}).get("rows")
    if (logomark or {}).get("type") != "pixel" or not rows:
        return None

    palette = logo_palette(colour)
    raw = bytearray()
    for row in rows:
        line = bytearray()
        for cell in row:
            pixel = bytes((0, 0, 0, 0)) if cell == "." else bytes((*palette.get(cell, palette["p"]), 255))
            line += pixel * LOGO_SCALE
        raw += (b"\x00" + line) * LOGO_SCALE

    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    size = len(rows) * LOGO_SCALE
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )


def refresh_loop():
    global channels
    while True:
        try:
            loaded = load_channels()
            with channels_lock:
                channels = loaded
            log(f"Loaded {len(loaded)} channels")
        except Exception as error:
            log(f"Channel refresh failed, keeping current data: {error}")
        time.sleep(REFRESH_HOURS * 3600)


# --- Schedule maths ---------------------------------------------------------

def cycle_items(channel, cycle):
    # The first pass through a manifest starts at startIndex; later passes
    # loop the whole list.
    items = channel["items"]
    return items[channel["startIndex"]:] if cycle == 0 else items


def cycle_length(channel, cycle):
    return sum(item["duration"] for item in cycle_items(channel, cycle))


def locate(channel, timestamp):
    """Return (cycle, index, offset into item) for a wall-clock time."""
    elapsed = max(0.0, timestamp - channel["effectiveAt"])
    first = cycle_length(channel, 0)

    if elapsed < first:
        cycle, position = 0, elapsed
    else:
        elapsed -= first
        full = cycle_length(channel, 1)
        cycle, position = 1 + int(elapsed // full), elapsed % full

    for index, item in enumerate(cycle_items(channel, cycle)):
        if position < item["duration"]:
            return cycle, index, position
        position -= item["duration"]

    return cycle + 1, 0, 0.0


def schedule(channel, start, hours):
    """Yield (start time, item, offset) from `start` for `hours`."""
    end = start + hours * 3600
    cycle, index, offset = locate(channel, start)
    cursor = start - offset

    while cursor < end:
        items = cycle_items(channel, cycle)
        item = items[index]
        yield cursor, item, offset
        cursor += item["duration"]
        offset = 0.0
        index += 1
        if index >= len(items):
            cycle, index = cycle + 1, 0


def media_url(item):
    filename = item.get("mp4") or item.get("file")
    return (
        "https://archive.org/download/"
        + quote(item["id"], safe="")
        + "/"
        + quote(filename, safe="/")
    )


# --- Streaming --------------------------------------------------------------

def write_concat(channel, now):
    """Write an ffconcat list covering the next STREAM_HOURS."""
    lines = ["ffconcat version 1.0"]
    for _, item, offset in schedule(channel, now, STREAM_HOURS):
        trim = item.get("inS", 0)
        lines.append(f"file '{media_url(item)}'")
        lines.append(f"inpoint {trim + offset:.3f}")
        lines.append(f"outpoint {trim + item['duration']:.3f}")

    handle, path = tempfile.mkstemp(suffix=".ffconcat")
    with os.fdopen(handle, "w") as file:
        file.write("\n".join(lines) + "\n")
    return path


def ffmpeg_command(concat_path):
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning",
        "-re",
        "-f", "concat", "-safe", "0",
        "-protocol_whitelist", "file,http,https,tcp,tls,crypto",
        "-i", concat_path,
        "-map", "0:v:0?", "-map", "0:a:0?",
    ]
    if MODE == "transcode":
        command += [
            "-vf", "scale=-2:720,fps=30000/1001",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-g", "60",
        ]
    else:
        command += ["-c:v", "copy"]
    # Audio is always re-encoded (cheap) so it is identical across show
    # changes. AC3 by default: FFmpeg only learns AAC's sample rate and
    # channels by decoding it, and Plex fails with "sample rate not set"
    # when its AAC decoder is missing, whereas AC3's are read from headers.
    command += [
        "-af", "aresample=async=1",
        "-c:a", AUDIO_CODEC, "-b:a", "192k", "-ac", "2", "-ar", "48000",
    ]
    return command + [
        "-f", "mpegts", "-mpegts_flags", "+resend_headers", "pipe:1",
    ]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        log("%s - %s" % (self.address_string(), fmt % args))

    def send_body(self, body, content_type):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/") or "/"
        with channels_lock:
            current = dict(channels)

        routes = {
            "/discover.json": lambda: (json.dumps(discover()), "application/json"),
            "/lineup_status.json": lambda: (json.dumps({
                "ScanInProgress": 0, "ScanPossible": 1,
                "Source": "Cable", "SourceList": ["Cable"],
            }), "application/json"),
            "/lineup.json": lambda: (json.dumps(lineup(current)), "application/json"),
            "/device.xml": lambda: (device_xml(), "application/xml"),
            "/guide.xml": lambda: (guide_xml(current), "application/xml"),
            "/playlist.m3u": lambda: (playlist(current), "audio/x-mpegurl"),
            "/": lambda: (status(current), "text/plain"),
        }

        if path in routes:
            body, content_type = routes[path]()
            return self.send_body(body, content_type)

        if path.startswith("/auto/v"):
            channel = current.get(path[len("/auto/v"):])
            if channel:
                return self.stream(channel)

        if path.startswith("/logo/") and path.endswith(".png"):
            channel = current.get(path[len("/logo/"):-len(".png")])
            if channel and channel["logo"]:
                return self.send_body(channel["logo"], "image/png")

        self.send_error(404)

    def stream(self, channel):
        global active_streams
        with active_lock:
            if active_streams >= TUNER_COUNT:
                return self.send_error(503, "All tuners busy")
            active_streams += 1

        self.send_response(200)
        self.send_header("Content-Type", "video/mp2t")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        if self.command == "HEAD":
            with active_lock:
                active_streams -= 1
            return

        # A write only fails once FFmpeg produces something, so a stalled
        # FFmpeg would otherwise hold a tuner forever. The watchdog notices a
        # vanished client or a stall and kills FFmpeg to unblock the loop.
        state = {"process": None, "last_data": time.time(), "gone": False}
        done = threading.Event()

        def watchdog():
            while not done.wait(1):
                state["gone"] = state["gone"] or client_gone(self.connection)
                stalled = time.time() - state["last_data"] > STALL_SECONDS
                process = state["process"]
                if process and process.poll() is None and (state["gone"] or stalled):
                    if not state["gone"]:
                        log(f"{channel['name']}: no data for {STALL_SECONDS}s, restarting ffmpeg")
                    process.kill()

        threading.Thread(target=watchdog, daemon=True).start()
        concat_path = None
        try:
            while not state["gone"]:
                now = time.time()
                _, item, offset = next(schedule(channel, now, 0.01))
                log(f"{channel['name']}: {item['title']} at {offset:.0f}s ({MODE})")

                concat_path = write_concat(channel, now)
                state["last_data"] = time.time()
                state["process"] = process = subprocess.Popen(
                    ffmpeg_command(concat_path), stdout=subprocess.PIPE, bufsize=0
                )
                while True:
                    chunk = process.stdout.read(188 * 348)
                    if not chunk:
                        break
                    state["last_data"] = time.time()
                    self.wfile.write(chunk)

                process.wait()
                os.unlink(concat_path)
                concat_path = None
                if state["gone"]:
                    break
                log(f"{channel['name']}: ffmpeg exited {process.returncode}, continuing")
                if time.time() - now < 5:
                    done.wait(5)  # avoid hammering archive.org if it keeps failing
            log(f"{channel['name']}: client disconnected")
        except (BrokenPipeError, ConnectionResetError):
            log(f"{channel['name']}: client disconnected")
        finally:
            done.set()
            process = state["process"]
            if process and process.poll() is None:
                process.kill()
                process.wait()
            if concat_path:
                os.unlink(concat_path)
            with active_lock:
                active_streams -= 1


def client_gone(sock):
    """True once the client has closed its end of the connection."""
    try:
        readable, _, _ = select.select([sock], [], [], 0)
        return bool(readable) and sock.recv(1, socket.MSG_PEEK) == b""
    except OSError:
        return True


# --- HDHomeRun / guide documents --------------------------------------------

def discover():
    return {
        "FriendlyName": "Spelunkr",
        "Manufacturer": "Silicondust",
        "ModelNumber": "HDTC-2US",
        "FirmwareName": "hdhomeruntc_atsc",
        "FirmwareVersion": "20200101",
        "DeviceID": DEVICE_ID,
        "DeviceAuth": "spelunkr",
        "TunerCount": TUNER_COUNT,
        "BaseURL": BASE_URL,
        "LineupURL": f"{BASE_URL}/lineup.json",
    }


def lineup(current):
    return [
        {
            "GuideNumber": c["number"],
            "GuideName": c["name"],
            "URL": f"{BASE_URL}/auto/v{c['number']}",
        }
        for c in sorted(current.values(), key=lambda c: int(c["number"]))
    ]


def device_xml():
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <URLBase>{BASE_URL}</URLBase>
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <device>
    <deviceType>urn:schemas-upnp-org:device:MediaServer:1</deviceType>
    <friendlyName>Spelunkr</friendlyName>
    <manufacturer>Silicondust</manufacturer>
    <modelName>HDTC-2US</modelName>
    <modelNumber>HDTC-2US</modelNumber>
    <serialNumber>{DEVICE_ID}</serialNumber>
    <UDN>uuid:{DEVICE_ID}</UDN>
  </device>
</root>
"""


def xmltv_time(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y%m%d%H%M%S +0000")


def guide_xml(current):
    out = ['<?xml version="1.0" encoding="UTF-8"?>', '<tv generator-info-name="spelunkr-plex">']
    ordered = sorted(current.values(), key=lambda c: int(c["number"]))

    for c in ordered:
        out.append(
            f'<channel id="{c["number"]}">'
            f'<display-name>{c["number"]} {escape(c["name"])}</display-name>'
            f'<display-name>{c["number"]}</display-name>'
            f'<display-name>{escape(c["name"])}</display-name>'
            + (f'<icon src="{logo_url(c)}" />' if c["logo"] else "")
            + '</channel>'
        )

    now = time.time()
    for c in ordered:
        for start, item, offset in schedule(c, now, GUIDE_HOURS):
            year = f'<date>{escape(item["year"])}</date>' if item.get("year") else ""
            out.append(
                f'<programme start="{xmltv_time(start)}" '
                f'stop="{xmltv_time(start + item["duration"])}" channel="{c["number"]}">'
                f'<title>{escape(item["title"])}</title>'
                f'<desc>{escape(c["description"])}</desc>'
                f'{year}<category>{escape(c["name"])}</category>'
                f'</programme>'
            )

    out.append("</tv>")
    return "\n".join(out)


def logo_url(channel):
    return f"{BASE_URL}/logo/{channel['number']}.png"


def playlist(current):
    # url-tvg / x-tvg-url let IPTV apps find the guide from the playlist alone.
    guide = f"{BASE_URL}/guide.xml"
    out = [f'#EXTM3U url-tvg="{guide}" x-tvg-url="{guide}"']
    for c in sorted(current.values(), key=lambda c: int(c["number"])):
        logo = f' tvg-logo="{logo_url(c)}"' if c["logo"] else ""
        out.append(
            f'#EXTINF:-1 tvg-id="{c["number"]}" tvg-chno="{c["number"]}" '
            f'tvg-name="{c["name"]}"{logo} group-title="Spelunkr",{c["name"]}'
        )
        out.append(f"{BASE_URL}/auto/v{c['number']}")
    return "\n".join(out) + "\n"


def status(current):
    lines = [f"Spelunkr tuner ({MODE} mode), {len(current)} channels, {active_streams} streaming", ""]
    now = time.time()
    for c in sorted(current.values(), key=lambda c: int(c["number"])):
        _, item, offset = next(schedule(c, now, 0.01))
        left = (item["duration"] - offset) / 60
        lines.append(f'{c["number"]:>4}  {c["name"]:<22} {item["title"]}  ({left:.0f} min left)')
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    channels = load_channels()
    log(f"Loaded {len(channels)} channels; serving on {BASE_URL} in {MODE} mode")
    threading.Thread(target=refresh_loop, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
