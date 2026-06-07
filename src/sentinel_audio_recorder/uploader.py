import hashlib
import logging
import os
import shutil
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)

DEFAULT_RECORDINGS_DIR = Path(__file__).resolve().parent.parent.parent / "recordings"
DEFAULT_SMALL_RECORDING_BYTES = 3984588


@dataclass
class UploadConfig:
    upload_url: Optional[str] = None
    upload_token: Optional[str] = None
    recordings_dir: Path = DEFAULT_RECORDINGS_DIR
    scan_interval: int = 30
    timeout: int = 60
    min_age_seconds: int = 10
    retry_base_seconds: int = 30
    retry_max_seconds: int = 3600
    storage_high_watermark: float = 80.0
    small_recording_bytes: int = DEFAULT_SMALL_RECORDING_BYTES
    deleted_retention_days: int = 30
    state_db: Optional[Path] = None

    @classmethod
    def from_env(cls) -> "UploadConfig":
        recordings_dir = Path(
            os.getenv("SENTINEL_RECORDINGS_DIR", str(DEFAULT_RECORDINGS_DIR))
        )
        return cls(
            upload_url=os.getenv("SENTINEL_UPLOAD_URL"),
            upload_token=os.getenv("SENTINEL_UPLOAD_TOKEN"),
            recordings_dir=recordings_dir,
            scan_interval=int(os.getenv("SENTINEL_UPLOAD_INTERVAL", "30")),
            timeout=int(os.getenv("SENTINEL_UPLOAD_TIMEOUT", "60")),
            min_age_seconds=int(os.getenv("SENTINEL_UPLOAD_MIN_AGE", "10")),
            retry_base_seconds=int(os.getenv("SENTINEL_UPLOAD_RETRY_BASE", "30")),
            retry_max_seconds=int(os.getenv("SENTINEL_UPLOAD_MAX_BACKOFF", "3600")),
            storage_high_watermark=float(
                os.getenv("SENTINEL_STORAGE_HIGH_WATERMARK", "80")
            ),
            small_recording_bytes=int(
                os.getenv(
                    "SENTINEL_SMALL_RECORDING_BYTES",
                    str(DEFAULT_SMALL_RECORDING_BYTES),
                )
            ),
            deleted_retention_days=int(
                os.getenv("SENTINEL_DELETED_RETENTION_DAYS", "30")
            ),
        )

    @property
    def enabled(self) -> bool:
        return bool(self.upload_url)

    @property
    def db_path(self) -> Path:
        if self.state_db is not None:
            return self.state_db
        return self.recordings_dir / ".upload_state.sqlite"

    def public_endpoint(self) -> Optional[str]:
        if not self.upload_url:
            return None
        parsed = urlparse(self.upload_url)
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


