from __future__ import annotations

import asyncio
from collections import defaultdict
import json
import hashlib
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .astrbot_compat import (
    append_temp_text,
    detect_private_companion_request,
    logger,
    manage_request_contexts,
    remove_temp_text,
    sanitize_request_history,
)
from .bridge import serialize_memory
from .classifier import MemoryClassifier
from .config import ConfigView
from .context_orchestrator import RetrievalIntent, RetrievalIntentBuilder
from .identity import IdentityResolver, maybe_await
from .injection import (
    REMEMBER_YOU_INJECTION_FOOTER,
    REMEMBER_YOU_INJECTION_HEADER,
    ContextComposer,
    InjectionComposer,
)
from .migration_livingmemory import LivingMemoryMigrator
from .models import EntityRef, MemoryRecord, SessionContext, clean_text, json_dumps, json_loads, utc_now
from .retrieval import RetrievalEngine
from .store import MemoryStore
from .summarizer import MemorySummarizer
from .turn_signal import analyze_turn_signal, message_terms
from .visibility import VisibilityPolicy


class RememberYouService:
    def __init__(self, *, context: Any, config: Any, plugin_root: Path, data_dir: Path):
        self.context = context
        self.config = ConfigView(config)
        self.plugin_root = Path(plugin_root)
        self.data_dir = Path(data_dir)

        self.store = MemoryStore(self.data_dir / "remember_you.db")
        self.store.initialize()
        normalized = self.store.normalize_legacy_manual_visibility()
        if normalized:
            logger.info("[RememberYou] 已收回早期过宽的手动记忆可见性: count=%s", normalized)

        self.identity = IdentityResolver()
        self.intent_builder = RetrievalIntentBuilder()
        self.classifier = MemoryClassifier(
            capture_min_chars=self.config.int("memory_capture.capture_min_chars", 2)
        )
        self.injection = InjectionComposer()
        self.summarizer = MemorySummarizer(
            max_input_chars=self.config.int("memory_summary.max_input_chars", 6000),
            max_summary_chars=self.config.int("memory_summary.max_summary_chars", 1200),
        )
        self._summary_locks: dict[str, asyncio.Lock] = {}
        self._decay_lock = asyncio.Lock()
        self._context_summary_cache: dict[str, dict[str, Any]] = {}
        self._context_summary_inflight: set[str] = set()
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self.migrator = LivingMemoryMigrator(self.store, self.plugin_root, self.data_dir)
        self.sleep_state_path = self.data_dir / "remember_you_sleep_state.json"

    async def handle_llm_request(self, event: Any, req: Any) -> None:
        ctx = await self.identity.resolve_event_context(event)
        await self.note_identity(ctx)
        self._sanitize_request_history_for_companion(ctx, req)

        if self.config.bool("memory_injection.enabled", True):
            await self.inject_memories(ctx, req, event=event)

        if self._private_companion_internal_generation_event(event):
            return

        if not self.config.bool("memory_capture.enabled", True):
            return
        if not self.config.bool("memory_capture.capture_user_messages", True):
            return
        record = self.classifier.from_user_message(ctx)
        if not record:
            return
        memory_id = ""
        if self.config.bool("memory_capture.store_raw_messages_as_memories", False):
            record.id = self.stable_id("user", ctx.session_id, ctx.message_id or ctx.message_text)
            memory_id = await self.store.insert_memory(record)
        await self.store.add_timeline_event(
            event_type="user_message",
            session_id=ctx.session_id,
            scope=ctx.scope,
            subject_id=ctx.user_id,
            object_id=ctx.current_target_id,
            content=ctx.message_text,
            metadata={"memory_id": memory_id, "sender_name": ctx.user_name},
        )
        if self.config.bool("memory_capture.record_relationship_edges", True):
            await self.note_relationships(ctx, source_memory_id=memory_id)
        if self.config.bool("memory_capture.extract_stable_facts", True):
            for derived in self.classifier.derived_user_memories(ctx, source_memory_id=memory_id):
                derived.id = self.stable_id("derived", derived.memory_type, ctx.session_id, derived.content)
                derived_id = await self.store.insert_memory(
                    derived,
                    review_reason="身份/关系声明需要人工确认" if derived.review_status == "pending" else "",
                )
                relation_type = str(derived.metadata.get("relation_type") or "")
                if relation_type and self.config.bool("memory_capture.record_relationship_edges", True):
                    await self.store.upsert_relationship(
                        subject=derived.subject,
                        object=EntityRef.bot_self(),
                        relation_type=relation_type,
                        scope=ctx.scope,
                        session_id=ctx.session_id,
                        group_id=ctx.group_id,
                        visibility=derived.visibility,
                        evidence=derived.evidence,
                        confidence=derived.confidence,
                        review_status=derived.review_status,
                        source_memory_id=derived_id,
                        metadata={"source": "relationship_claim"},
                    )
        self._schedule_context_precompression(ctx)
        if not self.config.bool("memory_capture.capture_bot_responses", True):
            self._schedule_session_summary(ctx, reason="after_user_message")

    async def handle_llm_response(self, event: Any, resp: Any) -> None:
        if self._private_companion_internal_generation_event(event):
            return
        if not self.config.bool("memory_capture.enabled", True):
            return
        if not self.config.bool("memory_capture.capture_bot_responses", True):
            return
        if getattr(resp, "role", "") != "assistant":
            return
        if getattr(resp, "tools_call_name", None) or getattr(resp, "tools_call_extra_content", None):
            return

        text = clean_text(getattr(resp, "completion_text", "") or "", 2000)
        if not text:
            return

        ctx = await self.identity.resolve_event_context(event)
        record = self.classifier.from_bot_response(ctx, text)
        if not record:
            return

        memory_id = ""
        if self.config.bool("memory_capture.store_raw_messages_as_memories", False):
            record.id = self.stable_id("bot", ctx.session_id, text)
            memory_id = await self.store.insert_memory(record)
        await self.store.add_timeline_event(
            event_type="bot_response",
            session_id=ctx.session_id,
            scope=ctx.scope,
            subject_id="self",
            object_id=ctx.current_target_id,
            content=text,
            metadata={"memory_id": memory_id},
        )
        self._schedule_context_precompression(ctx)
        self._schedule_session_summary(ctx, reason="after_bot_response")

    def _private_companion_internal_generation_event(self, event: Any) -> bool:
        if event is None:
            return False
        if bool(getattr(event, "private_companion_proactive_framework", False)):
            return True
        text = clean_text(getattr(event, "message_str", "") or "", 1200)
        if not text:
            return False
        return "这不是用户消息" in text and "Private Companion" in text and "主动消息" in text

    async def record_external_event(self, **kwargs: Any) -> str:
        if not self.config.bool("private_companion_bridge.accept_external_records", True):
            raise RuntimeError("外部记忆写入已关闭")

        explicit_id = clean_text(kwargs.pop("memory_id", "") or kwargs.pop("id", ""), 120)
        record = self.classifier.external_record(**kwargs)
        if explicit_id:
            record.id = explicit_id
        elif not record.id:
            record.id = self.stable_id(
                kwargs.get("source_plugin", "external"),
                kwargs.get("session_id", ""),
                kwargs.get("content", ""),
            )

        memory_id = await self.store.insert_memory(record)
        if record.reality_level == "bot_action":
            await self.store.add_timeline_event(
                event_type=record.memory_type,
                session_id=record.session_id,
                scope=record.scope,
                subject_id=record.subject.id or "self",
                object_id=record.object.id,
                content=record.content,
                metadata={"memory_id": memory_id, "source_plugin": record.source_plugin},
            )
        return memory_id

    async def bridge_search(
        self,
        query: str,
        *,
        session_context: SessionContext | dict[str, Any] | None = None,
        top_k: int | None = None,
    ) -> list[dict[str, Any]]:
        ctx = self.session_context_from_bridge(session_context)
        results = await self.search(query, ctx, top_k or self.config.int("memory_injection.top_k", 6))
        return [serialize_memory(item.memory, item.score, item.reason) for item in results]

    async def bridge_compose_injection(
        self,
        query: str,
        *,
        session_context: SessionContext | dict[str, Any] | None = None,
        top_k: int | None = None,
        max_chars: int | None = None,
    ) -> str:
        ctx = self.session_context_from_bridge(session_context)
        intent = self.intent_builder.build(ctx, explicit_query=query, use_companion_hints=False)
        results, _blocked, slot_map = await self.search_context_slots(
            intent.query, ctx, top_k or self.config.int("memory_injection.top_k", 6)
        )
        short_context = await self.short_context_for_session(ctx)
        return self.injection.compose(
            ctx,
            results,
            max_chars or self.config.int("memory_injection.max_chars", 1800),
            short_context=short_context,
            intent_context=self._intent_context_for_injection(intent),
            slot_sections=self._slot_sections(slot_map),
        )

    async def bridge_compose_context(
        self,
        *,
        query: str = "",
        session_context: SessionContext | dict[str, Any] | None = None,
        top_k: int | None = None,
        max_chars: int | None = None,
    ) -> str:
        ctx = self.session_context_from_bridge(session_context)
        intent = self.intent_builder.build(ctx, explicit_query=query or ctx.message_text, use_companion_hints=False)
        results, _blocked, slot_map = await self.search_context_slots(
            intent.query, ctx, top_k or self.config.int("memory_injection.top_k", 6)
        )
        short_context = await self.short_context_for_session(ctx)
        return self.injection.compose(
            ctx,
            results,
            max_chars or self.config.int("memory_injection.max_chars", 1800),
            short_context=short_context,
            intent_context=self._intent_context_for_injection(intent),
            slot_sections=self._slot_sections(slot_map),
        )

    async def search(self, query: str, ctx: SessionContext, top_k: int = 6):
        engine = RetrievalEngine(self.store, self.visibility_policy())
        return await engine.search(query, ctx, top_k)

    async def search_with_diagnostics(self, query: str, ctx: SessionContext, top_k: int = 6):
        engine = RetrievalEngine(self.store, self.visibility_policy())
        return await engine.search_with_diagnostics(query, ctx, top_k)

    async def search_context_slots(self, query: str, ctx: SessionContext, top_k: int = 6):
        engine = RetrievalEngine(self.store, self.visibility_policy())
        if not self.config.bool("context_orchestration.enabled", True):
            results, blocked = await engine.search_with_diagnostics(query, ctx, top_k)
            return results, blocked, {"stable_memory": results}
        return await engine.search_by_slots(
            query,
            ctx,
            slot_limits=self._slot_limits(top_k),
            total_limit=top_k,
        )

    def _spawn_background(self, coro: Any, *, label: str) -> None:
        try:
            task = asyncio.create_task(coro, name=f"remember_you:{label}")
        except RuntimeError:
            close = getattr(coro, "close", None)
            if callable(close):
                close()
            logger.warning("[RememberYou] 无运行事件循环，后台任务未启动: %s", label)
            return
        self._background_tasks.add(task)

        def _done(done_task: asyncio.Task[Any]) -> None:
            self._background_tasks.discard(done_task)
            if done_task.cancelled():
                return
            try:
                exc = done_task.exception()
            except Exception as error:
                logger.warning("[RememberYou] 读取后台任务状态失败: %s error=%s", label, error)
                return
            if exc:
                logger.warning(
                    "[RememberYou] 后台任务异常: %s error=%s",
                    label,
                    exc,
                    exc_info=(type(exc), exc, exc.__traceback__),
                )

        task.add_done_callback(_done)

    def _snapshot_context(self, ctx: SessionContext) -> SessionContext:
        return SessionContext(
            session_id=ctx.session_id,
            scope=ctx.scope,
            platform=ctx.platform,
            user_id=ctx.user_id,
            user_name=ctx.user_name,
            group_id=ctx.group_id,
            bot_id=ctx.bot_id,
            message_id=ctx.message_id,
            message_text=ctx.message_text,
        )

    def _schedule_session_summary(self, ctx: SessionContext, *, reason: str) -> None:
        if not self.config.bool("memory_summary.enabled", True):
            return
        snapshot = self._snapshot_context(ctx)
        self._spawn_background(self._background_summarize_session(snapshot, reason), label=f"summary:{reason}")

    async def _background_summarize_session(self, ctx: SessionContext, reason: str) -> None:
        memory_id = await self.maybe_summarize_session(ctx)
        if memory_id:
            logger.info("[RememberYou] 后台阶段性总结完成: session=%s reason=%s memory=%s", ctx.session_id, reason, memory_id)

    async def maybe_summarize_session(self, ctx: SessionContext, *, force: bool = False) -> str:
        if not force and not self.config.bool("memory_summary.enabled", True):
            return ""
        if not ctx.session_id:
            return ""

        lock = self._summary_locks.setdefault(ctx.session_id, asyncio.Lock())
        if lock.locked():
            return ""
        async with lock:
            window = await self.store.unsummarized_timeline_window(
                session_id=ctx.session_id,
                scope=ctx.scope,
                limit=self.config.int("memory_summary.max_events_per_summary", 40),
            )
            rows = list(window.get("rows") or [])
            total = int(window.get("total") or 0)
            min_events = self.config.int("memory_summary.min_events", 8)
            trigger_count = self.config.int("memory_summary.trigger_event_count", 12)
            trigger_minutes = self.config.int("memory_summary.trigger_interval_minutes", 60)
            if total < (1 if force else min_events):
                return ""
            count_ready = total >= max(min_events, trigger_count)
            time_ready = self.summarizer.interval_elapsed(
                str(window.get("first_occurred_at") or ""),
                trigger_minutes,
            )
            if not force and not count_ready and not time_ready:
                return ""

            failure = await self.store.get_summary_failure(ctx.session_id)
            max_retries = self.config.int("memory_summary.max_retries", 3)
            if failure and not force and int(failure.get("retry_count") or 0) >= max_retries:
                marked = await self.store.mark_timeline_summarized([str(row.get("id") or "") for row in rows])
                await self.store.clear_summary_failure(ctx.session_id)
                logger.warning(
                    "[RememberYou] 阶段性记忆总结连续失败已达上限，跳过当前窗口: session=%s retries=%s marked=%s last_error=%s",
                    ctx.session_id,
                    failure.get("retry_count"),
                    marked,
                    clean_text(failure.get("last_error"), 160),
                )
                return ""

            summary_attempts = await self._summary_provider_attempts(ctx)
            if not summary_attempts:
                logger.warning("[RememberYou] 无可用 Provider，跳过阶段性记忆总结: session=%s", ctx.session_id)
                return ""

            payload = None
            content = ""
            used_summary = {}
            last_error: Exception | None = None
            try:
                for attempt in summary_attempts:
                    try:
                        payload = await self.summarizer.summarize_with_provider(
                            attempt["provider"],
                            rows=rows,
                            session_label=ctx.label,
                            model=attempt["model"],
                        )
                        content = self.summarizer.compose_memory_content(payload or {})
                        if content:
                            used_summary = attempt
                            break
                        last_error = RuntimeError("empty summary content")
                        logger.warning(
                            "[RememberYou] 阶段性总结候选返回空内容，尝试下一个: session=%s provider=%s",
                            ctx.session_id,
                            attempt["provider_id"] or attempt["source"],
                        )
                    except Exception as exc:
                        last_error = exc
                        logger.warning(
                            "[RememberYou] 阶段性总结候选失败，尝试下一个: session=%s provider=%s error=%s",
                            ctx.session_id,
                            attempt["provider_id"] or attempt["source"],
                            exc,
                            exc_info=True,
                        )
            except Exception as exc:
                last_error = exc
            if not content:
                retries = await self.store.record_summary_failure(
                    session_id=ctx.session_id,
                    scope=ctx.scope,
                    start_timeline_id=str(rows[0].get("id") if rows else ""),
                    end_timeline_id=str(rows[-1].get("id") if rows else ""),
                    error=str(last_error or "summary failed"),
                    metadata={"reason": "provider_or_parse_error", "force": force},
                )
                logger.warning("[RememberYou] 阶段性记忆总结全部失败: session=%s error=%s", ctx.session_id, last_error)
                logger.warning("[RememberYou] 已记录阶段性总结待重试: session=%s retry=%s/%s", ctx.session_id, retries, max_retries)
                return ""

            visibility = "group_public" if ctx.scope == "group" else "private_pair"
            start_at = clean_text(rows[0].get("occurred_at") if rows else "", 80)
            end_at = clean_text(rows[-1].get("occurred_at") if rows else "", 80)
            evidence = "\n".join(
                clean_text(row.get("content"), 220)
                for row in rows[: self.config.int("memory_summary.evidence_events", 12)]
                if clean_text(row.get("content"), 220)
            )
            record = MemoryRecord(
                id=self.stable_id("summary", ctx.session_id, start_at, end_at, content),
                memory_type="conversation_summary",
                subject=EntityRef(kind="user", id=ctx.user_id, name=ctx.user_name, role="conversation_partner"),
                object=EntityRef.bot_self() if ctx.scope != "group" else EntityRef(kind="group", id=ctx.group_id, role="group"),
                scope=ctx.scope,
                session_id=ctx.session_id,
                platform=ctx.platform,
                group_id=ctx.group_id,
                visibility=visibility,
                sayability="direct",
                reality_level="llm_summary",
                lifecycle="stable_memory",
                content=content,
                evidence=evidence,
                confidence=0.72,
                importance=float((payload or {}).get("importance", 0.68) or 0.68),
                review_status="auto",
                tags=["summary", "long_term", ctx.scope] + [clean_text(topic, 80) for topic in (payload or {}).get("topics", [])[:5]],
                metadata={
                    "summary_event_count": len(rows),
                    "unsummarized_total": total,
                    "start_at": start_at,
                    "end_at": end_at,
                    "summarizer": "livingmemory_schema_v1",
                    "summary_schema_version": "livingmemory_like_v1",
                    "summary_quality": self.summarizer.summary_quality(payload or {}),
                    "canonical_summary": clean_text((payload or {}).get("canonical_summary"), 2000),
                    "persona_summary": clean_text((payload or {}).get("persona_summary") or (payload or {}).get("summary"), 2000),
                    "topics": (payload or {}).get("topics", []),
                    "key_facts": (payload or {}).get("key_facts", []),
                    "participants": (payload or {}).get("participants", []),
                    "sentiment": clean_text((payload or {}).get("sentiment"), 20),
                    "summary_provider_id": clean_text(used_summary.get("provider_id"), 120),
                    "summary_provider_source": clean_text(used_summary.get("source"), 40),
                    "summary_model": clean_text(used_summary.get("model"), 120),
                },
            )
            memory_id = await self.store.insert_memory(record)
            marked = await self.store.mark_timeline_summarized([str(row.get("id") or "") for row in rows])
            await self.store.clear_summary_failure(ctx.session_id)
            logger.info(
                "[RememberYou] 已生成阶段性长期记忆: session=%s memory=%s events=%s marked=%s",
                ctx.session_id,
                memory_id,
                len(rows),
                marked,
            )
            return memory_id

    async def _summary_provider_attempts(self, ctx: SessionContext) -> list[dict[str, Any]]:
        return await self._provider_attempts(
            ctx,
            prefix="memory_summary",
            provider_key="provider_id",
            model_key="model",
            fallback_provider_key="fallback_provider_id",
            fallback_model_key="fallback_model",
            include_current=True,
        )

    async def _context_summary_provider_attempts(self, ctx: SessionContext) -> list[dict[str, Any]]:
        attempts: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        configured = [
            (
                "primary",
                clean_text(self._context_str(ctx, "summary_provider_id", ""), 120),
                clean_text(self._context_str(ctx, "summary_model", ""), 120),
            ),
            (
                "fallback",
                clean_text(self._context_str(ctx, "summary_fallback_provider_id", ""), 120),
                clean_text(self._context_str(ctx, "summary_fallback_model", ""), 120),
            ),
        ]
        for source, provider_id, model in configured:
            if not provider_id:
                continue
            provider = await self._provider_by_id(provider_id, ctx, source)
            if provider is None:
                continue
            key = (provider_id, model)
            if key in seen:
                continue
            seen.add(key)
            attempts.append(
                {
                    "source": source,
                    "provider_id": provider_id,
                    "provider": provider,
                    "model": model,
                }
            )
        return attempts

    async def _provider_attempts(
        self,
        ctx: SessionContext,
        *,
        prefix: str,
        provider_key: str,
        model_key: str,
        fallback_provider_key: str,
        fallback_model_key: str,
        include_current: bool,
    ) -> list[dict[str, Any]]:
        attempts: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        configured = [
            (
                "primary",
                clean_text(self.config.get(f"{prefix}.{provider_key}", ""), 120),
                clean_text(self.config.get(f"{prefix}.{model_key}", ""), 120),
            ),
            (
                "fallback",
                clean_text(self.config.get(f"{prefix}.{fallback_provider_key}", ""), 120),
                clean_text(self.config.get(f"{prefix}.{fallback_model_key}", ""), 120),
            ),
        ]
        for source, provider_id, model in configured:
            if not provider_id:
                continue
            provider = await self._provider_by_id(provider_id, ctx, source)
            if provider is None:
                continue
            key = (provider_id, model)
            if key in seen:
                continue
            seen.add(key)
            attempts.append(
                {
                    "source": source,
                    "provider_id": provider_id,
                    "provider": provider,
                    "model": model,
                }
            )

        if not include_current:
            return attempts
        current = await self._current_provider(ctx)
        if current is not None:
            key = ("", "")
            if key not in seen:
                attempts.append(
                    {
                        "source": "current_session",
                        "provider_id": "",
                        "provider": current,
                        "model": "",
                    }
                )
        return attempts

    async def _summary_provider(self, ctx: SessionContext) -> Any:
        attempts = await self._summary_provider_attempts(ctx)
        return attempts[0]["provider"] if attempts else None

    async def _provider_by_id(self, provider_id: str, ctx: SessionContext, source: str) -> Any:
        provider_id = clean_text(provider_id, 120)
        if not provider_id or self.context is None:
            return None
        getter = getattr(self.context, "get_provider_by_id", None)
        if not callable(getter):
            return None
        try:
            provider = await maybe_await(getter(provider_id))
            if provider is not None:
                return provider
            logger.warning(
                "[RememberYou] 总结模型提供商不可用: source=%s provider_id=%s session=%s",
                source,
                provider_id,
                ctx.session_id,
            )
        except Exception as exc:
            logger.warning(
                "[RememberYou] 获取总结模型提供商失败: source=%s provider_id=%s error=%s",
                source,
                provider_id,
                exc,
                exc_info=True,
            )
        return None

    async def _current_provider(self, ctx: SessionContext) -> Any:
        if self.context is None:
            return None
        provider_getter = getattr(self.context, "get_using_provider", None)
        if not callable(provider_getter):
            return None
        try:
            return await maybe_await(provider_getter(ctx.session_id))
        except Exception as exc:
            logger.warning("[RememberYou] 获取当前会话 Provider 失败: session=%s error=%s", ctx.session_id, exc)
            return None

    async def search_for_event(self, event: Any, query: str, top_k: int = 6):
        ctx = await self.identity.resolve_event_context(event)
        return await self.search(query, ctx, top_k)

    async def explain_for_event(self, event: Any, query: str, top_k: int = 6):
        ctx = await self.identity.resolve_event_context(event)
        return await self.search_with_diagnostics(query, ctx, top_k)

    async def explain_context_for_event(self, event: Any, query: str, top_k: int = 6):
        ctx = await self.identity.resolve_event_context(event)
        intent = self.intent_builder.build(
            ctx,
            event=event,
            explicit_query=query,
            use_companion_hints=self.config.bool("context_orchestration.use_companion_hints", False),
            query_mode=str(self.config.get("context_orchestration.query_mode", "") or ""),
        )
        selected, blocked, slot_map = await self.search_context_slots(intent.query, ctx, top_k)
        return intent, selected, blocked, slot_map

    async def add_manual_memory(self, event: Any, content: str) -> str:
        ctx = await self.identity.resolve_event_context(event)
        visibility = "internal"
        if ctx.scope == "private":
            visibility = "private_pair"
        elif ctx.scope == "group":
            visibility = "group_public"
        record = MemoryRecord(
            memory_type="manual_memory",
            subject=EntityRef(kind="user", id=ctx.user_id, name=ctx.user_name, role="admin"),
            object=EntityRef.bot_self(),
            scope=ctx.scope,
            session_id=ctx.session_id,
            platform=ctx.platform,
            group_id=ctx.group_id,
            visibility=visibility,
            sayability="direct",
            reality_level="real_user_fact",
            lifecycle="stable_memory",
            content=content,
            evidence=content,
            confidence=0.9,
            importance=0.75,
            review_status="auto",
            tags=["manual"],
        )
        return await self.store.insert_memory(record)

    async def tool_remember(self, event: Any, content: str, *, note_type: str = "memory") -> dict[str, Any]:
        ctx = await self.identity.resolve_event_context(event)
        content = clean_text(content, 3000)
        note_type = clean_text(note_type, 40) or "memory"
        if not content:
            return {"ok": False, "error": "empty content"}
        visibility = "internal"
        if ctx.scope == "private":
            visibility = "private_pair"
        elif ctx.scope == "group":
            visibility = "group_public"
        auto_approve = self.config.bool("memory_tools.auto_approve_tool_memories", False)
        record = MemoryRecord(
            id=self.stable_id("tool", note_type, ctx.session_id, content),
            memory_type="tool_memory",
            subject=EntityRef.bot_self(),
            object=EntityRef(kind="user", id=ctx.user_id, name=ctx.user_name, role="conversation_partner"),
            scope=ctx.scope,
            session_id=ctx.session_id,
            platform=ctx.platform,
            message_id=ctx.message_id,
            group_id=ctx.group_id,
            visibility=visibility,
            sayability="indirect",
            reality_level="llm_tool_assertion",
            lifecycle="stable_memory",
            content=content,
            evidence=ctx.message_text,
            confidence=0.62,
            importance=0.66,
            review_status="auto" if auto_approve else "pending",
            tags=["llm_tool", note_type, ctx.scope],
            metadata={"tool": "remember_you_remember", "note_type": note_type},
        )
        memory_id = await self.store.insert_memory(
            record,
            review_reason="" if auto_approve else "LLM 主动记忆需要人工确认",
        )
        return {"ok": True, "memory_id": memory_id, "review_status": record.review_status}

    async def tool_recall(self, event: Any, query: str, top_k: int = 5) -> dict[str, Any]:
        ctx = await self.identity.resolve_event_context(event)
        query = clean_text(query, 1000)
        if not query:
            return {"ok": False, "error": "empty query", "memories": []}
        results = await self.search(query, ctx, max(1, min(10, int(top_k or 5))))
        return {
            "ok": True,
            "memories": [serialize_memory(item.memory, item.score, item.reason) for item in results],
        }

    async def tool_note_create(self, event: Any, title: str, content: str = "") -> dict[str, Any]:
        ctx = await self.identity.resolve_event_context(event)
        title = clean_text(title, 120)
        content = clean_text(content or title, 3000)
        if not title and not content:
            return {"ok": False, "error": "empty note"}
        record = MemoryRecord(
            id=self.stable_id("companion_note", ctx.session_id, title, content),
            memory_type="companion_note",
            subject=EntityRef.bot_self(),
            object=EntityRef(kind="session", id=ctx.session_id, role="companion_context"),
            scope="unknown",
            session_id=ctx.session_id,
            platform=ctx.platform,
            visibility="bot_self",
            sayability="indirect",
            reality_level="persona_life",
            lifecycle="stable_memory",
            content=content,
            evidence=ctx.message_text,
            confidence=0.82,
            importance=0.6,
            review_status="auto",
            tags=["companion_note", "bot_self", title] if title else ["companion_note", "bot_self"],
            metadata={"title": title, "tool": "remember_you_note_create"},
            source_plugin="remember_you_tool",
        )
        memory_id = await self.store.insert_memory(record)
        return {"ok": True, "memory_id": memory_id, "title": title}

    async def tool_note_read(self, event: Any, query: str = "", limit: int = 5) -> dict[str, Any]:
        ctx = await self.identity.resolve_event_context(event)
        query = clean_text(query, 500)
        records = await self.store.list_memories(
            limit=max(1, min(20, int(limit or 5))),
            include_pending=True,
            query=query,
            memory_type="companion_note",
            visibility="bot_self",
        )
        return {
            "ok": True,
            "notes": [
                {
                    "id": record.id,
                    "title": clean_text(record.metadata.get("title"), 120) if isinstance(record.metadata, dict) else "",
                    "content": record.content,
                    "created_at": record.created_at,
                    "session_id": record.session_id or ctx.session_id,
                }
                for record in records
            ],
        }

    async def import_livingmemory(self, *, configured_path: str = "") -> dict[str, Any]:
        if self.config.bool("maintenance.backup_before_import", True):
            backup = self.store.backup(".before_livingmemory_import")
            logger.info("[RememberYou] LivingMemory 导入前已备份数据库: %s", backup)
        return await self.migrator.import_data(
            configured_path=configured_path,
            default_review_status=str(
                self.config.get("livingmemory_migration.default_review_status", "pending") or "pending"
            ),
            limit=self.config.int("livingmemory_migration.import_limit", 5000),
        )

    async def sleep_maintenance(self, *, reason: str = "manual") -> dict[str, Any]:
        backup = ""
        if self.config.bool("maintenance.sleep_backup_enabled", False):
            backup = str(self.store.backup(".before_sleep_maintenance"))
        repair = await self.store.maintenance_repair()
        raw_retention = await self._run_raw_event_retention()
        decay = await self._run_memory_decay()
        stats = await self.store.stats()
        state = {
            "ok": True,
            "reason": clean_text(reason, 80),
            "ran_at": utc_now(),
            "backup": backup,
            "repair": repair,
            "raw_retention": raw_retention,
            "decay": decay,
            "stats": {
                "total_memories": stats.get("total_memories", 0),
                "stable_memories": stats.get("stable_memories", 0),
                "pending_review": stats.get("pending_review", 0),
                "timeline_events": stats.get("timeline_events", 0),
                "injection_logs": stats.get("injection_logs", 0),
            },
        }
        self.sleep_state_path.write_text(json_dumps(state), encoding="utf-8")
        return state

    def sleep_status(self) -> dict[str, Any]:
        if not self.sleep_state_path.exists():
            return {"ok": True, "ran_at": "", "message": "还没有执行过睡眠维护。"}
        try:
            return json_loads(self.sleep_state_path.read_text(encoding="utf-8"), {})
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    async def _run_raw_event_retention(self) -> dict[str, Any]:
        days = self.config.int("maintenance.retention_raw_event_days", 7)
        if days <= 0:
            return {"enabled": False, "archived": 0, "reason": "disabled"}
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        archived = await self.store.archive_raw_events_older_than(
            cutoff.isoformat(timespec="seconds"),
            limit=self.config.int("maintenance.retention_raw_event_limit", 1000),
        )
        return {"enabled": True, "days": days, "archived": archived}

    async def _run_memory_decay(self) -> dict[str, Any]:
        if not self.config.bool("maintenance.memory_decay_enabled", True):
            return {"enabled": False, "reason": "disabled", "candidates": 0, "archived": 0, "summaries": 0}
        if self._decay_lock.locked():
            return {"enabled": True, "reason": "already_running", "candidates": 0, "archived": 0, "summaries": 0}

        async with self._decay_lock:
            max_candidates = max(1, self.config.int("maintenance.memory_decay_max_candidates", 120))
            scan_limit = max(max_candidates * 12, self.config.int("maintenance.memory_decay_scan_limit", 2000))
            pool = await self.store.list_decay_candidate_pool(limit=scan_limit)
            candidates: list[dict[str, Any]] = []
            for record in pool:
                item = self._decay_candidate(record)
                if not item:
                    continue
                candidates.append(item)
                if len(candidates) >= max_candidates:
                    break

            if not candidates:
                return {
                    "enabled": True,
                    "scanned": len(pool),
                    "candidates": 0,
                    "archived": 0,
                    "summaries": 0,
                    "reason": "no_eligible_memories",
                }

            groups = self._decay_groups(candidates)
            max_groups = max(1, self.config.int("maintenance.memory_decay_max_groups", 8))
            min_items = max(2, self.config.int("maintenance.memory_decay_min_items_per_summary", 4))
            summaries = 0
            archived = 0
            skipped_groups = 0
            errors: list[str] = []
            group_reports: list[dict[str, Any]] = []

            for group in groups[:max_groups]:
                items = list(group.get("items") or [])
                if len(items) < min_items:
                    skipped_groups += 1
                    continue
                try:
                    result = await self._summarize_decay_group(group)
                    if result.get("summary_id"):
                        summaries += 1
                    archived += int(result.get("archived") or 0)
                    group_reports.append(result)
                except Exception as exc:
                    skipped_groups += 1
                    errors.append(clean_text(str(exc), 180))
                    logger.warning(
                        "[RememberYou] 睡眠衰减总结失败: bucket=%s error=%s",
                        group.get("bucket"),
                        exc,
                        exc_info=True,
                    )

            return {
                "enabled": True,
                "scanned": len(pool),
                "candidates": len(candidates),
                "groups": len(groups),
                "summaries": summaries,
                "archived": archived,
                "skipped_groups": skipped_groups,
                "reports": group_reports[:10],
                "errors": errors[:5],
            }

    def _decay_candidate(self, record: MemoryRecord) -> dict[str, Any] | None:
        memory_type = clean_text(record.memory_type, 80).lower()
        tags = {clean_text(tag, 80).lower() for tag in (record.tags or [])}
        protected_types = {
            "manual_memory",
            "user_profile",
            "user_preference",
            "explicit_memory",
            "relationship_claim",
            "companion_note",
            "schedule_fragment",
            "creative_work",
            "reading_memory",
            "proactive_message",
        }
        protected_tags = {
            "manual",
            "stable_fact",
            "relationship_claim",
            "needs_review",
            "protected",
            "no_decay",
            "keep",
        }
        if memory_type in protected_types or tags & protected_tags:
            return None
        if record.visibility == "bot_self" and not self.config.bool("maintenance.memory_decay_include_bot_self", False):
            return None
        if record.source_plugin == "remember_you_tool" and not self.config.bool(
            "maintenance.memory_decay_include_tool_memories",
            False,
        ):
            return None
        max_importance = self._config_percent("maintenance.memory_decay_max_importance_percent", 74)
        if record.importance > max_importance:
            return None
        if record.access_count > self.config.int("maintenance.memory_decay_max_access_count", 2):
            return None

        now = datetime.now(timezone.utc)
        anchor = self._parse_iso(record.occurred_at or record.created_at)
        accessed = self._parse_iso(record.last_accessed_at) or anchor
        if anchor is None:
            return None
        age_days = max(0.0, (now - anchor).total_seconds() / 86400)
        idle_days = max(0.0, (now - (accessed or anchor)).total_seconds() / 86400)
        min_age = max(1, self.config.int("maintenance.memory_decay_after_days", 180))
        min_idle = max(1, self.config.int("maintenance.memory_decay_idle_days", 90))
        if age_days < min_age or idle_days < min_idle:
            return None

        age_ratio = min(3.0, age_days / max(1, min_age))
        idle_ratio = min(3.0, idle_days / max(1, min_idle))
        decay_score = (
            age_ratio * 0.35
            + idle_ratio * 0.35
            + (1.0 - max(0.0, min(1.0, record.importance))) * 0.2
            + (1.0 - max(0.0, min(1.0, record.confidence))) * 0.1
        )
        if decay_score < self._config_percent("maintenance.memory_decay_score_threshold_percent", 75):
            return None
        return {
            "record": record,
            "age_days": round(age_days, 1),
            "idle_days": round(idle_days, 1),
            "score": round(decay_score, 3),
            "reason": (
                f"age={age_days:.1f}d idle={idle_days:.1f}d "
                f"importance={record.importance:.2f} access={record.access_count}"
            ),
        }

    def _decay_groups(self, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in candidates:
            record = item["record"]
            owner = self._decay_owner_id(record)
            key = "|".join(
                [
                    clean_text(record.scope, 40) or "unknown",
                    clean_text(owner, 160),
                    clean_text(record.visibility, 40),
                ]
            )
            buckets[key].append(item)
        groups = [
            {"bucket": key, "items": sorted(items, key=lambda value: value.get("score", 0), reverse=True)}
            for key, items in buckets.items()
        ]
        groups.sort(key=lambda group: (len(group["items"]), group["items"][0].get("score", 0)), reverse=True)
        return groups

    def _decay_owner_id(self, record: MemoryRecord) -> str:
        if record.scope == "group" or record.visibility == "group_public":
            return record.group_id or record.object.id or record.subject.id or record.session_id
        if record.scope == "private" or record.visibility == "private_pair":
            for entity in (record.subject, record.object):
                if entity.kind == "user" and entity.id and entity.id != "self":
                    return entity.id
        return record.session_id or record.group_id or record.object.id or record.subject.id

    async def _summarize_decay_group(self, group: dict[str, Any]) -> dict[str, Any]:
        items = list(group.get("items") or [])
        if not items:
            return {"bucket": group.get("bucket"), "summary_id": "", "archived": 0, "reason": "empty_group"}
        max_items = max(2, self.config.int("maintenance.memory_decay_max_items_per_summary", 24))
        items = items[:max_items]
        records = [item["record"] for item in items]
        sample = records[0]
        ctx = self._decay_context(sample)
        attempts = await self._summary_provider_attempts(ctx)
        if not attempts:
            return {"bucket": group.get("bucket"), "summary_id": "", "archived": 0, "reason": "no_summary_provider"}

        summary = ""
        used_attempt: dict[str, Any] = {}
        for attempt in attempts:
            try:
                summary = await self._summarize_decay_records_with_provider(
                    attempt["provider"],
                    ctx=ctx,
                    items=items,
                    model=attempt["model"],
                )
                if summary:
                    used_attempt = attempt
                    break
            except Exception as exc:
                logger.warning(
                    "[RememberYou] 睡眠衰减候选模型失败，尝试下一个: bucket=%s provider=%s error=%s",
                    group.get("bucket"),
                    attempt["provider_id"] or attempt["source"],
                    exc,
                )
        if not summary:
            return {"bucket": group.get("bucket"), "summary_id": "", "archived": 0, "reason": "empty_summary"}

        first_at = clean_text(min((record.occurred_at or record.created_at for record in records if record.occurred_at or record.created_at), default=""), 80)
        last_at = clean_text(max((record.occurred_at or record.created_at for record in records if record.occurred_at or record.created_at), default=""), 80)
        evidence = "\n".join(clean_text(record.content, 220) for record in records[:8] if clean_text(record.content, 220))
        summary_record = MemoryRecord(
            id=self.stable_id("decay_summary", group.get("bucket", ""), first_at, last_at, summary),
            memory_type="memory_decay_summary",
            subject=self._decay_subject(sample),
            object=self._decay_object(sample),
            scope=sample.scope,
            session_id=sample.session_id,
            platform=sample.platform,
            group_id=sample.group_id,
            visibility=sample.visibility,
            sayability="indirect",
            reality_level="llm_summary",
            lifecycle="stable_memory",
            content=summary,
            evidence=evidence,
            confidence=0.7,
            importance=max(0.45, min(0.78, max(record.importance for record in records) + 0.05)),
            review_status="auto",
            tags=["summary", "decay_summary", "sleep_maintenance", sample.scope],
            metadata={
                "source_memory_ids": [record.id for record in records],
                "source_memory_count": len(records),
                "start_at": first_at,
                "end_at": last_at,
                "bucket": clean_text(str(group.get("bucket") or ""), 240),
                "summary_provider_id": clean_text(used_attempt.get("provider_id"), 120),
                "summary_provider_source": clean_text(used_attempt.get("source"), 40),
                "summary_model": clean_text(used_attempt.get("model"), 120),
                "decay_policy": {
                    "after_days": self.config.int("maintenance.memory_decay_after_days", 180),
                    "idle_days": self.config.int("maintenance.memory_decay_idle_days", 90),
                    "max_importance": self._config_percent("maintenance.memory_decay_max_importance_percent", 74),
                    "max_access_count": self.config.int("maintenance.memory_decay_max_access_count", 2),
                },
            },
            source_plugin="remember_you",
        )
        summary_id = await self.store.insert_memory(summary_record)
        archived = await self.store.archive_memories(
            [record.id for record in records],
            reason="sleep_decay_consolidated",
            supersedes_id=summary_id,
        )
        logger.info(
            "[RememberYou] 睡眠衰减已压缩归档: bucket=%s summary=%s archived=%s",
            group.get("bucket"),
            summary_id,
            archived,
        )
        return {
            "bucket": group.get("bucket"),
            "summary_id": summary_id,
            "archived": archived,
            "source_count": len(records),
        }

    def _decay_context(self, sample: MemoryRecord) -> SessionContext:
        owner = self._decay_owner_id(sample)
        return SessionContext(
            session_id=sample.session_id,
            scope=sample.scope,
            platform=sample.platform,
            user_id=owner if sample.scope == "private" else sample.subject.id,
            user_name=sample.subject.name or sample.object.name,
            group_id=sample.group_id or (owner if sample.scope == "group" else ""),
            message_text="",
        )

    def _decay_subject(self, sample: MemoryRecord) -> EntityRef:
        if sample.scope == "private":
            for entity in (sample.subject, sample.object):
                if entity.kind == "user" and entity.id and entity.id != "self":
                    return entity
        if sample.scope == "group":
            return EntityRef.bot_self()
        return sample.subject

    def _decay_object(self, sample: MemoryRecord) -> EntityRef:
        if sample.scope == "group":
            group_id = sample.group_id or sample.object.id or sample.session_id
            return EntityRef(kind="group", id=group_id, name=sample.object.name, role="group")
        if sample.scope == "private":
            return EntityRef.bot_self()
        return sample.object

    async def _summarize_decay_records_with_provider(
        self,
        provider: Any,
        *,
        ctx: SessionContext,
        items: list[dict[str, Any]],
        model: str,
    ) -> str:
        lines: list[str] = []
        total = 0
        max_input_chars = self.config.int("maintenance.memory_decay_summary_input_chars", 6000)
        for item in items:
            record: MemoryRecord = item["record"]
            occurred = clean_text(str(record.occurred_at or record.created_at)[:16].replace("T", " "), 20)
            line = (
                f"[{record.id} | {record.memory_type} | {occurred} | "
                f"score={item.get('score')} | access={record.access_count}] "
                f"{clean_text(record.content, 700)}"
            )
            cost = len(line) + 1
            if lines and total + cost > max_input_chars:
                break
            lines.append(line)
            total += cost
        if not lines:
            return ""
        max_chars = self.config.int("maintenance.memory_decay_summary_chars", 900)
        prompt = (
            "请把下面这些即将衰减的长期记忆碎片合并成一条更高层、可检索、可长期保留的记忆摘要。\n"
            "要求：\n"
            "1. 只保留未来回复仍有价值的稳定信息、长期话题、重要互动结果和未完成事项。\n"
            "2. 删除流水账、重复表述、无意义寒暄和只在当时有用的细节。\n"
            "3. 不要编造；无法确定的内容用保守措辞。\n"
            "4. 必须保持当前窗口隐私边界，不要提到或合并其它私聊/群聊窗口的信息。\n"
            "5. 直接输出一段自然语言摘要，不要 Markdown，不要 JSON，不要解释。\n\n"
            f"窗口：{ctx.label}\n"
            f"摘要最多 {max_chars} 字。\n\n"
            "待压缩记忆：\n"
            f"{chr(10).join(lines)}"
        )
        kwargs: dict[str, Any] = {
            "prompt": prompt,
            "system_prompt": "你是长期记忆睡眠整理器。只输出一条更高层长期记忆摘要。",
            "request_max_retries": 1,
        }
        if model:
            kwargs["model"] = model
        resp = await provider.text_chat(**kwargs)
        text = clean_text(getattr(resp, "completion_text", "") or "", max(120, max_chars * 2))
        return clean_text(self._plain_decay_summary(text), max_chars)

    def _plain_decay_summary(self, text: str) -> str:
        text = clean_text(text, 2000)
        if not text:
            return ""
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            try:
                payload = json.loads(text[start : end + 1])
                if isinstance(payload, dict):
                    for key in ("canonical_summary", "summary", "content"):
                        value = clean_text(payload.get(key), 2000)
                        if value:
                            return value
            except Exception:
                pass
        return text

    def _parse_iso(self, value: str) -> datetime | None:
        value = clean_text(value, 80)
        if not value:
            return None
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            return None

    def _config_percent(self, dotted: str, default: int) -> float:
        value = max(0, min(100, self.config.int(dotted, default)))
        return value / 100.0

    def _context_value(self, ctx: SessionContext, key: str, default: Any) -> Any:
        marker = object()
        scope = clean_text(ctx.scope, 40)
        if scope in {"private", "group"}:
            value = self.config.get(f"context_management.{scope}.{key}", marker)
            if value is not marker:
                return value
        return self.config.get(f"context_management.{key}", default)

    def _context_bool(self, ctx: SessionContext, key: str, default: bool) -> bool:
        value = self._context_value(ctx, key, default)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on", "开", "开启"}
        return bool(value)

    def _context_int(self, ctx: SessionContext, key: str, default: int) -> int:
        try:
            return int(self._context_value(ctx, key, default))
        except Exception:
            return default

    def _context_float(self, ctx: SessionContext, key: str, default: float) -> float:
        try:
            return float(self._context_value(ctx, key, default))
        except Exception:
            return default

    def _context_str(self, ctx: SessionContext, key: str, default: str = "") -> str:
        return str(self._context_value(ctx, key, default) or "")

    def _context_read_limit(self, ctx: SessionContext) -> int:
        configured = self._context_int(ctx, "max_events", 300)
        if configured > 0:
            return configured
        return max(1, self._context_int(ctx, "max_unlimited_events", 80))

    async def inject_memories(self, ctx: SessionContext, req: Any, *, event: Any = None) -> None:
        removed = remove_temp_text(req, REMEMBER_YOU_INJECTION_HEADER, REMEMBER_YOU_INJECTION_FOOTER)
        if removed:
            logger.info("[RememberYou] 已清理历史上下文包注入片段: session=%s count=%s", ctx.session_id, removed)
        self._sanitize_request_history_for_companion(ctx, req)

        turn_signal = analyze_turn_signal(ctx.message_text)
        low_guard_enabled = self._context_bool(ctx, "low_information_guard_enabled", True)
        isolate_low_information = False
        isolate_topic_shift = False
        topic_shift_reason = ""
        suppress_low_information_memory = False
        previous_gap = None
        if low_guard_enabled and turn_signal.low_information:
            previous_gap = await self._previous_context_gap_minutes(ctx)
            gap_limit = max(0, self._context_int(ctx, "low_information_gap_minutes", 20))
            suppress_low_information_memory = self._context_bool(ctx, "suppress_memory_on_low_information", True)
            isolate_low_information = turn_signal.kind == "affection" or previous_gap is None
            if not isolate_low_information and gap_limit > 0:
                isolate_low_information = previous_gap >= gap_limit
        elif self._context_bool(ctx, "topic_shift_guard_enabled", True):
            recent_rows = await self.store.recent_timeline(
                limit=self._context_int(ctx, "topic_shift_guard_recent_events", 6),
                scope=ctx.scope,
                session_id=ctx.session_id,
                entity_id=ctx.current_target_id,
            )
            topic_shift_reason = self._topic_shift_guard_reason(turn_signal, recent_rows)
            isolate_topic_shift = bool(topic_shift_reason)

        isolate_request_context = isolate_low_information or isolate_topic_shift

        manage_history = self._context_bool(ctx, "manage_astrbot_history_enabled", False)
        if isolate_request_context or manage_history:
            managed = manage_request_contexts(
                req,
                "clear" if isolate_request_context else self._context_str(ctx, "astrbot_history_mode", "trim"),
                0 if isolate_request_context else self._context_int(ctx, "keep_recent_messages", 2),
                preserve_external_temp=(
                    self.config.bool("private_companion_bridge.preserve_external_prompt_context", True)
                    and not isolate_request_context
                ),
            )
            if int(managed.get("removed", 0) or 0) > 0:
                logger.info(
                    "[RememberYou] 已整理 AstrBot 原始上下文: session=%s mode=%s before=%s after=%s preserved=%s",
                    ctx.session_id,
                    managed.get("mode"),
                    managed.get("before"),
                    managed.get("after"),
                    managed.get("preserved"),
                )
        if isolate_low_information:
            logger.info(
                "[RememberYou] 低信息输入已隔离旧上下文: session=%s kind=%s reason=%s previous_gap=%s",
                ctx.session_id,
                turn_signal.kind,
                turn_signal.reason,
                f"{previous_gap:.1f}m" if previous_gap is not None else "none",
            )
        if isolate_topic_shift:
            logger.info(
                "[RememberYou] 新话题请求已隔离旧上下文: session=%s reason=%s",
                ctx.session_id,
                topic_shift_reason,
            )

        companion_state = detect_private_companion_request(req)
        companion_deferred = self._companion_deferred_sections(event, req)
        companion_memory_present = self._companion_memory_context_present(companion_state, companion_deferred)
        query_mode = str(self.config.get("context_orchestration.query_mode", "") or "")
        if suppress_low_information_memory or isolate_request_context:
            query_mode = "current_message"
        intent = self.intent_builder.build(
            ctx,
            req=req,
            event=event,
            use_companion_hints=(
                self.config.bool("context_orchestration.use_companion_hints", False)
                and not suppress_low_information_memory
                and not isolate_request_context
            ),
            query_mode=query_mode,
        )
        if not intent.query:
            self._log_injection_debug(
                ctx=ctx,
                intent=intent,
                results=[],
                slot_map={},
                blocked=[{"id": "", "reason": "empty_retrieval_query", "content": ""}],
                short_context="",
                intent_context="",
                injection="",
                note="empty_retrieval_query",
            )
            return
        blocked: list[dict[str, Any]] = []
        if suppress_low_information_memory:
            results = []
            slot_map = {}
            blocked.append(
                {
                    "id": "",
                    "reason": f"low_information_turn:{turn_signal.kind}:{turn_signal.reason}",
                    "content": clean_text(ctx.message_text, 180),
                }
            )
        else:
            results, blocked, slot_map = await self.search_context_slots(
                intent.query, ctx, self.config.int("memory_injection.top_k", 6)
            )
        slot_map, current_state_reasons = self._filter_current_state_memory_slots(ctx, slot_map)
        if current_state_reasons:
            blocked.extend({"id": "", "reason": reason, "content": clean_text(ctx.message_text, 180)} for reason in current_state_reasons)
            results = self._flatten_slot_map(slot_map)
        slot_map, slot_dedupe_reasons = self._dedupe_slots_for_companion(slot_map, companion_state, companion_memory_present)
        if slot_dedupe_reasons:
            blocked.extend({"id": "", "reason": reason, "content": ""} for reason in slot_dedupe_reasons)
            results = self._flatten_slot_map(slot_map)

        short_context = ""
        short_context_suppressed = (
            companion_memory_present
            and self.config.bool("private_companion_bridge.suppress_short_context_when_companion_seen", True)
        )
        if short_context_suppressed:
            blocked.append({"id": "", "reason": "companion_context_detected:short_context_suppressed", "content": ""})
        elif isolate_low_information:
            blocked.append(
                {
                    "id": "",
                    "reason": f"low_information_turn:short_context_suppressed:{turn_signal.kind}",
                    "content": f"previous_gap_minutes={previous_gap:.1f}" if previous_gap is not None else "previous_gap_minutes=none",
                }
            )
        elif isolate_topic_shift:
            blocked.append(
                {
                    "id": "",
                    "reason": "topic_shift_guard:short_context_suppressed",
                    "content": topic_shift_reason,
                }
            )
        else:
            short_context = await self.short_context_for_session(ctx)

        intent_context = self._intent_context_for_injection(intent)
        if isolate_request_context:
            guard_line = "- 当前消息被判定为新的独立请求；不要承接原始历史、短期上下文或联动插件中的旧话题。"
            intent_context = f"{guard_line}\n{intent_context}" if intent_context else guard_line

        injection = self.injection.compose(
            ctx,
            results,
            self.config.int("memory_injection.max_chars", 1800),
            short_context=short_context,
            intent_context=intent_context,
            slot_sections=self._slot_sections(slot_map),
        )
        self._log_injection_debug(
            ctx=ctx,
            intent=intent,
            results=results,
            slot_map=slot_map,
            blocked=blocked,
            short_context=short_context,
            intent_context=intent_context,
            injection=injection,
            note="composed" if injection else "no_injection_body",
        )
        if self.config.bool("memory_injection.enable_injection_logs", True):
            await self.store.add_injection_log(
                session_id=ctx.session_id,
                scope=ctx.scope,
                query=intent.query,
                selected_memory_ids=[item.memory.id for item in results],
                blocked_reasons=blocked[:30],
                injection_chars=len(injection),
            )
        if not injection:
            self._mark_remember_you_injection_state(event, req, injected=False, short_context=False, slot_map=slot_map)
            return

        self._mark_remember_you_injection_state(event, req, injected=True, short_context=bool(short_context), slot_map=slot_map)
        if append_temp_text(req, injection):
            logger.info(
                "[RememberYou] 已临时注入结构化记忆: session=%s source=%s count=%s chars=%s",
                ctx.session_id,
                intent.source,
                len(results),
                len(injection),
            )
            return

        prompt = clean_text(getattr(req, "prompt", "") or "", 8000)
        req.prompt = f"{prompt}\n\n{injection}" if prompt else injection
        logger.warning("[RememberYou] TextPart 不可用，已回退到 prompt 注入: session=%s", ctx.session_id)

    def _log_injection_debug(
        self,
        *,
        ctx: SessionContext,
        intent: Any,
        results: list[Any],
        slot_map: dict[str, list[Any]],
        blocked: list[dict[str, Any]],
        short_context: str,
        intent_context: str,
        injection: str,
        note: str,
    ) -> None:
        if not self.config.bool("memory_injection.debug_log_injection_enabled", True):
            return
        max_chars = max(1000, self.config.int("memory_injection.debug_log_max_chars", 12000))
        def clip(value: Any, limit: int = max_chars) -> str:
            text = "" if value is None else str(value)
            text = text.replace("\r\n", "\n").replace("\r", "\n")
            if len(text) > limit:
                return text[: max(0, limit - 1)].rstrip() + "…"
            return text

        slot_lines: list[str] = []
        for slot, items in (slot_map or {}).items():
            if not items:
                continue
            slot_lines.append(f"[{slot}] {len(items)}")
            for item in items[:10]:
                memory = getattr(item, "memory", None)
                if memory is None:
                    continue
                slot_lines.append(
                    "  - "
                    + " | ".join(
                        [
                            f"id={clean_text(memory.id, 120)}",
                            f"type={clean_text(memory.memory_type, 60)}",
                            f"score={float(getattr(item, 'score', 0.0) or 0.0):.2f}",
                            f"scope={clean_text(memory.scope, 40)}",
                            f"visibility={clean_text(memory.visibility, 40)}",
                            f"reason={clean_text(getattr(item, 'reason', ''), 180)}",
                            f"content={clean_text(memory.content, 360)}",
                        ]
                    )
                )
        if not slot_lines and results:
            slot_lines.append("[selected] no_slot_map")
            for item in results[:10]:
                memory = getattr(item, "memory", None)
                if memory is None:
                    continue
                slot_lines.append(
                    "  - "
                    + " | ".join(
                        [
                            f"id={clean_text(memory.id, 120)}",
                            f"type={clean_text(memory.memory_type, 60)}",
                            f"score={float(getattr(item, 'score', 0.0) or 0.0):.2f}",
                            f"content={clean_text(memory.content, 360)}",
                        ]
                    )
                )
        blocked_lines = [
            "  - "
            + " | ".join(
                [
                    f"id={clean_text(item.get('id') or item.get('memory_id'), 120)}",
                    f"reason={clean_text(item.get('reason'), 220)}",
                    f"content={clean_text(item.get('content'), 220)}",
                ]
            )
            for item in (blocked or [])[:20]
        ]
        summary = "\n".join(
            [
                "========== RememberYou 注入调试 ==========",
                f"note: {clean_text(note, 80)}",
                f"session: {clean_text(ctx.session_id, 200)}",
                f"scope: {clean_text(ctx.scope, 40)}",
                f"target: {clean_text(ctx.label, 240)}",
                f"query_source: {clean_text(getattr(intent, 'source', ''), 80)}",
                f"query: {clean_text(getattr(intent, 'query', ''), 1000)}",
                f"current_user_message: {clean_text(ctx.message_text, 1000)}",
                f"selected_count: {len(results or [])}",
                f"blocked_count: {len(blocked or [])}",
                f"short_context_chars: {len(short_context or '')}",
                f"intent_context_chars: {len(intent_context or '')}",
                f"injection_chars: {len(injection or '')}",
                "",
                "[slot_memories]",
                "\n".join(slot_lines) if slot_lines else "  - none",
                "",
                "[blocked_examples]",
                "\n".join(blocked_lines) if blocked_lines else "  - none",
                "",
                "[intent_context]",
                clip(intent_context),
                "",
                "[short_context]",
                clip(short_context),
                "",
                "[actual_injection]",
                clip(injection),
                "========== RememberYou 注入调试结束 ==========",
            ]
        )
        logger.info("%s", clip(summary))

    async def _previous_context_gap_minutes(self, ctx: SessionContext) -> float | None:
        rows = await self.store.recent_timeline(
            limit=1,
            scope=ctx.scope,
            session_id=ctx.session_id,
            entity_id=ctx.current_target_id,
        )
        if not rows:
            return None
        timestamp = str(rows[0].get("occurred_at") or rows[0].get("created_at") or "")
        previous = self._parse_utc_datetime(timestamp)
        if previous is None:
            return None
        return max(0.0, (datetime.now(timezone.utc) - previous).total_seconds() / 60)

    def _parse_utc_datetime(self, value: str) -> datetime | None:
        text = clean_text(value, 80)
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def _topic_shift_guard_reason(self, turn_signal: Any, rows: list[dict[str, Any]]) -> str:
        if turn_signal.low_information or turn_signal.context_dependent or not turn_signal.standalone_request:
            return ""
        current_terms = set(turn_signal.terms or [])
        if len(current_terms) < 2 or not rows:
            return ""
        recent_text = " ".join(clean_text(row.get("content"), 500) for row in rows if row.get("content"))
        recent_terms = set(message_terms(recent_text, limit=120))
        if not recent_terms:
            return ""
        overlap = current_terms & recent_terms
        if overlap:
            return ""
        preview_terms = "、".join(list(current_terms)[:8])
        return f"standalone_request_no_recent_overlap terms={preview_terms}"

    def _filter_current_state_memory_slots(
        self,
        ctx: SessionContext,
        slot_map: dict[str, list[Any]],
    ) -> tuple[dict[str, list[Any]], list[str]]:
        if not self.config.bool("context_orchestration.current_state_relevance_guard_enabled", True):
            return slot_map, []
        anchors = self._current_state_query_anchors(ctx.message_text)
        if not anchors:
            return slot_map, []
        cleaned: dict[str, list[Any]] = {}
        reasons: list[str] = []
        dropped = 0
        for slot, items in (slot_map or {}).items():
            kept = []
            for item in items or []:
                memory = getattr(item, "memory", None)
                if memory is not None and self._memory_matches_current_state_anchors(memory, anchors):
                    kept.append(item)
                else:
                    dropped += 1
            if kept:
                cleaned[slot] = kept
            else:
                cleaned[slot] = []
        if dropped:
            reasons.append(f"current_state_relevance_guard:dropped={dropped}:anchors={','.join(sorted(anchors))}")
        return cleaned, reasons

    def _current_state_query_anchors(self, text: str) -> set[str]:
        compact = clean_text(text, 500)
        compact = re.sub(r"\s+", "", compact).lower()
        if not compact:
            return set()
        anchors: set[str] = set()
        if any(token in compact for token in ("穿", "衣服", "衣着", "衣装", "制服", "裙", "外套", "裤", "胖次")):
            anchors.update({"穿", "衣服", "衣着", "衣装", "制服", "裙", "外套", "裤", "胖次"})
        if any(token in compact for token in ("吃什么", "吃了", "吃饭", "喝什么", "早餐", "午饭", "晚饭", "夜宵")):
            anchors.update({"吃", "喝", "饭", "早餐", "午饭", "晚饭", "夜宵", "餐"})
        if any(token in compact for token in ("在干嘛", "干什么", "做什么", "在做啥", "在干啥", "忙什么")):
            anchors.update({"做", "忙", "上课", "学习", "写", "画", "玩", "睡", "工作"})
        if any(token in compact for token in ("心情", "状态", "感觉怎么样", "累不累", "困不困")):
            anchors.update({"心情", "状态", "感觉", "累", "困", "开心", "难过"})
        if not anchors:
            return set()
        current_markers = ("今天", "现在", "此刻", "这会", "刚刚", "目前", "现在的", "今天的")
        question_markers = ("什么", "啥", "怎样", "怎么样", "吗", "呢", "？", "?")
        if any(marker in compact for marker in current_markers) or any(marker in compact for marker in question_markers):
            return anchors
        return set()

    def _memory_matches_current_state_anchors(self, memory: Any, anchors: set[str]) -> bool:
        if not anchors:
            return True
        text = " ".join(
            clean_text(value, 1000)
            for value in (
                getattr(memory, "content", ""),
                getattr(memory, "evidence", ""),
                " ".join(getattr(memory, "tags", []) or []),
                getattr(getattr(memory, "subject", None), "name", ""),
                getattr(getattr(memory, "object", None), "name", ""),
            )
            if clean_text(value, 1000)
        )
        return any(anchor in text for anchor in anchors)

    def _sanitize_request_history_for_companion(self, ctx: SessionContext, req: Any) -> None:
        sanitized = sanitize_request_history(
            req,
            clean_proactive_guidance=self.config.bool("private_companion_bridge.clean_proactive_history", True),
        )
        if int(sanitized.get("removed", 0) or 0) or int(sanitized.get("cleaned", 0) or 0):
            logger.info(
                "[RememberYou] 已清理陪伴主动消息历史残留: session=%s removed=%s cleaned=%s before=%s after=%s",
                ctx.session_id,
                sanitized.get("removed"),
                sanitized.get("cleaned"),
                sanitized.get("before"),
                sanitized.get("after"),
            )

    def companion_coordination_status(self) -> dict[str, Any]:
        return {
            "available": True,
            "bridge_enabled": self.config.bool("private_companion_bridge.enabled", True),
            "memory_injection_enabled": self.config.bool("memory_injection.enabled", True),
            "dedupe_prompt_context": self.config.bool("private_companion_bridge.dedupe_prompt_context", True),
            "prefer_remember_you_memory": self.config.bool("private_companion_bridge.prefer_remember_you_memory", True),
            "clean_proactive_history": self.config.bool("private_companion_bridge.clean_proactive_history", True),
            "suppress_short_context_when_companion_seen": self.config.bool(
                "private_companion_bridge.suppress_short_context_when_companion_seen",
                True,
            ),
            "suppress_self_timeline_when_companion_seen": self.config.bool(
                "private_companion_bridge.suppress_self_timeline_when_companion_seen",
                True,
            ),
            "suppress_user_context_when_companion_seen": self.config.bool(
                "private_companion_bridge.suppress_user_context_when_companion_seen",
                True,
            ),
        }

    def should_private_companion_defer_section(self, section: str) -> bool:
        if not self.config.bool("private_companion_bridge.enabled", True):
            return False
        if not self.config.bool("memory_injection.enabled", True):
            return False
        if not self.config.bool("private_companion_bridge.dedupe_prompt_context", True):
            return False
        if not self.config.bool("private_companion_bridge.prefer_remember_you_memory", True):
            return False
        normalized = clean_text(section, 80)
        if normalized in {"self_timeline", "bot_self_timeline"}:
            return self.config.bool("private_companion_bridge.suppress_self_timeline_when_companion_seen", True)
        if normalized in {
            "private_context",
            "companion_memory",
            "dialogue_history",
            "user_profile",
            "livingmemory_guidance",
        }:
            return self.config.bool("private_companion_bridge.suppress_user_context_when_companion_seen", True)
        return False

    def _companion_deferred_sections(self, event: Any, req: Any) -> set[str]:
        sections: set[str] = set()
        for target in (event, req):
            if target is None:
                continue
            for attr in ("remember_you_companion_deferred_sections", "_remember_you_companion_deferred_sections"):
                raw = getattr(target, attr, None)
                if isinstance(raw, str):
                    sections.update(clean_text(part, 80) for part in raw.split(",") if clean_text(part, 80))
                elif isinstance(raw, (list, tuple, set)):
                    sections.update(clean_text(part, 80) for part in raw if clean_text(part, 80))
        return sections

    def _companion_memory_context_present(self, state: dict[str, Any], deferred_sections: set[str]) -> bool:
        if not self.config.bool("private_companion_bridge.dedupe_prompt_context", True):
            return False
        if not bool(state.get("has_any")):
            return False
        state_memory_deferred = bool({"private_context", "companion_memory", "dialogue_history"} & deferred_sections)
        self_timeline_deferred = "self_timeline" in deferred_sections
        return bool(
            (state.get("has_state") and not state_memory_deferred)
            or state.get("has_group_context")
            or (state.get("has_self_timeline") and not self_timeline_deferred)
            or state.get("has_recall_query")
        )

    def _dedupe_slots_for_companion(
        self,
        slot_map: dict[str, list[Any]],
        companion_state: dict[str, Any],
        companion_memory_present: bool,
    ) -> tuple[dict[str, list[Any]], list[str]]:
        if not companion_memory_present:
            return slot_map, []
        cleaned = {key: list(value or []) for key, value in slot_map.items()}
        reasons: list[str] = []

        def drop(slot: str, reason: str) -> None:
            items = cleaned.get(slot) or []
            if not items:
                return
            cleaned[slot] = []
            reasons.append(reason)

        if self.config.bool("private_companion_bridge.suppress_self_timeline_when_companion_seen", True):
            if companion_state.get("has_self_timeline") or companion_state.get("has_state"):
                drop("self_timeline", "companion_context_detected:self_timeline_slot_suppressed")
        if self.config.bool("private_companion_bridge.suppress_user_context_when_companion_seen", True):
            if companion_state.get("has_state"):
                drop("user_profile", "companion_context_detected:user_profile_slot_suppressed")
                drop("conversation_summary", "companion_context_detected:conversation_summary_slot_suppressed")
        return cleaned, reasons

    def _flatten_slot_map(self, slot_map: dict[str, list[Any]]) -> list[Any]:
        items: list[Any] = []
        seen: set[str] = set()
        for slot in ["self_timeline", "user_profile", "current_window", "conversation_summary", "stable_memory"]:
            for item in slot_map.get(slot) or []:
                memory_id = clean_text(getattr(getattr(item, "memory", None), "id", ""), 120)
                if memory_id and memory_id in seen:
                    continue
                if memory_id:
                    seen.add(memory_id)
                items.append(item)
        return items

    def _mark_remember_you_injection_state(
        self,
        event: Any,
        req: Any,
        *,
        injected: bool,
        short_context: bool,
        slot_map: dict[str, list[Any]],
    ) -> None:
        payload = {
            "active": True,
            "injected": bool(injected),
            "short_context": bool(short_context),
            "slots": [slot for slot, items in slot_map.items() if items],
        }
        for target in (event, req):
            if target is None:
                continue
            try:
                setattr(target, "remember_you_injection_state", payload)
            except Exception:
                pass

    def _slot_limits(self, top_k: int) -> dict[str, int]:
        total = max(1, int(top_k or 1))
        return {
            "self_timeline": min(total, self.config.int("context_orchestration.self_timeline_limit", 2)),
            "user_profile": min(total, self.config.int("context_orchestration.user_profile_limit", 2)),
            "current_window": min(total, self.config.int("context_orchestration.current_window_limit", 3)),
            "conversation_summary": min(total, self.config.int("context_orchestration.conversation_summary_limit", 2)),
            "stable_memory": min(total, self.config.int("context_orchestration.stable_memory_limit", 3)),
        }

    def _intent_context_for_injection(self, intent: RetrievalIntent) -> str:
        if not self.config.bool("context_orchestration.include_intent_context", True):
            return ""
        return intent.format_for_injection(
            self.config.int("context_orchestration.intent_max_chars", 520)
        )

    def _slot_sections(self, slot_map: dict[str, list[Any]]) -> list[tuple[str, list[Any]]]:
        labels = {
            "self_timeline": "bot_self_timeline",
            "user_profile": "current_user_profile",
            "current_window": "current_window_memory",
            "conversation_summary": "conversation_continuity",
            "stable_memory": "stable_memory",
        }
        sections: list[tuple[str, list[Any]]] = []
        for key in ["self_timeline", "user_profile", "current_window", "conversation_summary", "stable_memory"]:
            items = slot_map.get(key) or []
            if items:
                sections.append((labels[key], items))
        return sections

    async def short_context_for_session(self, ctx: SessionContext) -> str:
        if not self._context_bool(ctx, "enabled", False):
            return ""
        max_chars = self._context_int(ctx, "max_chars", 1200)
        overflow_strategy = self._context_str(ctx, "overflow_strategy", "drop")
        max_events = self._context_int(ctx, "max_events", 300)
        rows = await self.store.recent_timeline(
            limit=self._context_read_limit(ctx),
            scope=ctx.scope,
            session_id=ctx.session_id,
            entity_id=ctx.current_target_id,
        )
        composer = ContextComposer(
            max_chars=max_chars,
            overflow_strategy=overflow_strategy,
            max_events=max_events,
            drop_events=self._context_int(ctx, "drop_events", 0),
            retain_recent_ratio=self._context_float(ctx, "retain_recent_ratio", 0.15),
        )
        lines = composer.lines_for(ctx, rows)
        if not lines:
            return ""
        body = "\n".join(lines)
        if len(body) <= composer.max_chars:
            self._schedule_context_precompression(ctx, rows=rows, lines=lines, composer=composer)
            return body
        if overflow_strategy != "summarize":
            return composer.drop_to_limit(lines)
        summarized = self._cached_context_summary(ctx, rows, composer)
        if summarized:
            return summarized
        self._schedule_context_precompression(ctx, rows=rows, lines=lines, composer=composer, force=True)
        if self._context_bool(ctx, "allow_sync_compression", False):
            timeout_ms = self._context_int(ctx, "sync_compression_timeout_ms", 0)
            try:
                if timeout_ms > 0:
                    summarized = await asyncio.wait_for(
                        self._summarize_overflow_context(ctx, lines, composer),
                        timeout=max(0.05, timeout_ms / 1000),
                    )
                else:
                    summarized = await self._summarize_overflow_context(ctx, lines, composer)
                if summarized:
                    self._store_context_summary_cache(ctx, rows, composer, summarized, source="sync")
                    return summarized
            except asyncio.TimeoutError:
                logger.warning("[RememberYou] 短期上下文同步压缩超时，降级为轻量裁剪: session=%s", ctx.session_id)
        return composer.compose(ctx, rows)

    def _schedule_context_precompression(
        self,
        ctx: SessionContext,
        *,
        rows: list[dict[str, Any]] | None = None,
        lines: list[str] | None = None,
        composer: ContextComposer | None = None,
        force: bool = False,
    ) -> None:
        if not self._context_bool(ctx, "enabled", False):
            return
        if not self._context_bool(ctx, "async_precompress_enabled", True):
            return
        if self._context_str(ctx, "overflow_strategy", "drop") != "summarize":
            return
        if not self._has_context_summary_provider_config(ctx):
            return
        snapshot = self._snapshot_context(ctx)
        self._spawn_background(
            self._background_precompress_context(
                snapshot,
                rows=list(rows) if rows is not None else None,
                lines=list(lines) if lines is not None else None,
                composer=composer,
                force=force,
            ),
            label="context_precompress",
        )

    async def _background_precompress_context(
        self,
        ctx: SessionContext,
        *,
        rows: list[dict[str, Any]] | None,
        lines: list[str] | None,
        composer: ContextComposer | None,
        force: bool,
    ) -> None:
        if composer is None:
            composer = ContextComposer(
                max_chars=self._context_int(ctx, "max_chars", 1200),
                overflow_strategy=self._context_str(ctx, "overflow_strategy", "drop"),
                max_events=self._context_int(ctx, "max_events", 300),
                drop_events=self._context_int(ctx, "drop_events", 0),
                retain_recent_ratio=self._context_float(ctx, "retain_recent_ratio", 0.15),
            )
        if rows is None:
            rows = await self.store.recent_timeline(
                limit=self._context_read_limit(ctx),
                scope=ctx.scope,
                session_id=ctx.session_id,
                entity_id=ctx.current_target_id,
            )
        if lines is None:
            lines = composer.lines_for(ctx, rows)
        if not rows or not lines:
            return
        body_len = len("\n".join(lines))
        threshold = max(1, int(composer.max_chars * self._context_int(ctx, "precompress_threshold_percent", 85) / 100))
        if not force and body_len < threshold:
            return
        recent = composer.drop_to_limit(lines)
        older_count = max(0, len(lines) - len(recent.splitlines() if recent else []))
        if older_count <= 0:
            return
        cache_key = self._context_summary_cache_key(ctx, composer)
        signature = self._context_rows_signature(rows)
        inflight_key = f"{cache_key}:{signature}"
        if inflight_key in self._context_summary_inflight:
            return
        if self._cached_context_summary(ctx, rows, composer):
            return
        self._context_summary_inflight.add(inflight_key)
        try:
            summarized = await self._summarize_overflow_context(ctx, lines, composer)
            if summarized:
                self._store_context_summary_cache(ctx, rows, composer, summarized, source="async")
                logger.info(
                    "[RememberYou] 已预热短期上下文压缩: session=%s events=%s chars=%s",
                    ctx.session_id,
                    len(rows),
                    len(summarized),
                )
        finally:
            self._context_summary_inflight.discard(inflight_key)

    def _cached_context_summary(
        self,
        ctx: SessionContext,
        rows: list[dict[str, Any]],
        composer: ContextComposer,
    ) -> str:
        item = self._context_summary_cache.get(self._context_summary_cache_key(ctx, composer))
        if not item:
            return ""
        if item.get("signature") != self._context_rows_signature(rows):
            return ""
        return clean_text(item.get("text"), composer.max_chars)

    def _store_context_summary_cache(
        self,
        ctx: SessionContext,
        rows: list[dict[str, Any]],
        composer: ContextComposer,
        text: str,
        *,
        source: str,
    ) -> None:
        self._context_summary_cache[self._context_summary_cache_key(ctx, composer)] = {
            "signature": self._context_rows_signature(rows),
            "text": clean_text(text, composer.max_chars),
            "source": source,
            "created_at": utc_now(),
        }
        while len(self._context_summary_cache) > 80:
            self._context_summary_cache.pop(next(iter(self._context_summary_cache)))

    def _context_summary_cache_key(self, ctx: SessionContext, composer: ContextComposer) -> str:
        return "|".join(
            [
                clean_text(ctx.scope, 40),
                clean_text(ctx.session_id, 200),
                clean_text(ctx.current_target_id, 160),
                str(composer.max_chars),
                str(composer.max_events),
                str(self._context_int(ctx, "summary_max_chars", 360)),
            ]
        )

    def _context_rows_signature(self, rows: list[dict[str, Any]]) -> str:
        return "|".join(sorted(clean_text(row.get("id"), 120) for row in rows if clean_text(row.get("id"), 120)))

    def _has_context_summary_provider_config(self, ctx: SessionContext) -> bool:
        return bool(
            clean_text(self._context_str(ctx, "summary_provider_id", ""), 120)
            or clean_text(self._context_str(ctx, "summary_fallback_provider_id", ""), 120)
        )

    async def _summarize_overflow_context(
        self,
        ctx: SessionContext,
        lines: list[str],
        composer: ContextComposer,
    ) -> str:
        recent = composer.recent_tail(lines)
        recent_lines = recent.splitlines() if recent else []
        older_count = max(0, len(lines) - len(recent_lines))
        if older_count <= 0:
            return recent

        attempts = await self._context_summary_provider_attempts(ctx)
        if not attempts:
            return ""

        older_lines = lines[:older_count]
        summary_max_chars = self._context_int(ctx, "summary_max_chars", 360)
        summary = ""
        for attempt in attempts:
            try:
                summary = await self._summarize_context_lines_with_provider(
                    attempt["provider"],
                    ctx=ctx,
                    older_lines=older_lines,
                    recent=recent,
                    model=attempt["model"],
                    max_chars=summary_max_chars,
                )
                if summary:
                    logger.info(
                        "[RememberYou] 已压缩短期上下文: session=%s provider=%s older=%s chars=%s",
                        ctx.session_id,
                        attempt["provider_id"] or attempt["source"],
                        older_count,
                        len(summary),
                    )
                    break
            except Exception as exc:
                logger.warning(
                    "[RememberYou] 短期上下文总结候选失败，尝试下一个: session=%s provider=%s error=%s",
                    ctx.session_id,
                    attempt["provider_id"] or attempt["source"],
                    exc,
                )
        if not summary:
            return ""

        prefix = f"- 较早上下文摘要：{summary}"
        if recent:
            text = f"{prefix}\n{recent}"
        else:
            text = prefix
        if len(text) <= composer.max_chars:
            return text

        room = max(80, composer.max_chars - len(recent) - 2) if recent else composer.max_chars
        prefix = f"- 较早上下文摘要：{clean_text(summary, max(40, room - 12))}"
        text = f"{prefix}\n{recent}" if recent else prefix
        if len(text) <= composer.max_chars:
            return text
        return text[: composer.max_chars - 1].rstrip() + "…"

    async def _summarize_context_lines_with_provider(
        self,
        provider: Any,
        *,
        ctx: SessionContext,
        older_lines: list[str],
        recent: str,
        model: str,
        max_chars: int,
    ) -> str:
        older_text = "\n".join(older_lines)
        default_prompt = (
            "请把下面较早的短期对话上下文压缩成给 Bot 阅读的一小段连续性摘要。\n"
            "要求：只保留会影响本轮回复的事实、未完成事项、情绪变化、称呼关系和话题承接；"
            "不要复述每句话；不要添加没有依据的内容；不要提到你在总结。\n\n"
            f"会话：{ctx.label}\n"
            f"摘要最多 {max_chars} 字。\n\n"
            "较早上下文：\n"
            f"{clean_text(older_text, 4000)}\n\n"
            "最近上下文会原样保留如下，摘要不要重复这些最近内容：\n"
            f"{clean_text(recent, 1600) or '（无）'}"
        )
        prompt_template = self._context_str(ctx, "summary_prompt", "") or default_prompt
        if "{older_context}" in prompt_template or "{recent_context}" in prompt_template:
            try:
                prompt = prompt_template.format(
                    session_label=ctx.label,
                    max_chars=max_chars,
                    older_context=clean_text(older_text, 4000),
                    recent_context=clean_text(recent, 1600) or "（无）",
                )
            except Exception:
                prompt = default_prompt
        elif prompt_template != default_prompt:
            prompt = (
                f"{prompt_template}\n\n"
                f"会话：{ctx.label}\n"
                f"摘要最多 {max_chars} 字。\n\n"
                "较早上下文：\n"
                f"{clean_text(older_text, 4000)}\n\n"
                "最近上下文会原样保留如下，摘要不要重复这些最近内容：\n"
                f"{clean_text(recent, 1600) or '（无）'}"
            )
        else:
            prompt = default_prompt
        kwargs: dict[str, Any] = {
            "prompt": prompt,
            "system_prompt": "你是短期上下文压缩器。只输出一段自然语言摘要，不要 Markdown，不要 JSON。",
            "request_max_retries": 1,
        }
        if model:
            kwargs["model"] = model
        resp = await provider.text_chat(**kwargs)
        return clean_text(getattr(resp, "completion_text", "") or "", max(80, max_chars))

    async def note_identity(self, ctx: SessionContext) -> None:
        if ctx.user_id:
            await self.store.upsert_identity(
                platform=ctx.platform,
                entity=EntityRef(kind="user", id=ctx.user_id, name=ctx.user_name, role="current_sender"),
                aliases=[ctx.user_name] if ctx.user_name else [],
                profile={"last_session": ctx.session_id, "last_scope": ctx.scope},
                confidence=0.7,
            )
        if ctx.group_id:
            await self.store.upsert_identity(
                platform=ctx.platform,
                entity=EntityRef(kind="group", id=ctx.group_id, name="", role="group"),
                profile={"last_session": ctx.session_id},
                confidence=0.7,
            )

    async def note_relationships(self, ctx: SessionContext, source_memory_id: str = "") -> None:
        if ctx.scope == "group" and ctx.user_id and ctx.group_id:
            await self.store.upsert_relationship(
                subject=EntityRef(kind="user", id=ctx.user_id, name=ctx.user_name, role="group_member"),
                object=EntityRef(kind="group", id=ctx.group_id, name="", role="group"),
                relation_type="member_of_group",
                scope="group",
                session_id=ctx.session_id,
                group_id=ctx.group_id,
                visibility="group_public",
                evidence=clean_text(ctx.message_text, 500),
                confidence=0.8,
                review_status="auto",
                source_memory_id=source_memory_id,
                metadata={"observed_from": "group_message"},
            )

    def visibility_policy(self) -> VisibilityPolicy:
        return VisibilityPolicy(
            allow_self_timeline_everywhere=self.config.bool("visibility.allow_self_timeline_everywhere", True),
            allow_group_public_in_private=self.config.bool("visibility.allow_group_public_in_private", False),
            hide_pending_review=self.config.bool("visibility.hide_pending_review", True),
            include_raw_events=self.config.bool("memory_injection.include_raw_events", False),
            enable_acl_rules=self.config.bool("visibility.enable_acl_rules", True),
        )

    def session_context_from_bridge(self, session_context: SessionContext | dict[str, Any] | None) -> SessionContext:
        if isinstance(session_context, SessionContext):
            return session_context
        payload = session_context or {}
        return SessionContext(
            session_id=str(payload.get("session_id") or ""),
            scope=str(payload.get("scope") or "unknown"),
            platform=str(payload.get("platform") or ""),
            user_id=str(payload.get("user_id") or ""),
            user_name=str(payload.get("user_name") or ""),
            group_id=str(payload.get("group_id") or ""),
            bot_id=str(payload.get("bot_id") or ""),
            message_id=str(payload.get("message_id") or ""),
            message_text=str(payload.get("message_text") or ""),
        )

    def stable_id(self, *parts: Any) -> str:
        raw = "|".join(clean_text(part, 500) for part in parts if part is not None)
        digest = hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()[:20]
        return f"mem_{digest}"

    def close(self) -> None:
        for task in list(self._background_tasks):
            task.cancel()
        self._background_tasks.clear()
        try:
            self.store.close()
        except Exception as exc:
            logger.warning("[RememberYou] 关闭记忆库连接失败: %s", exc, exc_info=True)
