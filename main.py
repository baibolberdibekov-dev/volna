import asyncio
import contextlib
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from starlette.background import BackgroundTask

from app import config

log = logging.getLogger("volna")
ACTIVE = {"queued", "checking", "downloading", "converting"}
JOB_ID = re.compile(r"^[a-f0-9]{32}$")


class SearchBody(BaseModel):
    query: str = Field(min_length=2, max_length=160)

    @field_validator("query")
    @classmethod
    def clean_query(cls, value):
        value = " ".join(value.split())
        if len(value) < 2 or any(ord(c) < 32 for c in value):
            raise ValueError("Введите хотя бы два символа")
        return value


class DownloadBody(BaseModel):
    video_id: str = Field(pattern=r"^[A-Za-z0-9_-]{11}$")
    quality: Literal[128, 192, 320] = 192


@dataclass
class Job:
    id: str
    video_id: str
    quality: int
    token: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    status: str = "queued"
    progress: int = 0
    track: dict | None = None
    error: str | None = None
    size: int = 0
    finished: float | None = None
    streams: int = 0

    @property
    def directory(self):
        return config.DATA_DIR / self.id

    def public(self):
        return {
            "id": self.id, "status": self.status, "progress": self.progress,
            "track": self.track, "error": self.error, "size": self.size,
            "quality": self.quality,
            "download_url": f"/api/files/{self.id}?token={self.token}" if self.status == "ready" else None,
        }


class State:
    def __init__(self):
        self.jobs: dict[str, Job] = {}
        self.tasks: set[asyncio.Task] = set()
        self.searches = 0
        self.cache = {}
        self.rates = defaultdict(deque)
        self.download_slots = asyncio.Semaphore(config.DOWNLOAD_WORKERS)

    def rate(self, request, action, limit):
        address = request.client.host if request.client else "unknown"
        now = time.monotonic()
        key = (address, action)
        hits = self.rates[key]
        while hits and hits[0] < now - 60:
            hits.popleft()
        if len(hits) >= limit:
            raise HTTPException(429, "Слишком много запросов. Подождите минуту.", headers={"Retry-After": "60"})
        hits.append(now)


async def stop_process(process):
    if process.returncode is not None:
        return
    if os.name == "nt":
        killer = await asyncio.create_subprocess_exec(
            "taskkill", "/PID", str(process.pid), "/T", "/F",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        await killer.wait()
    else:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
    await process.wait()


async def run_worker(arguments, callback, timeout):
    process_kwargs = {"start_new_session": True} if os.name != "nt" else {
        "creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
    }
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "app.worker", *arguments,
        cwd=config.ROOT, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        limit=1024 * 1024, **process_kwargs,
    )
    try:
        async with asyncio.timeout(timeout):
            while line := await process.stdout.readline():
                event = json.loads(line)
                callback(event)
            code = await process.wait()
            if code:
                raise RuntimeError("Источник не смог обработать запрос.")
    except BaseException:
        await stop_process(process)
        raise


def remove_directory(path: Path):
    # DATA_DIR is reserved for this application; only generated job directories are removed.
    if path.parent.resolve() != config.DATA_DIR or not JOB_ID.fullmatch(path.name):
        raise ValueError("Unsafe cleanup path")
    shutil.rmtree(path, ignore_errors=True)


def cleanup(state):
    now = time.monotonic()
    for job_id, job in list(state.jobs.items()):
        if job.finished is not None and now - job.finished > config.FILE_TTL and not job.streams:
            remove_directory(job.directory)
            state.jobs.pop(job_id, None)
    for key, (expires, _) in list(state.cache.items()):
        if expires < now:
            state.cache.pop(key, None)
    for key, hits in list(state.rates.items()):
        if not hits or hits[-1] < now - 60:
            state.rates.pop(key, None)


async def janitor(state):
    while True:
        await asyncio.sleep(30)
        cleanup(state)


@asynccontextmanager
async def lifespan(app):
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    # In-memory jobs do not survive restarts; remove their abandoned files.
    for path in config.DATA_DIR.iterdir():
        if path.is_dir() and JOB_ID.fullmatch(path.name):
            remove_directory(path)
    state = State()
    app.state.runtime = state
    sweeper = asyncio.create_task(janitor(state))
    yield
    sweeper.cancel()
    for task in list(state.tasks):
        task.cancel()
    await asyncio.gather(sweeper, *state.tasks, return_exceptions=True)


