from fastapi import APIRouter

from app.services.llm.provider import provider

router = APIRouter(prefix="/health", tags=["health"])


@router.get("")
async def health_check():
    llm_status = "ok" if provider.enabled else "disabled"
    
    return {
        "status": "ok",
        "service": "ai-desktop-agent-server",
        "llm_status": llm_status,
        "llm_enabled": provider.enabled,
        "llm_model": provider.model if provider.enabled else None,
        "llm_base_url": provider.base_url if provider.enabled else None,
    }