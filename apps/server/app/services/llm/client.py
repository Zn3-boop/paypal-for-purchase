from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Type, TypeVar

from openai import AsyncOpenAI, APIError, APIConnectionError, APITimeoutError
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

_LANGFUSE_ENABLED = os.getenv("LANGFUSE_ENABLED", "").lower() in ("true", "1", "yes")
_LANGFUSE_TRACE_ID = ""


def enable_langfuse(trace_id: str = "") -> None:
    global _LANGFUSE_ENABLED, _LANGFUSE_TRACE_ID
    _LANGFUSE_ENABLED = True
    if trace_id:
        _LANGFUSE_TRACE_ID = trace_id


def _lf_span(name: str, input_data: dict[str, Any] | None = None):
    from contextlib import contextmanager

    class _NoopObs:
        def update(self, *a, **kw): pass
        def end(self): pass

    @contextmanager
    def _noop():
        yield _NoopObs()

    if not _LANGFUSE_ENABLED:
        return _noop()
    try:
        from app.services.observability.langfuse_client import langfuse
        if not langfuse:
            return _noop()
        obs_kwargs = {"name": name, "as_type": "span", "input": input_data or {}}
        if _LANGFUSE_TRACE_ID:
            from langfuse.types import TraceContext
            obs_kwargs["trace_context"] = TraceContext(trace_id=_LANGFUSE_TRACE_ID)
        obs = langfuse.start_observation(**obs_kwargs)

        @contextmanager
        def _real():
            try:
                yield obs
            finally:
                try:
                    obs.end()
                except Exception:
                    pass
        return _real()
    except Exception:
        return _noop()


T = TypeVar("T", bound=BaseModel)

client: AsyncOpenAI | None = None


def get_llm_client() -> AsyncOpenAI:
    global client
    if client is None:
        # .env 由 app.core.config.Settings 统一加载（env_file），但 pydantic-settings
        # 不会把 .env 的值注入 os.environ —— 这里兜底从 Settings 读取，保证正式启动
        # （python -m uvicorn app.main:app，无手动 load_dotenv）也能拿到真实 key / 端点。
        # 优先级：进程环境变量 > .env（Settings）。
        from app.core.config import get_settings

        _s = get_settings()
        api_key = os.environ.get("LLM_API_KEY") or _s.llm_api_key
        base_url = os.environ.get("LLM_BASE_URL") or _s.llm_base_url
        timeout_s = float(
            os.environ.get("LLM_TIMEOUT")
            or os.environ.get("LLM_TIMEOUT_SECONDS")
            or _s.llm_timeout_seconds
        )
        client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout_s,
        )
    return client


def _resolve_llm_model() -> str:
    """模型名解析：进程环境变量优先，回退到 .env（Settings）。"""
    from app.core.config import get_settings

    return os.environ.get("LLM_MODEL") or get_settings().llm_model


def is_transient_llm_error(exc: Exception) -> bool:
    if isinstance(exc, (APITimeoutError, APIConnectionError)):
        return True
    if isinstance(exc, APIError):
        status = getattr(exc, "status_code", None)
        if status in {429, 500, 502, 503, 504}:
            return True
    return False


