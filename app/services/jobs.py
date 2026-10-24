import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Callable

from app.db.database import get_one, insert, now, update


class JobManager:
    def __init__(self, workers: int = 2):
        self.executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="vananetra-job")
        self.lock = threading.Lock()

    def submit(self, kind: str, task: Callable[[], dict]) -> str:
        job_id = str(uuid.uuid4())
        insert("jobs", {"id": job_id, "kind": kind, "status": "queued", "progress": 0.0, "result": None, "error": None, "created_at": now(), "updated_at": now()})
        self.executor.submit(self._run, job_id, task)
        return job_id

    def _run(self, job_id: str, task: Callable[[], dict]) -> None:
        update("jobs", "id", job_id, {"status": "running", "progress": 0.1, "updated_at": now()})
        try:
            result = task()
            update("jobs", "id", job_id, {"status": "completed", "progress": 1.0, "result": result, "updated_at": now()})
        except Exception as exc:  # jobs must preserve structured failure state
            update("jobs", "id", job_id, {"status": "failed", "progress": 1.0, "error": {"type": type(exc).__name__, "message": str(exc)}, "updated_at": now()})

    def get(self, job_id: str):
        return get_one("jobs", "id", job_id)
