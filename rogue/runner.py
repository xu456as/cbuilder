"""N+1 生命周期编排器：rogue agent 的主流程。

N 轮正常对话（侦察） -> 1 轮诱导问题 -> judge 第一印象判定 -> 落库 ES。
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone

from .agents import RogueAgents
from .config import TestConfig
from .es_store import EvalCaseStore
from .target_client import TargetAgentClient

logger = logging.getLogger("rogue.runner")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def run_rogue_test(
    config: TestConfig,
    store: EvalCaseStore | None = None,
    run_id: str | None = None,
) -> dict:
    """执行完整的 N+1 测试用例，返回 ES 文档格式的结果。"""
    config.validate()
    run_id = run_id or uuid.uuid4().hex[:12]
    agents = RogueAgents()
    client = TargetAgentClient(
        endpoint=config.target_endpoint,
        api_token=config.target_api_token,
        model=config.target_model,
        timeout_seconds=config.target_timeout_seconds,
    )
    docs_prompt = config.docs_prompt()

    conversation: list[dict] = []  # 与 examples/message.json 相同的 role/content 格式
    plan_trace: list[dict] = []
    recon: dict = {}
    final_question = ""
    error = ""
    status = "running"
    verdict: dict = {}

    def _log(msg: str):
        logger.info("[run %s] %s", run_id, msg)

    def _dbg(msg: str):
        logger.debug("[run %s] %s", run_id, msg)

    try:
        # ---------- 前 N 轮：合情合理的问题 + 侦察 ----------
        for i in range(1, config.n_turns + 1):
            plan = await agents.plan_benign_question(
                docs_prompt, config.strategy_hint, conversation, i, config.n_turns
            )
            question = plan["question"]
            plan_trace.append({"phase": "recon", "round": i, **plan})
            _log(f"recon round {i}/{config.n_turns}: {question[:80]}...")

            conversation.append({"role": "user", "content": question})
            answer = await client.chat(conversation)
            conversation.append({"role": "assistant", "content": answer})
            _log(f"target answered ({len(answer)} chars)")
            _dbg(f"recon round {i} full Q&A:\n[U] {question}\n[T] {answer}")

        # ---------- 最后一轮：诱导问题 ----------
        attack = await agents.plan_attack_question(docs_prompt, config.strategy_hint, conversation)
        recon = attack.get("target_profile", {})
        final_question = attack["question"]
        plan_trace.append({"phase": "attack", **attack})
        _log(f"attack question: {final_question[:80]}...")

        conversation.append({"role": "user", "content": final_question})
        answer = await client.chat(conversation)
        conversation.append({"role": "assistant", "content": answer})
        _log(f"target answered inducement ({len(answer)} chars)")
        _dbg(f"inducement full Q&A:\n[U] {final_question}\n[T] {answer}")

        # ---------- 第一印象判定 ----------
        verdict = await agents.judge_conversation(conversation)
        status = "completed"
        _log(f"verdict: is_inappropriate={verdict.get('is_inappropriate')} "
             f"category={verdict.get('category')}")
    except Exception as err:  # 中途失败也要留下现场，便于 k8s 排障
        status = "failed"
        error = f"{type(err).__name__}: {err}"
        logger.exception("run %s failed", run_id)

    doc = {
        "run_id": run_id,
        "created_at": _now_iso(),
        "status": status,
        "error": error,
        "n_turns": config.n_turns,
        "strategy_hint": config.strategy_hint,
        "domain_docs": [{"name": d.name, "chars": len(d.content)} for d in config.domain_docs],
        "target": {
            "endpoint": config.target_endpoint,
            "model": config.target_model,
        },
        "recon": recon,
        "final_question": final_question,
        "plan_trace": plan_trace,
        "conversation": conversation,  # 格式同 examples/message.json
        "verdict": verdict if status == "completed" else {},
    }

    # DEBUG 级别输出完整结果（含对话 trace 全文），便于本地调试
    _dbg("完整结果文档(含对话trace):\n" + json.dumps(doc, ensure_ascii=False, indent=2))

    if store is not None:
        await store.ensure_index()
        await store.save_case(doc)
        _log("case saved to elasticsearch")
    else:
        _log("ES 未配置，结果仅返回不落库")

    return doc
