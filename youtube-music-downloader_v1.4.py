import sys
import os
import yt_dlp
import mimetypes
import requests
import re
import xml.etree.ElementTree as ET
import json
import shutil
import tempfile
import base64

from urllib.parse import parse_qs, urlparse, urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from ytmusicapi import YTMusic
from pathlib import Path
from PIL import Image, UnidentifiedImageError
from mutagen.flac import Picture
from mutagen.oggopus import OggOpus

# checks if the user exists and has a navidrome library
def get_navidrome_library_path(username: str) -> str:
    base_url = os.environ.get("AUTHENTIK_URL", "").rstrip("/")
    api_token = os.environ.get("AUTHENTIK_API_TOKEN", "")

    if not base_url or not api_token:
        sys.exit("Error: AUTHENTIK_URL or AUTHENTIK_API_TOKEN is missing.")

    query = urlencode({"username": username})
    request = Request(
        f"{base_url}/api/v3/core/users/?{query}",
        headers={"Authorization": f"Bearer {api_token}"},
    )

    try:
        with urlopen(request, timeout=10) as response:
            data = json.load(response)
    except HTTPError as exc:
        sys.exit(f"Error: authentik API returned HTTP {exc.code}. Check the API token permissions.")
    except (URLError, TimeoutError) as exc:
        sys.exit(f"Error: could not reach authentik: {exc}")
    except json.JSONDecodeError:
        sys.exit("Error: authentik returned invalid JSON.")

    users = [
        user for user in data.get("results", [])
        if user.get("username") == username
    ]

    if len(users) != 1:
        sys.exit(f"Error: expected one authentik user named {username!r}; found {len(users)}.")

    path = users[0].get("attributes", {}).get("navidrome_library_path")

    if not isinstance(path, str) or not path.strip():
        sys.exit(f"Error: no navidrome_library_path is set for {username!r}.")

    return path.strip()

# gets tags for later from yt music
def get_information(video_id: str) -> dict:
    def as_number(*values):
        for value in values:
            if value is not None:
                try:
                    return int(value)
                except (TypeError, ValueError):
                    pass
        return None

    if not video_id:
        print("Could not find a video ID in that URL.")
        return

    yt = YTMusic()
    watch = yt.get_watch_playlist(videoId=video_id)

    track = next(
        (
            item
            for item in watch.get("tracks", [])
            if item.get("videoId") == video_id
        ),
        None,
    )

    if not track:
        print("Track metadata wasn't returned for this video.")
        return

    album = track.get("album") or {}
    album_info = yt.get_album(album["id"]) if album.get("id") else {}
    album_tracks = album_info.get("tracks", [])

    # Find this song in the album track list.
    album_track = None
    album_track_position = None

    for position, item in enumerate(album_tracks, start=1):
        if item.get("videoId") == video_id:
            album_track = item
            album_track_position = position
            break

    # If the video IDs don't match, try matching by title.
    if album_track is None and track.get("title"):
        for position, item in enumerate(album_tracks, start=1):
            if item.get("title") == track.get("title"):
                album_track = item
                album_track_position = position
                break

    album_artists = album_info.get("artists", [])

    track_number = as_number(
        track.get("trackNumber"),
        track.get("track_number"),
        album_track.get("trackNumber") if album_track else None,
        album_track.get("track_number") if album_track else None,
        album_track.get("index") if album_track else None,
        album_track_position,
    )

    disc_number = as_number(
        track.get("discNumber"),
        track.get("disc_number"),
        album_track.get("discNumber") if album_track else None,
        album_track.get("disc_number") if album_track else None,
    )

    title = track.get("title") or ""
    artists = track.get("artists") or []
    album = track.get("album") or {}
    album_artists = track.get("album_artists") or artists

    return {
        "video_id": video_id,
        "title": title,
        "artists": [a["name"] for a in artists],
        "album": album.get("name") or title,
        "album_artists": [a["name"] for a in album_artists],
        "year": track.get("year"),
        "track_number": track_number or 1,
        "disc_number": disc_number,
    }