async def llm_parse_with_retry(
    prompt: str,
    resp_model: Type[T],
    max_retry: int = 2,
    timeout: float | None = None,
) -> tuple[T | None, dict]:
    c = get_llm_client()
    model_name = _resolve_llm_model()
    use_structured_output = os.environ.get("LLM_STRUCTURED_OUTPUT", "auto").lower()
    last_raw: dict = {}
    for attempt in range(max_retry + 1):
        try:
            if use_structured_output in ("auto", "true"):
                try:
                    coro = c.beta.chat.completions.parse(
                        model=model_name,
                        messages=[{"role": "user", "content": prompt}],
                        response_format=resp_model,
                    )
                    if timeout is not None:
                        resp = await asyncio.wait_for(coro, timeout=timeout)
                    else:
                        resp = await coro
                    msg = resp.choices[0].message
                    # P2-10: msg 是 SDK 的 ParsedChatCompletionMessage[ContentType] 泛型模型，
                    # 直接 model_dump() 序列化 parsed 字段（Optional[~ResponseFormatT]）时，
                    # pydantic 泛型绑定与序列化器不匹配 → PydanticSerializationUnexpectedValue 警告
                    # （七个响应模型每次调用各报一条）。排除 parsed 再手动转 dict，警告清零且信息不丢。
                    last_raw = msg.model_dump(exclude={"parsed"})
                    if msg.parsed is not None:
                        last_raw["parsed"] = msg.parsed.model_dump(mode="json")
                        return msg.parsed, last_raw
                    if use_structured_output == "true":
                        return None, last_raw
                except Exception as e:
                    if use_structured_output == "true":
                        last_raw["error"] = str(e)
                        return None, last_raw
                    logger.info(f"beta.parse不可用({type(e).__name__})，降级到chat.completions.create+json解析")

            schema_str = json.dumps(resp_model.model_json_schema(), ensure_ascii=False)
            field_names = list(resp_model.model_fields.keys())
            example = {k: "..." for k in field_names}
            example_str = json.dumps(example, ensure_ascii=False)
            full_prompt = (
                f"{prompt}\n\n"
                f"请直接输出一个JSON对象，包含以下字段：{field_names}\n"
                f"示例格式：{example_str}\n"
                f"JSON Schema供参考：{schema_str}\n"
                f"只输出JSON，不要输出其他文字。"
            )
            coro = c.chat.completions.create(
                model=model_name,
                messages=[{"role": "user", "content": full_prompt}],
            )
            if timeout is not None:
                resp = await asyncio.wait_for(coro, timeout=timeout)
            else:
                resp = await coro
            content = resp.choices[0].message.content or ""
            last_raw = {"content": content}
            parsed_obj = _extract_json_and_validate(content, resp_model)
            if parsed_obj is not None:
                return parsed_obj, last_raw
            last_raw["error"] = "json_parse_failed"
            return None, last_raw
        except asyncio.TimeoutError:
            last_raw["error"] = "asyncio_timeout"
            return None, last_raw
        except Exception as e:
            last_raw["error"] = str(e)
            if not is_transient_llm_error(e) or attempt >= max_retry:
                return None, last_raw
    return None, last_raw


def _extract_json_and_validate(content: str, resp_model: Type[T]) -> T | None:
    import re
    json_str = content.strip()
    if "```json" in json_str:
        json_str = json_str.split("```json")[1].split("```")[0].strip()
    elif "```" in json_str:
        json_str = json_str.split("```")[1].split("```")[0].strip()
    try:
        return resp_model.model_validate_json(json_str)
    except Exception:
        pass
    match = re.search(r'\{[^{}]*\}', content, re.DOTALL)
    if match:
        try:
            return resp_model.model_validate_json(match.group())
        except Exception:
            pass
    for depth in range(2, 6):
        pattern = r'\{' + (r'[^{}]*' if depth == 1 else r'(?:[^{}]|' + r'\{') * (depth - 1) + r'[^{}]*' + r'\})*' * (depth - 1) + r'[^{}]*\}'
        try:
            match = re.search(pattern, content, re.DOTALL)
            if match:
                return resp_model.model_validate_json(match.group())
        except Exception:
            pass
    try:
        json.loads(content.strip())
        return resp_model.model_validate_json(content.strip())
    except Exception:
        pass
    return None


class IntentDetectOutput(BaseModel):
    intent: str
    intent_confidence: float


class EntityExtractOutput(BaseModel):
    entities: dict[str, str | dict]


class RouteOutput(BaseModel):
    route_target: str
    agent_input: dict[str, Any]
    route_confidence: float


class PlanStep(BaseModel):
    step_index: int
    agent: str
    action: str
    description: str
    depends_on: list[int] = Field(default_factory=list)
    priority: str = Field(default="P1", description="P0/P1/P2")
    deliverable: str = Field(default="", description="交付物描述")
    status: str = Field(default="pending", description="pending/done/blocked")


class PlanOutput(BaseModel):
    is_multi_step: bool
    steps: list[PlanStep]


class QueryRewriteOutput(BaseModel):
    standalone_query: str
    retrieval_queries: list[str]


