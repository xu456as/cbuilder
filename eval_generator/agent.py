"""eval_case_generator 根 agent 定义（供 `adk web` 自动发现）。"""

import os

from google.adk.agents import Agent

from . import tools
from .prompts import INSTRUCTIONS

root_agent = Agent(
    name="eval_case_generator",
    model=os.getenv("EVAL_GENERATOR_MODEL", "gemini-2.5-flash"),
    description=(
        "Generates LLM-agent evaluation datasets in Google ADK EvalSet format: "
        "single-turn QA cases and multi-turn conversations with tool calls."
    ),
    instruction=INSTRUCTIONS,
    tools=[
        tools.validate_eval_set,
        tools.save_eval_set,
        tools.list_eval_sets,
    ],
)
