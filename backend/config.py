from __future__ import annotations

import os
from typing import List

from dotenv import load_dotenv

load_dotenv()

# DeepSeek API 配置
DEEPSEEK_API_KEY: str = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL: str = "https://api.deepseek.com"
DEEPSEEK_MODEL: str = "deepseek-chat"

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
CHUNK_MAX_TOKENS: int = 500

# 审查配置
MAX_FIX_ATTEMPTS: int = 3
REVIEW_ROUNDS_DEBUG: int = 2

# FC 工具循环上限
MAX_TOOL_ROUNDS: int = 10
