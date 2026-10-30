
"""
Response sanitizer — strips internal debug markers so users never see
internal scaffolding in the output.
"""
import json
import re
from typing import Any


LEAKAGE_FALLBACK_MESSAGE = "I received your request and am processing it. If you need further analysis, please share more specific details."


class ResponseCleaner:
    """统一响应清洗工具类"""

    LEAKAGE_PATTERNS = [
        r"你是一个偏桌面效率.*?AI Agent",
        r"角色设定[：:]",
        r"最近对话记忆[：:]",
        r"当前可参考的系统摘要[：:]",
        r"系统监控摘要[：:]",
        r"语音上下文摘要[：:]",
        r"当前我先基于本地降级策略",
        r"内部改写要求",
        r"审核意见[：:]",
        r"原问题[：:]",
    ]

    # 需要清除的内部标记模式（按优先级排序）
    INTERNAL_PATTERNS = [
        # 审核相关标记
        (r'审核备注[：:]\s*[^\n]*', ''),
        (r'\[审核通过\]', ''),
        (r'\[审核拒绝\]', ''),

        # LLM配置/调试标记
        (r'LLM provider not configured[^\n]*', ''),
        (r'\[LLM[^\]]*\]', ''),
        (r'\[Provider[^\]]*\]', ''),

        # TTS相关标记
        (r'TTS 文案[：:]\s*', ''),
        (r'\[TTS[^\]]*\]', ''),

        # Rewritten Answer 标记
        (r'\[Rewritten Answer\s*\|?[^\]]*\]', ''),
        (r'Rewritten Answer[：:]\s*', ''),

        # 语音模式标记
        (r'\[mode=voice\]', ''),
        (r'\[voice[^\]]*\]', ''),
        (r'语音播报摘要[：:]\s*[^\n]*', ''),

        # 调试信息标记
        (r'\[DEBUG[^\]]*\]', ''),
        (r'\[INFO[^\]]*\]', ''),
        (r'\[WARN[^\]]*\]', ''),
        (r'\[ERROR[^\]]*\]', ''),

        # 执行相关标记
        (r'执行结果[：:]\s*[^\n]*', ''),
        (r'内部指令[：:]\s*[^\n]*', ''),

        # 意图分类结果（内部使用）
        (r'意图分类[：:]\s*[^\n]*', ''),
        (r'置信度[：:]\s*[^\n]*', ''),

        # Generator Draft 标记
        (r'\[Generator Draft[^\]]*\]', ''),
        (r'Generator Draft[：:]\s*', ''),
    ]

    @classmethod
    def _remove_code_block_wrappers(cls, text: str) -> str:
        """Strip wrapping Markdown code fences"""
        text = text.strip()
        if text.startswith('```') and text.endswith('```'):
            lines = text.splitlines()
            if len(lines) >= 2:
                first_line = lines[0].strip()
                if re.fullmatch(r'```(?:[a-zA-Z0-9_-]+)?', first_line, flags=re.IGNORECASE):
                    lines = lines[1:]
                if lines and lines[-1].strip() == '```':
                    lines = lines[:-1]
                return '\n'.join(lines).strip()
        return text

    @classmethod
    def contains_leakage(cls, text: str) -> bool:
        if not text:
            return False
        return any(re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL) for pattern in cls.LEAKAGE_PATTERNS)

    @classmethod
    def sanitize_user_input(cls, text: str) -> str:
        raw = str(text or "").strip()
        if not raw:
            return ""
        cut_positions: list[int] = []
        for pattern in cls.LEAKAGE_PATTERNS:
            match = re.search(pattern, raw, flags=re.IGNORECASE | re.DOTALL)
            if match:
                cut_positions.append(match.start())
        if cut_positions:
            raw = raw[: min(cut_positions)].strip()
        return raw

    @classmethod
    def clean(cls, text: str) -> str:
        """
        Clean internal markers out of text.

        Args:
            text: raw response text

        Returns:
            user-visible cleaned text
        """
        if not text:
            return text

        result = cls._remove_code_block_wrappers(text)

        # 按顺序应用所有清洗规则
        for pattern, replacement in cls.INTERNAL_PATTERNS:
            result = re.sub(pattern, replacement, result, flags=re.IGNORECASE)

        # 清理多余的空行（连续空行合并为最多一个）
        result = re.sub(r'\n{3,}', '\n\n', result)

        # 移除孤立的花括号或特殊字符（可能是内部数据结构残留）
        result = re.sub(r'^\s*[{}]\s*$', '', result, flags=re.MULTILINE)

        # 清理行首行尾空白
        result = result.strip()

        if cls.contains_leakage(result):
            return LEAKAGE_FALLBACK_MESSAGE

        return result

    @classmethod
    def clean_json_response(cls, data: Any) -> Any:
        """
        Clean every string field in a JSON response.

        Args:
            data: raw response dict

        Returns:
            sanitized response dict
        """
        if isinstance(data, dict):
            return {k: cls.clean_json_response(v) for k, v in data.items()}
        elif isinstance(data, list):
            return [cls.clean_json_response(item) for item in data]
        elif isinstance(data, str):
            return cls.clean(data)
        else:
            return data



def clean_fastapi_response_body(body: bytes) -> bytes:
    """Sanitize a FastAPI JSON response body; fall back to the raw body on failure."""
    try:
        data = json.loads(body.decode('utf-8'))
    except Exception:
        return body

    cleaned_data = ResponseCleaner.clean_json_response(data)
    return json.dumps(cleaned_data, ensure_ascii=False).encode('utf-8')


# 全局单例实例
cleaner = ResponseCleaner()
