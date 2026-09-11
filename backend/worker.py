"""独立工作流 Worker。

每个任务由独立 Python 进程执行，避免 Uvicorn reload 或 API 进程重启取消
正在运行的 LangGraph 工作流。
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import os
from datetime import datetime, timezone
from pathlib import Path

from backend.main import StartWorkflowRequest, run_workflow_async

logging.basicConfig(level=logging.INFO, format="%(asctime)s [worker] %(levelname)s: %(message)s")
logger = logging.getLogger("worker")


async def _run(task_path: Path) -> None:
    payload = json.loads(task_path.read_text(encoding="utf-8"))
    payload.update({"pid": os.getpid(), "status": "running", "heartbeat_at": datetime.now(timezone.utc).isoformat()})
    task_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    request = StartWorkflowRequest.model_validate(payload["request"])
    await run_workflow_async(payload["session_id"], request)


def main() -> int:
    if len(sys.argv) != 2:
        logger.error("用法: python -m backend.worker <task.json>")
        return 2
    task_path = Path(sys.argv[1]).resolve()
    try:
        asyncio.run(_run(task_path))
        payload = json.loads(task_path.read_text(encoding="utf-8")) if task_path.exists() else {}
        payload.update({"status": "done", "finished_at": datetime.now(timezone.utc).isoformat()})
        task_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return 0
    except Exception:
        logger.exception("Worker执行失败: %s", task_path)
        if task_path.exists():
            try:
                payload = json.loads(task_path.read_text(encoding="utf-8"))
                payload.update({"status": "failed", "finished_at": datetime.now(timezone.utc).isoformat()})
                task_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass
        return 1
    finally:
        try:
            task_path.unlink(missing_ok=True)
        except OSError:
            logger.warning("无法删除任务文件: %s", task_path)


if __name__ == "__main__":
    raise SystemExit(main())
