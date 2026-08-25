from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import time
import uuid

from aiohttp import web

from .catalog import CatalogStore
from .char_dictionary import CharacterDictionary
from .paths import (
    CATALOG_DIR,
    HISTORY_DIR,
    PARENT_DIR,
    PROJECT_ROOT,
    SOURCES_DIR,
    TEMP_DIR,
    TTS_CACHE_DIR,
    ensure_runtime_dirs,
)
from .worker_client import DictationWorkerClient, WorkerError


IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
OCR_MODES = {"zh", "zh_char", "zh_word", "en_vocab"}
OCR_TIMEOUT_SECONDS = max(60, int(os.environ.get("DICTATION_OCR_TIMEOUT", "180")))
SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9._-]+$")


def _safe_component(value: str) -> str | None:
    value = str(value or "")
    return value if value and SAFE_COMPONENT.fullmatch(value) and Path(value).name == value else None


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _read_json(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _prune_files(directory: Path, *, max_files: int, max_bytes: int,
                 max_age_days: int | None = None) -> None:
    files = [path for path in directory.iterdir() if path.is_file()]
    files.sort(key=lambda path: path.stat().st_mtime)
    cutoff = time.time() - max_age_days * 86400 if max_age_days else None
    total = sum(path.stat().st_size for path in files)
    while files and (len(files) > max_files or total > max_bytes
                     or (cutoff is not None and files[0].stat().st_mtime < cutoff)):
        oldest = files.pop(0)
        try:
            size = oldest.stat().st_size
            oldest.unlink()
            total -= size
        except OSError:
            continue


async def _multipart_to_temp(request: web.Request, *, max_bytes: int,
                             file_fields: set[str]) -> tuple[dict, dict[str, list[dict]]]:
    reader = await request.multipart()
    fields: dict[str, str] = {}
    files: dict[str, list[dict]] = {}
    total = 0
    while True:
        part = await reader.next()
        if part is None:
            break
        if part.name not in file_fields or not part.filename:
            value = await part.text()
            total += len(value.encode("utf-8"))
            if total > max_bytes:
                raise web.HTTPRequestEntityTooLarge(max_size=max_bytes, actual_size=total)
            fields[part.name] = value
            continue
        suffix = Path(part.filename).suffix.lower()
        if suffix not in IMAGE_SUFFIXES:
            suffix = ".jpg"
        fd, temp_name = tempfile.mkstemp(prefix="upload-", suffix=suffix, dir=TEMP_DIR)
        size = 0
        try:
            with os.fdopen(fd, "wb") as handle:
                while True:
                    chunk = await part.read_chunk(size=256 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    total += len(chunk)
                    if total > max_bytes:
                        raise web.HTTPRequestEntityTooLarge(max_size=max_bytes, actual_size=total)
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            Path(temp_name).unlink(missing_ok=True)
            raise
        files.setdefault(part.name, []).append({
            "path": Path(temp_name), "filename": part.filename, "suffix": suffix, "size": size,
        })
    return fields, files


class OcrJobs:
    def __init__(self, worker: DictationWorkerClient, max_history: int = 20) -> None:
        self.worker = worker
        self.max_history = max(4, max_history)
        self.jobs: dict[str, dict] = {}
        self.tasks: set[asyncio.Task] = set()
        self._lock = asyncio.Lock()
        self._slot = asyncio.Semaphore(1)
        self._pending = 0

    async def submit(self, image_path: Path, mode: str, crop: dict | None,
                     *, cleanup_image: bool = True) -> dict:
        async with self._lock:
            if self._pending >= 2:
                raise web.HTTPTooManyRequests(text=json.dumps({
                    "error": "OCR 正在处理其他照片，请稍后重试。"
                }, ensure_ascii=False), content_type="application/json")
            self._pending += 1
            job_id = uuid.uuid4().hex
            job = {
                "jobId": job_id, "status": "queued", "phase": "queued", "progress": 5,
                "message": "照片已收到，正在等待 OCR 处理", "createdAt": time.time(), "crop": crop,
            }
            self.jobs[job_id] = job
            self._trim_locked()
        task = asyncio.create_task(
            self._run(job_id, image_path, mode, crop, cleanup_image)
        )
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return deepcopy(job)

    def get(self, job_id: str) -> dict | None:
        job = self.jobs.get(job_id)
        return deepcopy(job) if job else None

    def _progress(self, job_id: str, message: dict) -> None:
        job = self.jobs.get(job_id)
        if job:
            job.update(status="running", phase=message.get("phase", "running"),
                       progress=message.get("progress", 20), message=message.get("message", "正在识别"),
                       details=message.get("details") or {})

    async def _run(self, job_id: str, image_path: Path, mode: str, crop: dict | None,
                   cleanup_image: bool) -> None:
        try:
            async with self._slot:
                self.jobs[job_id].update(status="running", phase="preparing", progress=8,
                                         message="正在准备照片")
                result = await self.worker.request(
                    "ocr", {"imagePath": str(image_path), "mode": mode, "crop": crop},
                    timeout=OCR_TIMEOUT_SECONDS,
                    progress=lambda item: self._progress(job_id, item),
                )
                self.jobs[job_id].update(status="completed", phase="completed", progress=100,
                                         message="识别完成", result=result)
        except asyncio.CancelledError:
            self.jobs.get(job_id, {}).update(status="cancelled", phase="cancelled", progress=100,
                                             message="识别已取消")
            raise
        except Exception as exc:
            self.jobs.get(job_id, {}).update(status="failed", phase="failed", progress=100,
                                             message=f"OCR 暂时不可用：{exc}")
        finally:
            if cleanup_image:
                image_path.unlink(missing_ok=True)
            async with self._lock:
                self._pending = max(0, self._pending - 1)

    def _trim_locked(self) -> None:
        if len(self.jobs) <= self.max_history:
            return
        finished = sorted(
            (item for item in self.jobs.values()
             if item.get("status") in {"completed", "failed", "cancelled"}),
            key=lambda item: item.get("createdAt", 0),
        )
        for item in finished[:max(0, len(self.jobs) - self.max_history)]:
            self.jobs.pop(item["jobId"], None)

    async def close(self) -> None:
        for task in list(self.tasks):
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)


class DictationService:
    def __init__(self, bridge=None) -> None:
        ensure_runtime_dirs()
        _prune_files(HISTORY_DIR, max_files=200, max_bytes=500 * 1024 * 1024)
        _prune_files(TTS_CACHE_DIR, max_files=2000, max_bytes=256 * 1024 * 1024,
                     max_age_days=30)
        self.bridge = bridge
        self.worker = DictationWorkerClient(idle_seconds=300, max_jobs=50)
        self.ocr_jobs = OcrJobs(self.worker)
        self.catalog = CatalogStore(CATALOG_DIR)
        self.dictionary = CharacterDictionary(
            CATALOG_DIR / "chinese_textbook.json", SOURCES_DIR / "jieba_dict.txt"
        )
        self._active_tts: dict[str, bool] = {}
        self._tts_lock = asyncio.Lock()
        self._tts_slots = asyncio.Semaphore(2)

    def setup_routes(self, app: web.Application) -> None:
        routes = [
            web.get("/parent", self.parent_page),
            web.get("/api/system/health", self.system_health),
            web.get("/api/dictation/health", self.health),
            web.get("/api/health", self.health),
            web.get("/api/history", self.list_history),
            web.post("/api/history", self.save_history),
            web.get("/api/history/{filename}", self.history_asset),
            web.delete("/api/history/{filename}", self.delete_history),
            web.post("/api/ocr/jobs", self.create_ocr_job),
            web.get("/api/ocr/jobs/{job_id}", self.get_ocr_job),
            web.post("/api/ocr/warmup", self.warmup_ocr),
            web.post("/api/ocr/visualize", self.visualize_disabled),
            web.post("/api/tts/synthesize", self.synthesize_tts),
            web.delete("/api/tts/requests/{request_id}", self.cancel_tts),
            web.get("/api/tts/audio/{filename}", self.tts_audio),
            web.get("/api/catalog", self.catalog_root),
            web.get("/api/catalog/{subject}/volumes", self.catalog_volumes),
            web.get("/api/catalog/{subject}/volumes/{volume_id}/units", self.catalog_units),
            web.get("/api/catalog/{subject}/volumes/{volume_id}/units/{unit_id}/lessons", self.catalog_lessons),
            web.get("/api/catalog/{subject}/volumes/{volume_id}/units/{unit_id}/entries", self.catalog_unit_entries),
            web.get("/api/catalog/{subject}/volumes/{volume_id}/units/{unit_id}/lessons/{lesson_id}/entries", self.catalog_entries),
            web.post("/api/dictionary/examples", self.dictionary_examples),
            web.get("/api/parent/records", self.list_parent_records),
            web.post("/api/parent/records", self.create_parent_record),
            web.get("/api/parent/records/{record_id}", self.get_parent_record),
            web.get("/api/parent/records/{record_id}/photos/{filename}", self.parent_photo),
            web.delete("/api/parent/records/{record_id}/photos/{filename}", self.delete_parent_photo),
        ]
        app.add_routes(routes)
        app.router.add_static("/static/", str(PROJECT_ROOT / "static"), show_index=False)

    async def parent_page(self, _request):
        return web.FileResponse(PROJECT_ROOT / "static" / "parent.html")

    async def system_health(self, _request):
        return web.json_response({"ok": True, "service": "ai-dictation-lamp", "port": 8765})

    async def health(self, _request):
        bridge = self.bridge
        return web.json_response({
            "ok": True,
            "workerRunning": self.worker.running,
            "ocrQueued": self.ocr_jobs._pending,
            "mode": getattr(bridge, "_active_mode", "standalone"),
            "audioUplinkQueued": (
                bridge._audio_send_queue.qsize()
                if bridge is not None and hasattr(bridge, "_audio_send_queue") else 0
            ),
            "audioUplinkDropped": getattr(bridge, "_audio_drop_count", 0),
        })

    async def list_history(self, _request):
        files = [p for p in HISTORY_DIR.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES]
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return web.json_response({"items": [{
            "id": p.name, "name": p.name, "url": f"/api/history/{p.name}",
            "createdAt": datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).isoformat(),
        } for p in files[:20]]})

    async def save_history(self, request):
        fields, files = await _multipart_to_temp(request, max_bytes=12 * 1024 * 1024,
                                                 file_fields={"image"})
        del fields
        upload = (files.get("image") or [None])[0]
        if not upload:
            raise web.HTTPBadRequest(text=json.dumps({"error": "缺少图片文件。"}, ensure_ascii=False),
                                     content_type="application/json")
        name = datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:8] + upload["suffix"]
        target = HISTORY_DIR / name
        os.replace(upload["path"], target)
        _prune_files(HISTORY_DIR, max_files=200, max_bytes=500 * 1024 * 1024)
        return web.json_response({"id": name, "name": name, "url": f"/api/history/{name}"}, status=201)

    async def history_asset(self, request):
        name = _safe_component(request.match_info["filename"])
        target = HISTORY_DIR / name if name else None
        if not target or not target.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(target)

    async def delete_history(self, request):
        name = _safe_component(request.match_info["filename"])
        target = HISTORY_DIR / name if name else None
        if not target or not target.is_file():
            return web.json_response({"error": "历史图片不存在。"}, status=404)
        target.unlink()
        return web.json_response({"ok": True, "id": name})

    @staticmethod
    def _parse_crop(text: str) -> dict | None:
        if not text:
            return None
        try:
            source = json.loads(text)
            crop = {key: float(source[key]) for key in ("x", "y", "w", "h")}
            if min(crop.values()) < 0 or crop["w"] <= 0 or crop["h"] <= 0:
                raise ValueError
            return crop
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            raise web.HTTPBadRequest(text=json.dumps({"error": "识别区域参数无效。"}, ensure_ascii=False),
                                     content_type="application/json") from exc

    async def create_ocr_job(self, request):
        if request.content_type.startswith("multipart/"):
            fields, files = await _multipart_to_temp(
                request, max_bytes=12 * 1024 * 1024, file_fields={"image"}
            )
        else:
            posted = await request.post()
            fields, files = {key: str(value) for key, value in posted.items()}, {}
        upload = (files.get("image") or [None])[0]
        asset_id = _safe_component(fields.get("assetId", ""))
        asset_path = HISTORY_DIR / asset_id if asset_id else None
        mode = fields.get("mode", "").strip()
        if not upload and (not asset_path or not asset_path.is_file()):
            return web.json_response({"error": "缺少图片文件。"}, status=400)
        if mode not in OCR_MODES:
            if upload:
                upload["path"].unlink(missing_ok=True)
            return web.json_response({"error": "不支持的识别模式。"}, status=400)
        try:
            image_path = upload["path"] if upload else asset_path
            result = await self.ocr_jobs.submit(
                image_path, mode, self._parse_crop(fields.get("crop", "")),
                cleanup_image=bool(upload),
            )
            return web.json_response(result, status=202)
        except Exception:
            if upload:
                upload["path"].unlink(missing_ok=True)
            raise

    async def get_ocr_job(self, request):
        job = self.ocr_jobs.get(request.match_info["job_id"])
        return web.json_response(job if job else {"error": "识别任务不存在或已经过期。"},
                                 status=200 if job else 404)

    async def warmup_ocr(self, _request):
        try:
            return web.json_response(await self.worker.request("warmup", timeout=20))
        except Exception as exc:
            return web.json_response({"ready": False, "reason": f"OCR 暂时不可用：{exc}"}, status=503)

    async def visualize_disabled(self, request):
        # 生产模式不接收第二份完整图片；浏览器端调试上传也会被关闭。
        await request.release()
        return web.json_response({"ok": True, "saved": False}, status=202)

    async def synthesize_tts(self, request):
        data = await request.json()
        request_id = str(data.get("request_id") or uuid.uuid4().hex)
        if len(request_id) > 64 or not request_id.replace("-", "").replace("_", "").isalnum():
            return web.json_response({"status": "error", "reason": "invalid request_id"}, status=400)
        async with self._tts_lock:
            if request_id in self._active_tts:
                return web.json_response({"status": "error", "reason": "request_id already active"}, status=409)
            if len(self._active_tts) >= 2:
                return web.json_response({"status": "unavailable", "reason": "TTS queue is full",
                                          "fallback": "speechSynthesis"}, status=429)
            self._active_tts[request_id] = False
        try:
            async with self._tts_slots:
                if self._active_tts.get(request_id):
                    return web.json_response({"request_id": request_id, "status": "cancelled"}, status=409)
                result = await self.worker.request("tts", {"payload": data}, timeout=30)
            status = result.get("status", "error")
            payload = {"request_id": request_id, **result,
                       "fallback": "speechSynthesis" if status != "ready" else None}
            if result.get("filename"):
                payload["audio_url"] = f"/api/tts/audio/{result['filename']}"
            _prune_files(TTS_CACHE_DIR, max_files=2000, max_bytes=256 * 1024 * 1024,
                         max_age_days=30)
            return web.json_response(payload, status={"ready": 200, "cancelled": 409,
                                      "unavailable": 503, "error": 502}.get(status, 500))
        except (WorkerError, asyncio.CancelledError) as exc:
            return web.json_response({"request_id": request_id, "status": "unavailable",
                                      "reason": str(exc), "fallback": "speechSynthesis"}, status=503)
        finally:
            async with self._tts_lock:
                self._active_tts.pop(request_id, None)

    async def cancel_tts(self, request):
        request_id = request.match_info["request_id"]
        async with self._tts_lock:
            if request_id not in self._active_tts:
                return web.json_response({"status": "not_found", "request_id": request_id}, status=404)
            self._active_tts[request_id] = True
        return web.json_response({"status": "cancel_requested", "request_id": request_id}, status=202)

    async def tts_audio(self, request):
        name = _safe_component(request.match_info["filename"])
        target = TTS_CACHE_DIR / name if name else None
        if not target or not target.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(target)

    async def catalog_root(self, _request):
        return web.json_response({"publishers": [{"id": "pep", "name": "人民教育出版社"}],
                                  "subjects": self.catalog.subjects(), "stats": self.catalog.stats()})

    def _catalog_response(self, loader):
        try:
            return web.json_response(loader())
        except (KeyError, IndexError, ValueError) as exc:
            return web.json_response({"error": str(exc.args[0] if exc.args else exc)}, status=404)

    async def catalog_volumes(self, request):
        return self._catalog_response(lambda: {"volumes": self.catalog.volumes(request.match_info["subject"])})

    async def catalog_units(self, request):
        m = request.match_info
        return self._catalog_response(lambda: {"units": self.catalog.units(m["subject"], m["volume_id"])})

    async def catalog_lessons(self, request):
        m = request.match_info
        return self._catalog_response(lambda: {"lessons": self.catalog.lessons(m["subject"], m["volume_id"], int(m["unit_id"]))})

    async def catalog_unit_entries(self, request):
        m = request.match_info
        return self._catalog_response(lambda: {"entries": self.catalog.unit_entries(m["subject"], m["volume_id"], int(m["unit_id"]))})

    async def catalog_entries(self, request):
        m = request.match_info
        return self._catalog_response(lambda: {"entries": self.catalog.entries(m["subject"], m["volume_id"], int(m["unit_id"]), int(m["lesson_id"]))})

    async def dictionary_examples(self, request):
        data = await request.json()
        chars = data.get("chars")
        if not isinstance(chars, list) or len(chars) > 200:
            return web.json_response({"error": "chars must be a list with at most 200 items"}, status=400)
        result = await asyncio.to_thread(self.dictionary.batch, chars)
        return web.json_response({"examples": result})

    async def list_parent_records(self, _request):
        records = []
        for path in PARENT_DIR.glob("*/record.json"):
            record = _read_json(path)
            if not record:
                continue
            photos = record.get("photos") or []
            records.append({"id": record.get("id", path.parent.name),
                            "createdAt": record.get("createdAt", ""),
                            "language": record.get("language", "zh-CN"),
                            "source": record.get("source", "unknown"),
                            "wordCount": len(record.get("words") or []),
                            "photoCount": len(photos),
                            "coverUrl": photos[0].get("url", "") if photos else ""})
        records.sort(key=lambda item: item.get("createdAt", ""), reverse=True)
        return web.json_response({"items": records[:100]})

    async def create_parent_record(self, request):
        fields, files = await _multipart_to_temp(request, max_bytes=30 * 1024 * 1024,
                                                 file_fields={"photos"})
        uploads = files.get("photos") or []
        try:
            metadata = json.loads(fields.get("record", "{}") or "{}")
        except json.JSONDecodeError:
            metadata = None
        raw_words = metadata.get("words") if isinstance(metadata, dict) else None
        if not isinstance(raw_words, list) or not raw_words:
            for item in uploads: item["path"].unlink(missing_ok=True)
            return web.json_response({"error": "缺少本次听写词表。"}, status=400)
        if not uploads or len(uploads) > 5:
            for item in uploads: item["path"].unlink(missing_ok=True)
            return web.json_response({"error": "请上传1到5张听写照片。"}, status=400)
        words = []
        for raw in raw_words[:300]:
            text = str(raw.get("text", "") if isinstance(raw, dict) else raw).strip()[:80]
            language = str(raw.get("language", raw.get("lang", "")) if isinstance(raw, dict) else "")[:16]
            if text: words.append({"text": text, "language": language})
        now = datetime.now(timezone.utc)
        record_id = now.strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:8]
        record_dir = PARENT_DIR / record_id
        record_dir.mkdir(parents=True, exist_ok=False)
        photos = []
        try:
            for index, upload in enumerate(uploads, 1):
                filename = f"photo_{index:02d}{upload['suffix']}"
                os.replace(upload["path"], record_dir / filename)
                photos.append({"name": filename,
                               "url": f"/api/parent/records/{record_id}/photos/{filename}"})
            record = {"id": record_id, "createdAt": now.isoformat(),
                      "language": str(metadata.get("language", "zh-CN"))[:16],
                      "source": str(metadata.get("source", "unknown"))[:32],
                      "order": str(metadata.get("order", "sequence"))[:16],
                      "repeat": max(1, min(5, int(metadata.get("repeat", 1)))),
                      "waitSeconds": max(0, min(60, int(metadata.get("waitSeconds", 0)))),
                      "words": words, "photos": photos}
            _atomic_json(record_dir / "record.json", record)
            return web.json_response(record, status=201)
        except Exception:
            shutil.rmtree(record_dir, ignore_errors=True)
            raise

    def _parent_record(self, record_id: str) -> tuple[Path | None, dict | None]:
        safe_id = _safe_component(record_id)
        path = PARENT_DIR / safe_id / "record.json" if safe_id else None
        return (path, _read_json(path)) if path and path.is_file() else (None, None)

    async def get_parent_record(self, request):
        _path, record = self._parent_record(request.match_info["record_id"])
        return web.json_response(record if record else {"error": "听写记录不存在。"},
                                 status=200 if record else 404)

    async def parent_photo(self, request):
        record_id = _safe_component(request.match_info["record_id"])
        name = _safe_component(request.match_info["filename"])
        target = PARENT_DIR / record_id / name if record_id and name else None
        if not target or not target.is_file(): raise web.HTTPNotFound()
        return web.FileResponse(target)

    async def delete_parent_photo(self, request):
        path, record = self._parent_record(request.match_info["record_id"])
        name = _safe_component(request.match_info["filename"])
        if not path or not record or not name:
            return web.json_response({"error": "照片不存在。"}, status=404)
        photos = record.get("photos") or []
        if not any(item.get("name") == name for item in photos):
            return web.json_response({"error": "照片不存在。"}, status=404)
        (path.parent / name).unlink(missing_ok=True)
        remaining = [item for item in photos if item.get("name") != name]
        if not remaining:
            shutil.rmtree(path.parent, ignore_errors=True)
            return web.json_response({"deleted": True, "id": path.parent.name})
        record["photos"] = remaining
        _atomic_json(path, record)
        return web.json_response({"deleted": False, "record": record})

    async def close(self) -> None:
        await self.ocr_jobs.close()
        await self.worker.close()
        for path in TEMP_DIR.glob("upload-*"):
            path.unlink(missing_ok=True)
