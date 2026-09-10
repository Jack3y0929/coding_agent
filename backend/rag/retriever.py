from __future__ import annotations

import logging
from typing import Any

import numpy as np

from backend.db.models import Database
from backend.rag.embedding import get_embedding_engine
from backend.project_scope import real_project_path

logger = logging.getLogger("rag.retriever")


# ---- 余弦相似度 ----

def cosine_similarity(vec_a: np.ndarray, vec_b: np.ndarray) -> float:
    """计算两个向量的余弦相似度"""
    dot_product = np.dot(vec_a, vec_b)
    norm_a = np.linalg.norm(vec_a)
    norm_b = np.linalg.norm(vec_b)
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return float(dot_product / (norm_a * norm_b))


# ---- RRF 融合排序 ----

def rrf_fusion(
    keyword_ranked: list[dict],
    semantic_ranked: list[dict],
    k: int = 60
) -> list[dict]:
    """
    Reciprocal Rank Fusion: 融合关键词和语义两路检索结果
    k=60 是常用常数
    """
    scores: dict[int, float] = {}
    merged_ids: dict[int, int] = {}  # id → rank position for tiebreaking

    for rank, item in enumerate(keyword_ranked):
        item_id = item["id"]
        scores[item_id] = scores.get(item_id, 0) + 1 / (k + rank + 1)
        merged_ids[item_id] = rank

    for rank, item in enumerate(semantic_ranked):
        item_id = item["id"]
        scores[item_id] = scores.get(item_id, 0) + 1 / (k + rank + 1)
        if item_id not in merged_ids:
            merged_ids[item_id] = rank

    # 按融合分数降序排列，同分按较早出现的位置
    sorted_ids = sorted(scores.items(), key=lambda x: (-x[1], merged_ids.get(x[0], 999)))

    # 重建结果列表
    id_to_item = {}
    for item in keyword_ranked + semantic_ranked:
        if item["id"] not in id_to_item:
            id_to_item[item["id"]] = item

    return [id_to_item[item_id] for item_id, _ in sorted_ids]


# ---- 混合检索入口 ----

async def hybrid_retrieve(
    query: str,
    top_k: int = 5,
    source_type: str | None = None,
    project_path: str | None = None,
    db_path: str | None = None,
) -> list[dict[str, Any]]:
    """
    混合检索：关键词搜索 + 语义搜索 → RRF融合 → 返回top_k条

    Args:
        query: 查询文本
        top_k: 返回结果数
        source_type: 限定来源类型 (file/memory/error/spec)，None表示不限
    """
    engine = get_embedding_engine()
    if project_path is not None:
        try:
            project_path = real_project_path(project_path)
        except ValueError:
            return []

    keyword_results = await _keyword_search(query, source_type=source_type, top_k=top_k * 2, project_path=project_path, db_path=db_path)

    query_vec = engine.encode(query)
    semantic_results = await _semantic_search(query_vec, source_type=source_type, top_k=top_k * 2, project_path=project_path, db_path=db_path)

    fused = rrf_fusion(keyword_results, semantic_results)
    results = fused[:top_k]
    if not results:
        try:
            from backend.trace import record_event
            await record_event("rag_miss", "rag", {"project_path": project_path or "", "query": query[:200]})
        except Exception:
            logger.debug("RAG miss Trace 记录失败", exc_info=True)
    return results


async def hybrid_retrieve_for_role(
    query: str,
    role: str,
    top_k: int = 5,
    project_path: str | None = None,
    db_path: str | None = None,
) -> str:
    """
    根据角色进行混合检索，返回格式化的上下文字符串。
    角色 → 检索策略：
    - analyst: 历史任务 (memory) top 3
    - developer: 源码 (file) top 8 + 历史任务 top 2
    - reviewer/review_*: 规范 (spec) top 5 + 历史错误 top 3
    - debugger: 源码 top 8 + 历史错误 top 3
    """
    if role == "analyst":
        results = await hybrid_retrieve(query, top_k=3, source_type="memory", project_path=project_path, db_path=db_path)
    elif role in ("developer", "fix"):
        code_results = await hybrid_retrieve(query, top_k=8, source_type="file", project_path=project_path, db_path=db_path)
        mem_results = await hybrid_retrieve(query, top_k=2, source_type="memory", project_path=project_path, db_path=db_path)
        results = code_results + mem_results
    elif role in ("reviewer", "review_logic", "review_security", "review_quality"):
        results = await hybrid_retrieve(query, top_k=5, source_type="spec", project_path=project_path, db_path=db_path)
        # 同时检索错误模式库
        error_results = await hybrid_retrieve(query, top_k=3, source_type="error", project_path=project_path, db_path=db_path)
        results = rrf_fusion(results, error_results)[:top_k]
    elif role == "debugger":
        code_results = await hybrid_retrieve(query, top_k=8, source_type="file", project_path=project_path, db_path=db_path)
        error_results = await hybrid_retrieve(query, top_k=3, source_type="error", project_path=project_path, db_path=db_path)
        results = rrf_fusion(code_results, error_results)[:top_k]
    else:
        results = await hybrid_retrieve(query, top_k=top_k, project_path=project_path, db_path=db_path)

    return _format_rag_context(results)


