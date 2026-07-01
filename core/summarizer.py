from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from .models import clean_text, json_loads


class MemorySummarizer:
    def __init__(self, *, max_input_chars: int = 6000, max_summary_chars: int = 1200):
        self.max_input_chars = max(1000, int(max_input_chars or 6000))
        self.max_summary_chars = max(300, int(max_summary_chars or 1200))

    def interval_elapsed(self, first_occurred_at: str, minutes: int) -> bool:
        if minutes <= 0:
            return False
        if not first_occurred_at:
            return False
        try:
            dt = datetime.fromisoformat(first_occurred_at.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except Exception:
            return False
        elapsed = datetime.now(timezone.utc) - dt
        return elapsed.total_seconds() >= minutes * 60

    async def summarize_with_provider(
        self,
        provider: Any,
        *,
        rows: list[dict[str, Any]],
        session_label: str,
        model: str = "",
    ) -> dict[str, Any] | None:
        if not rows:
            return None
        prompt = self._build_prompt(rows, session_label)
        if not prompt:
            return None
        kwargs: dict[str, Any] = {
            "prompt": prompt,
            "system_prompt": self._system_prompt(),
            "request_max_retries": 1,
        }
        if model:
            kwargs["model"] = model
        resp = await provider.text_chat(**kwargs)
        text = clean_text(getattr(resp, "completion_text", "") or "", self.max_summary_chars * 2)
        return self._normalize_payload(self._parse_response(text) or {}, rows)

    def compose_memory_content(self, payload: dict[str, Any]) -> str:
        canonical = clean_text(payload.get("canonical_summary"), self.max_summary_chars)
        if canonical:
            return canonical
        summary = clean_text(payload.get("summary"), self.max_summary_chars)
        key_facts = self._clean_list(payload.get("key_facts"), 8, 160)
        parts = [summary] if summary else []
        if key_facts:
            parts.append("；".join(key_facts))
        return clean_text(" | ".join(parts), self.max_summary_chars)

    def summary_quality(self, payload: dict[str, Any]) -> str:
        summary = clean_text(payload.get("summary"), 1000)
        key_facts = self._clean_list(payload.get("key_facts"), 8, 160)
        importance = payload.get("importance")
        try:
            importance_ok = 0.0 <= float(importance) <= 1.0
        except Exception:
            importance_ok = False
        generic_terms = ("某用户", "某人", "有人", "用户说", "对方说", "群成员", "某群成员")
        if len(summary) < 10 or not key_facts or not importance_ok:
            return "low"
        if any(term in summary for term in generic_terms):
            return "low"
        return "normal"

    def _build_prompt(self, rows: list[dict[str, Any]], session_label: str) -> str:
        transcript_lines: list[str] = []
        total = 0
        is_group = any(str(row.get("scope") or "") == "group" for row in rows)
        for row in rows:
            event_type = clean_text(row.get("event_type"), 40)
            metadata = json_loads(row.get("metadata"), {})
            if event_type == "bot_response" or row.get("subject_id") == "self":
                name = clean_text(metadata.get("sender_name") or "Bot", 80)
                speaker = f"Bot: {name}"
            else:
                name = clean_text(metadata.get("sender_name") or row.get("subject_id") or "未知", 80)
                speaker = name
            sender_id = clean_text(row.get("subject_id"), 80) or "unknown"
            occurred = clean_text(str(row.get("occurred_at") or "")[:16].replace("T", " "), 20)
            content = clean_text(row.get("content"), 700)
            if not content:
                continue
            line = f"[{speaker} | ID: {sender_id} | {occurred}] {content}"
            cost = len(line) + 1
            if transcript_lines and total + cost > self.max_input_chars:
                break
            transcript_lines.append(line)
            total += cost
        if not transcript_lines:
            return ""
        transcript = "\n".join(transcript_lines)
        participant_rule = (
            '\n  "participants": ["参与者昵称1", "参与者昵称2"],'
            if is_group
            else ""
        )
        group_rules = (
            "这是群聊窗口。必须列出 participants，并把每条关键事实关联到具体发言者昵称。"
            if is_group
            else "这是私聊窗口。必须把关键信息关联到当前私聊对象的具体昵称或稳定 ID。"
        )
        return (
            "请把下面这一段时间内的消息整理成长期记忆。不要逐句复述聊天记录；"
            "只保留未来对话真正有用的信息。没有依据的内容不要编造。\n\n"
            "重要规则：\n"
            "1. 必须区分 Bot 自己说的话和其他人说的话。以 [Bot: ...] 开头的是 Bot 自己的发言。\n"
            "2. 必须使用消息前缀里的具体昵称或稳定 ID，禁止用“用户、某用户、某人、有人、群成员”替代。\n"
            "3. 对话中的今天、明天、昨天、下周等相对时间，必须结合当前日期转换为具体日期后写入记忆。\n"
            f"4. {group_rules}\n"
            "5. summary 可以保留一点人格视角；canonical_summary 必须事实中性，适合检索。\n\n"
            "请只输出 JSON，不要 Markdown，不要解释。格式：\n"
            "{\n"
            '  "summary": "第一人称或贴近人格口吻的阶段性记忆摘要",\n'
            '  "canonical_summary": "事实中性、便于检索的一句话或短段落",\n'
            '  "topics": ["主题1", "主题2"],\n'
            '  "key_facts": ["具体昵称/ID 提到的关键事实1", "事实2"],'
            f"{participant_rule}\n"
            '  "sentiment": "positive|neutral|negative",\n'
            '  "importance": 0.7\n'
            "}\n\n"
            f"会话：{session_label}\n"
            f"当前日期：{datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
            "消息：\n"
            f"{transcript}"
        )

    def _parse_response(self, text: str) -> dict[str, Any] | None:
        if not text:
            return None
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            raw = text[start : end + 1]
            try:
                payload = json.loads(raw)
                if isinstance(payload, dict):
                    return payload
            except Exception:
                pass
        return {"summary": clean_text(text, self.max_summary_chars)}

    def _normalize_payload(self, payload: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
        payload = dict(payload or {})
        summary = clean_text(payload.get("summary"), self.max_summary_chars)
        key_facts = self._clean_list(
            payload.get("key_facts") or payload.get("facts"),
            8,
            160,
        )
        topics = self._clean_list(payload.get("topics"), 6, 80)
        participants = self._clean_list(payload.get("participants"), 10, 80)
        if not participants:
            participants = self._participants_from_rows(rows)
        sentiment = clean_text(payload.get("sentiment") or "neutral", 20).lower()
        if sentiment not in {"positive", "neutral", "negative"}:
            sentiment = "neutral"
        try:
            importance = max(0.0, min(1.0, float(payload.get("importance", 0.5))))
        except Exception:
            importance = 0.5
        canonical = clean_text(payload.get("canonical_summary"), self.max_summary_chars)
        if not canonical:
            parts = [summary] if summary else []
            if key_facts:
                parts.append("；".join(key_facts))
            canonical = clean_text(" | ".join(parts), self.max_summary_chars)
        payload.update(
            {
                "summary": summary,
                "persona_summary": clean_text(payload.get("persona_summary") or summary, self.max_summary_chars),
                "canonical_summary": canonical,
                "topics": topics,
                "key_facts": key_facts,
                "participants": participants,
                "sentiment": sentiment,
                "importance": importance,
            }
        )
        return payload

    def _participants_from_rows(self, rows: list[dict[str, Any]]) -> list[str]:
        participants: list[str] = []
        for row in rows:
            metadata = json_loads(row.get("metadata"), {})
            if row.get("subject_id") == "self" or row.get("event_type") == "bot_response":
                name = "Bot"
            else:
                name = clean_text(metadata.get("sender_name") or row.get("subject_id"), 80)
            if name and name not in participants:
                participants.append(name)
        return participants[:10]

    def _system_prompt(self) -> str:
        return (
            "你是长期记忆整理器。你的任务不是复述聊天记录，而是把一段短期消息整理成"
            "结构化、可检索、可长期使用的记忆。必须严格输出 JSON。"
        )

    def _clean_list(self, value: Any, limit: int, item_limit: int) -> list[str]:
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list):
            return []
        result: list[str] = []
        for item in value:
            text = clean_text(item, item_limit)
            if text and text not in result:
                result.append(text)
            if len(result) >= limit:
                break
        return result