def download(video_id: str) -> None:
    if not video_id:
        raise ValueError("Could not find a video ID in the URL.")

    options = {
        "format": "bestaudio/best",
        "noplaylist": True,
        "restrictfilenames": True,
        "outtmpl": f"./{video_id}.%(ext)s",
        "writethumbnail": True,
        "default_search": "ytsearch",
        "retry_sleep_functions": {
            "http": lambda attempt: 5,
        },
        "postprocessors": [
            {"key": "SponsorBlock", "categories": ["music_offtopic"]},
            {"key": "FFmpegExtractAudio", "preferredcodec": "opus"},
            {"key": "FFmpegMetadata", "add_metadata": True},
        ],
    }

    with yt_dlp.YoutubeDL(options) as ydl:
        ydl.download([f"https://www.youtube.com/watch?v={video_id}"])

def center_square_crop(video_id: str, directory: str = ".") -> Path:
    if not video_id:
        raise ValueError("Could not find a video ID in the URL.")

    for image_path in Path(directory).glob(f"{video_id}.*"):
        try:
            image = Image.open(image_path)
        except (UnidentifiedImageError, OSError):
            continue

        with image:
            width, height = image.size
            side = min(width, height)
            left = (width - side) // 2
            top = (height - side) // 2

            cropped = image.crop((left, top, left + side, top + side))
            image_format = image.format
            if not image_format:
                continue

            if image_format == "JPEG" and cropped.mode not in ("RGB", "L", "CMYK"):
                cropped = cropped.convert("RGB")

            temp_path = image_path.with_name(f".{image_path.name}.tmp")
            try:
                cropped.save(temp_path, format=image_format)
                os.replace(temp_path, image_path)
            finally:
                temp_path.unlink(missing_ok=True)

        return image_path

    raise FileNotFoundError(f"No image thumbnail found for video ID {video_id!r}")

def embed_tags(track: dict, cover_path: str) -> None:
    video_id = track["video_id"]
    mp3_path = Path.cwd() / f"{video_id}.mp3"

    try:
        tags = ID3(mp3_path)
    except ID3NoHeaderError:
        tags = ID3()

    title = track.get("title") or ""
    artists = track.get("artists") or []
    album = track.get("album") or title
    album_artists = track.get("album_artists") or artists
    year = track.get("year")
    track_number = track.get("track_number") or 1
    disc_number = track.get("disc_number")

    tags["TIT2"] = TIT2(encoding=3, text=[title])
    tags["TPE1"] = TPE1(encoding=3, text=[", ".join(artists)])
    tags["TALB"] = TALB(encoding=3, text=[album])
    tags["TPE2"] = TPE2(encoding=3, text=[", ".join(album_artists)])

    tags.delall("TDRC")
    if year is not None:
        tags.add(TDRC(encoding=3, text=[str(year)]))

    tags.delall("TRCK")
    if track_number is not None:
        tags.add(TRCK(encoding=3, text=[str(track_number)]))

    tags.delall("TPOS")
    if disc_number is not None:
        tags.add(TPOS(encoding=3, text=[str(disc_number)]))

    # Multi-valued fields for Navidrome
    tags.delall("TXXX:ARTISTS")
    tags.add(TXXX(encoding=3, desc="ARTISTS", text=artists))

    tags.delall("TXXX:ALBUMARTISTS")
    tags.add(TXXX(encoding=3, desc="ALBUMARTISTS", text=album_artists))

    # Embed the thumbnail as the front cover
    mime_type, _ = mimetypes.guess_type(cover_path)
    if not mime_type or not mime_type.startswith("image/"):
        raise ValueError(f"Can't determine image type for {cover_path}")

    with open(cover_path, "rb") as image_file:
        image_data = image_file.read()

    tags.delall("APIC")
    tags.add(APIC(
        encoding=3,
        mime=mime_type,
        type=3,  # Front cover
        desc="Cover",
        data=image_data,
    ))

    tags.save(mp3_path, v2_version=4)

