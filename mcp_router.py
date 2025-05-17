# filename: mcp_router.py

import uuid
import time
import json
import asyncio
import httpx
from typing import List, Dict
from fastapi import FastAPI, Request
from pydantic import BaseModel
from difflib import SequenceMatcher
import ssl
import certifi
from fastapi_mcp import FastApiMCP, mcp_handler

app = FastAPI()

mcp_router = FastApiMCP(
    app,
    name="MCP router",
    description="mcp router",
)

# ---------------
# MCP Probe Schema
# ---------------
class MCPTask(BaseModel):
    task_id: str
    type: str
    prompt: str
    expected: str = ""
    expected_keywords: List[str] = []

class MCPProbe(BaseModel):
    probe_id: str
    timestamp: float
    test_type: str = "capability_check"
    quality_weight: Dict[str, float]
    test_tasks: List[MCPTask]
    latency_probe: bool = True
    max_latency_ms: int = 2000

class MCPResult(BaseModel):
    model_id: str
    latency_ms: int
    task_results: List[Dict[str, float]]
    output: List[Dict[str, float]]
    overall_score: float

# ---------------
# Helper scoring function
# ---------------
def score_output(task: MCPTask, response: str) -> float:
    if task.expected:
        ratio = SequenceMatcher(None, task.expected.strip().lower(), response.strip().lower()).ratio()
        return round(ratio, 3)
    elif task.expected_keywords:
        hits = sum(1 for kw in task.expected_keywords if kw in response)
        return round(hits / len(task.expected_keywords), 3)
    return 0.5

# ---------------
# LLM call (OpenAI-compatible)
# ---------------
async def call_openai_model(prompt: str, api_url: str, api_key: str) -> str:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "gpt-3.5-turbo",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0
    }
    ssl_context = ssl.create_default_context(cafile=certifi.where())
    async with httpx.AsyncClient(timeout=15, verify=ssl_context) as client:
        r = await client.post(api_url, json=payload, headers=headers)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]

# ---------------
# Simulated local LLM response
# ---------------
async def simulate_llm_response(prompt: str) -> str:
    await asyncio.sleep(0.1)
    return f"Simulated response for prompt: {prompt}"

# ---------------
# Local MCP-compatible simulated model endpoint
# ---------------
@mcp_router.post("/v1/chat/completions")
async def simulate_chat_completions(request: Request):
    payload = await request.json()
    messages = payload.get("messages", [])
    prompt = messages[-1]["content"] if messages else ""
    response = await simulate_llm_response(prompt)
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": response
                }
            }
        ]
    }

# ---------------
# Probe One Model
# ---------------
async def probe_model(model_id: str, probe: MCPProbe, api_url: str, api_key: str) -> MCPResult:
    start = time.perf_counter()
    task_results = []
    total_score = 0.0

    for task in probe.test_tasks:
        try:
            if model_id == "model_simulated":
                response = await simulate_llm_response(task.prompt)
            else:
                response = await call_openai_model(task.prompt, api_url, api_key)
            score = score_output(task, response)
        except Exception as e:
            response = str(e)
            score = 0.0

        task_results.append({"task_id": task.task_id, "score": score, "output": response})
        total_score += score * probe.quality_weight.get(task.type, 0.0)

    latency_ms = int((time.perf_counter() - start) * 1000)
    return MCPResult(
        model_id=model_id,
        latency_ms=latency_ms,
        task_results=task_results,
        output=task_results,
        overall_score=round(total_score, 3)
    )

# ---------------
# MCP Router Probe Endpoint (MCP-compatible)
# ---------------
@mcp_router.post("/mcp/probe")
async def route_probe(probe: MCPProbe):
    model_configs = [
        {
            "model_id": "model_openai_main",
            "url": "https://api.openai.com/v1/chat/completions",
            "key": "sk-XXX"
        },
        {
            "model_id": "model_local_compat",
            "url": "http://localhost:8000/v1/chat/completions",
            "key": "local-key"
        },
        {
            "model_id": "model_deepseek",
            "url": "https://api.deepseek.com/v1/chat/completions",
            "key": "sk-DEEPSEEK"
        },
        {
            "model_id": "model_simulated",
            "url": "",
            "key": ""
        }
    ]

    tasks = [
        probe_model(cfg["model_id"], probe, cfg["url"], cfg["key"])
        for cfg in model_configs
    ]

    results = await asyncio.gather(*tasks)
    valid = [r for r in results if r.latency_ms <= probe.max_latency_ms]

    if not valid:
        return {"error": "No models responded within latency limit."}

    best = sorted(valid, key=lambda r: (-r.overall_score, r.latency_ms))[0]
    return best

# ---------------
# Expose a MCP endpoint for remote clients to send probes
# ---------------
@mcp_router.mcp("/mcp/receive")
async def mcp_receive_probe(probe: MCPProbe) -> MCPResult:
    return await route_probe(probe)