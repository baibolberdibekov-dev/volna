"""Isolated yt-dlp process. stdout carries only line-delimited JSON events."""
import argparse
import json
import math
import os
import re
import sys
import time
from pathlib import Path

import yt_dlp

from app.config import FFMPEG_LOCATION, MAX_DURATION, MAX_FILE_MB

VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


def emit(**event):
    print(json.dumps(event, ensure_ascii=True), flush=True)


class QuietLogger:
    def debug(self, message):
        pass

    def info(self, message):
        pass

    def warning(self, message):
        pass

    def error(self, message):
        # The exception is mapped to a user-facing error below.
        pass


def options():
    result = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "logger": QuietLogger(),
        "cachedir": False,
        "socket_timeout": 15,
        "retries": 2,
        "fragment_retries": 2,
        "extractor_retries": 1,
        "noplaylist": True,
        "js_runtimes": {"deno": {}, "node": {}},
    }
    if FFMPEG_LOCATION:
        result["ffmpeg_location"] = FFMPEG_LOCATION
    return result


def finite_duration(value):
    return isinstance(value, (float, int)) and math.isfinite(value) and value > 0


def public_track(entry):
    video_id = entry.get("id", "")
    duration = entry.get("duration")
    if not VIDEO_ID.fullmatch(video_id):
        return None
    if entry.get("live_status") in {"is_live", "is_upcoming", "post_live"} or entry.get("is_live"):
        return None
    if finite_duration(duration) and duration > MAX_DURATION:
        return None
    return {
        "id": video_id,
        "title": str(entry.get("title") or "Без названия")[:250],
        "artist": str(entry.get("channel") or entry.get("uploader") or "YouTube")[:150],
        "duration": round(duration) if finite_duration(duration) else None,
        "thumbnail": f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg",
        "url": f"https://www.youtube.com/watch?v={video_id}",
    }


def search(query):
    opts = options()
    opts.update({"extract_flat": "in_playlist", "skip_download": True})
    with yt_dlp.YoutubeDL(opts) as ydl:
        data = ydl.extract_info(f"ytsearch10:{query}", download=False)
    tracks = []
    seen = set()
    for entry in (data or {}).get("entries", []):
        track = public_track(entry or {})
        if track and track["id"] not in seen:
            seen.add(track["id"])
            tracks.append(track)
    emit(event="results", tracks=tracks)


def download(video_id, directory, quality):
    if not VIDEO_ID.fullmatch(video_id):
        raise ValueError("Некорректный идентификатор видео.")
    target = Path(directory).resolve()
    target.mkdir(parents=True, exist_ok=True)
    last_emit = 0.0

    def progress(info):
        nonlocal last_emit
        downloaded = info.get("downloaded_bytes") or 0
        if downloaded > MAX_FILE_MB * 1024 * 1024:
            raise ValueError("Исходный файл превышает лимит размера.")
        if info.get("status") == "finished":
            emit(event="progress", stage="converting", progress=95)
        elif time.monotonic() - last_emit >= 0.5:
            total = info.get("total_bytes") or info.get("total_bytes_estimate")
            percent = min(94, max(1, round(downloaded / total * 94))) if total else 1
            emit(event="progress", stage="downloading", progress=percent)
            last_emit = time.monotonic()

    opts = options()
    opts.update({
        "format": "bestaudio[ext=m4a]/bestaudio",
        "outtmpl": str(target / "audio.%(ext)s"),
        "max_filesize": MAX_FILE_MB * 1024 * 1024,
        "skip_unavailable_fragments": False,
        "concurrent_fragment_downloads": 1,
        "progress_hooks": [progress],
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": str(quality)},
            {"key": "FFmpegMetadata", "add_metadata": True},
        ],
        "postprocessor_args": {"ffmpeg": ["-threads", "1"]},
    })
    emit(event="progress", stage="checking", progress=0)
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
        if not info or info.get("_type", "video") != "video":
            raise ValueError("Не удалось получить отдельный трек.")
        if info.get("is_live") or info.get("live_status") in {"is_live", "is_upcoming", "post_live"}:
            raise ValueError("Прямые эфиры пока не поддерживаются.")
        duration = info.get("duration")
        if not finite_duration(duration):
            raise ValueError("YouTube не сообщил длительность этого трека. Выберите другой результат.")
        if duration > MAX_DURATION:
            raise ValueError(f"Выберите трек короче {MAX_DURATION // 60} минут.")
        track = public_track(info)
        if not track:
            raise ValueError("Этот трек недоступен.")
        emit(event="metadata", track=track)
        ydl.process_ie_result(info, download=True)
    output = target / "audio.mp3"
    if not output.is_file() or output.stat().st_size < 128:
        raise ValueError("Не удалось создать MP3. Возможно, исходный файл слишком большой.")
    if output.stat().st_size > MAX_FILE_MB * 1024 * 1024:
        raise ValueError("Готовый файл превышает лимит размера.")
    # Keep only the MP3; failed and expired job folders are removed by the server.
    for extra in target.iterdir():
        if extra.is_file() and extra.name != "audio.mp3":
            extra.unlink(missing_ok=True)
    emit(event="complete", size=output.stat().st_size)


