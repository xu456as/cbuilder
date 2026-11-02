"""测试配置：支持 JSON 文件加载（k8s Job 场景）与控制台构造。"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

MAX_N_TURNS = 20

# 领域文档注入 prompt 时的总量上限，防止撑爆上下文
MAX_DOC_TOTAL_CHARS = 24000
MAX_DOC_SINGLE_CHARS = 8000


@dataclass
class DomainDoc:
    name: str
    content: str


@dataclass
class TestConfig:
    n_turns: int = 3  # 前 N 轮合情合理的正常问题，N 取值 0..20
    target_endpoint: str = ""  # 被测 agent 的 OpenAI 兼容 endpoint
    target_api_token: str = ""  # Bearer token
    target_model: str = ""  # chat completions 请求体里的 model 名，可为空
    strategy_hint: str = ""  # 操作者给 rogue agent 的额外策略提示
    domain_docs: list[DomainDoc] = field(default_factory=list)
    target_timeout_seconds: float = 120.0

    def validate(self) -> None:
        if not (0 <= self.n_turns <= MAX_N_TURNS):
            raise ValueError(f"n_turns 必须在 0..{MAX_N_TURNS} 之间, 当前: {self.n_turns}")
        if not self.target_endpoint:
            raise ValueError("target_endpoint 不能为空")

    # ---------- 文档裁剪 ----------
    def truncated_docs(self) -> list[DomainDoc]:
        """超长文档做头尾截断，总量超限则按比例裁剪。"""
        trimmed: list[DomainDoc] = []
        budget = MAX_DOC_TOTAL_CHARS
        for doc in self.domain_docs:
            if budget <= 0:
                break
            limit = min(MAX_DOC_SINGLE_CHARS, budget)
            text = doc.content
            if len(text) > limit:
                keep_head = int(limit * 0.75)
                keep_tail = limit - keep_head
                text = text[:keep_head] + "\n...[截断]...\n" + text[-keep_tail:]
            budget -= len(text)
            trimmed.append(DomainDoc(name=doc.name, content=text))
        return trimmed

    def docs_prompt(self) -> str:
        docs = self.truncated_docs()
        if not docs:
            return "(未提供领域知识文档)"
        blocks = [f"### {d.name}\n{d.content}" for d in docs]
        return "\n\n".join(blocks)

    # ---------- 序列化 ----------
    def to_payload(self) -> dict:
        """完整 payload（含 token，仅用于注入 Job 环境变量/内部传递）。"""
        return {
            "n_turns": self.n_turns,
            "target_endpoint": self.target_endpoint,
            "target_api_token": self.target_api_token,
            "target_model": self.target_model,
            "strategy_hint": self.strategy_hint,
            "target_timeout_seconds": self.target_timeout_seconds,
            "domain_docs": [{"name": d.name, "content": d.content} for d in self.domain_docs],
        }

    def to_masked(self) -> dict:
        payload = self.to_payload()
        if payload["target_api_token"]:
            payload["target_api_token"] = "***masked***"
        return payload

    @classmethod
    def from_payload(cls, data: dict) -> "TestConfig":
        cfg = cls(
            n_turns=int(data.get("n_turns", 3)),
            target_endpoint=data.get("target_endpoint", ""),
            target_api_token=data.get("target_api_token", ""),
            target_model=data.get("target_model", ""),
            strategy_hint=data.get("strategy_hint", ""),
            target_timeout_seconds=float(data.get("target_timeout_seconds", 120.0)),
        )
        cfg.domain_docs = [
            DomainDoc(name=d.get("name", "doc"), content=d.get("content", ""))
            for d in data.get("domain_docs", [])
        ]
        return cfg

    @classmethod
    def from_json_file(cls, path: str | Path) -> "TestConfig":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.from_payload(data)

    @classmethod
    def from_env_json(cls, var: str = "ROGUE_CONFIG_JSON") -> "TestConfig":
        raw = os.getenv(var)
        if not raw:
            raise ValueError(f"环境变量 {var} 未设置，且未提供 --config 文件")
        return cls.from_payload(json.loads(raw))

    @classmethod
    def from_env(cls) -> "TestConfig":
        """从逐项环境变量构建（k8s/本地 .env 皆适用）。

        ROGUE_N_TURNS / ROGUE_TARGET_ENDPOINT / ROGUE_TARGET_API_TOKEN /
        ROGUE_TARGET_MODEL / ROGUE_STRATEGY_HINT
        """
        return cls(
            n_turns=int(os.getenv("ROGUE_N_TURNS", "3")),
            target_endpoint=os.getenv("ROGUE_TARGET_ENDPOINT", ""),
            target_api_token=os.getenv("ROGUE_TARGET_API_TOKEN", ""),
            target_model=os.getenv("ROGUE_TARGET_MODEL", ""),
            strategy_hint=os.getenv("ROGUE_STRATEGY_HINT", ""),
        )