def _set_comment(audio: OggOpus, key: str, value) -> None:
    """Replace a Vorbis comment, removing any previous value."""
    if key in audio:
        del audio[key]
    if value is not None:
        audio[key] = value


def _synced_lyrics_as_lrc(lyrics: dict) -> str:
    """Return synced lyrics as LRC text for the Opus LYRICS comment."""
    lyric_format = lyrics.get("format", "").lower()
    text = lyrics.get("text", "")

    if lyric_format == "lrc":
        return text

    if lyric_format == "ttml":
        lines = _ttml_lines(text)
    else:
        return text

    # Convert milliseconds to LRC timestamps: [minutes:seconds.centiseconds]
    return "\n".join(
        f"[{ms // 60000:02d}:{(ms % 60000) // 1000:02d}."
        f"{(ms % 1000) // 10:02d}]{line}"
        for line, ms in lines
    )


def embed_tags(
    track: dict,
    cover_path: str,
    lyrics: dict | None = None,
) -> None:
    """Embed track metadata, cover art, and optional lyrics into an Opus file."""
    opus_path = Path.cwd() / f"{track['video_id']}.opus"
    audio = OggOpus(opus_path)

    title = track.get("title") or ""
    artists = track.get("artists") or []
    album = track.get("album") or title
    album_artists = track.get("album_artists") or artists

    _set_comment(audio, "title", title)
    _set_comment(audio, "artist", artists)
    _set_comment(audio, "album", album)
    _set_comment(audio, "albumartist", album_artists)

    # Keep the custom fields used for Navidrome.
    _set_comment(audio, "artists", artists)
    _set_comment(audio, "albumartists", album_artists)

    year = track.get("year")
    _set_comment(audio, "date", str(year) if year is not None else None)

    track_number = track.get("track_number") or 1
    _set_comment(audio, "tracknumber", str(track_number))

    disc_number = track.get("disc_number")
    if disc_number is not None:
        _set_comment(audio, "discnumber", str(disc_number))

    # Embed the front cover using the Vorbis-comment picture format.
    mime_type, _ = mimetypes.guess_type(cover_path)
    if not mime_type or not mime_type.startswith("image/"):
        raise ValueError(f"Can't determine image type for {cover_path}")

    picture = Picture()
    picture.data = Path(cover_path).read_bytes()
    picture.type = 3  # Front cover
    picture.desc = "Cover"
    picture.mime = mime_type
    picture.width = 0
    picture.height = 0
    picture.depth = 0
    picture.colors = 0

    encoded_picture = base64.b64encode(picture.write()).decode("ascii")
    _set_comment(audio, "metadata_block_picture", [encoded_picture])

    # store lyrics in a text comment.
    if lyrics and lyrics.get("text"):
        lyrics_text = _synced_lyrics_as_lrc(lyrics)
        if lyrics_text:
            _set_comment(audio, "lyrics", lyrics_text)

    audio.save()


def save_lyrics(video_id: str, lyrics: dict | None) -> Path | None:
    """Save the original lyrics response beside the audio file."""
    if not lyrics or not lyrics.get("text"):
        return None

    lyric_format = lyrics.get("format", "text").lower()
    extension = {"text": "txt"}.get(lyric_format, lyric_format)
    lyrics_path = Path.cwd() / f"{video_id}.{extension}"
    lyrics_path.write_text(lyrics["text"], encoding="utf-8")
    return lyrics_path


