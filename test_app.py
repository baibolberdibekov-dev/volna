import asyncio
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config, main, worker

TRACK = {"id": "abcdefghijk", "title": "Песня / тест", "artist": "Автор", "duration": 10,
         "thumbnail": "https://i.ytimg.com/vi/abcdefghijk/mqdefault.jpg",
         "url": "https://www.youtube.com/watch?v=abcdefghijk"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "APP_KEY", "")
    with TestClient(main.app) as instance:
        yield instance


def wait_for_job(client, job_id):
    for _ in range(100):
        data = client.get(f"/api/downloads/{job_id}").json()
        if data["status"] in {"ready", "error"}:
            return data
        time.sleep(0.01)
    pytest.fail("Job did not complete")


def test_home_and_config(client):
    response = client.get("/")
    assert response.status_code == 200
    assert 'lang="ru"' in response.text
    assert "Content-Security-Policy" in response.headers
    assert client.get("/api/health").json() == {"status": "ok"}
    assert client.get("/api/config").json()["requires_key"] is False
    assert client.get("/../app/main.py").status_code == 404


@pytest.mark.parametrize("query", ["", " ", "a", "x" * 161])
def test_bad_queries(client, query):
    assert client.post("/api/search", json={"query": query}).status_code == 422


@pytest.mark.parametrize("video_id", ["https://localhost/secret", "../secret", "abcdefghij;", "x" * 12])
def test_only_youtube_ids(client, video_id):
    assert client.post("/api/downloads", json={"video_id": video_id}).status_code == 422


def test_quality_validation(client):
    assert client.post("/api/downloads", json={"video_id": TRACK["id"], "quality": 999}).status_code == 422


def test_large_body(client):
    assert client.post("/api/search", content=b"x" * 4097).status_code == 413


def test_key_required(client, monkeypatch):
    monkeypatch.setattr(config, "APP_KEY", "local-test-key")
    assert client.get("/api/config").json()["requires_key"] is True
    assert client.post("/api/search", json={"query": "test"}).status_code == 401
    assert client.get("/api/downloads/missing").status_code == 401
    assert client.get("/api/downloads/missing", headers={"X-App-Key": "local-test-key"}).status_code == 404


def test_search_and_cache(client, monkeypatch):
    calls = []

    async def fake(args, callback, timeout):
        calls.append(args)
        callback({"event": "results", "tracks": [TRACK]})

    monkeypatch.setattr(main, "run_worker", fake)
    assert client.post("/api/search", json={"query": "  test   song "}).json()["tracks"] == [TRACK]
    assert client.post("/api/search", json={"query": "TEST song"}).status_code == 200
    assert calls == [["search", "--", "test song"]]


def test_search_timeout_and_rate(client, monkeypatch):
    async def fake(*args):
        raise TimeoutError()

    monkeypatch.setattr(main, "run_worker", fake)
    for _ in range(12):
        assert client.post("/api/search", json={"query": "test"}).status_code == 504
    assert main.app.state.runtime.searches == 0
    assert client.post("/api/search", json={"query": "test"}).status_code == 429


def test_pipeline_file_token_range_and_deduplication(client, monkeypatch):
    payload = b"ID3" + b"a" * 1024

    async def fake(args, callback, timeout):
        callback({"event": "metadata", "track": TRACK})
        callback({"event": "progress", "stage": "converting", "progress": 95})
        directory = Path(args[args.index("--directory") + 1])
        (directory / "audio.mp3").write_bytes(payload)
        callback({"event": "complete", "size": len(payload)})

    monkeypatch.setattr(main, "run_worker", fake)
    response = client.post("/api/downloads", json={"video_id": TRACK["id"], "quality": 192})
    assert response.status_code == 202
    job_id = response.json()["id"]
    job = wait_for_job(client, job_id)
    assert job["status"] == "ready" and job["progress"] == 100
    assert client.get(f"/api/files/{job_id}").status_code == 404
    assert client.get(f"/api/files/{job_id}?token=wrong").status_code == 404
    assert client.get(f"/api/files/{job_id}", params={"token": "неверный"}).status_code == 404
    download = client.get(job["download_url"])
    assert download.content == payload
    assert download.headers["content-type"] == "audio/mpeg"
    assert "attachment" in download.headers["content-disposition"]
    preview = client.get(job["download_url"] + "&preview=true", headers={"Range": "bytes=0-2"})
    assert preview.status_code == 206 and preview.content == b"ID3"
    assert "inline" in preview.headers["content-disposition"]
    assert main.app.state.runtime.jobs[job_id].streams == 0
    duplicate = client.post("/api/downloads", json={"video_id": TRACK["id"], "quality": 192})
    assert duplicate.json()["id"] == job_id


