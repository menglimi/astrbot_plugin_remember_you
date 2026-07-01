from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from quart import jsonify, request

from .core.bridge import serialize_memory
from .core.models import SessionContext, clean_text

PLUGIN_NAME = "astrbot_plugin_remember_you"
PAGE_API_PREFIX = f"/{PLUGIN_NAME}/page"

THEME_NAME_TO_KEY = {
    "黄白游": "huangbaiyou",
    "天缥": "tianpiao",
    "海天霞": "haitianxia",
    "盈盈": "yingying",
    "欧碧": "oubi",
    "青冥": "qingming",
    "紫蒲": "zipu",
    "山岚": "shanlan",
    "窃蓝": "qielan",
    "退红": "tuihong",
    "葱倩": "congqing",
    "月白": "yuebai",
    "墨黪": "mocan",
    "骨缥": "gupiao",
}
THEME_KEYS = set(THEME_NAME_TO_KEY.values())
DEFAULT_THEME_NAME = "月白"
DEFAULT_THEME_KEY = THEME_NAME_TO_KEY[DEFAULT_THEME_NAME]


class PluginPageApi:
    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin

    def register_routes(self) -> None:
        register = self.plugin.context.register_web_api
        routes = [
            ("/stats", self.stats, ["GET"], "RememberYou Page stats"),
            ("/buckets", self.buckets, ["GET"], "RememberYou Page buckets"),
            ("/memories", self.memories, ["GET"], "RememberYou Page memories"),
            ("/memory", self.memory_detail, ["GET"], "RememberYou Page memory detail"),
            ("/memory/update", self.memory_update, ["POST"], "RememberYou Page memory update"),
            ("/memory/delete", self.memory_delete, ["POST"], "RememberYou Page memory delete"),
            ("/memory/visibility", self.memory_visibility, ["POST"], "RememberYou Page memory visibility"),
            ("/memory/lifecycle", self.memory_lifecycle, ["POST"], "RememberYou Page memory lifecycle"),
            ("/acl", self.acl, ["GET"], "RememberYou Page memory ACL rules"),
            ("/acl/upsert", self.acl_upsert, ["POST"], "RememberYou Page memory ACL upsert"),
            ("/acl/policy", self.acl_policy, ["POST"], "RememberYou Page memory ACL policy"),
            ("/acl/delete", self.acl_delete, ["POST"], "RememberYou Page memory ACL delete"),
            ("/search", self.search, ["POST"], "RememberYou Page search"),
            ("/review", self.review_list, ["GET"], "RememberYou Page review list"),
            ("/review/update", self.review_update, ["POST"], "RememberYou Page review update"),
            ("/review/approve_livingmemory", self.review_approve_livingmemory, ["POST"], "RememberYou Page approve LivingMemory imports"),
            ("/timeline", self.timeline, ["GET"], "RememberYou Page timeline"),
            ("/relations", self.relations, ["GET"], "RememberYou Page relations"),
            ("/threads", self.threads, ["GET"], "RememberYou Page threads"),
            ("/thread/status", self.thread_status, ["POST"], "RememberYou Page thread status"),
            ("/logs", self.logs, ["GET"], "RememberYou Page injection logs"),
            ("/context/config", self.context_config, ["GET"], "RememberYou Page context config"),
            ("/context/config/update", self.context_config_update, ["POST"], "RememberYou Page context config update"),
            ("/companion/personal-memory", self.companion_personal_memory, ["GET"], "RememberYou Page companion personal memory"),
            ("/maintenance", self.maintenance, ["POST"], "RememberYou Page maintenance"),
            ("/maintenance/sleep", self.sleep_maintenance, ["GET", "POST"], "RememberYou Page sleep maintenance"),
            ("/maintenance/repair_livingmemory_content", self.repair_livingmemory_content, ["POST"], "RememberYou Page repair LivingMemory content"),
            ("/maintenance/clear_all", self.clear_all, ["POST"], "RememberYou Page clear all memory data"),
            ("/import/livingmemory/preview", self.import_preview, ["GET"], "RememberYou Page import preview"),
            ("/import/livingmemory/run", self.import_run, ["POST"], "RememberYou Page import run"),
        ]
        for route, handler, methods, desc in routes:
            register(f"{PAGE_API_PREFIX}{route}", handler, methods, desc)

    async def stats(self):
        stats = await self.plugin.service.store.stats()
        return self._ok({"stats": stats})

    async def buckets(self):
        buckets = await self.plugin.service.store.list_memory_buckets(limit=self._query_int("limit", 160))
        return self._ok({"buckets": buckets})

    async def memories(self):
        limit = self._query_int("limit", 50)
        query = clean_text(request.args.get("q", ""), 200)
        scope = clean_text(request.args.get("scope", ""), 40)
        review_status = clean_text(request.args.get("review_status", ""), 40)
        visibility = clean_text(request.args.get("visibility", ""), 40)
        lifecycle = clean_text(request.args.get("lifecycle", ""), 40)
        records = await self.plugin.service.store.list_memories(
            limit=limit,
            include_pending=True,
            query=query,
            memory_type=clean_text(request.args.get("memory_type", ""), 80),
            scope=scope,
            visibility=visibility,
            review_status=review_status,
            lifecycle=lifecycle,
            session_id=clean_text(request.args.get("session_id", ""), 200),
            group_id=clean_text(request.args.get("group_id", ""), 120),
            entity_id=clean_text(request.args.get("entity_id", ""), 120),
        )
        return self._ok({"memories": [serialize_memory(record) for record in records]})

    async def memory_detail(self):
        memory_id = clean_text(request.args.get("id", ""), 120)
        if not memory_id:
            return self._err("missing id", 400)
        record = await self.plugin.service.store.get_memory(memory_id)
        if not record:
            return self._err("memory not found", 404)
        payload = serialize_memory(record)
        payload["evidence"] = record.evidence
        payload["metadata"] = record.metadata
        payload["merged_count"] = record.merged_count
        payload["content_fingerprint"] = record.content_fingerprint
        return self._ok({"memory": payload})

    async def memory_update(self):
        payload = await self._json()
        memory_id = clean_text(payload.get("id"), 120)
        if not memory_id:
            return self._err("missing id", 400)
        ok = await self.plugin.service.store.update_memory_payload(
            memory_id,
            memory_type=payload.get("memory_type"),
            content=payload.get("content"),
            evidence=payload.get("evidence"),
            importance=payload.get("importance"),
            confidence=payload.get("confidence"),
        )
        return self._ok({"updated": ok})

    async def memory_delete(self):
        payload = await self._json()
        ok = await self.plugin.service.store.delete_memory(clean_text(payload.get("id"), 120))
        return self._ok({"deleted": ok})

    async def memory_visibility(self):
        payload = await self._json()
        ok = await self.plugin.service.store.update_memory_visibility(
            clean_text(payload.get("id"), 120),
            clean_text(payload.get("visibility"), 40),
        )
        return self._ok({"updated": ok})

    async def memory_lifecycle(self):
        payload = await self._json()
        ok = await self.plugin.service.store.update_memory_lifecycle(
            clean_text(payload.get("id"), 120),
            clean_text(payload.get("lifecycle"), 40),
        )
        return self._ok({"updated": ok})

    async def acl(self):
        owner_scope = clean_text(request.args.get("scope", ""), 40)
        owner_id = clean_text(request.args.get("id", ""), 160)
        error = self._acl_window_error(owner_scope, owner_id)
        if error:
            return self._err(error, 400)
        can_read = await self.plugin.service.store.list_acl_rules(
            reader_scope=owner_scope,
            reader_id=owner_id,
            enabled_only=True,
        )
        can_be_read_by = await self.plugin.service.store.list_acl_rules(
            owner_scope=owner_scope,
            owner_id=owner_id,
            enabled_only=True,
        )
        policy = await self.plugin.service.store.get_acl_policy(owner_scope, owner_id)
        return self._ok(
            {
                "owner": {"scope": owner_scope, "id": owner_id},
                "policy": policy,
                "can_read": can_read,
                "can_be_read_by": can_be_read_by,
            }
        )

    async def acl_upsert(self):
        payload = await self._json()
        owner_scope = clean_text(payload.get("owner_scope"), 40)
        owner_id = clean_text(payload.get("owner_id"), 160)
        reader_scope = clean_text(payload.get("reader_scope"), 40)
        reader_id = clean_text(payload.get("reader_id"), 160)
        error = self._acl_window_error(owner_scope, owner_id) or self._acl_window_error(reader_scope, reader_id)
        if error:
            return self._err(error, 400)
        if owner_scope == reader_scope and owner_id == reader_id:
            return self._err("same window does not need ACL", 400)
        rule = await self.plugin.service.store.upsert_acl_rule(
            owner_scope=owner_scope,
            owner_id=owner_id,
            reader_scope=reader_scope,
            reader_id=reader_id,
            effect=self._acl_effect(payload.get("effect")),
            enabled=self._bool(payload.get("enabled"), True),
            note=clean_text(payload.get("note"), 300),
        )
        return self._ok({"rule": rule})

    async def acl_policy(self):
        payload = await self._json()
        window_scope = clean_text(payload.get("scope") or payload.get("window_scope"), 40)
        window_id = clean_text(payload.get("id") or payload.get("window_id"), 160)
        error = self._acl_window_error(window_scope, window_id)
        if error:
            return self._err(error, 400)
        policy = await self.plugin.service.store.upsert_acl_policy(
            window_scope=window_scope,
            window_id=window_id,
            read_mode=self._acl_mode(payload.get("read_mode")),
            share_mode=self._acl_mode(payload.get("share_mode")),
        )
        return self._ok({"policy": policy})

    async def acl_delete(self):
        payload = await self._json()
        ok = await self.plugin.service.store.delete_acl_rule(clean_text(payload.get("id"), 120))
        return self._ok({"deleted": ok})

    async def search(self):
        payload = await self._json()
        query = clean_text(payload.get("query"), 500)
        if not query:
            return self._err("missing query", 400)
        ctx = SessionContext(
            session_id=clean_text(payload.get("session_id"), 200),
            scope=clean_text(payload.get("scope"), 40) or "unknown",
            platform=clean_text(payload.get("platform"), 80),
            user_id=clean_text(payload.get("user_id"), 120),
            user_name=clean_text(payload.get("user_name"), 80),
            group_id=clean_text(payload.get("group_id"), 120),
            message_text=query,
        )
        results, blocked = await self.plugin.service.search_with_diagnostics(
            query, ctx, self._int(payload.get("top_k"), 8)
        )
        return self._ok(
            {
                "results": [
                    serialize_memory(item.memory, item.score, item.reason)
                    for item in results
                ],
                "blocked": blocked[:30],
            }
        )

    async def review_list(self):
        rows = await self.plugin.service.store.list_review_queue(limit=self._query_int("limit", 50))
        return self._ok({"items": rows})

    async def review_update(self):
        payload = await self._json()
        ok = await self.plugin.service.store.update_review_status(
            clean_text(payload.get("id"), 120),
            clean_text(payload.get("status"), 40),
        )
        return self._ok({"updated": ok})

    async def review_approve_livingmemory(self):
        result = await self.plugin.service.store.approve_livingmemory_imports()
        return self._ok({"result": result})

    async def timeline(self):
        rows = await self.plugin.service.store.recent_timeline(
            limit=self._query_int("limit", 30),
            scope=clean_text(request.args.get("scope", ""), 40),
            session_id=clean_text(request.args.get("session_id", ""), 200),
            entity_id=clean_text(request.args.get("entity_id", ""), 120),
        )
        return self._ok({"items": rows})

    async def relations(self):
        rows = await self.plugin.service.store.list_relationships(
            limit=self._query_int("limit", 50),
            entity_id=clean_text(request.args.get("entity_id", ""), 120),
            scope=clean_text(request.args.get("scope", ""), 40),
            session_id=clean_text(request.args.get("session_id", ""), 200),
            group_id=clean_text(request.args.get("group_id", ""), 120),
        )
        return self._ok({"items": rows})

    async def threads(self):
        rows = await self.plugin.service.store.list_cross_window_threads(
            status=clean_text(request.args.get("status", "open"), 40) or "open",
            limit=self._query_int("limit", 30),
            session_id=clean_text(request.args.get("session_id", ""), 200),
        )
        return self._ok({"items": rows})

    async def thread_status(self):
        payload = await self._json()
        ok = await self.plugin.service.store.update_cross_window_thread_status(
            clean_text(payload.get("id"), 120),
            clean_text(payload.get("status"), 40),
        )
        return self._ok({"updated": ok})

    async def logs(self):
        rows = await self.plugin.service.store.recent_injection_logs(
            limit=self._query_int("limit", 20),
            scope=clean_text(request.args.get("scope", ""), 40),
            session_id=clean_text(request.args.get("session_id", ""), 200),
        )
        return self._ok({"items": rows})

    async def context_config(self):
        config = self.plugin.service.config
        theme_name = str(config.get("appearance.theme", DEFAULT_THEME_NAME))
        return self._ok(
            {
                "appearance": {
                    "theme": theme_name,
                    "theme_key": self._theme_key(theme_name),
                    "available_themes": list(THEME_NAME_TO_KEY.keys()),
                },
                "context_management": {
                    "enabled": config.bool("context_management.enabled", False),
                    "max_events": config.int("context_management.max_events", 300),
                    "drop_events": config.int("context_management.drop_events", 0),
                    "max_chars": config.int("context_management.max_chars", 1200),
                    "overflow_strategy": str(config.get("context_management.overflow_strategy", "drop")),
                    "summary_prompt": str(config.get("context_management.summary_prompt", "") or ""),
                    "retain_recent_ratio": self._float(config.get("context_management.retain_recent_ratio", 0.15), 0.15),
                    "summary_provider_id": str(config.get("context_management.summary_provider_id", "") or ""),
                    "summary_model": str(config.get("context_management.summary_model", "") or ""),
                    "summary_fallback_provider_id": str(config.get("context_management.summary_fallback_provider_id", "") or ""),
                    "summary_fallback_model": str(config.get("context_management.summary_fallback_model", "") or ""),
                    "summary_max_chars": config.int("context_management.summary_max_chars", 360),
                    "model_context_tokens": config.int("context_management.model_context_tokens", 0),
                    "async_precompress_enabled": config.bool("context_management.async_precompress_enabled", True),
                    "precompress_threshold_percent": config.int("context_management.precompress_threshold_percent", 82),
                    "allow_sync_compression": config.bool("context_management.allow_sync_compression", False),
                    "sync_compression_timeout_ms": config.int("context_management.sync_compression_timeout_ms", 0),
                    "low_information_guard_enabled": config.bool("context_management.low_information_guard_enabled", True),
                    "low_information_gap_minutes": config.int("context_management.low_information_gap_minutes", 20),
                    "suppress_memory_on_low_information": config.bool(
                        "context_management.suppress_memory_on_low_information", True
                    ),
                    "topic_shift_guard_enabled": config.bool("context_management.topic_shift_guard_enabled", True),
                    "topic_shift_guard_recent_events": config.int(
                        "context_management.topic_shift_guard_recent_events", 6
                    ),
                    "manage_astrbot_history_enabled": config.bool(
                        "context_management.manage_astrbot_history_enabled", False
                    ),
                    "astrbot_history_mode": str(config.get("context_management.astrbot_history_mode", "keep")),
                    "keep_recent_messages": config.int("context_management.keep_recent_messages", 2),
                },
                "context_profiles": {
                    "private": self._context_profile("private"),
                    "group": self._context_profile("group"),
                },
                "provider_options": self._provider_options(),
                "memory_injection": {
                    "enabled": config.bool("memory_injection.enabled", True),
                    "top_k": config.int("memory_injection.top_k", 6),
                    "max_chars": config.int("memory_injection.max_chars", 1800),
                    "include_raw_events": config.bool("memory_injection.include_raw_events", False),
                    "enable_injection_logs": config.bool("memory_injection.enable_injection_logs", True),
                    "debug_log_injection_enabled": config.bool(
                        "memory_injection.debug_log_injection_enabled",
                        True,
                    ),
                    "debug_log_max_chars": config.int("memory_injection.debug_log_max_chars", 12000),
                },
                "context_orchestration": {
                    "enabled": config.bool("context_orchestration.enabled", True),
                    "query_mode": str(config.get("context_orchestration.query_mode", "current_message") or "current_message"),
                    "use_companion_hints": config.bool("context_orchestration.use_companion_hints", False),
                    "include_intent_context": config.bool("context_orchestration.include_intent_context", True),
                    "intent_max_chars": config.int("context_orchestration.intent_max_chars", 520),
                    "self_timeline_limit": config.int("context_orchestration.self_timeline_limit", 2),
                    "user_profile_limit": config.int("context_orchestration.user_profile_limit", 2),
                    "current_window_limit": config.int("context_orchestration.current_window_limit", 3),
                    "conversation_summary_limit": config.int("context_orchestration.conversation_summary_limit", 2),
                    "stable_memory_limit": config.int("context_orchestration.stable_memory_limit", 3),
                },
                "memory_summary": {
                    "enabled": config.bool("memory_summary.enabled", True),
                    "provider_id": str(config.get("memory_summary.provider_id", "") or ""),
                    "model": str(config.get("memory_summary.model", "") or ""),
                    "fallback_provider_id": str(config.get("memory_summary.fallback_provider_id", "") or ""),
                    "fallback_model": str(config.get("memory_summary.fallback_model", "") or ""),
                    "min_events": config.int("memory_summary.min_events", 8),
                    "trigger_event_count": config.int("memory_summary.trigger_event_count", 12),
                    "trigger_interval_minutes": config.int("memory_summary.trigger_interval_minutes", 60),
                    "max_events_per_summary": config.int("memory_summary.max_events_per_summary", 40),
                    "max_retries": config.int("memory_summary.max_retries", 3),
                },
                "memory_tools": {
                    "enable_recall_tool": config.bool("memory_tools.enable_recall_tool", True),
                    "enable_remember_tool": config.bool("memory_tools.enable_remember_tool", True),
                    "enable_note_tools": config.bool("memory_tools.enable_note_tools", True),
                    "auto_approve_tool_memories": config.bool("memory_tools.auto_approve_tool_memories", False),
                },
                "private_companion_bridge": {
                    "enabled": config.bool("private_companion_bridge.enabled", True),
                    "accept_external_records": config.bool("private_companion_bridge.accept_external_records", True),
                    "dedupe_prompt_context": config.bool("private_companion_bridge.dedupe_prompt_context", True),
                    "prefer_remember_you_memory": config.bool("private_companion_bridge.prefer_remember_you_memory", True),
                    "preserve_external_prompt_context": config.bool(
                        "private_companion_bridge.preserve_external_prompt_context",
                        True,
                    ),
                    "clean_proactive_history": config.bool("private_companion_bridge.clean_proactive_history", True),
                    "suppress_short_context_when_companion_seen": config.bool(
                        "private_companion_bridge.suppress_short_context_when_companion_seen",
                        True,
                    ),
                    "suppress_self_timeline_when_companion_seen": config.bool(
                        "private_companion_bridge.suppress_self_timeline_when_companion_seen",
                        True,
                    ),
                    "suppress_user_context_when_companion_seen": config.bool(
                        "private_companion_bridge.suppress_user_context_when_companion_seen",
                        True,
                    ),
                },
                "visibility": {
                    "allow_self_timeline_everywhere": config.bool("visibility.allow_self_timeline_everywhere", True),
                    "allow_group_public_in_private": config.bool("visibility.allow_group_public_in_private", False),
                    "hide_pending_review": config.bool("visibility.hide_pending_review", True),
                    "enable_acl_rules": config.bool("visibility.enable_acl_rules", True),
                },
                "maintenance": {
                    "retention_raw_event_days": config.int("maintenance.retention_raw_event_days", 7),
                    "retention_raw_event_limit": config.int("maintenance.retention_raw_event_limit", 1000),
                    "memory_decay_enabled": config.bool("maintenance.memory_decay_enabled", True),
                    "memory_decay_after_days": config.int("maintenance.memory_decay_after_days", 180),
                    "memory_decay_idle_days": config.int("maintenance.memory_decay_idle_days", 90),
                    "memory_decay_max_importance_percent": config.int(
                        "maintenance.memory_decay_max_importance_percent",
                        74,
                    ),
                    "memory_decay_max_access_count": config.int("maintenance.memory_decay_max_access_count", 2),
                    "memory_decay_score_threshold_percent": config.int(
                        "maintenance.memory_decay_score_threshold_percent",
                        75,
                    ),
                    "memory_decay_max_candidates": config.int("maintenance.memory_decay_max_candidates", 120),
                    "memory_decay_max_groups": config.int("maintenance.memory_decay_max_groups", 8),
                    "memory_decay_min_items_per_summary": config.int(
                        "maintenance.memory_decay_min_items_per_summary",
                        4,
                    ),
                    "memory_decay_max_items_per_summary": config.int(
                        "maintenance.memory_decay_max_items_per_summary",
                        24,
                    ),
                },
                "sleep_maintenance": self.plugin.service.sleep_status(),
            }
        )

    async def context_config_update(self):
        payload = await self._json()
        scope = clean_text(payload.get("scope"), 40)
        if scope not in {"private", "group"}:
            return self._err("scope must be private or group", 400)
        raw = self.plugin.service.config.raw
        if not isinstance(raw, dict):
            return self._err("runtime config is not writable", 500)
        values = self._clean_context_profile_payload(payload.get("context") if isinstance(payload.get("context"), dict) else payload)
        raw.setdefault("context_management", {})
        if not isinstance(raw["context_management"], dict):
            raw["context_management"] = {}
        raw["context_management"][scope] = values
        self._write_plugin_config(raw)
        return self._ok({"scope": scope, "context": self._context_profile(scope), "provider_options": self._provider_options()})

    def _theme_key(self, theme: str) -> str:
        value = clean_text(theme, 40)
        if value in THEME_NAME_TO_KEY:
            return THEME_NAME_TO_KEY[value]
        if value in THEME_KEYS:
            return value
        return DEFAULT_THEME_KEY

    async def companion_personal_memory(self):
        status = self._private_companion_status()
        if not status["available"]:
            return self._ok(status)

        limit = self._query_int("limit", 80)
        selected_date = clean_text(request.args.get("date", ""), 16)
        query = clean_text(request.args.get("q", ""), 200)
        payload = dict(status)
        records = await self.plugin.service.store.list_memories(
            limit=max(limit * 6, 240),
            include_pending=True,
            query=query,
            visibility="bot_self",
        )
        dates = self._private_companion_dates(status.get("plugin"), records)
        if not selected_date:
            selected_date = dates[0] if dates else ""
        if selected_date and selected_date not in dates:
            dates.insert(0, selected_date)

        payload["selected_date"] = selected_date
        payload["dates"] = dates
        payload["snapshot"] = self._private_companion_snapshot(status.get("plugin"), selected_date)
        payload.pop("plugin", None)
        filtered = [record for record in records if self._memory_date_key(record) == selected_date] if selected_date else records
        payload["actions"] = [serialize_memory(record) for record in filtered if self._is_personal_action(record)][:limit]
        return self._ok(payload)

    def _private_companion_status(self) -> dict[str, Any]:
        for module_name in (
            "data.plugins.astrbot_plugin_private_companion.main",
            "astrbot_plugin_private_companion.main",
        ):
            module = sys.modules.get(module_name)
            if module is None:
                continue
            getter = getattr(module, "get_private_companion_api", None)
            if not callable(getter):
                continue
            try:
                api = getter()
            except Exception:
                api = None
            if api is None:
                continue
            plugin = getattr(api, "_plugin", None)
            return {
                "available": True,
                "plugin_name": "astrbot_plugin_private_companion",
                "daily_plan_enabled": bool(getattr(plugin, "enable_daily_plan", False)) if plugin else False,
                "detail_enabled": bool(getattr(plugin, "enable_detail_enhancement", False)) if plugin else False,
                "plugin": plugin,
            }
        return {
            "available": False,
            "plugin_name": "astrbot_plugin_private_companion",
            "reason": "未检测到已加载的主动陪伴插件",
        }

    def _private_companion_snapshot(self, plugin: Any, selected_date: str = "") -> dict[str, Any]:
        if plugin is None:
            return {}
        data = getattr(plugin, "data", {})
        if not isinstance(data, dict):
            data = {}
        plan = self._private_companion_plan_for_date(data, selected_date)
        if not isinstance(plan, dict):
            plan = {}
        state = data.get("daily_state", {})
        if not isinstance(state, dict):
            state = {}
        enhanced = data.get("detail_enhanced_segments", {})
        if not isinstance(enhanced, dict):
            enhanced = {}
        detail_day = clean_text(data.get("detail_enhanced_day"), 16)
        if selected_date and detail_day and selected_date != detail_day:
            enhanced = {}
        current_item = None
        getter = getattr(plugin, "_get_current_plan_item", None)
        if callable(getter) and (not selected_date or selected_date == clean_text(plan.get("date"), 16)):
            try:
                current_item = getter(plan)
            except Exception:
                current_item = None
        if not isinstance(current_item, dict):
            current_item = {}
        return {
            "bot_name": str(getattr(plugin, "bot_name", "") or ""),
            "plan": self._compact_plan(plan),
            "current_item": self._compact_plan_item(current_item),
            "daily_state": {
                "date": clean_text(state.get("date"), 40),
                "energy": state.get("energy", ""),
                "mood_bias": clean_text(state.get("mood_bias"), 80),
                "sleep": clean_text(state.get("sleep"), 120),
                "weather": clean_text(state.get("weather"), 160),
                "note": clean_text(state.get("note"), 240),
            },
            "details": self._compact_details(enhanced),
        }

    def _private_companion_plan_for_date(self, data: dict[str, Any], selected_date: str) -> dict[str, Any]:
        plan = data.get("daily_plan", {})
        if isinstance(plan, dict) and (not selected_date or clean_text(plan.get("date"), 16) == selected_date):
            return plan
        history = data.get("daily_plan_history", [])
        if isinstance(history, list):
            for entry in reversed(history):
                if not isinstance(entry, dict):
                    continue
                if clean_text(entry.get("date"), 16) != selected_date:
                    continue
                return {
                    "date": selected_date,
                    "source": entry.get("source") or "history",
                    "items": self._history_samples_to_plan_items(entry.get("sample")),
                }
        return plan if isinstance(plan, dict) else {}

    def _history_samples_to_plan_items(self, samples: Any) -> list[dict[str, Any]]:
        if not isinstance(samples, list):
            return []
        rows = []
        for sample in samples:
            text = clean_text(sample, 180)
            if not text:
                continue
            parts = text.split(maxsplit=1)
            if parts and ":" in parts[0]:
                rows.append({"index": len(rows), "time": parts[0], "activity": parts[1] if len(parts) > 1 else ""})
            else:
                rows.append({"index": len(rows), "time": "", "activity": text})
        return rows

    def _private_companion_dates(self, plugin: Any, records: list[Any]) -> list[str]:
        dates: set[str] = set()
        if plugin is not None:
            data = getattr(plugin, "data", {})
            if isinstance(data, dict):
                plan = data.get("daily_plan", {})
                if isinstance(plan, dict) and clean_text(plan.get("date"), 16):
                    dates.add(clean_text(plan.get("date"), 16))
                history = data.get("daily_plan_history", [])
                if isinstance(history, list):
                    for entry in history:
                        if isinstance(entry, dict) and clean_text(entry.get("date"), 16):
                            dates.add(clean_text(entry.get("date"), 16))
                if clean_text(data.get("detail_enhanced_day"), 16):
                    dates.add(clean_text(data.get("detail_enhanced_day"), 16))
        for record in records:
            key = self._memory_date_key(record)
            if key and (self._is_personal_action(record) or self._is_personal_schedule_memory(record)):
                dates.add(key)
        return sorted(dates, reverse=True)

    def _memory_date_key(self, record: Any) -> str:
        return self._date_key(getattr(record, "occurred_at", "") or getattr(record, "created_at", ""))

    def _date_key(self, value: Any) -> str:
        text = clean_text(value, 80)
        if not text:
            return ""
        try:
            normalized = text.replace("Z", "+00:00")
            dt = datetime.fromisoformat(normalized)
            if dt.tzinfo is not None:
                dt = dt.astimezone(ZoneInfo("Asia/Shanghai"))
            return dt.date().isoformat()
        except Exception:
            return text[:10] if len(text) >= 10 else ""

    def _is_personal_action(self, record: Any) -> bool:
        action_types = {
            "self_action",
            "proactive_message",
            "search_action",
            "creative_work",
            "image_action",
            "qzone_action",
            "reading_memory",
        }
        return (
            getattr(record, "visibility", "") == "bot_self"
            and (
                getattr(record, "memory_type", "") in action_types
                or getattr(record, "source_plugin", "") == "private_companion"
                and getattr(record, "memory_type", "") != "schedule_fragment"
            )
        )

    def _is_personal_schedule_memory(self, record: Any) -> bool:
        tags = getattr(record, "tags", []) or []
        return (
            getattr(record, "visibility", "") == "bot_self"
            and (
                getattr(record, "memory_type", "") in {"schedule_fragment", "persona_life"}
                or "schedule" in tags
                or "persona_life" in tags
            )
        )

    def _compact_plan(self, plan: dict[str, Any]) -> dict[str, Any]:
        items = plan.get("items", [])
        if not isinstance(items, list):
            items = []
        return {
            "date": clean_text(plan.get("date"), 40),
            "source": clean_text(plan.get("source"), 40),
            "items": [
                self._compact_plan_item(item, index=index)
                for index, item in enumerate(items)
                if isinstance(item, dict)
            ][:18],
        }

    def _compact_plan_item(self, item: dict[str, Any], index: int | None = None) -> dict[str, Any]:
        return {
            "index": index if index is not None else "",
            "time": clean_text(item.get("time"), 20),
            "activity": clean_text(item.get("activity") or item.get("title"), 180),
            "mood": clean_text(item.get("mood"), 80),
            "message_seed": clean_text(item.get("message_seed"), 220),
        }

    def _compact_details(self, enhanced: dict[str, Any]) -> list[dict[str, Any]]:
        rows = []
        for key, item in enhanced.items():
            if not isinstance(item, dict):
                continue
            key_text = clean_text(key, 80)
            rows.append(
                {
                    "key": key_text,
                    "index": self._detail_index_from_key(key_text),
                    "status": clean_text(item.get("status"), 40),
                    "time": clean_text(item.get("time") or self._detail_time_from_key(key_text) or item.get("started_at"), 40),
                    "summary": clean_text(item.get("summary"), 180),
                    "today_events": self._compact_detail_events(item.get("today_events")),
                    "proactive_events": self._compact_detail_events(item.get("proactive_events")),
                    "state_variables": self._compact_detail_events(item.get("state_variables")),
                }
            )
        return rows[-12:]

    def _detail_time_from_key(self, key: Any) -> str:
        parts = clean_text(key, 80).split(":")
        if len(parts) >= 4:
            return f"{parts[2]}:{parts[3]}"
        if len(parts) >= 3:
            return parts[2]
        return ""

    def _detail_index_from_key(self, key: Any) -> Any:
        parts = clean_text(key, 80).split(":")
        if len(parts) >= 2:
            try:
                return int(parts[1])
            except Exception:
                return ""
        return ""

    def _compact_detail_events(self, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        rows = []
        for item in value[:5]:
            if isinstance(item, dict):
                window = clean_text(
                    item.get("window")
                    or item.get("time")
                    or item.get("range")
                    or item.get("when"),
                    40,
                )
                text = (
                    item.get("event")
                    or item.get("content")
                    or item.get("detail")
                    or item.get("description")
                    or item.get("text")
                    or item.get("topic")
                    or item.get("why")
                    or item.get("motive")
                    or item.get("reason")
                    or item.get("action")
                    or item.get("label")
                    or item.get("title")
                )
            else:
                window = ""
                text = item
            cleaned = clean_text(text, 180)
            if cleaned:
                rows.append(f"{window} {cleaned}".strip() if window else cleaned)
        return rows

    async def maintenance(self):
        result = await self.plugin.service.sleep_maintenance(reason="page_maintenance")
        return self._ok({"result": result})

    async def sleep_maintenance(self):
        if request.method == "POST":
            result = await self.plugin.service.sleep_maintenance(reason="page_sleep")
        else:
            result = self.plugin.service.sleep_status()
        return self._ok({"result": result})

    async def repair_livingmemory_content(self):
        payload = await self._json()
        result = await self.plugin.service.migrator.repair_imported_content(
            configured_path=clean_text(payload.get("path"), 1000)
        )
        return self._ok({"result": result})

    async def clear_all(self):
        payload = await self._json()
        if clean_text(payload.get("confirm"), 20) != "清空":
            return self._err("confirmation mismatch", 400)
        result = await self.plugin.service.store.clear_all_memory_data()
        return self._ok({"result": result})

    async def import_preview(self):
        configured = clean_text(request.args.get("path", ""), 1000)
        report = self.plugin.service.migrator.preview(configured)
        return self._ok({"report": report})

    async def import_run(self):
        payload = await self._json()
        result = await self.plugin.service.import_livingmemory(
            configured_path=clean_text(payload.get("path"), 1000)
        )
        return self._ok({"result": result})

    @staticmethod
    def _ok(data: dict[str, Any] | None = None):
        body = {"success": True}
        if data:
            body.update(data)
        return jsonify(body)

    @staticmethod
    def _err(message: str, status: int = 500):
        response = jsonify({"success": False, "error": message})
        response.status_code = status
        return response

    async def _json(self) -> dict[str, Any]:
        payload = await request.get_json(silent=True)
        return payload if isinstance(payload, dict) else {}

    def _context_profile(self, scope: str) -> dict[str, Any]:
        return {
            "scope": scope,
            "enabled": self._context_bool(scope, "enabled", False),
            "max_events": self._context_int(scope, "max_events", 300),
            "drop_events": self._context_int(scope, "drop_events", 0),
            "overflow_strategy": self._context_strategy(self._context_value(scope, "overflow_strategy", "drop")),
            "summary_prompt": str(self._context_value(scope, "summary_prompt", "") or ""),
            "retain_recent_ratio": self._context_float(scope, "retain_recent_ratio", 0.15),
            "auto_understand_images": self._context_bool(scope, "auto_understand_images", False),
            "proactive_reply_enabled": self._context_bool(scope, "proactive_reply_enabled", False),
            "summary_provider_id": str(self._context_value(scope, "summary_provider_id", "") or ""),
            "summary_model": str(self._context_value(scope, "summary_model", "") or ""),
            "summary_fallback_provider_id": str(self._context_value(scope, "summary_fallback_provider_id", "") or ""),
            "summary_fallback_model": str(self._context_value(scope, "summary_fallback_model", "") or ""),
            "summary_max_chars": self._context_int(scope, "summary_max_chars", 360),
            "model_context_tokens": self._context_int(scope, "model_context_tokens", 0),
            "max_chars": self._context_int(scope, "max_chars", 1200),
            "async_precompress_enabled": self._context_bool(scope, "async_precompress_enabled", True),
            "precompress_threshold_percent": self._context_int(scope, "precompress_threshold_percent", 82),
            "allow_sync_compression": self._context_bool(scope, "allow_sync_compression", False),
            "sync_compression_timeout_ms": self._context_int(scope, "sync_compression_timeout_ms", 0),
            "manage_astrbot_history_enabled": self._context_bool(scope, "manage_astrbot_history_enabled", False),
            "astrbot_history_mode": str(self._context_value(scope, "astrbot_history_mode", "keep") or "keep"),
            "keep_recent_messages": self._context_int(scope, "keep_recent_messages", 2),
        }

    def _context_value(self, scope: str, key: str, default: Any) -> Any:
        marker = object()
        config = self.plugin.service.config
        if scope in {"private", "group"}:
            value = config.get(f"context_management.{scope}.{key}", marker)
            if value is not marker:
                return value
        return config.get(f"context_management.{key}", default)

    def _context_int(self, scope: str, key: str, default: int) -> int:
        return self._int(self._context_value(scope, key, default), default)

    def _context_float(self, scope: str, key: str, default: float) -> float:
        return self._float(self._context_value(scope, key, default), default)

    def _context_bool(self, scope: str, key: str, default: bool) -> bool:
        return self._bool(self._context_value(scope, key, default), default)

    def _clean_context_profile_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        ratio = max(0.0, min(1.0, self._float(payload.get("retain_recent_ratio"), 0.15)))
        model_context_tokens = max(0, self._int(payload.get("model_context_tokens"), 0))
        max_events = max(0, self._int(payload.get("max_events"), 0))
        default_keep_recent = 0
        manage_history = self._bool(payload.get("manage_astrbot_history_enabled"), False)
        mode = clean_text(payload.get("astrbot_history_mode"), 20).lower()
        if mode not in {"keep", "trim", "clear"}:
            mode = "trim" if manage_history and max_events > 0 else "keep"
        return {
            "enabled": self._bool(payload.get("enabled"), False),
            "max_events": max_events,
            "drop_events": max(0, self._int(payload.get("drop_events"), 0)),
            "overflow_strategy": self._context_strategy(payload.get("overflow_strategy")),
            "summary_prompt": clean_text(payload.get("summary_prompt"), 6000),
            "retain_recent_ratio": ratio,
            "auto_understand_images": self._bool(payload.get("auto_understand_images"), False),
            "proactive_reply_enabled": self._bool(payload.get("proactive_reply_enabled"), False),
            "summary_provider_id": clean_text(payload.get("summary_provider_id"), 160),
            "summary_model": clean_text(payload.get("summary_model"), 160),
            "summary_fallback_provider_id": clean_text(payload.get("summary_fallback_provider_id"), 160),
            "summary_fallback_model": clean_text(payload.get("summary_fallback_model"), 160),
            "summary_max_chars": max(80, self._int(payload.get("summary_max_chars"), 360)),
            "model_context_tokens": model_context_tokens,
            "max_chars": max(200, self._int(payload.get("max_chars"), 1200)),
            "async_precompress_enabled": self._bool(payload.get("async_precompress_enabled"), True),
            "precompress_threshold_percent": max(1, min(100, self._int(payload.get("precompress_threshold_percent"), 82))),
            "allow_sync_compression": self._bool(payload.get("allow_sync_compression"), False),
            "sync_compression_timeout_ms": max(0, self._int(payload.get("sync_compression_timeout_ms"), 0)),
            "manage_astrbot_history_enabled": manage_history,
            "astrbot_history_mode": mode,
            "keep_recent_messages": max(0, self._int(payload.get("keep_recent_messages"), default_keep_recent)),
        }

    def _write_plugin_config(self, raw: dict[str, Any]) -> None:
        path = self._plugin_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")

    def _plugin_config_path(self) -> Path:
        data_dir = Path(getattr(self.plugin.service, "data_dir", ""))
        root = data_dir.parent.parent if data_dir.parent.name == "plugin_data" else data_dir.parent
        return root / "config" / f"{PLUGIN_NAME}_config.json"

    def _provider_options(self) -> list[dict[str, str]]:
        options = [{"id": "", "label": "不使用 LLM 压缩"}]
        context = getattr(self.plugin, "context", None)
        getter = getattr(context, "get_all_providers", None)
        if not callable(getter):
            return options
        try:
            providers = getter()
        except Exception:
            return options
        for provider in providers or []:
            try:
                meta = provider.meta()
            except Exception:
                meta = None
            provider_id = str(getattr(meta, "id", "") or "")
            if not provider_id:
                continue
            provider_type = str(getattr(meta, "type", "") or "").strip()
            model_name = str(getattr(meta, "model", "") or getattr(provider, "model_name", "") or "").strip()
            label = provider_id
            if provider_type:
                label = f"{provider_type} ({provider_id})"
            if model_name and model_name not in label:
                label = f"{label} - {model_name}"
            options.append({"id": provider_id, "label": label})
        return options

    def _query_int(self, key: str, default: int) -> int:
        return self._int(request.args.get(key), default)

    @staticmethod
    def _context_strategy(value: Any) -> str:
        text = clean_text(value, 40).lower()
        if text in {"drop", "truncate", "trim", "cut"}:
            return "drop"
        if text in {"summarize", "summary", "compress", "llm"}:
            return "summarize"
        return "drop"

    @staticmethod
    def _acl_window_error(scope: str, window_id: str) -> str:
        if scope not in {"private", "group"}:
            return "ACL scope must be private or group"
        if not window_id:
            return "ACL window id is required"
        return ""

    @staticmethod
    def _acl_effect(value: Any) -> str:
        return "deny" if clean_text(value, 20).lower() in {"deny", "block", "blacklist"} else "allow"

    @staticmethod
    def _acl_mode(value: Any) -> str:
        text = clean_text(value, 20).lower()
        if not text:
            return ""
        return "blacklist" if text in {"blacklist", "deny", "block"} else "whitelist"

    @staticmethod
    def _int(value: Any, default: int) -> int:
        try:
            return int(value)
        except Exception:
            return default

    @staticmethod
    def _float(value: Any, default: float) -> float:
        try:
            return float(value)
        except Exception:
            return default

    @staticmethod
    def _bool(value: Any, default: bool) -> bool:
        if value is None:
            return default
        if isinstance(value, str):
            return value.strip().lower() not in {"0", "false", "off", "no", "否", "关"}
        return bool(value)
