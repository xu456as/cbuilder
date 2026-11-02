"""Rogue Agent Web 控制台。

- 配置 N / 上传领域知识文档 / 指定被测 agent endpoint 与 token / 触发测试
- 触发模式（环境变量 ROGUE_EXECUTION_MODE）:
    local : 控制台进程内后台执行（本地开发/调试）
    k8s   : 通过 k8s API 创建一次性 Job（pipeline 生产模式），跑完资源自动回收
- 测试结果从 Elasticsearch 查询展示
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse

load_dotenv()  # 本地调试: 从 .env 读取配置
# rogue.* 日志直接打到 stderr; 设 ROGUE_LOG_LEVEL=DEBUG 可看完整对话 trace
logging.basicConfig(
    level=os.getenv("ROGUE_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

from ..config import MAX_DOC_SINGLE_CHARS, TestConfig
from ..es_store import EvalCaseStore
from ..runner import run_rogue_test

logger = logging.getLogger("rogue.console")

STATIC_DIR = Path(__file__).parent / "static"
MAX_DOC_FILE_BYTES = 2 * 1024 * 1024  # 单个文档上传上限 2MB

app = FastAPI(title="Rogue Agent Console")

# 本地执行模式的运行注册表：run_id -> 概要信息
RUNS: dict[str, dict] = {}
_background_tasks: set[asyncio.Task] = set()
_store: EvalCaseStore | None = None


async def get_store() -> EvalCaseStore | None:
    global _store
    if _store is None and os.getenv("ES_ENDPOINT"):
        _store = EvalCaseStore()
        await _store.ensure_index()
    return _store


# ------------------------------------------------------------------ 页面

@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


# ------------------------------------------------------------------ 提交测试

@app.post("/api/runs")
async def create_run(
    n_turns: int = Form(...),
    target_endpoint: str = Form(...),
    target_api_token: str = Form(""),
    target_model: str = Form(""),
    strategy_hint: str = Form(""),
    execution_mode: str = Form(""),
    docs: list[UploadFile] = File(default=[]),
):
    # 读取领域知识文档
    domain_docs = []
    for f in docs or []:
        if not f.filename:
            continue
        raw = await f.read()
        if len(raw) > MAX_DOC_FILE_BYTES:
            raise HTTPException(400, f"文档 {f.filename} 超过 2MB 上限")
        domain_docs.append(
            {"name": f.filename, "content": raw.decode("utf-8", errors="replace")}
        )

    try:
        config = TestConfig(
            n_turns=n_turns,
            target_endpoint=target_endpoint.strip(),
            target_api_token=target_api_token.strip(),
            target_model=target_model.strip(),
            strategy_hint=strategy_hint.strip(),
            domain_docs=[_mk_doc(d) for d in domain_docs],
        )
        config.validate()
    except ValueError as err:
        raise HTTPException(400, str(err))

    run_id = uuid.uuid4().hex[:12]
    mode = execution_mode or os.getenv("ROGUE_EXECUTION_MODE", "local")

    if mode == "k8s":
        job_name = _submit_k8s_job(run_id, config)
        RUNS[run_id] = {
            "run_id": run_id, "status": "job_submitted", "mode": "k8s",
            "job": job_name, "n_turns": config.n_turns, "created_at": _now(),
        }
    else:
        RUNS[run_id] = {
            "run_id": run_id, "status": "running", "mode": "local",
            "n_turns": config.n_turns, "created_at": _now(),
        }
        task = asyncio.create_task(_execute_local(run_id, config))
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)

    return {"run_id": run_id, "mode": mode, "status": RUNS[run_id]["status"]}


def _mk_doc(d: dict):
    from ..config import DomainDoc

    return DomainDoc(name=d["name"], content=d["content"])


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


async def _execute_local(run_id: str, config: TestConfig):
    store = await get_store()
    try:
        doc = await run_rogue_test(config, store=store, run_id=run_id)
        RUNS[run_id]["status"] = doc["status"]
        RUNS[run_id]["category"] = doc.get("verdict", {}).get("category", "")
        RUNS[run_id]["is_inappropriate"] = doc.get("verdict", {}).get("is_inappropriate")
        # ES 未配置时完整结果(含对话 trace)仅存内存，供详情页展示
        RUNS[run_id]["_result"] = doc
    except Exception as err:
        logger.exception("local run %s crashed", run_id)
        RUNS[run_id]["status"] = "failed"
        RUNS[run_id]["error"] = str(err)


# ------------------------------------------------------------------ k8s Job 提交

def _submit_k8s_job(run_id: str, config: TestConfig) -> str:
    """用 k8s API 创建一次性 Job，配置经 ROGUE_CONFIG_JSON 注入。"""
    try:
        from kubernetes import client as k8s_client
        from kubernetes import config as k8s_config
    except ImportError:
        raise HTTPException(
            501, "k8s 触发模式需要安装 kubernetes 依赖 (pip install kubernetes)"
        )

    k8s_config.load_incluster_config()
    batch = k8s_client.BatchV1Api()

    job_name = f"rogue-agent-{run_id}"
    envs = [
        k8s_client.V1EnvVar(
            name="ROGUE_CONFIG_JSON", value=json.dumps(config.to_payload(), ensure_ascii=False)
        ),
        k8s_client.V1EnvVar(name="ROGUE_RUN_ID", value=run_id),
    ]
    # Google API key 从 Secret 注入
    secret_name = os.getenv("ROGUE_SECRET_NAME", "rogue-secrets")
    for key in ("GOOGLE_API_KEY", "EVAL_GENERATOR_MODEL", "ES_ENDPOINT", "ES_API_KEY",
                "ES_USERNAME", "ES_PASSWORD", "ES_INDEX_PREFIX", "ES_INSECURE"):
        if key == "EVAL_GENERATOR_MODEL" and os.getenv(key):
            envs.append(k8s_client.V1EnvVar(name=key, value=os.getenv(key)))
        elif key.startswith("ES") and os.getenv(key):
            envs.append(k8s_client.V1EnvVar(name=key, value=os.getenv(key)))
        elif key == "GOOGLE_API_KEY":
            envs.append(k8s_client.V1EnvVar(
                name=key,
                value_from=k8s_client.V1EnvVarSource(
                    secret_key_ref=k8s_client.V1SecretKeySelector(name=secret_name, key=key)
                ),
            ))

    job = k8s_client.V1Job(
        metadata=k8s_client.V1ObjectMeta(name=job_name, labels={"app": "rogue-agent"}),
        spec=k8s_client.V1JobSpec(
            backoff_limit=0,
            ttl_seconds_after_finished=int(os.getenv("ROGUE_JOB_TTL_SECONDS", "3600")),
            template=k8s_client.V1PodTemplateSpec(
                spec=k8s_client.V1PodSpec(
                    restart_policy="Never",
                    containers=[
                        k8s_client.V1Container(
                            name="rogue-agent",
                            image=os.getenv("ROGUE_JOB_IMAGE", "rogue-agent:latest"),
                            command=["python", "-m", "rogue.cli"],
                            env=envs,
                        )
                    ],
                )
            ),
        ),
    )
    namespace = os.getenv("ROGUE_NAMESPACE", "default")
    batch.create_namespaced_job(namespace=namespace, body=job)
    return job_name


# ------------------------------------------------------------------ 查询

@app.get("/api/runs")
async def list_runs():
    items: dict[str, dict] = {}

    store = await get_store()
    if store is not None:
        for case in await store.list_cases():
            verdict = case.get("verdict") or {}
            items[case["run_id"]] = {
                "run_id": case["run_id"],
                "status": case.get("status"),
                "created_at": case.get("created_at"),
                "n_turns": case.get("n_turns"),
                "target_endpoint": (case.get("target") or {}).get("endpoint", ""),
                "category": verdict.get("category", ""),
                "is_inappropriate": verdict.get("is_inappropriate"),
                "final_question": case.get("final_question", ""),
            }

    for run_id, info in RUNS.items():  # 本地注册表补充 running / failed 状态
        items.setdefault(
            run_id,
            {"run_id": run_id, **{k: v for k, v in info.items() if not k.startswith("_")}},
        )

    return sorted(items.values(), key=lambda x: x.get("created_at") or "", reverse=True)


@app.get("/api/runs/{run_id}")
async def get_run(run_id: str):
    store = await get_store()
    if store is not None:
        case = await store.get_case(run_id)
        if case is not None:
            return case
    if run_id in RUNS:
        info = dict(RUNS[run_id])
        result = info.pop("_result", None)
        if result:  # 本地模式(无 ES): 返回内存中的完整结果
            return {**result, **info, "run_id": run_id}
        return {"run_id": run_id, **info}
    raise HTTPException(404, f"run {run_id} 不存在")


@app.on_event("shutdown")
async def _shutdown():
    if _store is not None:
        await _store.close()


def serve(host: str = "0.0.0.0", port: int = 4080):
    import uvicorn

    uvicorn.run(app, host=host, port=port)
