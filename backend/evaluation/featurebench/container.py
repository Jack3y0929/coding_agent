"""FeatureBench 官方任务容器及受限 /testbed 文件操作。"""

from __future__ import annotations

import asyncio
import re
import tempfile
import uuid
from pathlib import Path
from pathlib import PurePosixPath

from backend.config import FEATUREBENCH_CONTAINER_CPUS, FEATUREBENCH_CONTAINER_MEMORY

_SAFE_CONTAINER = re.compile(r"[^a-zA-Z0-9_.-]+")


class FeatureBenchContainerError(RuntimeError):
    """容器生命周期或工具操作失败。"""


class FeatureBenchContainer:
    """为单任务持有一个 Docker 容器，所有源码操作限定在 /testbed。"""

    def __init__(self, image_name: str, run_id: str, task_id: str) -> None:
        if not image_name.strip():
            raise FeatureBenchContainerError("任务缺少 image_name")
        suffix = uuid.uuid4().hex[:8]
        self.name = _SAFE_CONTAINER.sub("-", f"jc-fb-{run_id}-{task_id}-{suffix}").lower()[:63]
        self.image_name = image_name
        self.baseline_tests: set[str] = set()

    async def start(self) -> None:
        await _docker(["info", "--format", "{{.ServerVersion}}"], timeout=30)
        await _docker([
            "run", "--detach", "--name", self.name,
            "--cpus", str(FEATUREBENCH_CONTAINER_CPUS),
            "--memory", FEATUREBENCH_CONTAINER_MEMORY,
            "--workdir", "/testbed", "--entrypoint", "sh", self.image_name,
            "-lc", "while true; do sleep 3600; done",
        ], timeout=120)
        await self.exec(["git", "rev-parse", "--is-inside-work-tree"])
        paths = await self.exec(["git", "ls-files"])
        self.baseline_tests = {path for path in paths.splitlines() if _is_test_path(path)}

    async def stop(self) -> None:
        try:
            await _docker(["rm", "--force", self.name], timeout=60)
        except FeatureBenchContainerError:
            pass

    async def exec(self, command: list[str], timeout: int = 120) -> str:
        return await _docker(["exec", "--workdir", "/testbed", self.name, *command], timeout)

    async def read_file(self, path: str, start: int = 1, end: int = 240) -> str:
        normalized = _safe_path(path)
        script = (
            "import pathlib,sys; p=pathlib.Path(sys.argv[1]); "
            "lines=p.read_text(encoding='utf-8',errors='replace').splitlines(); "
            "a=int(sys.argv[2]); b=int(sys.argv[3]); "
            "print('\\n'.join(f'{i+1:>6}: {lines[i]}' for i in range(a-1,min(b,len(lines)))))"
        )
        return await self.exec(["python", "-c", script, normalized, str(max(1, start)), str(max(start, end))])

    async def search_code(self, pattern: str, file_pattern: str | None = None) -> str:
        script = _SEARCH_SCRIPT
        return await self.exec(["python", "-c", script, pattern, file_pattern or ""], timeout=120)

    async def write_file(self, path: str, content: str) -> str:
        normalized = _safe_path(path)
        parent = str(PurePosixPath(normalized).parent)
        await self.exec(["mkdir", "-p", parent])
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False) as handle:
                handle.write(content)
                temporary_path = Path(handle.name)
            await _docker(["cp", str(temporary_path), f"{self.name}:/testbed/{normalized}"], timeout=120)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
        return f"写入成功: /testbed/{normalized} ({len(content)} 字符)"

    async def status_paths(self) -> list[str]:
        output = await self.exec(["git", "status", "--porcelain=v1", "-z"])
        entries = output.split("\0")
        paths: list[str] = []
        for entry in entries:
            if len(entry) >= 4:
                paths.append(entry[3:].replace("\\", "/"))
        return paths


def _safe_path(path: str) -> str:
    candidate = PurePosixPath(path.replace("\\", "/"))
    if candidate.is_absolute() or ".." in candidate.parts or not candidate.parts:
        raise FeatureBenchContainerError(f"路径不在 /testbed 范围内: {path}")
    return str(candidate)


def _is_test_path(path: str) -> bool:
    normalized = path.replace("\\", "/").lower()
    parts = normalized.split("/")
    name = parts[-1]
    return "test" in parts or "tests" in parts or name.startswith("test_") or name.endswith("_test.py")


async def _docker(arguments: list[str], timeout: int) -> str:
    try:
        process = await asyncio.create_subprocess_exec(
            "docker", *arguments,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, OSError) as exc:
        raise FeatureBenchContainerError(f"无法启动 Docker: {exc}") from exc
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.wait()
        raise FeatureBenchContainerError(f"Docker 操作超过 {timeout} 秒") from exc
    output = stdout.decode("utf-8", errors="replace")
    error = stderr.decode("utf-8", errors="replace")
    if process.returncode != 0:
        raise FeatureBenchContainerError(f"Docker 操作失败({process.returncode}): {error[-2000:]}")
    return output


_SEARCH_SCRIPT = """
import pathlib,re,sys
pattern=re.compile(sys.argv[1]); glob=sys.argv[2] or '*'; count=0
skip={'.git','node_modules','dist','build','target','.venv','venv','__pycache__'}
for p in pathlib.Path('.').rglob(glob):
    if count>=50: break
    if not p.is_file() or any(part in skip for part in p.parts): continue
    try: lines=p.read_text(encoding='utf-8',errors='replace').splitlines()
    except OSError: continue
    for no,line in enumerate(lines,1):
        if pattern.search(line):
            print(f'{p.as_posix()}:{no}: {line[:500]}'); count+=1
            if count>=50: break
""".strip()
