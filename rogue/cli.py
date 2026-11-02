"""一次性 Job 入口：k8s pipeline 拉起 -> 完整 N+1 测试 -> 写 ES -> 进程退出。

用法:
    python -m rogue.cli --config /etc/rogue/config.json
    # 或通过环境变量注入完整 JSON 配置
    ROGUE_CONFIG_JSON='{...}' python -m rogue.cli
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import uuid

from dotenv import load_dotenv

from .config import TestConfig
from .es_store import EvalCaseStore
from .runner import run_rogue_test


async def _main_async(config: TestConfig, run_id: str) -> int:
    store = EvalCaseStore.from_env()
    try:
        doc = await run_rogue_test(config, store=store, run_id=run_id)
    finally:
        if store is not None:
            await store.close()

    print(json.dumps(doc, ensure_ascii=False, indent=2))
    # 测试判定为"不恰当"属于测试成功（用例价值），只有 infra 失败才返回非零
    return 0 if doc["status"] == "completed" else 1


def main() -> int:
    load_dotenv()
    logging.basicConfig(
        level=os.getenv("ROGUE_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    parser = argparse.ArgumentParser(description="Rogue Agent 一次性测评任务")
    parser.add_argument("--config", help="测试配置 JSON 文件路径")
    parser.add_argument("--run-id", help="可选，指定 run_id（默认自动生成）")
    args = parser.parse_args()

    if args.config:
        config = TestConfig.from_json_file(args.config)
    elif os.getenv("ROGUE_CONFIG_JSON"):
        config = TestConfig.from_env_json()
    else:
        config = TestConfig.from_env()  # 逐项环境变量（.env 已由 load_dotenv 注入）
    config.validate()

    run_id = args.run_id or os.getenv("ROGUE_RUN_ID") or uuid.uuid4().hex[:12]
    return asyncio.run(_main_async(config, run_id))


if __name__ == "__main__":
    sys.exit(main())
