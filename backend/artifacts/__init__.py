"""正式中间产物的结构化渲染与校验。

兼容旧版 ``backend/artifacts.py`` 的写入 API。仓库同时保留了旧模块文件
和新版包目录，因此这里显式加载旧模块并重新导出其函数，避免 Python
优先解析包目录后造成 ImportError。
"""

import importlib.util
from pathlib import Path

from backend.artifacts.renderer import render_diagnosis_document, render_requirement_document
from backend.artifacts.validators import validate_artifact

_legacy_path = Path(__file__).resolve().parent.parent / "artifacts.py"
_legacy_spec = importlib.util.spec_from_file_location("backend._legacy_artifacts", _legacy_path)
if _legacy_spec and _legacy_spec.loader:
    _legacy = importlib.util.module_from_spec(_legacy_spec)
    _legacy_spec.loader.exec_module(_legacy)
    allocate_document_id = _legacy.allocate_document_id
    write_requirement = _legacy.write_requirement
    write_prompt = _legacy.write_prompt
    write_example_code = _legacy.write_example_code
    write_diagnosis = _legacy.write_diagnosis
    write_feedback = _legacy.write_feedback

__all__ = [
    "render_diagnosis_document", "render_requirement_document", "validate_artifact",
    "allocate_document_id", "write_requirement", "write_prompt", "write_example_code",
    "write_diagnosis", "write_feedback",
]