def test_error_removes_partial_files(client, monkeypatch):
    async def fake(args, callback, timeout):
        directory = Path(args[args.index("--directory") + 1])
        (directory / "partial.webm").write_bytes(b"partial")
        callback({"event": "error", "message": "Источник недоступен"})
        raise RuntimeError()

    monkeypatch.setattr(main, "run_worker", fake)
    job_id = client.post("/api/downloads", json={"video_id": TRACK["id"]}).json()["id"]
    job = wait_for_job(client, job_id)
    assert job["status"] == "error" and job["error"] == "Источник недоступен"
    assert not (config.DATA_DIR / job_id).exists()
    assert job["download_url"] is None


def test_timeout_removes_job_files(client, monkeypatch):
    async def fake(*args):
        raise TimeoutError()

    monkeypatch.setattr(main, "run_worker", fake)
    job_id = client.post("/api/downloads", json={"video_id": TRACK["id"]}).json()["id"]
    assert wait_for_job(client, job_id)["status"] == "error"
    assert not (config.DATA_DIR / job_id).exists()


def test_cache_reservation_limit(client, monkeypatch):
    monkeypatch.setattr(config, "MAX_CACHE_MB", 1)
    assert client.post("/api/downloads", json={"video_id": TRACK["id"]}).status_code == 503


def test_queue_limit(client, monkeypatch):
    monkeypatch.setattr(config, "MAX_JOBS", 1)
    state = main.app.state.runtime
    state.jobs["a" * 32] = main.Job(id="a" * 32, video_id="zyxwvutsrqp", quality=192)
    assert client.post("/api/downloads", json={"video_id": TRACK["id"]}).status_code == 503


def test_expiry_preserves_active_stream(client):
    state = main.app.state.runtime
    job = main.Job(id="f" * 32, video_id=TRACK["id"], quality=192, status="ready",
                   finished=time.monotonic() - config.FILE_TTL - 1, streams=1)
    state.jobs[job.id] = job
    job.directory.mkdir()
    (job.directory / "audio.mp3").write_bytes(b"test")
    main.cleanup(state)
    assert job.directory.exists()
    job.streams = 0
    main.cleanup(state)
    assert not job.directory.exists() and job.id not in state.jobs


def test_cleanup_path_guard(client, tmp_path):
    with pytest.raises(ValueError):
        main.remove_directory(tmp_path.parent)


@pytest.mark.parametrize("entry", [
    {"id": "invalid"}, {"id": TRACK["id"], "duration": 99999},
    {"id": TRACK["id"], "is_live": True}, {"id": TRACK["id"], "live_status": "is_upcoming"},
])
def test_worker_filters(entry):
    assert worker.public_track(entry) is None


@pytest.mark.parametrize("metadata", [
    {"duration": 99999}, {"duration": None}, {"duration": 20, "is_live": True},
])
def test_worker_rejects_before_download(monkeypatch, tmp_path, metadata):
    class Source:
        def __init__(self, options):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def extract_info(self, url, download):
            return {"id": TRACK["id"], **metadata}

        def process_ie_result(self, *args, **kwargs):
            pytest.fail("Invalid video must not be downloaded")

    monkeypatch.setattr(worker.yt_dlp, "YoutubeDL", Source)
    with pytest.raises(ValueError):
        worker.download(TRACK["id"], str(tmp_path), 192)


def test_worker_options_compatible():
    with worker.yt_dlp.YoutubeDL(worker.options()) as ydl:
        assert ydl.params["noplaylist"]


def test_real_subprocess_protocol(tmp_path):
    # No external network: invalid input exercises the actual subprocess protocol.
    events = []
    with pytest.raises(RuntimeError):
        asyncio.run(main.run_worker(["download", "invalid", "--directory", str(tmp_path)], events.append, 30))
    assert events[-1]["event"] == "error"


def test_real_subprocess_timeout(tmp_path):
    with pytest.raises(TimeoutError):
        asyncio.run(main.run_worker(
            ["download", "invalid", "--directory", str(tmp_path)], lambda event: None, 0.001,
        ))
