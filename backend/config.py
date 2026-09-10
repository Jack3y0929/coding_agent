from __future__ import annotations

import os
from typing import List

from dotenv import load_dotenv

# 项目配置优先于宿主进程中残留的旧变量，避免切换模型后仍使用旧 Key。
load_dotenv(override=True)

# OpenAI 兼容 API 配置（当前指向百炼）
DEEPSEEK_API_KEY: str = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL: str = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL: str = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

# 预算配置
MAX_COST_YUAN: float = 5.0
API_TIMEOUT: float = 30.0
MAX_RETRIES: int = 3

# 项目路径配置
PROJECT_ROOT: str = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH: str = os.path.join(PROJECT_ROOT, "just_codding.db")

# Shell 命令白名单
SHELL_WHITELIST: List[str] = [
    "cargo check",
    "cargo build",
    "cargo test",
    "wasm-pack build",
    "npm run build",
    "npm run lint",
    "npm run typecheck",
    "npx tsc --noEmit",
    "pytest",
    "python -m pytest",
    "git diff",
    "git status",
    "git log --oneline",
]

# RAG 配置
EMBEDDING_MODEL: str = "all-MiniLM-L6-v2"
RAG_EMBEDDING_BACKEND: str = os.getenv("RAG_EMBEDDING_BACKEND", "sentence-transformers")
CHUNK_MAX_TOKENS: int = 500

# 审查配置
MAX_FIX_ATTEMPTS: int = 3
REVIEW_ROUNDS_DEBUG: int = 2

# FC 工具循环上限
MAX_TOOL_ROUNDS: int = 10

# 工具输出限制：工具结果只作为当前节点的短期工作记忆，不应无限增长。
READ_FILE_MAX_LINES: int = 240
READ_FILE_MAX_CHARS: int = 12000
TOOL_CONTEXT_MAX_CHARS: int = 6000
TOOL_ROUND_MAX_CHARS: int = 12000
