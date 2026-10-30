from __future__ import annotations

import logging
import os
from typing import Any

from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver

try:
    from langgraph.types import RetryPolicy
except ImportError:
    try:
        from langgraph.pregel import RetryPolicy
    except ImportError:
        RetryPolicy = None

from app.services.graph.state import (
    AgentState,
    ERROR_TYPE_TRANSIENT,
    ERROR_TYPE_BUSINESS,
    ERROR_TYPE_SECURITY,
)

logger = logging.getLogger(__name__)

ROUTE_CONFIDENCE_THRESHOLD = float(os.getenv("ROUTE_CONFIDENCE_THRESHOLD", "0.7"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "2"))
DEFAULT_RECURSION_LIMIT = int(os.getenv("GRAPH_RECURSION_LIMIT", "15"))
MAX_CORRECTION_LOOP = int(os.getenv("MAX_CORRECTION_LOOP", "2"))


def get_checkpointer() -> Any:
    """构造 checkpointer（带 msgpack 允许列表，TrustProfile 等应用类型可正常反序列化）。

    langgraph 1.x 的 JsonPlusSerializer 对未注册类型在反序列化时会告警
    （LANGGRAPH_STRICT_MSGPACK 关闭时仍可读，但日志有噪音；开启时直接失败）。
    这里显式注册本项目的 TrustProfile 等 dataclass，保持双程探测 stdout 干净。
    """
    env = os.getenv("GRAPH_ENV", "dev")

    def _serde_with_app_types():
        from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

        # 允许列表：付款子图的会话级 dataclass（TrustProfile / GoalMemory 等）
        from app.services.paypal.authz import TrustProfile
        from app.services.payment_agent.goals import GoalMemory, SavingGoal

        return JsonPlusSerializer(
            allowed_msgpack_modules=(TrustProfile, GoalMemory, SavingGoal)
        )

    if env == "dev":
        return MemorySaver(serde=_serde_with_app_types())
    elif env == "test":
        try:
            from langgraph.checkpoint.sqlite import SqliteSaver
            db_path = os.getenv("GRAPH_CHECKPOINT_DB", "./graph_checkpoint.db")
            saver = SqliteSaver.from_conn_string(db_path)
            saver.serde = _serde_with_app_types()
            return saver
        except ImportError:
            logger.warning("langgraph-checkpoint-sqlite 未安装，降级 MemorySaver")
            return MemorySaver(serde=_serde_with_app_types())
    elif env == "prod":
        try:
            from langgraph.checkpoint.postgres import PostgresSaver
            conn_string = os.getenv("GRAPH_POSTGRES_CONN", "")
            if not conn_string:
                raise ValueError("生产环境必须设置 GRAPH_POSTGRES_CONN")
            saver = PostgresSaver.from_conn_string(conn_string)
            saver.serde = _serde_with_app_types()
            return saver
        except ImportError:
            logger.warning("postgres检查点不可用，降级sqlite")
            try:
                from langgraph.checkpoint.sqlite import SqliteSaver
                db_path = os.getenv("GRAPH_CHECKPOINT_DB", "./graph_checkpoint.db")
                saver = SqliteSaver.from_conn_string(db_path)
                saver.serde = _serde_with_app_types()
                return saver
            except ImportError:
                logger.warning("SqliteSaver不可用，降级MemorySaver")
                return MemorySaver(serde=_serde_with_app_types())
    return MemorySaver(serde=_serde_with_app_types())


def route_after_llm(state: AgentState) -> str:
    # 支付意图优先于文化推荐：raw_query 含购买语义直接进入支付子图
    if _has_payment_intent(state):
        return "payment_parse_intent"
    conf = state.get("route_confidence", 0.0)
    route_target = state.get("route_target", "")
    if conf < ROUTE_CONFIDENCE_THRESHOLD or not route_target:
        return "clarification_node"
    if route_target == "rag_retrieve_node":
        return "rag_retrieve_node"
    if route_target == "code_search_node":
        return "code_search_node"
    return "planning_node"


