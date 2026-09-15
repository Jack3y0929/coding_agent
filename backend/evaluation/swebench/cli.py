"""SWE-bench 单任务命令行入口。"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path

from backend.config import SWEBENCH_DATASET_NAME, SWEBENCH_WORK_ROOT
from backend.evaluation.swebench.runner import run_trial


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="运行一个 Just_codding SWE-bench trial")
    parser.add_argument("--instance-id", required=True, help="SWE-bench instance_id")
    parser.add_argument("--trials", type=int, default=1, choices=[1], help="Phase 1 仅支持 1")
    parser.add_argument("--run-id", help="可选运行标识；省略时自动生成")
    parser.add_argument("--dataset-name", default=SWEBENCH_DATASET_NAME)
    parser.add_argument("--work-root", type=Path, default=Path(SWEBENCH_WORK_ROOT))
    return parser


async def _main() -> int:
    args = build_parser().parse_args()
    result = await run_trial(
        instance_id=args.instance_id,
        trial_index=0,
        run_id=args.run_id,
        dataset_name=args.dataset_name,
        work_root=args.work_root,
        max_workers=1,
    )
    print(json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2))
    return 0 if result.status == "completed" else 1


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    raise SystemExit(asyncio.run(_main()))


if __name__ == "__main__":
    main()
