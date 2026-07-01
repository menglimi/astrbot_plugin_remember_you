from __future__ import annotations

from typing import Any

from .models import SearchResult, SessionContext, clean_text

REMEMBER_YOU_INJECTION_HEADER = "<RememberYou-Context>"
REMEMBER_YOU_INJECTION_FOOTER = "</RememberYou-Context>"


class ContextComposer:
    def __init__(
        self,
        *,
        max_chars: int = 1200,
        overflow_strategy: str = "drop",
        max_events: int = 12,
        drop_events: int = 0,
        retain_recent_ratio: float = 0.15,
    ) -> None:
        self.max_chars = max(200, int(max_chars or 1200))
        self.overflow_strategy = overflow_strategy if overflow_strategy in {"drop", "summarize"} else "drop"
        self.max_events = max(0, int(max_events or 0))
        self.drop_events = max(0, int(drop_events or 0))
        try:
            ratio = float(retain_recent_ratio)
        except Exception:
            ratio = 0.15
        self.retain_recent_ratio = max(0.0, min(1.0, ratio))

    def compose(self, ctx: SessionContext, timeline_rows: list[dict[str, Any]]) -> str:
        if not timeline_rows:
            return ""

        lines = self.lines_for(ctx, timeline_rows)
        if not lines:
            return ""

        body = "\n".join(lines)
        if len(body) <= self.max_chars:
            return body
        if self.overflow_strategy == "drop":
            return self._drop_to_limit(lines)
        return self._summarize_to_limit(lines)

    def lines_for(self, ctx: SessionContext, timeline_rows: list[dict[str, Any]]) -> list[str]:
        if not timeline_rows:
            return []
        rows = list(reversed(timeline_rows[-self.max_events :])) if self.max_events > 0 else list(reversed(timeline_rows))
        lines = [self._line_for_event(ctx, row) for row in rows]
        return [line for line in lines if line]

    def drop_to_limit(self, lines: list[str]) -> str:
        return self._drop_to_limit(lines)

    def summarize_to_limit(self, lines: list[str]) -> str:
        return self._summarize_to_limit(lines)

    def _drop_to_limit(self, lines: list[str]) -> str:
        if self.drop_events > 0 and len(lines) > self.drop_events:
            candidate = lines[self.drop_events :]
            if candidate:
                lines = candidate
        kept: list[str] = []
        total = 0
        for line in reversed(lines):
            cost = len(line) + 1
            if kept and total + cost > self.max_chars:
                break
            kept.append(line)
            total += cost
        return "\n".join(reversed(kept))

    def _summarize_to_limit(self, lines: list[str]) -> str:
        recent = self.recent_tail(lines)
        recent_lines = recent.splitlines() if recent else []
        older_count = max(0, len(lines) - len(recent_lines))
        if older_count <= 0:
            return recent
        summary = f"- 较早上下文：前面还有 {older_count} 条同一会话事件，已压缩；优先顺着下面最近几条继续。"
        text = f"{summary}\n{recent}" if recent else summary
        if len(text) <= self.max_chars:
            return text
        return text[: self.max_chars - 1].rstrip() + "…"

    def recent_tail(self, lines: list[str]) -> str:
        if not lines:
            return ""
        if self.retain_recent_ratio > 0:
            keep_count = max(1, min(len(lines), int(round(len(lines) * self.retain_recent_ratio))))
            recent = lines[-keep_count:]
            text = "\n".join(recent)
            if len(text) <= self.max_chars:
                return text
        return self._drop_to_limit(lines)

    def _line_for_event(self, ctx: SessionContext, row: dict[str, Any]) -> str:
        event_type = clean_text(row.get("event_type"), 40)
        content = clean_text(row.get("content"), 260)
        if not content:
            return ""
        occurred = clean_text(str(row.get("occurred_at") or "")[:16].replace("T", " "), 20)
        speaker = "Bot" if event_type == "bot_response" or str(row.get("subject_id")) == "self" else "用户"
        if ctx.scope == "group" and speaker == "用户":
            speaker = f"群成员 {clean_text(row.get('subject_id'), 40) or 'unknown'}"
        return f"- {occurred}｜{speaker}：{content}"