app = FastAPI(title="VOLNA", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def security_headers(request, call_next):
    # Read at most 4 KiB for POST bodies, including chunked requests.
    if request.method == "POST":
        content = bytearray()
        async for chunk in request.stream():
            content.extend(chunk)
            if len(content) > 4096:
                return JSONResponse({"detail": "Слишком большой запрос."}, status_code=413)
        request._body = bytes(content)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' https://i.ytimg.com data:; media-src 'self'; "
        "connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    )
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


async def authorize(request: Request):
    if config.APP_KEY and not hmac.compare_digest(
        request.headers.get("X-App-Key", "").encode(), config.APP_KEY.encode()
    ):
        raise HTTPException(401, "Введите ключ доступа к этому сайту.")


@app.get("/api/health")
async def health():
    return {"status": "ok"}


@app.get("/api/config")
async def settings():
    return {"requires_key": bool(config.APP_KEY), "max_duration_minutes": config.MAX_DURATION // 60,
            "file_ttl_minutes": config.FILE_TTL // 60}


@app.post("/api/search", dependencies=[Depends(authorize)])
async def search(body: SearchBody, request: Request):
    state = request.app.state.runtime
    state.rate(request, "search", 12)
    key = body.query.casefold()
    cached = state.cache.get(key)
    if cached and cached[0] > time.monotonic():
        return {"tracks": cached[1]}
    if state.searches >= 2:
        raise HTTPException(503, "Сейчас поиск занят. Повторите через несколько секунд.")
    state.searches += 1
    result = None
    message = None

    def receive(event):
        nonlocal result, message
        if event.get("event") == "results":
            result = event["tracks"]
        elif event.get("event") == "error":
            message = event["message"]

    try:
        await run_worker(["search", "--", body.query], receive, config.SEARCH_TIMEOUT)
        if result is None:
            raise RuntimeError("No results event")
        if len(state.cache) >= 100:
            state.cache.pop(next(iter(state.cache)))
        state.cache[key] = (time.monotonic() + 180, result)
        return {"tracks": result}
    except TimeoutError:
        raise HTTPException(504, "Поиск занял слишком много времени. Попробуйте ещё раз.")
    except Exception:
        raise HTTPException(502, message or "Не удалось выполнить поиск. Попробуйте позже.")
    finally:
        state.searches -= 1


async def prepare(job, state):
    try:
        async with state.download_slots:
            job.directory.mkdir(parents=True, exist_ok=True)
            job.status = "checking"

            def receive(event):
                kind = event.get("event")
                if kind == "progress":
                    job.status = event["stage"]
                    job.progress = event["progress"]
                elif kind == "metadata":
                    job.track = event["track"]
                elif kind == "complete":
                    job.size = event["size"]
                elif kind == "error":
                    job.error = event["message"]

            await run_worker(
                ["download", "--directory", str(job.directory), "--quality", str(job.quality), "--", job.video_id],
                receive, config.JOB_TIMEOUT,
            )
            if not (job.directory / "audio.mp3").is_file() or not job.track or job.size <= 0:
                raise RuntimeError("Missing output")
            job.status = "ready"
            job.progress = 100
    except asyncio.CancelledError:
        job.status = "error"
        job.error = "Сервер перезапущен. Подготовьте файл ещё раз."
        raise
    except TimeoutError:
        job.status = "error"
        job.error = "Подготовка заняла слишком много времени. Попробуйте другой трек."
    except Exception:
        job.status = "error"
        job.error = job.error or "Не удалось подготовить MP3. Попробуйте другой результат."
    finally:
        job.finished = time.monotonic()
        if job.status != "ready":
            remove_directory(job.directory)


@app.post("/api/downloads", status_code=202, dependencies=[Depends(authorize)])
async def create_download(body: DownloadBody, request: Request):
    state = request.app.state.runtime
    state.rate(request, "download", 6)
    cleanup(state)
    for job in state.jobs.values():
        if job.video_id == body.video_id and job.quality == body.quality and job.status != "error":
            return job.public()
    active_count = sum(job.status in ACTIVE for job in state.jobs.values())
    if active_count >= config.MAX_JOBS:
        raise HTTPException(503, "Очередь заполнена. Попробуйте через несколько минут.")
    used = sum(job.size for job in state.jobs.values() if job.status == "ready")
    # Reserve room for source + converted files for every admitted job.
    reserved = (active_count + 1) * config.MAX_FILE_MB * 2 * 1024 * 1024
    if used + reserved > config.MAX_CACHE_MB * 1024 * 1024 or len(state.jobs) >= 200:
        raise HTTPException(503, "Временное хранилище заполнено. Попробуйте позже.")
    job = Job(id=secrets.token_hex(16), video_id=body.video_id, quality=body.quality)
    state.jobs[job.id] = job
    task = asyncio.create_task(prepare(job, state))
    state.tasks.add(task)
    task.add_done_callback(state.tasks.discard)
    return job.public()


@app.get("/api/downloads/{job_id}", dependencies=[Depends(authorize)])
async def get_download(job_id: str, request: Request):
    state = request.app.state.runtime
    cleanup(state)
    job = state.jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Срок хранения файла истёк или сервер перезапустился. Подготовьте MP3 снова.")
    return job.public()


@app.get("/api/files/{job_id}")
async def file(job_id: str, request: Request, token: str = "", preview: bool = False):
    state = request.app.state.runtime
    cleanup(state)
    job = state.jobs.get(job_id)
    if not job or not hmac.compare_digest(token.encode(), job.token.encode()):
        raise HTTPException(404, "Файл не найден.")
    path = job.directory / "audio.mp3"
    if job.status != "ready" or not path.is_file():
        raise HTTPException(409, "MP3 ещё не готов или уже удалён.")
    title = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "", job.track["title"]).strip(". ")[:150] or "track"
    job.streams += 1

    async def release():
        job.streams = max(0, job.streams - 1)

    return FileResponse(path, media_type="audio/mpeg", filename=f"{title}.mp3",
                        content_disposition_type="inline" if preview else "attachment",
                        background=BackgroundTask(release))


app.mount("/", StaticFiles(directory=config.ROOT / "static", html=True), name="static")
