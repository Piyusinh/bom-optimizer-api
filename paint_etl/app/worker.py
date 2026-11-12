"""Background worker: watches the inbox folder and loads every file it finds.

  inbox/       files waiting (UI uploads land here; you can also copy files in by hand)
  processing/  the file currently being loaded
  processed/   loaded OK (or skipped because the same content was loaded before)
  failed/      anything that could not be loaded - the reason is in etl_ingest_batch.message
"""
import datetime as dt
import logging
import os
import threading
import time
from pathlib import Path

from . import config, pipeline
from .db import connect

log = logging.getLogger("worker")
SUPPORTED = {".xlsx", ".xlsm", ".csv"}


def _stamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d_%H%M%S")


def _unique(path: Path) -> Path:
    if not path.exists():
        return path
    return path.with_name(f"{_stamp()}_{path.name}")


def ready_files() -> list[Path]:
    """Files in the inbox that are finished copying."""
    out = []
    now = time.time()
    for p in config.INBOX.iterdir():
        if not p.is_file() or p.name.endswith(".part") or p.name.startswith(("~$", ".")):
            continue
        if now - p.stat().st_mtime < config.FILE_SETTLE_SECONDS:
            continue
        out.append(p)
    return sorted(out, key=lambda p: p.stat().st_mtime)


class Worker(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, name="inbox-worker")
        self.stop_event = threading.Event()
        self.current: str | None = None
        self.dirty = False            # something loaded since the last summary rebuild
        self.last_rebuild: dict | None = None

    # ── lifecycle ──
    def run(self):
        try:
            self.recover()
        except Exception:  # noqa: BLE001
            log.exception("recovery failed")
        while not self.stop_event.is_set():
            try:
                files = ready_files()
                if files:
                    self.handle(files[0])
                    continue  # look again immediately
                if self.dirty:
                    self.rebuild()
            except Exception:  # noqa: BLE001
                log.exception("worker loop error")
            self.stop_event.wait(config.POLL_SECONDS)

    def recover(self):
        """After a crash/restart: put half-processed files back and close dangling batches."""
        for p in config.PROCESSING.iterdir():
            if p.is_file():
                os.replace(p, _unique(config.INBOX / p.name))
        with connect(autocommit=True) as c:
            c.execute("UPDATE etl_ingest_batch SET status='FAILED', finished_at=now(), "
                      "message='Interrupted (service restarted) - file re-queued' WHERE status='RUNNING'")

    def rebuild(self):
        n = pipeline.rebuild_summary()
        self.dirty = False
        self.last_rebuild = {"rows": n, "at": dt.datetime.now().isoformat(timespec="seconds")}
        log.info("summary rebuilt: %s rows", n)

    # ── one file ──
    def handle(self, src: Path):
        work = _unique(config.PROCESSING / src.name)
        os.replace(src, work)
        self.current = src.name
        any_fail = False
        recognised = False
        try:
            with connect(autocommit=True) as ctl:
                if work.suffix.lower() not in SUPPORTED:
                    self._record(ctl, "UNKNOWN", src.name, "", "0" * 64, "FAILED",
                                 f"Unsupported file type '{work.suffix}'. Use .xlsx or .csv")
                    any_fail = True
                else:
                    try:
                        sha = pipeline.sha256_of(work)
                        sheets = pipeline.list_sheets(work)
                    except Exception as e:  # noqa: BLE001
                        self._record(ctl, "UNKNOWN", src.name, "", "0" * 64, "FAILED",
                                     f"Cannot read file: {e}"[:500])
                        any_fail = True
                        sheets = []
                        sha = ""
                    for sheet, header in sheets:
                        ds = pipeline.detect_dataset(header)
                        if ds is None:
                            cols = ", ".join(str(c) for c in header[:8] if c is not None)
                            self._record(ctl, "UNKNOWN", src.name, sheet, sha, "SKIPPED",
                                         f"Columns not recognised ({cols} ...)")
                            continue
                        recognised = True
                        dup = ctl.execute(
                            "SELECT batch_id FROM etl_ingest_batch WHERE dataset=%s AND file_sha256=%s "
                            "AND source_sheet=%s AND status IN ('RUNNING','SUCCESS')",
                            (ds, sha, sheet)).fetchone()
                        if dup:
                            self._record(ctl, ds, src.name, sheet, sha, "SKIPPED",
                                         f"Same content already loaded (batch {dup['batch_id']})")
                            continue
                        any_fail |= not self._load(ctl, work, src.name, sheet, header, ds, sha)
                    if sheets and not recognised and not any_fail:
                        any_fail = True
        except Exception as e:  # noqa: BLE001
            log.exception("handle failed")
            any_fail = True
            try:
                with connect(autocommit=True) as ctl:
                    self._record(ctl, "UNKNOWN", src.name, "", "0" * 64, "FAILED", str(e)[:500])
            except Exception:  # noqa: BLE001
                pass
        finally:
            dest_dir = config.FAILED if (any_fail or not recognised) else config.PROCESSED
            os.replace(work, _unique(dest_dir / f"{_stamp()}_{src.name}"))
            self.current = None

    @staticmethod
    def _record(ctl, ds, fname, sheet, sha, status, msg):
        ctl.execute("INSERT INTO etl_ingest_batch (dataset,source_file,source_sheet,file_sha256,status,message,finished_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,now())", (ds, fname, sheet, sha, status, msg))

    def _load(self, ctl, work: Path, fname: str, sheet: str, header, ds: str, sha: str) -> bool:
        batch_id = ctl.execute(
            "INSERT INTO etl_ingest_batch (dataset,source_file,source_sheet,file_sha256) "
            "VALUES (%s,%s,%s,%s) RETURNING batch_id", (ds, fname, sheet, sha)).fetchone()["batch_id"]

        def progress(n_in, n_ok, n_rej):
            ctl.execute("UPDATE etl_ingest_batch SET rows_in_file=%s, rows_loaded=%s, rows_rejected=%s "
                        "WHERE batch_id=%s", (n_in, n_ok, n_rej, batch_id))

        t0 = time.time()
        try:
            n_in, n_ok, n_rej = pipeline.load_sheet(work, sheet, header, ds, batch_id, progress)
            ctl.execute("UPDATE etl_ingest_batch SET status='SUCCESS', rows_in_file=%s, rows_loaded=%s, "
                        "rows_rejected=%s, finished_at=now(), message=%s WHERE batch_id=%s",
                        (n_in, n_ok, n_rej, f"Loaded in {time.time() - t0:.1f}s", batch_id))
            self.dirty = True
            return True
        except Exception as e:  # noqa: BLE001
            log.exception("load failed")
            ctl.execute("UPDATE etl_ingest_batch SET status='FAILED', finished_at=now(), message=%s "
                        "WHERE batch_id=%s", (str(e)[:500], batch_id))
            return False