class RewriteAgentInputOutput(BaseModel):
    agent_input: dict[str, Any]


class TaskPlanGlobalItem(BaseModel):
    step_index: int
    description: str
    expected_output: str
    status: str = Field(default="pending", description="pending/completed")


class TaskPlanGlobalOutput(BaseModel):
    items: list[TaskPlanGlobalItem]


class SubStepItem(BaseModel):
    sub_step_index: int
    description: str
    success_criteria: str
    risk: str = Field(default="")


class SubPlanOutput(BaseModel):
    target: str
    steps: list[SubStepItem]


class TaskExecuteOutput(BaseModel):
    content: str
    is_complete: bool = True


async def task_plan_global_llm(raw_query: str) -> list[dict[str, Any]]:
    with _lf_span("task_plan_global_llm", {"raw_query": raw_query[:500]}) as span:
        t0 = asyncio.get_event_loop().time()
        prompt = (
            f"你是长任务规划Agent。根据用户原始任务，生成一份全局总计划。\n"
            f"任务：{raw_query}\n\n"
            f"要求：\n"
            f"- 输出JSON数组，每一项是一个TaskPlanGlobalItem\n"
            f"- step_index: 序号，从1开始\n"
            f"- description: 任务描述，要具体可执行\n"
            f"- expected_output: 该任务预期产出物\n"
            f"- status: pending\n"
            f"- 规划规则：任务要有先后依赖顺序；不要合并任务；每一项产出物对应一个任务\n"
            f"schema {TaskPlanGlobalOutput.model_json_schema()}"
        )
        parsed, _ = await llm_parse_with_retry(prompt, TaskPlanGlobalOutput)
        latency_ms = int((asyncio.get_event_loop().time() - t0) * 1000)
        if parsed is None:
            span.update(output={"ok": False, "items": 0}, metadata={"latency_ms": latency_ms})
            return []
        items = [item.model_dump() for item in parsed.items]
        span.update(output={"ok": True, "items": len(items)}, metadata={"latency_ms": latency_ms})
        return items


async def task_plan_sub_plan_llm(pending_task: dict[str, Any], history_log: list[str]) -> dict[str, Any]:
    with _lf_span("task_plan_sub_plan_llm", {"step_index": pending_task.get("step_index"), "description": str(pending_task.get("description", ""))[:200]}) as span:
        t0 = asyncio.get_event_loop().time()
        prompt = (
            f"基于全局计划，为当前待办任务生成本轮子执行计划。\n"
            f"当前待办任务：{json.dumps(pending_task, ensure_ascii=False)}\n"
            f"历史日志（最近5条）：{json.dumps(history_log[-5:], ensure_ascii=False)}\n\n"
            f"输出SubPlan JSON：\n"
            f"- target：本轮目标\n"
            f"- steps：子步骤列表，每个包含 sub_step_index、description、success_criteria、risk\n"
            f"schema {SubPlanOutput.model_json_schema()}"
        )
        parsed, _ = await llm_parse_with_retry(prompt, SubPlanOutput)
        latency_ms = int((asyncio.get_event_loop().time() - t0) * 1000)
        if parsed is None:
            span.update(output={"ok": False}, metadata={"latency_ms": latency_ms})
            return {"target": pending_task.get("description", ""), "steps": []}
        result = parsed.model_dump()
        span.update(output={"ok": True, "steps": len(result.get("steps", []))}, metadata={"latency_ms": latency_ms})
        return result


