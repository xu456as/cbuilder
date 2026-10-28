"""eval_case_generator 的工具：校验、保存、列出 ADK EvalSet 文件。

EvalSet 文件保存在项目根目录 eval_data/ 下，命名遵循 ADK 约定
`<eval_set_id>.evalset.json`，可直接被 `adk eval` 消费。

Schema 对齐 google-adk 2.x：顶层字段为 `eval_cases`，工具轨迹放在
`intermediate_data.tool_uses` / `tool_responses`，最终回复是直接的 Content。
"""

import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List

EVAL_DATA_DIR = Path(__file__).resolve().parent.parent / "eval_data"

# ADK 对各类 id 的硬性要求：^[a-zA-Z0-9_]+$
_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_]+$")
_ID_SANITIZE = re.compile(r"[^a-zA-Z0-9_]")


def _adk_eval_set_cls():
    """返回 ADK 的 EvalSet Pydantic 模型类，不可用时返回 None。"""
    try:
        from google.adk.evaluation.eval_set import EvalSet

        return EvalSet
    except Exception:  # pragma: no cover - 环境无 adk 时降级为仅基础校验
        return None


def _sanitize_ids(data: Dict[str, Any]) -> None:
    """原地把各类 id 规范为 [a-zA-Z0-9_]（LLM 常见错误是使用连字符）。"""
    for key in ("eval_set_id",):
        value = data.get(key)
        if isinstance(value, str) and not _ID_PATTERN.match(value):
            data[key] = _ID_SANITIZE.sub("_", value)

    for case in data.get("eval_cases") or []:
        if isinstance(case.get("eval_id"), str) and not _ID_PATTERN.match(case["eval_id"]):
            case["eval_id"] = _ID_SANITIZE.sub("_", case["eval_id"])
        for invocation in case.get("conversation") or []:
            if isinstance(invocation.get("invocation_id"), str) and not _ID_PATTERN.match(
                invocation["invocation_id"]
            ):
                invocation["invocation_id"] = _ID_SANITIZE.sub("_", invocation["invocation_id"])
            intermediate = invocation.get("intermediate_data") or {}
            for entry in (intermediate.get("tool_uses") or []) + (
                intermediate.get("tool_responses") or []
            ):
                if isinstance(entry.get("id"), str) and not _ID_PATTERN.match(entry["id"]):
                    entry["id"] = _ID_SANITIZE.sub("_", entry["id"])


def _basic_checks(data: Dict[str, Any]) -> List[str]:
    """ADK 模型之外的实用规则校验，返回问题列表（不抛异常）。"""
    problems = []
    if not isinstance(data, dict):
        return ["eval_set_json must be a JSON object"]

    cases = data.get("eval_cases")
    if not isinstance(cases, list) or not cases:
        problems.append('eval_cases must be a non-empty list (note: the key is "eval_cases", not "cases")')
        return problems

    seen_eval_ids, seen_inv_ids = set(), set()
    for i, case in enumerate(cases):
        eval_id = case.get("eval_id") or f"<case_{i}>"
        if eval_id in seen_eval_ids:
            problems.append(f"duplicate eval_id: {eval_id}")
        seen_eval_ids.add(eval_id)

        conversation = case.get("conversation")
        if not isinstance(conversation, list) or not conversation:
            problems.append(f"case {eval_id}: conversation must be a non-empty list")
            continue

        for j, invocation in enumerate(conversation):
            inv_id = invocation.get("invocation_id") or f"<{eval_id}_turn_{j}>"
            if inv_id in seen_inv_ids:
                problems.append(f"duplicate invocation_id: {inv_id}")
            seen_inv_ids.add(inv_id)

            user_content = invocation.get("user_content")
            if not (isinstance(user_content, dict) and user_content.get("parts")):
                problems.append(f"{inv_id}: user_content must have parts")

            # final_response 是直接的 Content 对象：{"role": "model", "parts": [...]}
            final_response = invocation.get("final_response")
            if final_response is not None:
                text = None
                if isinstance(final_response, dict):
                    parts = final_response.get("parts") or []
                    if parts and isinstance(parts[0], dict):
                        text = parts[0].get("text")
                if not text:
                    problems.append(f"{inv_id}: final_response.parts[0].text is missing")

            intermediate = invocation.get("intermediate_data") or {}
            tool_uses = intermediate.get("tool_uses") or []
            tool_responses = intermediate.get("tool_responses") or []

            use_ids = []
            for k, tool in enumerate(tool_uses):
                tag = f"{inv_id}/tool_uses[{k}]"
                if not tool.get("name"):
                    problems.append(f"{tag}: missing name")
                if not isinstance(tool.get("args"), dict):
                    problems.append(f"{tag}: args must be an object")
                if tool.get("id"):
                    use_ids.append(tool["id"])

            responded = set()
            for k, tool in enumerate(tool_responses):
                tag = f"{inv_id}/tool_responses[{k}]"
                if not tool.get("name"):
                    problems.append(f"{tag}: missing name")
                if not isinstance(tool.get("response"), dict):
                    problems.append(f"{tag}: response must be an object (wrap primitives)")
                if tool.get("id"):
                    responded.add(tool["id"])

            for tool_id in use_ids:
                if tool_id not in responded:
                    problems.append(f"{inv_id}: tool use id={tool_id} has no matching tool_response")

    return problems


