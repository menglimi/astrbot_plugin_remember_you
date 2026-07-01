from __future__ import annotations

import re
from typing import Any

from .identity import entity_for_current_target, entity_for_user, looks_like_command
from .models import EntityRef, MemoryRecord, SessionContext, clean_text


class MemoryClassifier:
    def __init__(self, capture_min_chars: int = 2):
        self.capture_min_chars = max(1, int(capture_min_chars or 1))

    def from_user_message(self, ctx: SessionContext) -> MemoryRecord | None:
        text = clean_text(ctx.message_text, 1800)
        if len(text) < self.capture_min_chars or looks_like_command(text):
            return None

        visibility = "group_public" if ctx.scope == "group" else "private_pair"
        content = self._format_user_content(ctx, text)
        importance = self._importance_for_text(text)
        return MemoryRecord(
            memory_type="conversation_event",
            subject=entity_for_user(ctx),
            object=entity_for_current_target(ctx),
            scope=ctx.scope,
            session_id=ctx.session_id,
            platform=ctx.platform,
            message_id=ctx.message_id,
            group_id=ctx.group_id,
            visibility=visibility,
            sayability="indirect",
            reality_level="observed_utterance",
            lifecycle="raw_event",
            content=content,
            evidence=text,
            confidence=0.72,
            importance=importance,
            review_status="auto",
            tags=["user_message", ctx.scope],
            metadata={"raw_text": text, "sender_name": ctx.user_name, "sender_id": ctx.user_id},
        )

    def from_bot_response(self, ctx: SessionContext, response_text: str) -> MemoryRecord | None:
        text = clean_text(response_text, 2000)
        if len(text) < self.capture_min_chars or looks_like_command(text):
            return None
        visibility = "group_public" if ctx.scope == "group" else "private_pair"
        target = entity_for_current_target(ctx)
        content = self._format_bot_content(ctx, text)
        return MemoryRecord(
            memory_type="self_action",
            subject=EntityRef.bot_self(),
            object=target,
            scope=ctx.scope,
            session_id=ctx.session_id,
            platform=ctx.platform,
            message_id=ctx.message_id,
            group_id=ctx.group_id,
            visibility=visibility,
            sayability="direct",
            reality_level="bot_action",
            lifecycle="raw_event",
            content=content,
            evidence=text,
            confidence=0.9,
            importance=self._importance_for_text(text),
            review_status="auto",
            tags=["bot_response", ctx.scope],
            metadata={"response_text": text},
        )

    def derived_user_memories(self, ctx: SessionContext, source_memory_id: str = "") -> list[MemoryRecord]:
        text = clean_text(ctx.message_text, 1800)
        if len(text) < self.capture_min_chars or looks_like_command(text):
            return []

        records: list[MemoryRecord] = []
        visibility = "group_public" if ctx.scope == "group" else "private_pair"
        base = {
            "subject": entity_for_user(ctx),
            "object": entity_for_current_target(ctx),
            "scope": ctx.scope,
            "session_id": ctx.session_id,
            "platform": ctx.platform,
            "message_id": ctx.message_id,
            "group_id": ctx.group_id,
            "visibility": visibility,
            "sayability": "direct",
            "reality_level": "real_user_fact",
            "lifecycle": "stable_memory",
            "confidence": 0.82,
            "importance": 0.68,
            "review_status": "auto",
            "source_plugin": "remember_you",
        }

        fact = self._extract_preference_or_profile(ctx, text)
        if fact:
            records.append(
                MemoryRecord(
                    memory_type=fact["memory_type"],
                    content=fact["content"],
                    evidence=text,
                    tags=fact["tags"],
                    metadata={"source_memory_id": source_memory_id, "extractor": "rule_v1"},
                    **base,
                )
            )

        claim = self._extract_relation_claim(ctx, text)
        if claim:
            review_status = "pending"
            records.append(
                MemoryRecord(
                    memory_type="relationship_claim",
                    content=claim["content"],
                    evidence=text,
                    tags=["relationship_claim", "needs_review"],
                    metadata={
                        "source_memory_id": source_memory_id,
                        "relation_type": claim["relation_type"],
                        "extractor": "rule_v1",
                    },
                    confidence=0.55,
                    importance=0.7,
                    review_status=review_status,
                    **{k: v for k, v in base.items() if k not in {"confidence", "importance", "review_status"}},
                )
            )

        return records

    def external_record(
        self,
        *,
        content: str,
        memory_type: str = "external_event",
        subject: EntityRef | None = None,
        object: EntityRef | None = None,
        scope: str = "unknown",
        session_id: str = "",
        platform: str = "",
        message_id: str = "",
        group_id: str = "",
        visibility: str = "bot_self",
        sayability: str = "direct",
        reality_level: str = "bot_action",
        lifecycle: str = "stable_memory",
        confidence: float = 0.85,
        importance: float = 0.5,
        review_status: str = "auto",
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        source_plugin: str = "external",
    ) -> MemoryRecord:
        return MemoryRecord(
            memory_type=memory_type,
            subject=subject or EntityRef.bot_self(),
            object=object or EntityRef(kind="session", id=session_id, role="target_session"),
            scope=scope,
            session_id=session_id,
            platform=platform,
            message_id=message_id,
            group_id=group_id,
            visibility=visibility,
            sayability=sayability,
            reality_level=reality_level,
            lifecycle=lifecycle,
            content=clean_text(content, 4000),
            evidence=clean_text(content, 4000),
            confidence=confidence,
            importance=importance,
            review_status=review_status,
            tags=tags or ["external"],
            metadata=metadata or {},
            source_plugin=source_plugin,
        )

    def _format_user_content(self, ctx: SessionContext, text: str) -> str:
        if ctx.scope == "group":
            name = ctx.user_name or ctx.user_id or "某个群成员"
            return f"群 {ctx.group_id or 'unknown'} 中，{name} 说过：{text}"
        name = ctx.user_name or ctx.user_id or "当前私聊用户"
        return f"私聊中，{name} 说过：{text}"

    def _format_bot_content(self, ctx: SessionContext, text: str) -> str:
        if ctx.scope == "group":
            return f"Bot 在群 {ctx.group_id or 'unknown'} 回复过：{text}"
        name = ctx.user_name or ctx.user_id or "当前私聊用户"
        return f"Bot 在与 {name} 的私聊中回复过：{text}"

    def _importance_for_text(self, text: str) -> float:
        score = 0.25
        if len(text) >= 30:
            score += 0.1
        if len(text) >= 80:
            score += 0.1
        important_markers = (
            "记住",
            "别忘",
            "喜欢",
            "讨厌",
            "生日",
            "名字",
            "主人",
            "朋友",
            "约定",
            "以后",
            "密码",
            "作品",
            "写了",
        )
        if any(marker in text for marker in important_markers):
            score += 0.25
        return min(0.85, score)

    def _extract_preference_or_profile(self, ctx: SessionContext, text: str) -> dict[str, Any] | None:
        name = ctx.user_name or ctx.user_id or "当前用户"
        patterns = [
            (r"(?:我|咱|俺)(?:很|最|超|特别|有点)?喜欢(.{1,40})", "user_preference", "喜欢"),
            (r"(?:我|咱|俺)(?:很|最|超|特别)?讨厌(.{1,40})", "user_preference", "讨厌"),
            (r"(?:我|咱|俺)(?:不喜欢)(.{1,40})", "user_preference", "不喜欢"),
            (r"(?:我|咱|俺)(?:的)?生日(?:是|在)?(.{2,20})", "user_profile", "生日"),
            (r"(?:以后|之后)?(?:叫我|喊我)(.{1,20})", "user_profile", "称呼"),
            (r"(?:记住|别忘了?|你要记得)(.{2,80})", "explicit_memory", "明确要求"),
        ]
        for pattern, memory_type, label in patterns:
            match = re.search(pattern, text)
            if not match:
                continue
            value = clean_text(match.group(1).strip(" ，。！？!?.：:"), 80)
            if not value:
                continue
            return {
                "memory_type": memory_type,
                "content": f"{name}明确表达过：{label} {value}",
                "tags": ["stable_fact", memory_type, label],
            }
        return None

    def _extract_relation_claim(self, ctx: SessionContext, text: str) -> dict[str, str] | None:
        name = ctx.user_name or ctx.user_id or "当前用户"
        compact = re.sub(r"\s+", "", text)
        relation_markers = [
            ("主人", "claimed_owner"),
            ("朋友", "claimed_friend"),
            ("同学", "claimed_classmate"),
            ("老师", "claimed_teacher"),
            ("室友", "claimed_roommate"),
            ("家人", "claimed_family"),
        ]
        for label, relation_type in relation_markers:
            if f"我是你的{label}" in compact or f"我是{label}" in compact or f"你是我的{label}" in compact:
                return {
                    "relation_type": relation_type,
                    "content": f"{name}声称与 Bot 的关系是：{label}。这类身份关系需要审核后才能当作稳定关系使用。",
                }
        return None