async def task_execute_llm(task_description: str, sub_plan: dict[str, Any], expected_output: str, prev_results: list[dict[str, Any]]) -> str:
    with _lf_span("task_execute_llm", {"task": task_description[:200], "expected_output": expected_output[:200]}) as span:
        t0 = asyncio.get_event_loop().time()
        prev_summary = ""
        if prev_results:
            for r in prev_results[-3:]:
                tr = r.get("tool_result", {})
                if tr.get("ok") and tr.get("content_length", 0) > 0:
                    prev_summary += f"\n[前置步骤产出]: {str(tr.get('data', ''))[:500]}"
        prompt = (
            f"执行下面子计划，产出对应交付物。\n"
            f"全局任务描述：{task_description}\n"
            f"本轮子计划：{json.dumps(sub_plan, ensure_ascii=False)}\n"
            f"预期输出：{expected_output}\n"
            f"{prev_summary}\n"
            f"直接输出交付物正文，不要输出其他说明文字。"
        )
        c = get_llm_client()
        model_name = _resolve_llm_model()
        timeout = int(os.environ.get("LLM_TIMEOUT", "300"))
        max_retry = 2
        for attempt in range(max_retry + 1):
            try:
                resp = await c.chat.completions.create(
                    model=model_name,
                    messages=[{"role": "user", "content": prompt}],
                    timeout=timeout,
                )
                content = resp.choices[0].message.content or ""
                latency_ms = int((asyncio.get_event_loop().time() - t0) * 1000)
                span.update(output={"ok": True, "content_length": len(content)}, metadata={"latency_ms": latency_ms, "model": model_name, "attempt": attempt})
                return content
            except (APITimeoutError, APIConnectionError) as e:
                if attempt >= max_retry:
                    latency_ms = int((asyncio.get_event_loop().time() - t0) * 1000)
                    span.update(output={"ok": False, "error": str(e)[:200]}, metadata={"latency_ms": latency_ms, "attempt": attempt})
                    raise
                await asyncio.sleep(2 * (attempt + 1))


async def detect_intent_llm(raw_query: str) -> tuple[str, float]:
    prompt = (
        f"分析用户指令，输出意图与置信度。用户输入：{raw_query}\n"
        f"输出JSON schema：{IntentDetectOutput.model_json_schema()}"
    )
    parsed, _ = await llm_parse_with_retry(prompt, IntentDetectOutput)
    if parsed is None:
        return "unknown", 0.0
    return parsed.intent, parsed.intent_confidence


async def extract_entities_llm(raw_query: str, intent: str) -> dict[str, Any]:
    prompt = (
        f"用户输入：{raw_query}，识别实体，intent={intent}，"
        f"schema {EntityExtractOutput.model_json_schema()}"
    )
    parsed, _ = await llm_parse_with_retry(prompt, EntityExtractOutput)
    if parsed is None:
        return {"raw": raw_query, "_fallback": True}
    return parsed.entities


async def route_via_llm_real(raw_query: str) -> tuple[str, dict[str, Any], float]:
    prompt = (
        f"根据用户指令路由Agent，用户输入 {raw_query}\n"
        f"候选agent：app_launch_agent_node(启动/关闭应用)、"
        f"system_clean_agent_node(清理/优化)、browser_agent_node(搜索/网络请求)、"
        f"store_agent_node(应用商店/安装卸载)、content_gen_agent_node(生成内容/代码/文档)、"
        f"planning_node(复杂任务/多步规划/搭建/写论文/面试材料/长任务)、"
        f"clarification_node(需要澄清)。\n"
        f"规则：如果用户请求涉及多个步骤、需要规划、搭建材料包、写论文、面试准备等复杂任务，必须路由到planning_node。\n"
        f"schema {RouteOutput.model_json_schema()}"
    )
    parsed, _ = await llm_parse_with_retry(prompt, RouteOutput)
    if parsed is None:
        return "clarification_node", {}, 0.0
    return parsed.route_target, parsed.agent_input, parsed.route_confidence