def validate_eval_set(eval_set_json: Dict[str, Any]) -> Dict[str, Any]:
    """校验 EvalSet JSON 是否符合 ADK Schema 及基本质量规则。

    Args:
        eval_set_json: 完整的 EvalSet JSON 对象。

    Returns:
        {"status": "ok"|"error", "errors": [...]}
    """
    errors = _basic_checks(eval_set_json)

    cls = _adk_eval_set_cls()
    if cls is not None:
        try:
            cls.model_validate(eval_set_json)
        except Exception as e:
            errors.append(f"ADK schema validation failed: {str(e)[:800]}")

    return {"status": "ok" if not errors else "error", "errors": errors}


def save_eval_set(eval_set_json: Dict[str, Any], filename: str) -> Dict[str, Any]:
    """校验并将 EvalSet JSON 保存为 `<name>.evalset.json`，供 `adk eval` 使用。

    id 中的非法字符会被自动替换为下划线；保存内容经 ADK 模型规范化，
    与 `adk web` 评测编辑器落盘格式一致。

    Args:
        eval_set_json: 完整的 EvalSet JSON 对象。
        filename: 文件名（不含扩展名），通常为 eval_set_id。

    Returns:
        {"status": ..., "path": ..., "num_cases": ...} 或 {"status": "error", "errors": [...]}
    """
    _sanitize_ids(eval_set_json)
    validation = validate_eval_set(eval_set_json)
    if validation["status"] == "error":
        return {"status": "error", "errors": validation["errors"]}

    name = re.sub(r"\.evalset\.json$", "", filename.strip(), flags=re.IGNORECASE)
    name = re.sub(r"\.json$", "", name, flags=re.IGNORECASE)
    name = _ID_SANITIZE.sub("_", name) or f"evalset_{int(time.time())}"

    # 优先经 ADK 模型规范化序列化，与 LocalEvalSetsManager 落盘格式完全一致
    cls = _adk_eval_set_cls()
    if cls is not None:
        try:
            model = cls.model_validate(eval_set_json)
            content = model.model_dump_json(
                indent=2, exclude_unset=True, exclude_defaults=True, exclude_none=True
            )
        except Exception:
            content = None
    else:
        content = None
    if content is None:
        content = json.dumps(eval_set_json, indent=2, ensure_ascii=False)

    EVAL_DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = EVAL_DATA_DIR / f"{name}.evalset.json"
    path.write_text(content, encoding="utf-8")

    return {
        "status": "success",
        "path": str(path),
        "num_cases": len(eval_set_json.get("eval_cases", [])),
    }


def list_eval_sets() -> Dict[str, Any]:
    """列出 eval_data/ 下已有的 EvalSet 文件及其用例数。"""
    if not EVAL_DATA_DIR.exists():
        return {"status": "success", "eval_sets": []}

    eval_sets = []
    for path in sorted(EVAL_DATA_DIR.glob("*.evalset.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            eval_sets.append(
                {
                    "file": path.name,
                    "eval_set_id": data.get("eval_set_id", ""),
                    "num_cases": len(data.get("eval_cases", [])),
                }
            )
        except Exception as e:
            eval_sets.append({"file": path.name, "error": str(e)})

    return {"status": "success", "eval_sets": eval_sets}