class RecordingUploader:
    def __init__(
        self,
        config: Optional[UploadConfig] = None,
        request_post: Optional[Callable] = None,
        disk_usage: Optional[Callable[[Path], shutil._ntuple_diskusage]] = None,
    ):
        self.config = config or UploadConfig.from_env()
        self.request_post = request_post or requests.post
        self.disk_usage = disk_usage or shutil.disk_usage
        self._stop_event = threading.Event()
        self._last_cleanup: List[Dict[str, object]] = []
        self.config.recordings_dir.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self):
        return sqlite3.connect(self.config.db_path)

    def _init_db(self):
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS uploads (
                    filename TEXT PRIMARY KEY,
                    size INTEGER NOT NULL,
                    mtime REAL NOT NULL,
                    sha256 TEXT,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_retry_at REAL NOT NULL DEFAULT 0,
                    last_error TEXT,
                    uploaded_at TEXT,
                    updated_at TEXT NOT NULL
                )
                """
            )

    def stop(self):
        self._stop_event.set()

    def run_forever(self):
        logger.info("Upload sync loop started.")
        while not self._stop_event.is_set():
            self.run_once()
            self._stop_event.wait(self.config.scan_interval)

    def run_once(self) -> Dict[str, object]:
        self._sync_existing_files()
        pruned = self.prune_deleted_history()
        uploaded = 0
        if self.config.enabled:
            for path in self._eligible_files():
                if not self._can_upload(path):
                    continue
                try:
                    self._upload_file(path)
                    uploaded += 1
                except Exception as exc:
                    self._record_failure(path, str(exc))
                    logger.warning("Upload failed for %s: %s", path, exc)
        else:
            logger.debug("Upload sync disabled: SENTINEL_UPLOAD_URL is not set.")

        cleanup = self.cleanup_storage()
        return {"uploaded": uploaded, "cleanup": cleanup, "pruned_deleted": pruned}

    def cleanup_storage(self) -> Dict[str, object]:
        usage_before = self._disk_percent()
        deleted: List[Dict[str, object]] = []
        if usage_before < self.config.storage_high_watermark:
            self._last_cleanup = []
            return {
                "triggered": False,
                "usage_before": usage_before,
                "usage_after": usage_before,
                "deleted": deleted,
            }

        for path in self._wav_files_oldest_first():
            if path.stat().st_size >= self.config.small_recording_bytes:
                continue
            deleted.append(self._delete_recording(path, "small_recording"))

        for path in self._wav_files_oldest_first():
            if self._disk_percent() < self.config.storage_high_watermark:
                break
            if self.is_uploaded(path):
                deleted.append(self._delete_recording(path, "uploaded_cache"))

        usage_after = self._disk_percent()
        self._last_cleanup = deleted
        return {
            "triggered": True,
            "usage_before": usage_before,
            "usage_after": usage_after,
            "deleted": deleted,
        }

    def status(self) -> Dict[str, object]:
        self._sync_existing_files()
        pruned = self.prune_deleted_history()
        wav_files = list(self.config.recordings_dir.glob("*.wav"))
        small_files = [
            path
            for path in wav_files
            if path.is_file() and path.stat().st_size < self.config.small_recording_bytes
        ]
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) FROM uploads GROUP BY status"
            ).fetchall()
            last = conn.execute(
                "SELECT uploaded_at FROM uploads WHERE uploaded_at IS NOT NULL "
                "ORDER BY uploaded_at DESC LIMIT 1"
            ).fetchone()
            error = conn.execute(
                "SELECT filename, last_error FROM uploads WHERE last_error IS NOT NULL "
                "ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()

        counts = {status: count for status, count in rows}
        return {
            "enabled": self.config.enabled,
            "endpoint": self.config.public_endpoint(),
            "recordings_dir": str(self.config.recordings_dir),
            "disk_usage_percent": self._disk_percent(),
            "high_watermark_percent": self.config.storage_high_watermark,
            "small_recording_bytes": self.config.small_recording_bytes,
            "deleted_retention_days": self.config.deleted_retention_days,
            "filesystem_wav_count": len(wav_files),
            "filesystem_small_wav_count": len(small_files),
            "filesystem_unuploaded_wav_count": sum(
                1 for path in wav_files if not self.is_uploaded(path)
            ),
            "pending": counts.get("pending", 0),
            "failed": counts.get("failed", 0),
            "uploaded": counts.get("uploaded", 0),
            "deleted": counts.get("deleted", 0),
            "pruned_deleted": pruned,
            "last_upload_at": last[0] if last else None,
            "last_error": {"filename": error[0], "message": error[1]} if error else None,
            "last_cleanup": self._last_cleanup,
        }

    def is_uploaded(self, path: Path) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT status FROM uploads WHERE filename = ?", (path.name,)
            ).fetchone()
        return bool(row and row[0] == "uploaded")

    def _eligible_files(self) -> List[Path]:
        now = time.time()
        files = []
        for path in self._wav_files_oldest_first():
            if not path.is_file():
                continue
            if now - path.stat().st_mtime < self.config.min_age_seconds:
                continue
            files.append(path)
        return files

    def _wav_files_oldest_first(self) -> List[Path]:
        return sorted(
            self.config.recordings_dir.glob("*.wav"),
            key=lambda path: path.stat().st_mtime,
        )

    def _can_upload(self, path: Path) -> bool:
        row = self._get_upload(path)
        now = time.time()
        if row and row["status"] == "uploaded":
            return False
        if row and row["next_retry_at"] > now:
            return False
        return True

    def _upload_file(self, path: Path):
        headers = {}
        if self.config.upload_token:
            headers["Authorization"] = f"Bearer {self.config.upload_token}"

        checksum = self._sha256(path)
        with path.open("rb") as file_handle:
            response = self.request_post(
                self.config.upload_url,
                files={"file": (path.name, file_handle, "audio/wav")},
                data={"sha256": checksum},
                headers=headers,
                timeout=self.config.timeout,
            )

        if response.status_code < 200 or response.status_code >= 300:
            raise RuntimeError(f"HTTP {response.status_code}: {response.text[:200]}")

        self._record_success(path, checksum)
        logger.info("Uploaded %s to %s", path.name, self.config.public_endpoint())

    def _get_upload(self, path: Path) -> Optional[Dict[str, object]]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM uploads WHERE filename = ?", (path.name,)
            ).fetchone()
        return dict(row) if row else None

    def _record_success(self, path: Path, checksum: str):
        stat = path.stat()
        timestamp = self._now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO uploads (
                    filename, size, mtime, sha256, status, attempts,
                    next_retry_at, last_error, uploaded_at, updated_at
                )
                VALUES (?, ?, ?, ?, 'uploaded', 0, 0, NULL, ?, ?)
                ON CONFLICT(filename) DO UPDATE SET
                    size = excluded.size,
                    mtime = excluded.mtime,
                    sha256 = excluded.sha256,
                    status = 'uploaded',
                    next_retry_at = 0,
                    last_error = NULL,
                    uploaded_at = excluded.uploaded_at,
                    updated_at = excluded.updated_at
                """,
                (path.name, stat.st_size, stat.st_mtime, checksum, timestamp, timestamp),
            )

    def _record_failure(self, path: Path, error: str):
        stat = path.stat()
        existing = self._get_upload(path)
        attempts = int(existing["attempts"]) + 1 if existing else 1
        backoff = min(
            self.config.retry_base_seconds * (2 ** (attempts - 1)),
            self.config.retry_max_seconds,
        )
        now = time.time()
        timestamp = self._now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO uploads (
                    filename, size, mtime, status, attempts, next_retry_at,
                    last_error, updated_at
                )
                VALUES (?, ?, ?, 'failed', ?, ?, ?, ?)
                ON CONFLICT(filename) DO UPDATE SET
                    size = excluded.size,
                    mtime = excluded.mtime,
                    status = 'failed',
                    attempts = excluded.attempts,
                    next_retry_at = excluded.next_retry_at,
                    last_error = excluded.last_error,
                    updated_at = excluded.updated_at
                """,
                (path.name, stat.st_size, stat.st_mtime, attempts, now + backoff, error, timestamp),
            )

    def _sync_existing_files(self):
        timestamp = self._now()
        with self._connect() as conn:
            for path in self.config.recordings_dir.glob("*.wav"):
                stat = path.stat()
                conn.execute(
                    """
                    INSERT INTO uploads (filename, size, mtime, status, updated_at)
                    VALUES (?, ?, ?, 'pending', ?)
                    ON CONFLICT(filename) DO UPDATE SET
                        size = excluded.size,
                        mtime = excluded.mtime,
                        status = CASE
                            WHEN uploads.status = 'deleted' THEN 'pending'
                            ELSE uploads.status
                        END,
                        updated_at = excluded.updated_at
                    """,
                    (path.name, stat.st_size, stat.st_mtime, timestamp),
                )

    def prune_deleted_history(self) -> int:
        if self.config.deleted_retention_days < 0:
            return 0

        cutoff = time.time() - (self.config.deleted_retention_days * 24 * 60 * 60)
        cutoff_text = (
            datetime.fromtimestamp(cutoff, timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM uploads WHERE status = 'deleted' AND updated_at < ?",
                (cutoff_text,),
            )
            return cursor.rowcount

    def _delete_recording(self, path: Path, reason: str) -> Dict[str, object]:
        size = path.stat().st_size
        mtime = path.stat().st_mtime
        path.unlink()
        timestamp = self._now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO uploads (filename, size, mtime, status, updated_at)
                VALUES (?, ?, ?, 'deleted', ?)
                ON CONFLICT(filename) DO UPDATE SET
                    size = excluded.size,
                    mtime = excluded.mtime,
                    status = 'deleted',
                    updated_at = excluded.updated_at
                """,
                (path.name, size, mtime, timestamp),
            )
        logger.info("Deleted %s (%s, %s bytes)", path.name, reason, size)
        return {"filename": path.name, "reason": reason, "bytes": size}

    def _disk_percent(self) -> float:
        usage = self.disk_usage(self.config.recordings_dir)
        return (usage.used / usage.total) * 100 if usage.total else 0.0

    def _sha256(self, path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as file_handle:
            for chunk in iter(lambda: file_handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
