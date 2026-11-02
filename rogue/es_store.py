"""Elasticsearch 存储层：对话 trace + 第一印象判定结果落库。"""

from __future__ import annotations

import os
from datetime import datetime, timezone

INDEX_SUFFIX = "-cases"


def es_enabled() -> bool:
    return bool(os.getenv("ES_ENDPOINT") or os.getenv("ES_URL"))


def index_name() -> str:
    return os.getenv("ES_INDEX_PREFIX", "rogue-eval") + INDEX_SUFFIX


class EvalCaseStore:
    def __init__(self):
        from elasticsearch import AsyncElasticsearch

        url = os.getenv("ES_ENDPOINT") or os.getenv("ES_URL")
        kwargs: dict = {"request_timeout": 30}
        if api_key := os.getenv("ES_API_KEY"):
            kwargs["api_key"] = api_key
        elif os.getenv("ES_USERNAME"):
            kwargs["basic_auth"] = (os.getenv("ES_USERNAME"), os.getenv("ES_PASSWORD", ""))
        if os.getenv("ES_INSECURE", "").lower() in {"1", "true"}:
            kwargs["verify_certs"] = False
            kwargs["ssl_show_warn"] = False
        self._es = AsyncElasticsearch(hosts=[url], **kwargs)
        self._index = index_name()

    @classmethod
    def from_env(cls) -> "EvalCaseStore | None":
        return cls() if es_enabled() else None

    async def ensure_index(self) -> None:
        if await self._es.indices.exists(index=self._index):
            return
        await self._es.indices.create(
            index=self._index,
            settings={"number_of_shards": 1, "number_of_replicas": 0},
            mappings={
                "properties": {
                    "run_id": {"type": "keyword"},
                    "created_at": {"type": "date"},
                    "status": {"type": "keyword"},
                    "error": {"type": "text"},
                    "n_turns": {"type": "integer"},
                    "final_question": {"type": "text"},
                    "target.endpoint": {"type": "keyword"},
                    "target.model": {"type": "keyword"},
                    "strategy_hint": {"type": "text"},
                    "conversation": {"type": "object", "enabled": True},
                    "plan_trace": {"type": "object", "enabled": True},
                    "recon": {"type": "object", "enabled": True},
                    "verdict.is_inappropriate": {"type": "boolean"},
                    "verdict.category": {"type": "keyword"},
                    "verdict.confidence": {"type": "float"},
                    "verdict.evidence": {"type": "text"},
                    "verdict.first_impression": {"type": "text"},
                    "verdict.reasoning": {"type": "text"},
                }
            },
        )

    async def save_case(self, doc: dict) -> str:
        doc.setdefault("created_at", datetime.now(timezone.utc).isoformat())
        resp = await self._es.index(index=self._index, document=doc, id=doc.get("run_id"))
        await self._es.indices.refresh(index=self._index)
        return resp["_id"]

    async def list_cases(self, size: int = 50) -> list[dict]:
        resp = await self._es.search(
            index=self._index,
            query={"match_all": {}},
            sort=[{"created_at": {"order": "desc"}}],
            size=size,
        )
        return [
            {"_id": hit["_id"], **hit["_source"]}
            for hit in resp["hits"]["hits"]
        ]

    async def get_case(self, run_id: str) -> dict | None:
        try:
            resp = await self._es.get(index=self._index, id=run_id)
        except Exception:
            return None
        return {"_id": resp["_id"], **resp["_source"]}

    async def close(self) -> None:
        await self._es.close()
