"""FeatureBench Phase 1 单任务 CLI。"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path

from backend.config import (
    FEATUREBENCH_DATASET,
    FEATUREBENCH_DATA_VERSION,
    FEATUREBENCH_SPLIT,
    FEATUREBENCH_WORK_ROOT,
)
from backend.evaluation.featurebench.runner import run_trial


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="运行一个 Just_codding FeatureBench trial")
    parser.add_argument("--task-id", required=True, help="FeatureBench task/instance ID")
    parser.add_argument("--split", default=FEATUREBENCH_SPLIT)
    parser.add_argument("--data-version", default=FEATUREBENCH_DATA_VERSION)
    parser.add_argument("--attempts", type=int, default=1, choices=[1], help="Phase 1 仅支持 1")
    parser.add_argument("--run-id", help="可选运行标识")
    parser.add_argument("--dataset", default=FEATUREBENCH_DATASET)
    parser.add_argument("--work-root", type=Path, default=Path(FEATUREBENCH_WORK_ROOT))
    return parser


async def _main() -> int:
    args = build_parser().parse_args()
    result = await run_trial(
        task_id=args.task_id, attempt=1, run_id=args.run_id,
        dataset_name=args.dataset, data_version=args.data_version,
        split=args.split, work_root=args.work_root,
    )
    print(json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2))
    return 0 if result.status == "completed" else 1


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    raise SystemExit(asyncio.run(_main()))


if __name__ == "__main__":
    main()