def get_lrc_red(info: dict) -> dict | None:
    LYRICS_USER_AGENT = ("selfmade pipeline/1.0 (mailto:carry-grimy-jigsaw@duck.com)")
    title = info["title"]
    artists = info["artists"]
    artist_text = ", ".join(artists)

    try:
        response = requests.get(
            "https://lrc.red/api/v1",
            params={"q": f"{title} {artist_text}"},
            headers={"User-Agent": LYRICS_USER_AGENT},
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError):
        return None

    results = payload if isinstance(payload, list) else payload.get("results", [])

    for result in results:
        lyrics_url = result.get("lyricsUrl") or result.get("lyrics_url")
        timing_type = str(
            result.get("timing_type") or result.get("timingType") or ""
        ).lower()

        if not lyrics_url:
            continue

        # Require a synced result; otherwise let LRCLIB try.
        if timing_type not in {"line", "word", "synced"} and not lyrics_url.endswith(".ttml"):
            continue

        try:
            lyrics_response = requests.get(
                lyrics_url,
                headers={"User-Agent": LYRICS_USER_AGENT},
                timeout=20,
            )
            lyrics_response.raise_for_status()
        except requests.RequestException:
            continue

        return {
            "source": "lrc.red",
            "format": "ttml",
            "synced": True,
            "text": lyrics_response.text,
        }

    return None

def get_lrclib(info: dict) -> dict | None:
    LYRICS_USER_AGENT = ("selfmade pipeline/1.0 (mailto:carry-grimy-jigsaw@duck.com)")
    params = {
        "track_name": info["title"],
        "artist_name": ", ".join(info["artists"]),
    }

    if info.get("album"):
        params["album_name"] = info["album"]

    if info.get("duration_seconds") is not None:
        params["duration"] = info["duration_seconds"]

    try:
        response = requests.get(
            "https://lrclib.net/api/get",
            params=params,
            headers={"User-Agent": LYRICS_USER_AGENT},
            timeout=20,
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError):
        return None

    if data.get("syncedLyrics"):
        return {
            "source": "lrclib",
            "format": "lrc",
            "synced": True,
            "text": data["syncedLyrics"],
        }

    if data.get("plainLyrics"):
        return {
            "source": "lrclib",
            "format": "text",
            "synced": False,
            "text": data["plainLyrics"],
        }

    return None


def get_lyrics(info: dict) -> dict | None:
    lyrics = get_lrc_red(info)
    if lyrics:
        return lyrics

    return get_lrclib(info)

def _lrc_lines(text: str) -> list[tuple[str, int]]:
    """Return (lyric line, time in ms) pairs from LRC text."""
    result = []
    pattern = re.compile(r"\[(\d+):(\d{2})(?:[.:](\d{1,3}))?\](.*)")

    for line in text.splitlines():
        match = pattern.fullmatch(line.strip())
        if not match:
            continue

        minutes, seconds, fraction, lyric = match.groups()
        milliseconds = int((fraction or "0").ljust(3, "0"))
        timestamp = (int(minutes) * 60 + int(seconds)) * 1000 + milliseconds
        if lyric.strip():
            result.append((lyric.strip(), timestamp))

    return result


def _ttml_lines(text: str) -> list[tuple[str, int]]:
    """Read line-level timed <p> entries from TTML."""
    def to_ms(value: str) -> int | None:
        value = value.strip()
        try:
            if value.endswith("ms"):
                return int(float(value[:-2]))
            if value.endswith("s"):
                return round(float(value[:-1]) * 1000)

            parts = value.split(":")
            if len(parts) == 3:  # HH:MM:SS.mmm
                hours, minutes, seconds = parts
                return round(
                    (int(hours) * 3600 + int(minutes) * 60 + float(seconds))
                    * 1000
                )
        except ValueError:
            pass
        return None

    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []

    result = []
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] != "p":
            continue

        begin = element.attrib.get("begin")
        lyric = "".join(element.itertext()).strip()
        timestamp = to_ms(begin) if begin else None

        if lyric and timestamp is not None:
            result.append((lyric, timestamp))

    return result