async def plan_steps_llm(raw_query: str, intent: str, route_target: str, taste_context: str = "") -> tuple[bool, list[dict[str, Any]]]:
    simple_intents = {"启动应用程序", "关闭应用", "结束进程", "系统监控", "系统清理", "查看进程"}
    if intent in simple_intents or route_target in ("app_launch_agent_node", "clarification_node"):
        return False, []
    taste_block = ""
    if taste_context:
        taste_block = f"\n[Background context]\n{taste_context}\n[/Background context]\n"
    prompt = (
        f"用户请求：{raw_query}\n"
        f"意图：{intent}\n"
        f"初步路由：{route_target}\n"
        f"{taste_block}\n"
        f"判断是否需要多步执行。如果需要，规划步骤顺序。\n"
        f"每步必须指定priority(P0最高/P1/P2可选)、deliverable(交付物)。\n"
        f"P0=核心前置步骤必须先做；P1=主要步骤；P2=可选优化步骤。\n"
        f"depends_on填写依赖的step_index列表，禁止跳过依赖。\n\n"
        f"可用Agent：\n"
        f"  - browser_agent_node：搜索网页、查资料、获取参考信息\n"
        f"  - store_agent_node：搜索应用商店、安装工具\n"
        f"  - content_gen_agent_node：生成文章、代码、文档、创建项目\n"
        f"  - image_agent_node：图像处理（截图、OCR文字识别、裁剪、格式转换、滤镜、二维码识别、图片对比）\n"
        f"  - algorithm_agent_node：算法应用（数学计算、加密解密、压缩解压、数据转换、哈希、可视化绘图、随机生成）\n"
        f"  - rag_retrieve_node：RAG知识库检索（向量+BM25混搭→RRF重排→鉴权前置→证据回溯→压幻觉）\n"
        f"  - evolve_feedback_node：自进化Agent（经验驱动→四类闭环→评估/回滚/权限/供应链治理）\n"
        f"  - code_search_node：智能代码搜索（策略路由→Glob/Grep/Semantic三引擎→权限检查→聚合输出）\n"
        f"  - system_monitor_agent_node：已移除\n"
        f"  - system_clean_agent_node：清理优化系统\n"
        f"  - app_launch_agent_node：启动/关闭应用\n\n"
        f"示例：用户说'写一篇论文'→多步：\n"
        f"  step0(P0) browser搜索资料, deliverable=调研笔记\n"
        f"  step1(P0) rag_retrieve检索知识库, deliverable=知识库证据, depends_on=[0]\n"
        f"  step2(P0) content_gen生成论文, deliverable=论文文档, depends_on=[0,1]\n"
        f"  step3(P1) algorithm可视化图表, deliverable=图表文件, depends_on=[2]\n"
        f"  step4(P2) content_gen润色摘要, deliverable=最终论文, depends_on=[2,3]\n\n"
        f"示例：用户说'写一个登录页面'→多步：1.browser搜索参考 2.content_gen生成代码\n"
        f"示例：用户说'帮我查一下Python怎么装然后装上'→多步：1.browser搜索教程 2.store安装Python\n"
        f"示例：用户说'截个屏然后识别文字'→多步：1.image截图 2.image OCR识别\n"
        f"示例：用户说'打开微信'→单步，不需要规划\n"
        f"schema {PlanOutput.model_json_schema()}"
    )
    parsed, _ = await llm_parse_with_retry(prompt, PlanOutput)
    if parsed is None:
        return False, []
    if not parsed.is_multi_step or not parsed.steps:
        return False, []
    steps = [s.model_dump() for s in parsed.steps]
    return True, steps


async def rewrite_query_pipeline_real(raw_query: str, entities: dict) -> tuple[str, list[str]]:
    prompt = (
        f"原始query：{raw_query}，实体 {json.dumps(entities, ensure_ascii=False)}\n"
        f"生成standalone_query消除指代；生成多条retrieval_queries检索候选。"
        f"schema {QueryRewriteOutput.model_json_schema()}"
    )
    parsed, _ = await llm_parse_with_retry(prompt, QueryRewriteOutput)
    if parsed is None:
        return raw_query, [raw_query]
    return parsed.standalone_query, parsed.retrieval_queries


async def rewrite_agent_input_llm(
    raw_query: str,
    old_input: dict,
    error_msg: str,
    rewrite_timeout: float = 10.0,
) -> dict[str, Any]:
    prompt = (
        f"用户原始请求：{raw_query}\n"
        f"上一轮agent_input:{json.dumps(old_input, ensure_ascii=False)}\n"
        f"上一轮执行失败原因：{error_msg}\n"
        f"请修正agent_input，避免该错误。schema {RewriteAgentInputOutput.model_json_schema()}"
    )
    parsed, _ = await llm_parse_with_retry(
        prompt, RewriteAgentInputOutput, max_retry=1, timeout=rewrite_timeout,
    )
    if parsed is None:
        return old_input
    return parsed.agent_input