_PAYMENT_KEYWORDS = (
    "帮我买", "我要买", "想买", "购买", "下单", "付款", "买单", "结账",
    "order this", "buy this", "purchase this", "pay for", "i want to buy",
    "帮我下单", "帮我付款", "买一个", "买一张", "买一份", "来一个",
)


def _has_payment_intent(state: AgentState) -> bool:
    """从 raw_query / route_target 探测支付意图（优先级高于文化推荐）。"""
    raw = (state.get("raw_query") or "").lower()
    if any(kw in raw for kw in _PAYMENT_KEYWORDS):
        return True
    route_target = state.get("route_target", "") or ""
    return route_target in ("payment_node", "shopping_payment", "payment_parse_intent")


def route_after_payment_authority(state: AgentState) -> str:
    """授权判定后路由：block 终止；allow_auto 跳过审批；need_approval 走闸门。"""
    verdict = (state.get("authority") or {}).get("verdict", "")
    if verdict == "block":
        return "memory_persist_node"
    if verdict == "allow_auto":
        return "payment_paypal_create"
    return "payment_approval_gate"


def route_after_payment_approval(state: AgentState) -> str:
    """审批闸门后路由：approved 放行创建订单；否则（cancelled/pending）终止。"""
    status = state.get("approval_status", "")
    if status == "approved":
        return "payment_paypal_create"
    return "memory_persist_node"


def route_after_risk(state: AgentState) -> str:
    if state.get("approval_required", False):
        return "approval_node"
    return "tool_search_node"


def route_after_approval(state: AgentState) -> str:
    approval_status = state.get("approval_status", "pending")
    if approval_status == "ok":
        return "tool_search_node"
    if approval_status == "reject":
        return "error_fallback_node"
    return "clarification_node"


def route_after_validate(state: AgentState) -> str:
    if state.get("last_error"):
        error_type = state.get("error_type", "")
        if error_type == ERROR_TYPE_SECURITY:
            return "error_fallback_node"
        if error_type == ERROR_TYPE_BUSINESS:
            loop_cnt = state.get("correction_loop_count", 0)
            max_loop = state.get("max_correction_loop", MAX_CORRECTION_LOOP)
            if loop_cnt < max_loop:
                return "rewrite_agent_input_node"
            from app.services.graph.metrics import NodeMetrics
            NodeMetrics.count_correction_exhaust()
            return "error_fallback_node"
        retry_count = state.get("retry_count", 0)
        error = state.get("last_error", "")
        if error_type == ERROR_TYPE_TRANSIENT and retry_count < MAX_RETRIES:
            return "tool_execute_node"
        if _is_transient_error(error) and retry_count < MAX_RETRIES:
            return "tool_execute_node"
        return "error_fallback_node"
    if state.get("enable_evolve_feedback", False):
        return "evolve_feedback_node"
    return "memory_persist_node"


def route_after_agent(state: AgentState) -> str:
    if state.get("last_error"):
        return "error_fallback_node"
    if not state.get("tool_name"):
        return "memory_persist_node"
    return "tool_execute_node"


def route_after_guard(state: AgentState) -> str:
    if state.get("last_error"):
        return "error_fallback_node"
    return "risk_check_node"


def _is_transient_error(error: str) -> bool:
    transient_keywords = [
        "timeout", "timed out", "connection", "network",
        "subprocess", "electron", "retry", "temporarily",
        "econnrefused", "econnreset", "etimedout",
    ]
    error_lower = error.lower()
    return any(kw in error_lower for kw in transient_keywords)


def route_after_fast_brain(state: AgentState) -> str:
    mode = state.get("fast_brain_mode", "")
    if mode == "reject":
        return "error_fallback_node"
    if mode == "fast_answer":
        return END
    return "detect_intent"