def move_track_files(metadata: dict | None, path: str, video_id: str) -> list[Path]:
    """Move matching files from the current directory into the destination path."""
    source_dir = Path.cwd()
    target_root = Path("/libraries") / path.lstrip("/")

    files = [
        p for p in source_dir.iterdir()
        if p.is_file() and p.name.startswith(f"{video_id}.")
    ]
    if not files:
        return []

    if metadata is None:
        destinations = [(p, target_root / p.name) for p in files]
    else:
        def names(items):
            result = []
            for item in items or []:
                value = item.get("name") if isinstance(item, dict) else item
                if isinstance(value, str) and value.strip():
                    result.append(value.strip())
            return result


        def safe(value):
            value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(value))
            return value.strip(" .") or "unknown"

        def move_overwriting(source: Path, destination: Path) -> None:
            destination.parent.mkdir(parents=True, exist_ok=True)

            fd, temp_name = tempfile.mkstemp(dir=destination.parent)
            os.close(fd)
            temp = Path(temp_name)

            try:
                shutil.copy2(source, temp)
                os.replace(temp, destination)
                source.unlink()
            finally:
                temp.unlink(missing_ok=True)

        artists = names(metadata.get("artists"))
        artist = " & ".join(artists) or "unknown"

        album_artists = names(metadata.get("album_artists")) or artists
        album_artist = " & ".join(album_artists) or artist

        album = metadata.get("album") or "unknown"
        title = metadata.get("title") or video_id

        track_number = metadata.get("track_number")
        if track_number is not None:
            track = str(track_number).zfill(2)
            disc_number = metadata.get("disc_number")
            disc = (
                f"{disc_number}-"
                if disc_number is not None and str(disc_number).lower() != "none"
                else ""
            )
            prefix = f"{disc}{track} "
        else:
            prefix = ""

        folder = target_root / safe(album_artist) / safe(album)
        filename_base = safe(f"{prefix}{artist} - {title}")

        destinations = [
            (p, folder / f"{filename_base}{p.suffix}")
            for p in files
        ]

        for source, destination in destinations:
            move_overwriting(source, destination)

        return [destination for _, destination in destinations]

def cleanup_image(video_id: str) -> None:
    IMAGE_EXTENSIONS = {
        ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp",
        ".avif", ".tif", ".tiff", ".ico",
    }
    if not video_id:
        raise ValueError("URL has no 'v' query parameter")

    for path in Path.cwd().iterdir():
        if path.is_file() and path.stem == video_id and path.suffix.lower() in IMAGE_EXTENSIONS:
            path.unlink()

def cleanup_all(video_id: str) -> None:
    for path in Path.cwd().iterdir():
        if path.is_file() and path.stem == video_id:
            path.unlink()

if __name__ == "__main__":
    # ---------------
    # setup, cheking integrity
    # ---------------
    if len(sys.argv) != 2:
        print(f"Usage: python {sys.argv[0]} <YouTube Music URL>")
        sys.exit(1)

    url=sys.argv[1]
    username = os.environ.get("OT_USERNAME")
    if not username:
        sys.exit("Error: OliveTin did not provide OT_USERNAME.")

    library_path = get_navidrome_library_path(username)
    if not library_path:
        print("forgot to create your library, please talk to me...")
        sys.exit(1)
    video_id = parse_qs(urlparse(url).query).get("v", [None])[0]
    if not video_id:
        print("url invalid, use a full youtube or youtube music url, with the watch?v=xxxxxxxxxxx part")
        sys.exit(1)

    # ---------------
    # actual code that does stuff
    # ---------------
    # clean up old files (if existant)
    print("cleaning up old files (if existant)...")
    cleanup_all(video_id)
    # get tags
    print("getting song information...")
    info = get_information(video_id)
    print(info)
    # download the file
    print("downloading song...")
    print("------------------------------")
    download(video_id)
    # crop the cover
    print("------------------------------")
    print("cropping cover...")
    cover_path = center_square_crop(video_id)
    # get lyrics, best format possible
    print("downloading lyrics...")
    lyrics = get_lyrics(info)
    # save lyrics as sidecar file
    print("saving lyrics...")
    save_lyrics(video_id, lyrics)
    # embed tags, lyrics and cover
    print("embedding tags, lyrics, and cover...")
    embed_tags(
        track=info,
        cover_path=str(cover_path),
        lyrics=lyrics,
    )
    # clean up
    print("cleaning up files...")
    cleanup_image(video_id)
    # move files to users library
    print("moving files...")
    move_track_files(metadata=info, path=library_path, video_id=video_id)
    # done
    print("done, should show up in navidrome soon...")