def _format_rag_context(results: list[dict[str, Any]]) -> str:
    """格式化 RAG 检索结果为 prompt 可注入文本"""
    if not results:
        return "（无相关上下文）"

    lines = ["--- RAG 检索上下文 ---"]
    for i, item in enumerate(results, 1):
        src = item.get("source_path", "unknown")
        content = item.get("content", "")
        # 截断过长内容
        if len(content) > 1500:
            content = content[:1500] + "\n...（内容已截断）"
        lines.append(f"\n[{i}] 项目: {item.get('project_path', '')} 来源: {src} "
                     f"符号: {item.get('symbol') or '-'} 哈希: {item.get('file_hash') or '-'}")
        lines.append(content)
    lines.append("--- RAG 检索上下文结束 ---")
    return "\n".join(lines)


# ---- 内部检索函数 ----

async def _keyword_search(query: str, source_type: str | None = None,
                          top_k: int = 20, project_path: str | None = None,
                          db_path: str | None = None) -> list[dict]:
    """关键词检索：SQL LIKE模糊匹配"""
    async with (Database(db_path) if db_path else Database()) as db:
        results = await db.keyword_search_rag(query, source_type=source_type, limit=top_k, project_path=project_path)
    return [{
        "id": r["id"],
        "content": r["content"],
        "source_type": r["source_type"],
        "source_path": r["source_path"],
        "project_path": r.get("project_path", ""), "file_hash": r.get("file_hash"),
        "symbol": r.get("symbol"),
        "chunk_index": r.get("chunk_index", 0),
    } for r in results]


async def _semantic_search(query_vec: np.ndarray, source_type: str | None = None,
                           top_k: int = 20, project_path: str | None = None,
                           db_path: str | None = None) -> list[dict]:
    """语义检索：加载所有向量，计算余弦相似度，取top_k"""
    async with (Database(db_path) if db_path else Database()) as db:
        all_embeddings = await db.get_all_rag_embeddings(source_type=source_type, project_path=project_path)

    if source_type:
        all_embeddings = [e for e in all_embeddings if e.get("source_type") == source_type]

    if not all_embeddings:
        return []

    scored = []
    for item in all_embeddings:
        emb_data = item.get("embedding")
        if emb_data is None:
            continue
        if len(emb_data) % np.dtype(np.float32).itemsize:
            logger.warning("跳过损坏的 RAG 向量: id=%s", item.get("id"))
            continue
        stored_vec = np.frombuffer(emb_data, dtype=np.float32)
        sim = cosine_similarity(query_vec, stored_vec)
        scored.append({
            "id": item["id"],
            "content": item["content"],
            "source_type": item["source_type"],
            "source_path": item["source_path"],
            "project_path": item.get("project_path", ""), "file_hash": item.get("file_hash"),
            "symbol": item.get("symbol"),
            "chunk_index": item.get("chunk_index", 0),
            "score": sim,
        })

    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:top_k]


# ---- 规范库初始化 ----

RULE_ENTRIES = [
    ("spec", "Plan/Build模式严格分离",
     "Plan模式下严禁写文件或执行修改性shell命令。Build模式下写文件需路径校验，shell命令需白名单校验。"
     "Agent自主判断切换时机：Plan模式规划充分后通过save_checkpoint工具请求切换。"),
    ("spec", "角色上下文隔离",
     "每个LangGraph节点必须使用独立对话上下文。节点间仅传递正式产出文档，禁止传递对话历史。"
     "每个角色通过RAG独立检索所需上下文。"),
    ("spec", "Token预算硬上限",
     "单次工作流总费用不得超过5元（约100万token）。每次AI API调用前后必须预估和累加token消耗。"
     "超限时立即中断当前节点，输出已完成总结，不静默失败。"),
    ("spec", "Shell命令白名单",
     "Agent执行的任何shell命令必须匹配预定义白名单。白名单仅包含编译/检查/读取类命令。"
     "不在白名单内的命令拒绝执行并记录日志。"),
    ("spec", "写文件路径校验",
     "write_file操作必须在项目源码目录内，禁止写入系统目录或非项目配置文件。"
     "写入前检查目标路径是否在允许的目录范围内。"),
    ("spec", "RAG索引增量更新",
     "每次工作流完成后必须增量更新代码索引，只重新索引变更的文件。禁止全量重建索引。"),
    ("spec", "审查轮次强制",
     "DEV工作流至少1轮代码审查，DEBUG工作流至少2轮。第1轮侧重逻辑，第2轮侧重规范。"
     "审查不通过时自动触发修复循环，最多3次。"),
    ("spec", "最终验收必须人工",
     "代码产出后必须进入HUMAN_ACCEPT节点等待人工确认。严禁Agent自动提交代码。"
     "人工验收不通过时回到修复循环。"),
]


async def init_spec_library() -> None:
    """初始化规范库：将8条红线规则写入RAG索引"""
    engine = get_embedding_engine()
    async with Database() as db:
        for source_type, title, content in RULE_ENTRIES:
            existing = await db.keyword_search_rag(title, source_type="spec", limit=1)
            if existing:
                continue  # 已存在则跳过

            emb_bytes = engine.encode_to_bytes(content)
            await db.insert_rag_embedding(
                source_type=source_type,
                source_path=f"spec://{title}",
                chunk_index=0,
                content=f"【{title}】{content}",
                embedding=emb_bytes,
                token_count=len(content),
            )
    logger.info("规范库初始化完成")