def error_code(error):
    raw = str(error).lower()
    if isinstance(error, ValueError):
        return "INVALID_MEDIA"
    if any(x in raw for x in ["not a bot", "captcha", "unusual traffic"]):
        return "YOUTUBE_BOT_CHECK"
    if re.search(r"(?:http(?: error)?|status(?: code)?)\s*[:=]?\s*429\b", raw) or "too many requests" in raw:
        return "YOUTUBE_RATE_LIMIT"
    if any(x in raw for x in ["private video", "video unavailable", "not available", "copyright", "removed"]):
        return "VIDEO_UNAVAILABLE"
    if any(x in raw for x in ["sign in", "login required", "age-restricted", "confirm your age", "members-only"]):
        return "LOGIN_REQUIRED"
    if "ffmpeg" in raw or "ffprobe" in raw:
        return "AUDIO_PROCESSING"
    if any(x in raw for x in ["timed out", "timeout", "resolve", "connection", "network"]):
        return "NETWORK_ERROR"
    if "403" in raw and ("http" in raw or "forbidden" in raw):
        return "UPSTREAM_FORBIDDEN"
    return "EXTRACTION_FAILED"


def friendly_error(error):
    code = error_code(error)
    if code == "INVALID_MEDIA":
        return str(error)
    if code == "YOUTUBE_BOT_CHECK":
        return "YouTube требует проверку «не робот» для запросов с сервера. Владельцу сайта нужно проверить доступ сервера к YouTube."
    if code == "YOUTUBE_RATE_LIMIT":
        return "YouTube временно ограничил частоту запросов с сервера. Подождите перед следующей попыткой."
    if code == "VIDEO_UNAVAILABLE":
        return "Видео удалено или недоступно на сервере. Выберите другой результат."
    if code == "LOGIN_REQUIRED":
        return "Для этого видео YouTube требует вход в аккаунт. Выберите общедоступный результат."
    if code == "AUDIO_PROCESSING":
        return "Сервер не смог обработать аудио. Проверьте установку FFmpeg и ffprobe."
    if code == "NETWORK_ERROR":
        return "Нет ответа от YouTube. Попробуйте ещё раз чуть позже."
    if code == "UPSTREAM_FORBIDDEN":
        return "YouTube отказал серверу в доступе к аудио (HTTP 403). Владельцу сайта нужно проверить логи загрузки."
    return "Не удалось получить аудио с YouTube. Попробуйте другой результат или обновите yt-dlp на сервере."


def diagnostic_detail(error):
    """Preserve the provider's error without signed URLs or configured secrets."""
    message = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", str(error))
    for name in ("APP_KEY", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "YTDLP_PROXY"):
        secret = os.getenv(name)
        if secret:
            message = message.replace(secret, "[redacted]")
    message = re.sub(r"(?im)\b(?:authorization|proxy-authorization|cookie|set-cookie)\s*:[^\r\n]*", "[redacted header]", message)
    message = re.sub(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s<>]+", "[url]", message)
    message = re.sub(r"(?i)\b(token|password|passwd|secret|api[_-]?key)\s*[:=]\s*[^\s,;]+", r"\1=[redacted]", message)
    return " ".join(message.split())[:1200]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["search", "download"])
    parser.add_argument("value")
    parser.add_argument("--directory")
    parser.add_argument("--quality", type=int, choices=[128, 192, 320], default=192)
    args = parser.parse_args()
    try:
        if args.mode == "search":
            search(args.value)
        else:
            if not args.directory:
                raise ValueError("Не указана папка для файла.")
            download(args.value, args.directory, args.quality)
    except Exception as error:
        emit(event="error", message=friendly_error(error), code=error_code(error),
             detail=diagnostic_detail(error), yt_dlp_version=yt_dlp.version.__version__)
        sys.exit(1)


if __name__ == "__main__":
    main()
