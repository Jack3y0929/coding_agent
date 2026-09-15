from __future__ import annotations

import os
from typing import List

from dotenv import load_dotenv

# 项目配置优先于宿主进程中残留的旧变量，避免切换模型后仍使用旧 Key。
load_dotenv(override=True)

# OpenAI 兼容 API 配置（当前指向百炼）
DEEPSEEK_API_KEY: str = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL: str = os.getenv("DEEPSEEK_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
DEEPSEEK_MODEL: str = os.getenv("DEEPSEEK_MODEL", "Qwen3.7-Flash")
DEEPSEEK_FALLBACK_MODELS: List[str] = [item.strip() for item in os.getenv("DEEPSEEK_FALLBACK_MODELS", "").split(",") if item.strip()]

# 预算配置
MAX_COST_YUAN: float = 5.0
# 模型请求分离连接与读取超时：复杂工具调用需要更长的响应窗口。
API_TIMEOUT: float = float(os.getenv("API_TIMEOUT", "180"))
API_CONNECT_TIMEOUT: float = 20.0
API_WRITE_TIMEOUT: float = 60.0
API_POOL_TIMEOUT: float = 20.0
MAX_RETRIES: int = int(os.getenv("MAX_RETRIES", "2"))

# 项目路径配置
PROJECT_ROOT: str = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH: str = os.path.join(PROJECT_ROOT, "just_codding.db")

# SWE-bench 评测配置。官方 Harness 在独立 Docker 环境中执行，不复用产品 Shell 白名单。
SWEBENCH_DATASET_NAME: str = os.getenv(
    "SWEBENCH_DATASET_NAME", "princeton-nlp/SWE-bench_Verified"
)
SWEBENCH_SPLIT: str = os.getenv("SWEBENCH_SPLIT", "test")
SWEBENCH_WORK_ROOT: str = os.path.abspath(os.getenv(
    "SWEBENCH_WORK_ROOT", os.path.join(PROJECT_ROOT, ".swebench")
))
SWEBENCH_MAX_WORKERS: int = int(os.getenv("SWEBENCH_MAX_WORKERS", "1"))
SWEBENCH_TRIAL_TIMEOUT: int = int(os.getenv("SWEBENCH_TRIAL_TIMEOUT", "1800"))
SWEBENCH_DOCKER_REQUIRED: bool = (
    os.getenv("SWEBENCH_DOCKER_REQUIRED", "true").lower() == "true"
)
SWEBENCH_MAX_FIX_ATTEMPTS: int = int(os.getenv("SWEBENCH_MAX_FIX_ATTEMPTS", "3"))

# FeatureBench 评测配置。Agent 的文件操作在官方任务容器 /testbed 中完成。
FEATUREBENCH_DATASET: str = os.getenv("FEATUREBENCH_DATASET", "LiberCoders/FeatureBench")
FEATUREBENCH_DATA_VERSION: str = os.getenv("FEATUREBENCH_DATA_VERSION", "v1.1")
FEATUREBENCH_SPLIT: str = os.getenv("FEATUREBENCH_SPLIT", "fast")
FEATUREBENCH_WORK_ROOT: str = os.path.abspath(os.getenv(
    "FEATUREBENCH_WORK_ROOT", os.path.join(PROJECT_ROOT, ".featurebench")
))
FEATUREBENCH_MAX_WORKERS: int = int(os.getenv("FEATUREBENCH_MAX_WORKERS", "1"))
FEATUREBENCH_TASK_TIMEOUT: int = int(os.getenv("FEATUREBENCH_TASK_TIMEOUT", "3600"))
FEATUREBENCH_MAX_FIX_ATTEMPTS: int = int(os.getenv("FEATUREBENCH_MAX_FIX_ATTEMPTS", "3"))
FEATUREBENCH_CONTAINER_CPUS: float = float(os.getenv("FEATUREBENCH_CONTAINER_CPUS", "2"))
FEATUREBENCH_CONTAINER_MEMORY: str = os.getenv("FEATUREBENCH_CONTAINER_MEMORY", "8g")

# Shell 命令白名单
# 版本查询属于无副作用的读取操作，应允许 Agent 用于确认运行环境。
SHELL_WHITELIST: List[str] = [
    # 只读目录与环境探测
    "dir",
    "tree",
    "where",
    "node --version",
    "npm --version",
    "python --version",
    # 前端/JavaScript 校验
    "node --check",
    "cargo check",
    "cargo build",
    "cargo test",
    "wasm-pack build",
    "npm run build",
    "npm run lint",
    "npm run typecheck",
    "npm --version",
    "npx tsc --noEmit",
    "pytest",
    "python -m pytest",
    "python -m compileall",
    "git diff",
    "git diff --check",
    "git status",
    "git log --oneline",
]

# RAG 配置。模型切换后通过版本字段拒绝混用旧向量。
EMBEDDING_MODEL: str = os.getenv("EMBEDDING_MODEL", "BAAI/bge-m3")
EMBEDDING_MODEL_VERSION: str = os.getenv("EMBEDDING_MODEL_VERSION", "bge-m3-v1")
RAG_EMBEDDING_BACKEND: str = os.getenv("RAG_EMBEDDING_BACKEND", "sentence-transformers")
CHUNK_MAX_TOKENS: int = 500

# 审查配置
MAX_FIX_ATTEMPTS: int = 3
REVIEW_ROUNDS_DEBUG: int = 2

# FC 工具循环上限
MAX_TOOL_ROUNDS: int = 10
# 单个工具出现可纠正错误时允许的模型纠错次数。
MAX_TOOL_RETRIES: int = int(os.getenv("MAX_TOOL_RETRIES", "1"))
# 连续相同调用超过此次数时提前终止，避免模型陷入重复循环。
MAX_IDENTICAL_TOOL_CALLS: int = int(os.getenv("MAX_IDENTICAL_TOOL_CALLS", "2"))

# 工具输出限制：工具结果只作为当前节点的短期工作记忆，不应无限增长。
READ_FILE_MAX_LINES: int = 240
READ_FILE_MAX_CHARS: int = 12000
TOOL_CONTEXT_MAX_CHARS: int = 6000
TOOL_ROUND_MAX_CHARS: int = 12000
