"""Effective LLM mode probe.

The startup banner must report the mode that is actually usable, not the mode
that happens to be written in settings. The real path used by the graph and
payment nodes is app.services.llm.client (see the LLM_MODE branches in
graph/nodes.py and payment_agent/nodes.py): llm.client builds an AsyncOpenAI
client from LLM_API_KEY / LLM_BASE_URL and resolves LLM_MODEL. Probing those
two calls exercises the real entry and surfaces the degraded case
("llm_mode=real" but no usable client) instead of echoing the config value.

This file is ASCII only on purpose: tooling in this repo has corrupted
non-ASCII sources before, and a startup probe must never be the reason the
process fails to boot.
"""
from __future__ import annotations


def _short(exc: BaseException, limit: int = 120) -> str:
    text = str(exc).replace("\n", " ").strip()
    return text[:limit] if text else "no-message"


def probe_llm_mode() -> str:
    """Return the effective LLM mode string printed at startup.

    - mock / unset  -> config value (the mock path needs no client)
    - real + usable -> "real(ok)"
    - real + broken -> "real(FALLBACK: <reason>)"
    """
    try:
        from app.core.config import get_settings

        settings = get_settings(cache=True)
    except Exception as exc:
        return "unknown(FALLBACK: settings:{}:{})".format(type(exc).__name__, _short(exc))

    mode = (getattr(settings, "llm_mode", "") or "").strip().lower()
    if mode != "real":
        return mode or "unset"

    try:
        from app.services.llm.client import get_llm_client, _resolve_llm_model

        client = get_llm_client()
        api_key = (getattr(client, "api_key", "") or "").strip()
        model = (_resolve_llm_model() or "").strip()
        if not api_key:
            return "real(FALLBACK: missing api_key)"
        if not model:
            return "real(FALLBACK: missing model)"
        return "real(ok)"
    except Exception as exc:
        return "real(FALLBACK: {}:{})".format(type(exc).__name__, _short(exc))
