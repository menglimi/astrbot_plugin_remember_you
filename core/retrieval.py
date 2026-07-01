from __future__ import annotations

import math
import re
from datetime import datetime, timezone

from .models import MemoryRecord, SearchResult, SessionContext, clean_text
from .store import MemoryStore
from .visibility import VisibilityPolicy


class RetrievalEngine:
    def __init__(self, store: MemoryStore, policy: VisibilityPolicy):
        self.store = store
        self.policy = policy

    async def search(self, query: str, ctx: SessionContext, top_k: int = 6) -> list[SearchResult]:
        results, _blocked = await self.search_with_diagnostics(query, ctx, top_k)
        return results

    async def search_with_diagnostics(
        self, query: str, ctx: SessionContext, top_k: int = 6
    ) -> tuple[list[SearchResult], list[dict[str, str]]]:
        results, blocked = await self._rank_candidates(query, ctx)
        selected = results[: max(1, int(top_k or 1))]
        await self.store.mark_accessed([item.memory.id for item in selected])
        return selected, blocked

    async def search_by_slots(
        self,
        query: str,
        ctx: SessionContext,
        *,
        slot_limits: dict[str, int],
        total_limit: int = 6,
    ) -> tuple[list[SearchResult], list[dict[str, str]], dict[str, list[SearchResult]]]:
        ranked, blocked = await self._rank_candidates(query, ctx)
        total = max(1, int(total_limit or 1))
        slot_order = [
            "self_timeline",
            "user_profile",
            "current_window",
            "conversation_summary",
            "stable_memory",
        ]
        selected: list[SearchResult] = []
        selected_ids: set[str] = set()
        slot_map: dict[str, list[SearchResult]] = {slot: [] for slot in slot_order}

        for slot in slot_order:
            limit = max(0, int(slot_limits.get(slot, 0) or 0))
            if limit <= 0:
                continue
            for item in ranked:
                if len(selected) >= total or len(slot_map[slot]) >= limit:
                    break
                if item.memory.id in selected_ids:
                    continue
                if self._slot_for_memory(item.memory, ctx) != slot:
                    continue
                item.reason = self._with_slot_reason(item.reason, slot)
                slot_map[slot].append(item)
                selected.append(item)
                selected_ids.add(item.memory.id)

        if len(selected) < total:
            for item in ranked:
                if len(selected) >= total:
                    break
                if item.memory.id in selected_ids:
                    continue
                slot = self._slot_for_memory(item.memory, ctx)
                item.reason = self._with_slot_reason(item.reason, slot)
                slot_map.setdefault(slot, []).append(item)
                selected.append(item)
                selected_ids.add(item.memory.id)

        await self.store.mark_accessed([item.memory.id for item in selected])
        return selected, blocked, {slot: items for slot, items in slot_map.items() if items}

    def _with_slot_reason(self, reason: str, slot: str) -> str:
        if reason.startswith("slot="):
            return reason
        return f"slot={slot};{reason}"

    async def _rank_candidates(
        self, query: str, ctx: SessionContext
    ) -> tuple[list[SearchResult], list[dict[str, str]]]:
        query = clean_text(query, 1000)
        candidates = await self.store.list_candidate_memories(limit=600, include_pending=not self.policy.hide_pending_review)
        acl_state = await self._acl_state() if self.policy.enable_acl_rules else self._empty_acl_state()
        terms = self._terms(query)
        profile = self._query_profile(query, terms)
        term_stats = self._term_document_stats(candidates, terms)
        results: list[SearchResult] = []
        blocked: list[dict[str, str]] = []
        for memory in candidates:
            visible, visibility_reason = self.policy.is_visible(memory, ctx)
            acl_deny_reason = self._acl_deny_reason(memory, ctx, acl_state)
            if visible and acl_deny_reason:
                if len(blocked) < 40:
                    blocked.append(
                        {
                            "id": memory.id,
                            "reason": acl_deny_reason,
                            "content": clean_text(memory.content, 120),
                        }
                    )
                continue
            if not visible:
                if acl_deny_reason:
                    visibility_reason = acl_deny_reason
                else:
                    acl_reason = self._acl_visibility_reason(memory, ctx, visibility_reason, acl_state)
                    if acl_reason:
                        visible = True
                        visibility_reason = acl_reason
                    else:
                        privacy_reason = self._acl_privacy_guard_reason(memory, ctx, visibility_reason, acl_state)
                        if privacy_reason:
                            visibility_reason = privacy_reason
            if not visible:
                if len(blocked) < 40:
                    blocked.append(
                        {
                            "id": memory.id,
                            "reason": visibility_reason,
                            "content": clean_text(memory.content, 120),
                        }
                    )
                continue
            score, reason = self._score(memory, terms, ctx, profile, term_stats)
            if score <= 0:
                if len(blocked) < 40:
                    blocked.append(
                        {
                            "id": memory.id,
                            "reason": reason,
                            "content": clean_text(memory.content, 120),
                        }
                    )
                continue
            results.append(SearchResult(memory=memory, score=score, reason=f"{visibility_reason};{reason}"))
        results.sort(key=lambda item: item.score, reverse=True)
        return results, blocked

    def _slot_for_memory(self, memory: MemoryRecord, ctx: SessionContext) -> str:
        tags = {str(tag).lower() for tag in (memory.tags or [])}
        memory_type = (memory.memory_type or "").lower()
        reality = (memory.reality_level or "").lower()
        if (
            memory.visibility == "bot_self"
            or reality in {"bot_action", "persona_life", "fictional_content"}
            or memory_type
            in {
                "self_action",
                "persona_life",
                "proactive_message",
                "search_action",
                "creative_work",
                "image_action",
                "qzone_action",
                "reading_memory",
                "schedule_fragment",
                "companion_note",
            }
        ):
            return "self_timeline"
        if (
            memory_type
            in {
                "user_profile",
                "user_preference",
                "relationship_claim",
                "explicit_memory",
                "manual_memory",
            }
            or "stable_fact" in tags
            or "relationship_claim" in tags
        ):
            return "user_profile"
        if memory_type == "conversation_summary" or "summary" in tags:
            return "conversation_summary"
        if (
            (ctx.scope == "private" and (memory.visibility == "private_pair" or memory.scope == "private"))
            or (ctx.scope == "group" and (memory.visibility == "group_public" or memory.scope == "group"))
        ):
            return "current_window"
        return "stable_memory"

    async def _acl_state(self) -> dict[str, object]:
        rules = await self.store.list_acl_rules(enabled_only=True)
        policies = await self.store.list_acl_policies()
        allow_pairs: set[tuple[str, str, str, str]] = set()
        deny_pairs: set[tuple[str, str, str, str]] = set()
        for rule in rules:
            owner_scope = clean_text(rule.get("owner_scope"), 40)
            owner_id = clean_text(rule.get("owner_id"), 160)
            reader_scope = clean_text(rule.get("reader_scope"), 40)
            reader_id = clean_text(rule.get("reader_id"), 160)
            effect = "deny" if clean_text(rule.get("effect"), 20).lower() == "deny" else "allow"
            if not (owner_scope and owner_id and reader_scope and reader_id):
                continue
            pair = (owner_scope, owner_id, reader_scope, reader_id)
            if effect == "deny":
                deny_pairs.add(pair)
            else:
                allow_pairs.add(pair)
        policy_map: dict[tuple[str, str], dict[str, str]] = {}
        for policy in policies:
            scope = clean_text(policy.get("window_scope"), 40)
            window_id = clean_text(policy.get("window_id"), 160)
            if not scope or not window_id:
                continue
            policy_map[(scope, window_id)] = {
                "read_mode": self._normalize_acl_mode(policy.get("read_mode")),
                "share_mode": self._normalize_acl_mode(policy.get("share_mode")),
            }
        return {"allow": allow_pairs, "deny": deny_pairs, "policies": policy_map}

    def _empty_acl_state(self) -> dict[str, object]:
        return {"allow": set(), "deny": set(), "policies": {}}

    def _acl_deny_reason(
        self,
        memory: MemoryRecord,
        ctx: SessionContext,
        acl_state: dict[str, object],
    ) -> str:
        owner = self._memory_owner(memory)
        reader = self._reader_window(ctx)
        if not owner or not reader or owner == reader:
            return ""
        deny_pairs = acl_state.get("deny", set())
        pair = (owner[0], owner[1], reader[0], reader[1])
        if pair in deny_pairs:
            return f"acl_denied:{owner[0]}:{owner[1]}->{reader[0]}:{reader[1]}"
        return ""

    def _acl_visibility_reason(
        self,
        memory: MemoryRecord,
        ctx: SessionContext,
        default_reason: str,
        acl_state: dict[str, object],
    ) -> str:
        if default_reason not in {"other_group_public", "other_private_pair", "private_pair_not_current_private"}:
            return ""
        owner = self._memory_owner(memory)
        reader = self._reader_window(ctx)
        if not owner or not reader or owner == reader:
            return ""
        pair = (owner[0], owner[1], reader[0], reader[1])
        deny_pairs = acl_state.get("deny", set())
        if pair in deny_pairs:
            return ""
        allow_pairs = acl_state.get("allow", set())
        if pair in allow_pairs:
            return f"acl_allowed:{owner[0]}:{owner[1]}->{reader[0]}:{reader[1]}"
        policies = acl_state.get("policies", {})
        owner_policy = self._acl_policy_for(policies, owner)
        reader_policy = self._acl_policy_for(policies, reader)
        if owner_policy.get("share_mode") == "blacklist" and reader_policy.get("read_mode") == "blacklist":
            if self._requires_explicit_allow(owner, reader):
                return ""
            return f"acl_blacklist_default:{owner[0]}:{owner[1]}->{reader[0]}:{reader[1]}"
        return ""

    def _acl_privacy_guard_reason(
        self,
        memory: MemoryRecord,
        ctx: SessionContext,
        default_reason: str,
        acl_state: dict[str, object],
    ) -> str:
        if default_reason not in {"other_group_public", "other_private_pair", "private_pair_not_current_private"}:
            return ""
        owner = self._memory_owner(memory)
        reader = self._reader_window(ctx)
        if not owner or not reader or owner == reader or not self._requires_explicit_allow(owner, reader):
            return ""
        pair = (owner[0], owner[1], reader[0], reader[1])
        if pair in acl_state.get("allow", set()) or pair in acl_state.get("deny", set()):
            return ""
        policies = acl_state.get("policies", {})
        owner_policy = self._acl_policy_for(policies, owner)
        reader_policy = self._acl_policy_for(policies, reader)
        if owner_policy.get("share_mode") == "blacklist" and reader_policy.get("read_mode") == "blacklist":
            return f"acl_privacy_guard_requires_allow:{owner[0]}:{owner[1]}->{reader[0]}:{reader[1]}"
        return ""

    def _requires_explicit_allow(self, owner: tuple[str, str], reader: tuple[str, str]) -> bool:
        return owner[0] == "private" and reader[0] == "group"

    def _acl_policy_for(self, policies: object, window: tuple[str, str]) -> dict[str, str]:
        if isinstance(policies, dict):
            policy = policies.get(window)
            if isinstance(policy, dict):
                return {
                    "read_mode": self._normalize_acl_mode(policy.get("read_mode")),
                    "share_mode": self._normalize_acl_mode(policy.get("share_mode")),
                }
        return {"read_mode": "whitelist", "share_mode": "whitelist"}

    def _normalize_acl_mode(self, mode: object) -> str:
        return "blacklist" if clean_text(mode, 20).lower() == "blacklist" else "whitelist"

    def _memory_owner(self, memory: MemoryRecord) -> tuple[str, str] | None:
        if memory.scope == "group" or memory.visibility == "group_public":
            owner_id = memory.group_id
            if not owner_id and memory.object.kind == "group":
                owner_id = memory.object.id
            if not owner_id and memory.subject.kind == "group":
                owner_id = memory.subject.id
            if not owner_id:
                owner_id = memory.session_id
            return ("group", clean_text(owner_id, 160)) if owner_id else None
        if memory.scope == "private" or memory.visibility == "private_pair":
            owner_id = ""
            for entity in (memory.subject, memory.object):
                if entity.kind == "user" and entity.id and entity.id != "self":
                    owner_id = entity.id
                    break
            if not owner_id:
                owner_id = memory.session_id
            return ("private", clean_text(owner_id, 160)) if owner_id else None
        return None

    def _reader_window(self, ctx: SessionContext) -> tuple[str, str] | None:
        if ctx.scope == "group":
            reader_id = ctx.group_id or ctx.session_id
            return ("group", clean_text(reader_id, 160)) if reader_id else None
        if ctx.scope == "private":
            reader_id = ctx.user_id or ctx.session_id
            return ("private", clean_text(reader_id, 160)) if reader_id else None
        return None

    def _terms(self, query: str) -> list[str]:
        words = re.findall(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]{2,}", query)
        terms: list[str] = []
        for word in words:
            if re.fullmatch(r"[\u4e00-\u9fff]{4,}", word):
                terms.extend(word[i : i + 2] for i in range(0, len(word) - 1))
            terms.append(word)
        return list(dict.fromkeys(term.lower() for term in terms if len(term.strip()) >= 2))[:20]

    def _query_profile(self, query: str, terms: list[str]) -> dict[str, object]:
        compact = re.sub(r"\s+", "", query).lower()
        cjk_phrases = re.findall(r"[\u4e00-\u9fff]{4,}", compact)
        exact_phrases = [phrase for phrase in cjk_phrases if len(phrase) >= 4]
        # Long concrete Chinese phrases should not match by relation/recency alone.
        # Require at least two overlapping fragments unless the whole phrase appears.
        min_hits = 1
        if exact_phrases:
            min_hits = min(3, max(2, len(exact_phrases[0]) // 3))
        elif len(terms) >= 4:
            min_hits = 2
        return {"exact_phrases": exact_phrases, "min_hits": min_hits}

    def _term_document_stats(self, memories: list[MemoryRecord], terms: list[str]) -> dict[str, float]:
        if not terms:
            return {}
        document_count = max(1, len(memories))
        dfs = dict.fromkeys(terms, 0)
        for memory in memories:
            haystack = self._haystack(memory)
            for term in terms:
                if term in haystack:
                    dfs[term] += 1
        return {
            term: math.log(1 + (document_count - df + 0.5) / (df + 0.5))
            for term, df in dfs.items()
            if df > 0
        }

    def _haystack(self, memory: MemoryRecord) -> str:
        return " ".join(
            [
                memory.content,
                memory.evidence,
                " ".join(memory.tags),
                memory.subject.name,
                memory.subject.id,
                memory.object.name,
                memory.object.id,
                memory.group_id,
            ]
        ).lower()

    def _score(
        self,
        memory: MemoryRecord,
        terms: list[str],
        ctx: SessionContext,
        profile: dict[str, object],
        term_stats: dict[str, float],
    ) -> tuple[float, str]:
        haystack = self._haystack(memory)
        compact_haystack = re.sub(r"\s+", "", haystack)
        term_hits = sum(1 for term in terms if term and term in haystack)
        exact_phrases = [str(item) for item in profile.get("exact_phrases", []) if str(item)]
        exact_hit = any(phrase in compact_haystack for phrase in exact_phrases)
        min_hits = int(profile.get("min_hits", 1) or 1)
        if terms and not exact_hit and term_hits < min_hits:
            return 0.0, f"keyword_hit_too_weak hits={term_hits}/{min_hits}"
        bm25 = 0.0
        if terms:
            for term in terms:
                if not term:
                    continue
                freq = haystack.count(term)
                if freq <= 0:
                    continue
                idf = term_stats.get(term, 0.0)
                bm25 += idf * ((freq * 2.2) / (freq + 1.2))
            lexical = (0.42 if exact_hit else 0.24) + min(0.72, bm25 * 0.18)
        else:
            lexical = 0.0

        scope_bonus = 0.0
        if memory.session_id and memory.session_id == ctx.session_id:
            scope_bonus += 0.25
        if ctx.user_id and ctx.user_id in {memory.subject.id, memory.object.id}:
            scope_bonus += 0.15
        if ctx.group_id and ctx.group_id == memory.group_id:
            scope_bonus += 0.15
        if memory.visibility == "bot_self":
            scope_bonus += 0.08

        age_bonus = self._recency_bonus(memory.occurred_at or memory.created_at)
        score = lexical + scope_bonus + memory.importance * 0.55 + memory.confidence * 0.25 + age_bonus
        if not terms:
            score = scope_bonus + memory.importance * 0.8 + age_bonus
        return score, f"hits={term_hits};exact={int(exact_hit)};bm25={bm25:.2f};importance={memory.importance:.2f};recency={age_bonus:.2f}"

    def _recency_bonus(self, iso_text: str) -> float:
        if not iso_text:
            return 0.0
        try:
            dt = datetime.fromisoformat(iso_text.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            days = max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 86400)
        except Exception:
            return 0.0
        return 0.2 * math.exp(-days / 14.0)