def route_after_tool_exec(state: AgentState) -> str:
    err = state.get("last_error")
    auto_diag = state.get("enable_auto_diagnose", False)
    if err and auto_diag:
        return "supervisor_diagnose_node"
    return "post_validate_gate"


def route_after_supervisor(state: AgentState) -> str:
    sig_id = state.get("correction_signal_id", "")
    if sig_id:
        return "governor_scan_node"
    return END


def route_after_governor(state: AgentState) -> str:
    return END


def approval_node(state: AgentState) -> dict:
    current = state.get("approval_status", "")
    if current in ("ok", "reject"):
        return {}
    return {"approval_status": "pending"}


_UNSET = object()


def build_agent_graph(
    checkpointer: Any = _UNSET,
    interrupt_before_approval: bool = True,
):
    # A档保底路径：最简可编译图（无 checkpointer），保留供 A档测试 / 降级参考；
    from app.services.graph.nodes import (
        detect_intent_node,
        extract_entities_node,
        route_llm_node,
        clarification_node,
        risk_check_node,
        planning_node,
        step_loop_node,
        route_after_planning,
        route_after_step_execute,
        app_launch_agent_node,
        system_clean_agent_node,
        browser_agent_node,
        store_agent_node,
        content_gen_agent_node,
        image_agent_node,
        algorithm_agent_node,
        rag_retrieve_node,
        evolve_feedback_node,
        code_search_node,
        agent_dispatch_node,
        route_after_dispatch,
        tool_execute_node,
        step_collect_node,
        route_after_tool_exec_or_step,
        post_validate_gate_node,
        error_fallback_node,
        memory_persist_node,
        tool_search_node,
        supervisor_diagnose_node,
        governor_scan_node,
        rewrite_agent_input_node,
        task_planner_node,
        task_plan_collect_node,
        route_after_task_plan,
        route_after_task_plan_exec,
        governor_node,
        hitl_approval_node,
        route_after_governor,
        route_after_hitl,
    )
    from app.services.payment_agent.nodes import (
        parse_intent_node,
        taste_select_node,
        authority_check_node,
        approval_gate_node,
        paypal_create_node,
        paypal_capture_node,
        explain_node,
    )
    from app.services.graph.checkers import fast_brain_node, guard_pre_check_node

    use_hooks = os.getenv("HOOKS_ENABLED", "true").lower() in ("true", "1", "yes")
    if use_hooks:
        try:
            from app.services.hooks.graph_integration import (
                rag_retrieve_with_hooks,
                tool_execute_with_hooks,
                turn_end_with_hooks,
                detect_intent_with_hooks,
                clarification_with_hooks,
                extract_entities_with_hooks,
            )
            logger.info("Graph: using Hook-enhanced nodes for detect_intent / extract_entities / clarification / rag_retrieve / tool_execute / turn_end")
        except ImportError as e:
            logger.warning(f"Hook integration import failed, falling back to original nodes: {e}")
            use_hooks = False

    builder = StateGraph(AgentState)

    builder.add_node("fast_brain", fast_brain_node)
    builder.add_node("detect_intent", detect_intent_with_hooks if use_hooks else detect_intent_node)
    builder.add_node("extract_entities", extract_entities_with_hooks if use_hooks else extract_entities_node)
    builder.add_node("route_llm", route_llm_node)
    builder.add_node("clarification_node", clarification_with_hooks if use_hooks else clarification_node)
    builder.add_node("risk_check_node", risk_check_node)
    builder.add_node("approval_node", approval_node)
    builder.add_node("planning_node", planning_node)
    builder.add_node("step_loop_node", step_loop_node)
    builder.add_node("agent_dispatch_node", agent_dispatch_node)
    builder.add_node("app_launch_agent_node", app_launch_agent_node)
    builder.add_node("system_clean_agent_node", system_clean_agent_node)
    builder.add_node("browser_agent_node", browser_agent_node)
    builder.add_node("store_agent_node", store_agent_node)
    builder.add_node("content_gen_agent_node", content_gen_agent_node)
    builder.add_node("image_agent_node", image_agent_node)
    builder.add_node("algorithm_agent_node", algorithm_agent_node)
    builder.add_node("rag_retrieve_node", rag_retrieve_with_hooks if use_hooks else rag_retrieve_node)
    builder.add_node("evolve_feedback_node", evolve_feedback_node)
    builder.add_node("code_search_node", code_search_node)
    builder.add_node("tool_search_node", tool_search_node)
    builder.add_node("guard_pre_check", guard_pre_check_node)
    builder.add_node("post_validate_gate", post_validate_gate_node)
    builder.add_node("step_collect_node", step_collect_node)
    builder.add_node("error_fallback_node", error_fallback_node)
    builder.add_node("memory_persist_node", memory_persist_node)
    builder.add_node("governor_scan_node", governor_scan_node)
    builder.add_node("supervisor_diagnose_node", supervisor_diagnose_node)
    builder.add_node("rewrite_agent_input_node", rewrite_agent_input_node)
    builder.add_node("task_planner_node", task_planner_node)
    builder.add_node("task_plan_collect_node", task_plan_collect_node)
    builder.add_node("governor_node", governor_node)
    builder.add_node("hitl_approval_node", hitl_approval_node)

    # ===== 授权边界支付子图节点 =====
    builder.add_node("payment_parse_intent", parse_intent_node)
    builder.add_node("payment_taste_select", taste_select_node)
    builder.add_node("payment_authority_check", authority_check_node)
    builder.add_node("payment_approval_gate", approval_gate_node)
    builder.add_node("payment_paypal_create", paypal_create_node)
    builder.add_node("payment_paypal_capture", paypal_capture_node)
    builder.add_node("payment_explain", explain_node)

    transient_retry = None
    if RetryPolicy is not None:
        transient_retry = RetryPolicy(
            max_attempts=MAX_RETRIES,
            retry_on=(ConnectionError, TimeoutError),
        )
    _tool_exec_fn = tool_execute_with_hooks if use_hooks else tool_execute_node
    if transient_retry:
        builder.add_node("tool_execute_node", _tool_exec_fn, retry_policy=transient_retry)
    else:
        builder.add_node("tool_execute_node", _tool_exec_fn)

    builder.add_edge(START, "fast_brain")
    builder.add_conditional_edges(
        "fast_brain", route_after_fast_brain,
        {END: END, "detect_intent": "detect_intent", "error_fallback_node": "error_fallback_node"},
    )
    builder.add_edge("detect_intent", "extract_entities")
    builder.add_edge("extract_entities", "route_llm")
    builder.add_conditional_edges(
        "route_llm", route_after_llm,
        {"clarification_node": "clarification_node", "planning_node": "planning_node", "rag_retrieve_node": "rag_retrieve_node", "code_search_node": "code_search_node", "payment_parse_intent": "payment_parse_intent"},
    )
    # ===== 授权边界支付子图连线 =====
    builder.add_edge("payment_parse_intent", "payment_taste_select")
    builder.add_edge("payment_taste_select", "payment_authority_check")
    builder.add_conditional_edges(
        "payment_authority_check", route_after_payment_authority,
        {
            "payment_paypal_create": "payment_paypal_create",
            "payment_approval_gate": "payment_approval_gate",
            "memory_persist_node": "memory_persist_node",
        },
    )
    builder.add_conditional_edges(
        "payment_approval_gate", route_after_payment_approval,
        {
            "payment_paypal_create": "payment_paypal_create",
            "memory_persist_node": "memory_persist_node",
        },
    )
    builder.add_edge("payment_paypal_create", "payment_paypal_capture")
    builder.add_edge("payment_paypal_capture", "payment_explain")
    builder.add_edge("payment_explain", "memory_persist_node")
    builder.add_conditional_edges(
        "rag_retrieve_node", route_after_agent,
        {"error_fallback_node": "error_fallback_node", "tool_execute_node": "tool_execute_node", "memory_persist_node": "memory_persist_node"},
    )
    builder.add_conditional_edges(
        "code_search_node", route_after_agent,
        {"error_fallback_node": "error_fallback_node", "tool_execute_node": "tool_execute_node", "memory_persist_node": "memory_persist_node"},
    )
    builder.add_conditional_edges(
        "planning_node", route_after_planning,
        {"task_planner_node": "task_planner_node", "guard_pre_check": "guard_pre_check", "step_loop_node": "step_loop_node"},
    )
    builder.add_conditional_edges(
        "task_planner_node", route_after_task_plan,
        {
            "app_launch_agent_node": "app_launch_agent_node",
            "system_clean_agent_node": "system_clean_agent_node",
            "browser_agent_node": "browser_agent_node",
            "store_agent_node": "store_agent_node",
            "content_gen_agent_node": "content_gen_agent_node",
            "image_agent_node": "image_agent_node",
            "algorithm_agent_node": "algorithm_agent_node",
            "rag_retrieve_node": "rag_retrieve_node",
            "code_search_node": "code_search_node",
            "memory_persist_node": "memory_persist_node",
            "task_planner_node": "task_planner_node",
            "task_plan_collect_node": "task_plan_collect_node",
            "clarification_node": "clarification_node",
            "post_validate_gate": "post_validate_gate",
        },
    )
    builder.add_conditional_edges(
        "task_plan_collect_node", route_after_task_plan_exec,
        {"post_validate_gate": "post_validate_gate", "memory_persist_node": "memory_persist_node", "task_planner_node": "task_planner_node"},
    )
    builder.add_edge("step_loop_node", "tool_search_node")
    builder.add_edge("tool_search_node", "agent_dispatch_node")
    builder.add_conditional_edges(
        "agent_dispatch_node", route_after_dispatch,
        {
            "app_launch_agent_node": "app_launch_agent_node",
            "system_clean_agent_node": "system_clean_agent_node",
            "browser_agent_node": "browser_agent_node",
            "store_agent_node": "store_agent_node",
            "content_gen_agent_node": "content_gen_agent_node",
            "image_agent_node": "image_agent_node",
            "algorithm_agent_node": "algorithm_agent_node",
            "rag_retrieve_node": "rag_retrieve_node",
            "code_search_node": "code_search_node",
        },
    )
    builder.add_conditional_edges(
        "app_launch_agent_node", route_after_agent,
        {"error_fallback_node": "error_fallback_node", "tool_execute_node": "tool_execute_node", "memory_persist_node": "memory_persist_node"},
    )
    builder.add_conditional_edges(
        "system_clean_agent_node", route_after_agent,
        {"error_fallback_node": "error_fallback_node", "tool_execute_node": "tool_execute_node", "memory_persist_node": "memory_persist_node"},
    )
    builder.add_conditional_edges(
        "browser_agent_node", route_after_agent,
        {"error_fallback_node": "error_fallback_node", "tool_execute_node": "tool_execute_node", "memory_persist_node": "memory_persist_node"},
    )
    builder.add_conditional_edges(
        "store_agent_node", route_after_agent,
        {"error_fallback_node": "error_fallback_node", "tool_execute_node": "tool_execute_node", "memory_persist_node": "memory_persist_node"},
    )
    builder.add_conditional_edges(
        "content_gen_agent_node", route_after_agent,
        {"error_fallback_node": "error_fallback_node", "tool_execute_node": "tool_execute_node", "memory_persist_node": "memory_persist_node"},
    )
    builder.add_conditional_edges(
        "image_agent_node", route_after_agent,
        {"error_fallback_node": "error_fallback_node", "tool_execute_node": "tool_execute_node", "memory_persist_node": "memory_persist_node"},
    )
    builder.add_conditional_edges(
        "algorithm_agent_node", route_after_agent,
        {"error_fallback_node": "error_fallback_node", "tool_execute_node": "tool_execute_node", "memory_persist_node": "memory_persist_node"},
    )
    builder.add_conditional_edges(
        "tool_execute_node", route_after_tool_exec_or_step,
        {"step_collect_node": "step_collect_node", "task_plan_collect_node": "task_plan_collect_node", "supervisor_diagnose_node": "supervisor_diagnose_node", "post_validate_gate": "post_validate_gate"},
    )
    builder.add_conditional_edges(
        "step_collect_node", route_after_step_execute,
        {"step_loop_node": "step_loop_node", "memory_persist_node": "memory_persist_node", "error_fallback_node": "error_fallback_node"},
    )
    builder.add_conditional_edges(
        "guard_pre_check", route_after_guard,
        {"error_fallback_node": "error_fallback_node", "risk_check_node": "risk_check_node"},
    )
    builder.add_conditional_edges(
        "risk_check_node", route_after_risk,
        {"approval_node": "approval_node", "tool_search_node": "tool_search_node"},
    )
    builder.add_conditional_edges(
        "approval_node", route_after_approval,
        {"clarification_node": "clarification_node", "tool_search_node": "tool_search_node", "error_fallback_node": "error_fallback_node"},
    )
    builder.add_conditional_edges(
        "post_validate_gate", route_after_validate,
        {"tool_execute_node": "tool_execute_node", "error_fallback_node": "error_fallback_node", "memory_persist_node": "memory_persist_node", "rewrite_agent_input_node": "rewrite_agent_input_node", "evolve_feedback_node": "evolve_feedback_node"},
    )
    builder.add_edge("error_fallback_node", "memory_persist_node")
    builder.add_edge("clarification_node", "memory_persist_node")
    builder.add_edge("evolve_feedback_node", "memory_persist_node")
    builder.add_edge("rewrite_agent_input_node", "agent_dispatch_node")
    builder.add_conditional_edges(
        "governor_node", route_after_governor,
        {
            "hitl_approval_node": "hitl_approval_node",
            "task_planner_node": "task_planner_node",
            "memory_persist_node": "memory_persist_node",
        },
    )
    builder.add_conditional_edges(
        "hitl_approval_node", route_after_hitl,
        {
            "hitl_approval_node": "hitl_approval_node",
            "task_planner_node": "task_planner_node",
            "memory_persist_node": "memory_persist_node",
        },
    )
    if use_hooks:
        builder.add_node("turn_end_hook", turn_end_with_hooks)
        builder.add_edge("memory_persist_node", "turn_end_hook")
        builder.add_edge("turn_end_hook", END)
    else:
        builder.add_edge("memory_persist_node", END)
    builder.add_conditional_edges(
        "supervisor_diagnose_node", route_after_supervisor,
        {"governor_scan_node": "governor_scan_node", END: END},
    )
    builder.add_conditional_edges(
        "governor_scan_node", route_after_governor,
        {END: END},
    )

    cp = get_checkpointer() if checkpointer is _UNSET else checkpointer
    interrupt_nodes = ["approval_node"] if interrupt_before_approval else []
    interrupt_nodes.append("hitl_approval_node")
    interrupt_nodes.append("payment_approval_gate")

    graph = builder.compile(
        checkpointer=cp,
        interrupt_before=interrupt_nodes,
    )
    return graph


def build_invoke_config(
    user_id: str,
    session_id: str,
    trace_id: str = "",
    recursion_limit: int = DEFAULT_RECURSION_LIMIT,
) -> dict:
    callbacks = []
    try:
        from app.services.observability.langfuse_client import get_langfuse_handler
        handler = get_langfuse_handler()
        if handler is not None:
            callbacks.append(handler)
    except Exception:
        pass
    config: dict[str, Any] = {
        "configurable": {
            "thread_id": f"{user_id}_{session_id}",
            "trace_id": trace_id,
            "recursion_limit": recursion_limit,
        },
    }
    if callbacks:
        config["callbacks"] = callbacks
    return config