class InjectionComposer:
    def compose(
        self,
        ctx: SessionContext,
        results: list[SearchResult],
        max_chars: int = 1800,
        *,
        short_context: str = "",
        intent_context: str = "",
        slot_sections: list[tuple[str, list[SearchResult]]] | None = None,
    ) -> str:
        if not results and not short_context and not intent_context:
            return ""

        allowed = "self_timeline, current_private, shareable"
        blocked = "other_private, unrelated_group, pending_review"
        if ctx.scope == "group":
            allowed = "self_timeline, current_group_public, shareable"
        lines = [
            "<remember_you_context>",
            "<instruction>",
            "这是 RememberYou 为本轮整理的临时附加资料，不是用户新发言，也不是新的回复任务。",
            "回复目标只能是本轮当前用户消息；先直接回应当前用户消息，再决定是否需要引用记忆补充。",
            "短期上下文和长期记忆只可作为辅助资料：只有与当前用户消息直接相关、能补全称呼/偏好/事实时才使用。",
            "禁止因为记忆或短期上下文主动延续旧话题；如果资料与当前用户消息无关或冲突，必须忽略资料。",
            "严格保留来源边界，不要把群聊、私聊、Bot 自我时间线混成同一件事。",
            "本包已经按可见性、ACL、审核状态和分槽上限过滤；不要自行推断或泄露其它窗口的私密内容。",
            f"允许使用：{allowed}；禁止使用：{blocked}。",
            "</instruction>",
            "",
            "<current_user_message>",
            clean_text(ctx.message_text, 500) or "未读取到文本；以 AstrBot 当前轮真实用户消息为准。",
            "</current_user_message>",
            "",
            "<current_window>",
            f"会话类型：{ctx.scope or 'unknown'}",
            f"当前对象：{ctx.label}",
            "</current_window>",
            "",
        ]
        if intent_context:
            lines.extend(
                [
                    "<retrieval_intent>",
                    intent_context,
                    "</retrieval_intent>",
                    "",
                ]
            )
        if short_context:
            lines.extend(
                [
                    "<short_context>",
                    short_context,
                    "</short_context>",
                    "",
                ]
            )

        lines.append("<long_term_memory>")
        if slot_sections:
            for slot_name, slot_results in slot_sections:
                if not slot_results:
                    continue
                lines.append(f"<{slot_name}>")
                self._append_results(lines, slot_results)
                lines.append(f"</{slot_name}>")
        else:
            self._append_results(lines, results)
        if not results:
            lines.append("- 没有检索到足够相关的长期记忆；只依据当前用户消息回复，短期上下文也只能用于确认是否承接当前消息。")
        lines.append("</long_term_memory>")
        lines.extend(
            [
                "",
                "</remember_you_context>",
            ]
        )

        limit = max(300, int(max_chars or 1800))
        inner_limit = max(120, limit - len(REMEMBER_YOU_INJECTION_HEADER) - len(REMEMBER_YOU_INJECTION_FOOTER) - 2)
        text = "\n".join(lines)
        if len(text) > inner_limit:
            text = text[: inner_limit - 1].rstrip() + "…"
        return f"{REMEMBER_YOU_INJECTION_HEADER}\n{text}\n{REMEMBER_YOU_INJECTION_FOOTER}"

    def _append_results(self, lines: list[str], results: list[SearchResult]) -> None:
        for item in results:
            memory = item.memory
            line = "｜".join(
                [
                    clean_text(memory.memory_type, 40),
                    clean_text(memory.occurred_at[:16].replace("T", " "), 20),
                    clean_text(memory.content, 220),
                    f"来源:{self._source_label(memory)}",
                    f"可见性:{memory.visibility}",
                    f"现实层:{memory.reality_level}",
                    f"置信:{memory.confidence:.2f}",
                    f"score:{item.score:.2f}",
                ]
            )
            lines.append(f"- {line}")

    def _source_label(self, memory) -> str:
        if memory.scope == "group":
            return f"群聊:{memory.group_id or memory.session_id or 'unknown'}"
        if memory.scope == "private":
            target = memory.object.name or memory.object.id or memory.session_id or "unknown"
            return f"私聊:{target}"
        if memory.visibility == "bot_self":
            return "Bot自我时间线"
        return memory.source_plugin or "unknown"
