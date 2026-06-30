import asyncio
import json
import os
import re
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

try:
    from astrbot.core.utils.astrbot_path import get_astrbot_data_path
except Exception:
    get_astrbot_data_path = None


PLUGIN_NAME = "astrbot_plugin_new_member_forwarder"


class TempSessionNotReadyError(RuntimeError):
    pass


@register(
    PLUGIN_NAME,
    "Codex",
    "管理员私聊录制新人入群资料，新人进群时自动私聊转发文字、图片和聊天记录。",
    "1.4.43",
)
class NewMemberForwarderPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}
        self.plugin_dir = Path(__file__).resolve().parent
        self._recent_events: dict[str, float] = {}
        self._recording_sessions: dict[str, dict[str, Any]] = {}
        self._image_reply_recording_sessions: dict[str, dict[str, Any]] = {}
        self._image_buckets: dict[str, dict[str, Any]] = {}
        self._image_tasks: dict[str, asyncio.Task] = {}
        self._delivery_inflight: dict[str, int] = {}
        self._delivery_queue_lock = asyncio.Lock()
        self._last_delivery_finished_at = 0.0
        self._qq_desktop_warmup_sent_at: dict[str, float] = {}
        self._qq_human_group_warmup_last_at: dict[str, float] = {}
        self._qq_human_group_warmup_results: dict[str, dict[str, Any]] = {}
        self._test_delivery_running_until = 0.0
        self.data_dir = self._resolve_data_dir()
        self.media_dir = self.data_dir / "media"
        self.record_file = self.data_dir / "recorded_material.json"
        self.image_reply_file = self.data_dir / "image_reply_assets.json"
        self.delivery_history_file = self.data_dir / "delivery_history.json"
        self.pending_file = self.data_dir / "pending_deliveries.json"
        self.warmup_source_file = self.data_dir / "warmup_source.json"
        self.config_file = self._resolve_config_file()
        self._config_file_cache: dict[str, Any] = {}
        self._config_file_mtime: float | None = None
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.media_dir.mkdir(parents=True, exist_ok=True)
        logger.info(
            "new_member_forwarder: config file=%s human_warmup=%s queue_gap=%.1fs original_forward_only=%s",
            self.config_file,
            self._get_bool("qq_human_group_warmup_enabled", False),
            max(0.0, self._get_float("delivery_queue_gap_seconds", 1.0)),
            True,
        )

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def private_recorder(self, event: AstrMessageEvent):
        sender_id = self._string(event.get_sender_id())
        if not sender_id or not self._is_admin(sender_id):
            return

        text = self._normalize_control_text(event.get_message_str())
        if text in self._start_words():
            self._recording_sessions[sender_id] = {
                "started_at": int(time.time()),
                "items": [],
            }
            yield event.plain_result(
                "已开始录制新人资料。\n"
                "接下来请直接私聊发送要转发给新人的内容：文字、图片、合并聊天记录都可以。\n"
                "发送“结束”保存，发送“取消”放弃。"
            )
            return

        if text in self._cancel_words():
            if self._recording_sessions.pop(sender_id, None) is not None:
                yield event.plain_result("已取消本次录制，未覆盖已保存资料。")
            elif self._image_reply_recording_sessions.pop(sender_id, None) is not None:
                yield event.plain_result("已取消添加图片回复。")
            else:
                yield event.plain_result("当前没有正在录制的内容。")
            return

        if text in self._end_words():
            session = self._recording_sessions.pop(sender_id, None)
            if not session:
                yield event.plain_result("当前没有正在录制的内容。先发送“开始”。")
                return

            items = session.get("items") or []
            if not items:
                yield event.plain_result("本次没有录到任何内容，已取消保存。")
                return

            payload = {
                "version": 2,
                "updated_at": int(time.time()),
                "updated_by": sender_id,
                "items": items,
            }
            self._save_recorded_payload(payload)
            yield event.plain_result(f"已保存新人资料，共 {len(items)} 条发送项。新人入群后会自动私聊发送。")
            return

        if text in self._status_words():
            saved_count = len(self._load_recorded_payload().get("items") or [])
            current_count = len((self._recording_sessions.get(sender_id) or {}).get("items") or [])
            recording = "是" if sender_id in self._recording_sessions else "否"
            yield event.plain_result(
                f"正在录制：{recording}\n"
                f"本次已录：{current_count} 条\n"
                f"已保存资料：{saved_count} 条\n"
                f"正在添加图片回复：{'是' if sender_id in self._image_reply_recording_sessions else '否'}\n"
                f"存储位置：{self.record_file}"
            )
            return

        if text in self._clear_words():
            self._save_recorded_payload(
                {
                    "version": 2,
                    "updated_at": int(time.time()),
                    "updated_by": sender_id,
                    "items": [],
                }
            )
            yield event.plain_result("已清空已保存的新人资料。")
            return

        session = self._recording_sessions.get(sender_id)
        if not session:
            return

        bot = getattr(event, "bot", None)
        try:
            captured_items = await self._capture_event_items(event, bot)
        except Exception as exc:
            logger.exception("new_member_forwarder: capture material failed: %s", exc)
            yield event.plain_result(f"这条内容录制失败：{exc}")
            return

        if not captured_items:
            yield event.plain_result("这条消息没有可保存的文字、图片或聊天记录。")
            return

        session["items"].extend(captured_items)
        yield event.plain_result(f"已录入 {len(captured_items)} 条发送项，当前共 {len(session['items'])} 条。")

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_new_member_notice(self, event: AstrMessageEvent):
        raw = self._raw_event(event)
        if not self._is_group_increase(raw):
            return
        if not self._get_bool("enabled", True):
            return

        group_id = self._string(raw.get("group_id") or event.get_group_id())
        user_id = self._string(raw.get("user_id") or event.get_sender_id())
        self_id = self._string(raw.get("self_id") or event.get_self_id())
        if not group_id or not user_id:
            logger.warning("new_member_forwarder: group_increase event missing group_id/user_id: %s", raw)
            return
        if self_id and user_id == self_id and not self._get_bool("send_to_bot_itself", False):
            return
        if not self._is_group_allowed(group_id):
            return
        if self._is_duplicate(group_id, user_id):
            return
        delivery_slot_key = self._reserve_delivery_slot(group_id, user_id)
        if delivery_slot_key is None:
            return

        try:
            payload = self._load_recorded_payload()
            items = payload.get("items") or []
            if not items:
                logger.info("new_member_forwarder: no recorded material, skip delivery.")
                return

            delay = max(0.0, self._get_float("send_delay_seconds", 1.5))
            if delay:
                await asyncio.sleep(delay)

            bot = getattr(event, "bot", None)
            if not bot:
                logger.warning("new_member_forwarder: current event has no aiocqhttp bot instance.")
                return

            try:
                await self._deliver_private_with_retries(bot, user_id, items, self_id, group_id)
                if delivery_slot_key:
                    self._mark_delivery_success(group_id, user_id)
            except Exception as exc:
                logger.exception(
                    "new_member_forwarder: human warmup or recorded material delivery failed for user %s in group %s; "
                    "delivery was not counted: %s",
                    user_id,
                    group_id,
                    self._short_error(exc),
                )
        finally:
            if delivery_slot_key:
                self._release_delivery_slot(delivery_slot_key)

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.command("新人欢迎测试")
    async def test_delivery(self, event: AstrMessageEvent, target_qq: str = "", source_group_id: str = ""):
        if not self._can_run_admin_or_self_command(event):
            return

        message = await self._run_test_delivery(event, target_qq, source_group_id)
        if message:
            yield event.plain_result(message)

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.ALL, priority=900)
    async def self_sent_test_delivery(self, event: AstrMessageEvent):
        if not self._is_self_sender(event):
            return
        if getattr(event, "is_at_or_wake_command", False):
            return
        command_args = self._parse_self_test_command(event.get_message_str())
        if command_args is None:
            return
        if time.time() < self._test_delivery_running_until:
            return

        target_qq, source_group_id = command_args
        message = await self._run_test_delivery(event, target_qq, source_group_id)
        if message:
            self._stop_event(event)
            yield event.plain_result(message)

    @filter.after_message_sent()
    async def after_message_sent_test_delivery(self, event: AstrMessageEvent):
        if time.time() < self._test_delivery_running_until:
            return
        command_args = self._parse_self_test_command(self._result_plain_text(event))
        if command_args is None:
            return

        target_qq, source_group_id = command_args
        message = await self._run_test_delivery(event, target_qq, source_group_id)
        if not message:
            return
        try:
            await event.send(event.plain_result(message))
        except Exception as exc:
            logger.exception("new_member_forwarder: failed to send after-sent test result: %s", exc)

    async def _run_test_delivery(self, event: AstrMessageEvent, target_qq: str = "", source_group_id: str = "") -> str:
        user_id = self._string(target_qq or event.get_sender_id())
        group_id = self._string(source_group_id or event.get_group_id())
        if not user_id.isdigit() or not group_id.isdigit():
            return "用法：/新人欢迎测试 QQ号 来源群号"
        payload = self._load_recorded_payload()
        items = payload.get("items") or []
        if not items:
            return "还没有保存新人资料。请私聊发送“开始”录制。"

        bot = getattr(event, "bot", None)
        if not bot:
            return "当前事件没有 OneBot bot 实例，无法测试发送。"

        self._test_delivery_running_until = time.time() + 300.0
        try:
            await self._deliver_private_with_retries(bot, user_id, items, event.get_self_id(), group_id)
        except Exception as exc:
            logger.exception("new_member_forwarder: test delivery failed: %s", exc)
            return f"测试发送失败：{self._short_error(exc, 220)}"
        finally:
            self._test_delivery_running_until = time.time() + 5.0

        return f"已私聊 QQ {user_id} 执行一次测试发送。"

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.command("新人欢迎重置发送次数", alias={"新人欢迎清除发送次数", "新人欢迎重置次数"})
    async def reset_delivery_count(self, event: AstrMessageEvent, target_qq: str = "", source_group_id: str = ""):
        if not self._is_admin(self._string(event.get_sender_id())):
            return

        user_id = self._string(target_qq)
        group_id = self._string(source_group_id or event.get_group_id())
        if not user_id.isdigit():
            yield event.plain_result("用法：/新人欢迎重置发送次数 QQ号 [群号]")
            return

        removed = self._reset_delivery_history_for(group_id, user_id)
        self._delivery_inflight.pop(self._delivery_history_key(group_id, user_id), None)
        if removed:
            yield event.plain_result(f"已重置 QQ {user_id} 的新人欢迎发送次数，可重新用进群事件测试。")
        else:
            yield event.plain_result(f"QQ {user_id} 当前没有发送次数记录。")

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.command("新人欢迎开路测试")
    async def test_warmup_delivery(self, event: AstrMessageEvent, target_qq: str = "", source_group_id: str = ""):
        if not self._is_admin(self._string(event.get_sender_id())):
            return

        user_id = self._string(target_qq or event.get_sender_id())
        group_id = self._string(source_group_id or event.get_group_id())
        if not user_id.isdigit() or not group_id.isdigit():
            yield event.plain_result("用法：/新人欢迎开路测试 QQ号 来源群号")
            return

        ok = await self._send_qq_human_group_warmup_message_queued(group_id, user_id, force=True)
        if ok:
            yield event.plain_result(f"真人开路已执行：QQ {user_id}，来源群 {group_id}。")
            return
        result = self._qq_human_group_warmup_results.get(f"{group_id}:{user_id}") or {}
        reason = self._string(result.get("reason")) or "unknown"
        stage = self._string(result.get("stage")) or "-"
        yield event.plain_result(f"真人开路未成功执行：stage={stage}，reason={reason}")

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.command("新人欢迎真人开路测试")
    async def test_human_group_warmup_delivery(
        self,
        event: AstrMessageEvent,
        target_qq: str = "",
        source_group_id: str = "",
    ):
        if not self._can_run_admin_or_self_command(event):
            return

        user_id = self._string(target_qq or event.get_sender_id())
        group_id = self._string(source_group_id or event.get_group_id())
        text = self._string(self._get("forward_warmup_message_text", "欢迎进群")).strip()
        if not text:
            yield event.plain_result("真人开路消息为空，请先在后台设置 forward_warmup_message_text。")
            return
        if not user_id.isdigit() or not group_id.isdigit():
            yield event.plain_result("用法：/新人欢迎真人开路测试 QQ号 来源群号")
            return

        ok = await self._send_qq_human_group_warmup_message_queued(
            group_id,
            user_id,
            force=True,
        )
        if ok:
            yield event.plain_result(f"真人开路已执行：QQ {user_id}，来源群 {group_id}。")
        else:
            result = self._qq_human_group_warmup_results.get(f"{group_id}:{user_id}") or {}
            reason = self._string(result.get("reason")) or "unknown"
            stage = self._string(result.get("stage")) or "-"
            yield event.plain_result(f"真人开路未成功执行：stage={stage}，reason={reason}")

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.command("添加一图回复图片", alias={"设置一图回复图片", "录入一图回复图片"})
    async def add_one_image_reply_asset(self, event: AstrMessageEvent):
        async for result in self._add_image_reply_asset_command(event, "one"):
            yield result

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.command("添加两图回复图片", alias={"设置两图回复图片", "录入两图回复图片"})
    async def add_two_image_reply_asset(self, event: AstrMessageEvent):
        async for result in self._add_image_reply_asset_command(event, "two"):
            yield result

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.command("删除一图回复图片", alias={"移除一图回复图片"})
    async def delete_one_image_reply_asset(self, event: AstrMessageEvent):
        async for result in self._delete_image_reply_asset_command(event, "one"):
            yield result

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.command("删除两图回复图片", alias={"移除两图回复图片"})
    async def delete_two_image_reply_asset(self, event: AstrMessageEvent):
        async for result in self._delete_image_reply_asset_command(event, "two"):
            yield result

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def private_image_reply(self, event: AstrMessageEvent):
        if not self._get_bool("enabled", True):
            return
        if not self._get_bool("private_image_reply_enabled", True):
            return

        sender_id = self._string(event.get_sender_id())
        if not sender_id:
            return
        if self._is_pending_private_consumed(sender_id):
            return

        text = self._normalize_control_text(event.get_message_str())
        image_segments = self._image_segments_from_event(event)

        if self._is_admin(sender_id) and sender_id in self._image_reply_recording_sessions:
            if text in self._cancel_words() or self._is_image_reply_command_text(text):
                return
            if not image_segments:
                yield event.plain_result("这条没有识别到图片。请直接发送图片；发送“取消”可取消。")
                return
            rule_kind = self._string(self._image_reply_recording_sessions.get(sender_id, {}).get("rule_kind"))
            if rule_kind not in {"one", "two"}:
                self._image_reply_recording_sessions.pop(sender_id, None)
                return
            asset = await self._extract_image_reply_asset(image_segments)
            if not asset:
                yield event.plain_result("这条图片保存失败，请重新发送图片。")
                return
            self._image_reply_recording_sessions.pop(sender_id, None)
            self._save_image_reply_asset(rule_kind, asset)
            yield event.plain_result(f"已添加{self._image_rule_label(rule_kind)}回复图片。")
            return

        if sender_id in self._recording_sessions:
            return
        if self._is_private_control_text(text):
            return
        if not image_segments:
            return

        bot = getattr(event, "bot", None)
        if not bot:
            logger.warning("new_member_forwarder: private image reply skipped because event has no bot instance.")
            return
        await self._handle_private_images(bot, sender_id, len(image_segments), self._event_group_id(event))

    async def _capture_event_items(self, event: AstrMessageEvent, bot: Any) -> list[dict[str, Any]]:
        raw = self._raw_event(event)
        raw_segments = raw.get("message")
        if not isinstance(raw_segments, list):
            raw_segments = [segment.toDict() for segment in event.get_messages() if hasattr(segment, "toDict")]

        items: list[dict[str, Any]] = []
        normal_segments: list[dict[str, Any]] = []

        async def flush_normal() -> None:
            nonlocal normal_segments
            if normal_segments:
                items.append({"kind": "message", "segments": normal_segments})
                normal_segments = []

        source_message_id = self._message_id_from_event(event)
        for index, segment in enumerate(raw_segments):
            if not isinstance(segment, dict):
                continue

            segment_type = self._string(segment.get("type")).lower()
            data = segment.get("data") if isinstance(segment.get("data"), dict) else {}

            if segment_type == "reply":
                continue
            if segment_type == "text":
                text = self._string(data.get("text"))
                if text:
                    normal_segments.append({"type": "text", "data": {"text": text}})
                continue
            if segment_type == "image":
                image_file = await self._persist_image_segment(data, index)
                if image_file:
                    normal_segments.append({"type": "image", "data": {"file": image_file}})
                continue
            if segment_type == "forward":
                await flush_normal()
                forward_id = self._forward_id_from_segment(segment)
                if forward_id:
                    item = {
                        "kind": "forward_id",
                        "id": forward_id,
                        "forward_id": forward_id,
                    }
                    if source_message_id:
                        item["source_message_id"] = source_message_id
                        item["message_id"] = source_message_id
                    items.append(item)
                continue

            copied = self._copy_segment(segment)
            if copied:
                normal_segments.append(copied)

        await flush_normal()
        return items

    def _should_send_warmup_message(self, items: list[dict[str, Any]]) -> bool:
        if not self._get_bool("forward_warmup_message_enabled", True):
            return False
        if not self._string(self._get("forward_warmup_message_text", "欢迎进群")).strip():
            return False
        for item in items:
            if not isinstance(item, dict):
                continue
            kind = self._string(item.get("kind"))
            if kind == "message" and self._normalize_content_segments(item.get("segments")):
                return True
            if kind in {"forward_id", "forward"} and self._forward_item_has_reference(item):
                return True
        return False

    async def _send_plain_warmup_message(
        self,
        bot: Any,
        user_id: str,
        self_id: str = "",
        group_id: str = "",
    ) -> None:
        raise RuntimeError("旧 API 普通文字开路链路已停用")
        text = self._string(self._get("forward_warmup_message_text", "欢迎进群")).strip()
        if not text:
            return
        try:
            await self._send_private_segments(
                bot,
                user_id,
                [{"type": "text", "data": {"text": text}}],
                group_id=group_id,
                self_id=self_id,
                allow_without_group_retry=False,
            )
            logger.info(
                "new_member_forwarder: sent plain warmup message via send_private_msg to user %s in group %s.",
                user_id,
                self._string(group_id) or "-",
            )
            delay = max(0.0, self._get_float("forward_warmup_delay_seconds", 1.0))
            if delay:
                await asyncio.sleep(delay)
        except Exception as exc:
            logger.warning(
                "new_member_forwarder: plain warmup message failed for user %s in group %s: %s",
                user_id,
                group_id,
                exc,
            )
            raise

    async def _send_plain_warmup_message_with_retries(
        self,
        bot: Any,
        user_id: str,
        self_id: str = "",
        group_id: str = "",
    ) -> None:
        raise RuntimeError("旧 API 普通文字开路链路已停用")
        retry_delays = self._get_float_list("temp_session_retry_delays_seconds", [3.0, 8.0])
        attempt = 0
        while True:
            try:
                await self._send_plain_warmup_message(bot, user_id, self_id, group_id)
                return
            except Exception as exc:
                if not self._is_friend_required_error(exc) or attempt >= len(retry_delays):
                    raise

                delay = max(0.0, retry_delays[attempt])
                attempt += 1
                logger.warning(
                    "new_member_forwarder: plain warmup message for user %s in group %s was rejected; "
                    "retry warmup %s/%s after %.1f seconds.",
                    user_id,
                    group_id,
                    attempt,
                    len(retry_delays),
                    delay,
                )
                if delay:
                    await asyncio.sleep(delay)
                await self._prepare_private_context(bot, group_id, user_id, self_id)

    async def _send_source_warmup_message(
        self,
        bot: Any,
        user_id: str,
        self_id: str = "",
        group_id: str = "",
    ) -> None:
        raise RuntimeError("旧 source node 开路链路已停用")
        text = self._string(self._get("forward_warmup_message_text", "欢迎进群")).strip()
        if not text:
            return

        source_message_id = await self._ensure_warmup_source_message(bot, text, self_id)
        if not source_message_id:
            raise RuntimeError("failed to create warmup source message")

        await self._send_source_node_forward(
            bot,
            user_id,
            source_message_id,
            self_id,
            group_id,
            source_kind="warmup_source_message_id",
        )
        logger.info(
            "new_member_forwarder: sent warmup source node via send_forward_msg to user %s in group %s.",
            user_id,
            self._string(group_id) or "-",
        )
        delay = max(0.0, self._get_float("forward_warmup_delay_seconds", 1.0))
        if delay:
            await asyncio.sleep(delay)

    async def _send_source_warmup_message_with_retries(
        self,
        bot: Any,
        user_id: str,
        self_id: str = "",
        group_id: str = "",
    ) -> None:
        raise RuntimeError("旧 source node 开路链路已停用")
        retry_delays = self._get_float_list("temp_session_retry_delays_seconds", [3.0, 8.0])
        attempt = 0
        while True:
            try:
                await self._send_source_warmup_message(bot, user_id, self_id, group_id)
                return
            except Exception as exc:
                if not self._is_friend_required_error(exc) or attempt >= len(retry_delays):
                    raise

                delay = max(0.0, retry_delays[attempt])
                attempt += 1
                logger.warning(
                    "new_member_forwarder: warmup source node for user %s in group %s was rejected; "
                    "retry warmup %s/%s after %.1f seconds.",
                    user_id,
                    group_id,
                    attempt,
                    len(retry_delays),
                    delay,
                )
                if delay:
                    await asyncio.sleep(delay)
                await self._prepare_private_context(bot, group_id, user_id, self_id)

    async def _ensure_warmup_source_message(self, bot: Any, text: str, self_id: str) -> str:
        text = self._string(text).strip()
        self_id = self._string(self_id)
        cached = self._load_warmup_source()
        cached_message_id = self._string(cached.get("message_id"))
        if (
            cached_message_id
            and self._string(cached.get("text")) == text
            and self._string(cached.get("self_id")) == self_id
        ):
            return cached_message_id

        message_id = await self._create_warmup_source_message(bot, text, self_id)
        if message_id:
            self._save_warmup_source(
                {
                    "version": 1,
                    "text": text,
                    "self_id": self_id,
                    "message_id": message_id,
                    "updated_at": int(time.time()),
                }
            )
        return message_id

    async def _create_warmup_source_message(self, bot: Any, text: str, self_id: str) -> str:
        candidates = self._warmup_source_recipients(self_id)
        last_error: Exception | None = None
        for recipient in candidates:
            try:
                result = await bot.call_action(
                    "send_private_msg",
                    user_id=int(recipient),
                    message=[{"type": "text", "data": {"text": text}}],
                    **self._routing_kwargs(self_id),
                )
                message_id = self._message_id_from_action_result(result)
                if message_id:
                    logger.info(
                        "new_member_forwarder: created warmup source message id=%s via recipient %s.",
                        message_id,
                        recipient,
                    )
                    return message_id
                logger.warning(
                    "new_member_forwarder: warmup source message sent to %s but no message_id returned: %s",
                    recipient,
                    result,
                )
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "new_member_forwarder: failed to create warmup source message via recipient %s: %s",
                    recipient,
                    exc,
                )
        if last_error:
            raise last_error
        return ""

    def _warmup_source_recipients(self, self_id: str) -> list[str]:
        candidates: list[str] = []
        for value in [
            self_id,
            self._string(self._load_recorded_payload().get("updated_by")),
            *[self._string(item) for item in self._get_list("admin_user_ids")],
        ]:
            if value and value.isdigit() and value not in candidates:
                candidates.append(value)
        return candidates

    def _message_id_from_action_result(self, result: Any) -> str:
        if isinstance(result, dict):
            for key in ("message_id", "id"):
                value = self._string(result.get(key))
                if value:
                    return value
            data = result.get("data")
            if isinstance(data, dict):
                for key in ("message_id", "id"):
                    value = self._string(data.get(key))
                    if value:
                        return value
        return self._string(getattr(result, "message_id", ""))

    def _load_warmup_source(self) -> dict[str, Any]:
        if not self.warmup_source_file.exists():
            return {}
        try:
            data = json.loads(self.warmup_source_file.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("new_member_forwarder: failed to read warmup source cache: %s", exc)
            return {}
        return data if isinstance(data, dict) else {}

    def _save_warmup_source(self, payload: dict[str, Any]) -> None:
        self.warmup_source_file.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _normalize_content_segments(self, content: Any) -> list[dict[str, Any]]:
        if isinstance(content, str):
            return [{"type": "text", "data": {"text": content}}] if content else []
        if not isinstance(content, list):
            return []

        segments: list[dict[str, Any]] = []
        for segment in content:
            if not isinstance(segment, dict):
                continue
            if segment.get("type") == "forward":
                forward_id = self._forward_id_from_segment(segment)
                if forward_id:
                    segments.append({"type": "forward", "data": {"id": forward_id}})
                continue
            copied = self._copy_segment(segment)
            if copied:
                segments.append(copied)
        return segments

    async def _deliver_private(
        self,
        bot: Any,
        user_id: str,
        items: list[dict[str, Any]],
        self_id: str = "",
        group_id: str = "",
    ) -> None:
        await self._deliver_private_with_retries(bot, user_id, items, self_id, group_id)

    async def _deliver_private_with_retries(
        self,
        bot: Any,
        user_id: str,
        items: list[dict[str, Any]],
        self_id: str = "",
        group_id: str = "",
    ) -> None:
        user_id = self._string(user_id)
        group_id = self._string(group_id)
        wait_started = time.time()
        if self._delivery_queue_lock.locked():
            logger.info(
                "new_member_forwarder: queued delivery for user %s in group %s because another delivery is running.",
                user_id,
                group_id or "-",
            )
        async with self._delivery_queue_lock:
            waited = time.time() - wait_started
            if waited >= 0.1:
                logger.info(
                    "new_member_forwarder: delivery queue turn started for user %s in group %s after %.1fs.",
                    user_id,
                    group_id or "-",
                    waited,
                )
            await self._sleep_before_next_delivery(user_id, group_id)
            try:
                await self._deliver_private_locked(bot, user_id, items, self_id, group_id)
            finally:
                self._last_delivery_finished_at = time.time()

    async def _deliver_private_locked(
        self,
        bot: Any,
        user_id: str,
        items: list[dict[str, Any]],
        self_id: str = "",
        group_id: str = "",
    ) -> None:
        gap = max(0.0, self._get_float("message_gap_seconds", 0.8))
        for item in items:
            if not isinstance(item, dict):
                continue
            kind = self._string(item.get("kind"))
            if kind in {"forward_id", "forward"} and not self._forward_item_forward_id(item):
                raise RuntimeError("录制的聊天记录缺少原始转发编号，请重新录制这条聊天记录")

        group_id = self._string(group_id)
        user_id = self._string(user_id)
        if not group_id.isdigit():
            raise RuntimeError("缺少来源群号，无法执行真人开路")
        if not user_id.isdigit():
            raise RuntimeError("目标 QQ 号无效，无法执行真人开路")

        if not await self._send_qq_human_group_warmup_message(group_id, user_id):
            result = self._qq_human_group_warmup_results.get(f"{group_id}:{user_id}") or {}
            stage = self._string(result.get("stage")) or "-"
            reason = self._string(result.get("reason")) or "真人开路未成功发送"
            raise RuntimeError(f"真人开路失败：stage={stage}，reason={reason}")

        for item in items:
            if not isinstance(item, dict):
                continue
            sent = await self._deliver_private_item(bot, user_id, item, self_id, group_id)
            if sent and gap:
                await asyncio.sleep(gap)

    async def _sleep_before_next_delivery(self, user_id: str, group_id: str) -> None:
        queue_gap = max(0.0, self._get_float("delivery_queue_gap_seconds", 1.0))
        if not queue_gap or not self._last_delivery_finished_at:
            return
        remaining = queue_gap - (time.time() - self._last_delivery_finished_at)
        if remaining <= 0:
            return
        logger.info(
            "new_member_forwarder: wait %.1fs before next queued delivery for user %s in group %s.",
            remaining,
            user_id,
            group_id or "-",
        )
        await asyncio.sleep(remaining)

    async def _deliver_private_item(
        self,
        bot: Any,
        user_id: str,
        item: dict[str, Any],
        self_id: str,
        group_id: str,
    ) -> bool:
        kind = self._string(item.get("kind"))
        if kind == "message":
            segments = self._normalize_content_segments(item.get("segments"))
            if segments:
                await self._send_private_segments(
                    bot,
                    user_id,
                    segments,
                    group_id=group_id,
                    self_id=self_id,
                )
                return True
        elif kind in {"forward_id", "forward"}:
            await self._send_recorded_forward(bot, user_id, item, self_id, group_id)
            return True
        return False

    def _forward_item_has_reference(self, item: dict[str, Any]) -> bool:
        return bool(self._forward_item_source_message_id(item) or self._forward_item_forward_id(item))

    def _forward_item_source_message_id(self, item: dict[str, Any]) -> str:
        return self._string(item.get("source_message_id") or item.get("message_id"))

    def _forward_item_forward_id(self, item: dict[str, Any]) -> str:
        return self._string(item.get("forward_id") or item.get("id"))

    async def _send_recorded_forward(
        self,
        bot: Any,
        user_id: str,
        item: dict[str, Any],
        self_id: str,
        group_id: str,
    ) -> None:
        forward_id = self._forward_item_forward_id(item)
        if not forward_id:
            raise RuntimeError("录制的聊天记录缺少原始转发编号，请重新录制这条聊天记录")
        await self._send_original_forward(bot, user_id, forward_id, self_id, group_id)
        logger.info(
            "new_member_forwarder: sent recorded original forward segment to user %s in group %s.",
            user_id,
            self._string(group_id) or "-",
        )

    async def _send_source_node_forward(
        self,
        bot: Any,
        user_id: str,
        message_id: str,
        self_id: str,
        group_id: str,
        *,
        source_kind: str,
    ) -> bool:
        raise RuntimeError("旧 source node 转发链路已停用")
        if not self._string(message_id):
            return False
        values = self._id_variants(message_id)
        last_exc: Exception | None = None
        for value in values:
            if not self._string(value):
                continue
            payload: dict[str, Any] = {
                "message_type": "private",
                "user_id": int(user_id),
                "messages": [{"type": "node", "data": {"id": value}}],
            }
            group_id = self._string(group_id)
            if group_id.isdigit():
                payload["group_id"] = int(group_id)
            try:
                logger.info(
                    "new_member_forwarder: source node send_forward_msg payload keys=%s kind=%s user=%s group=%s id=%s",
                    sorted(payload.keys()),
                    source_kind,
                    user_id,
                    group_id or "-",
                    value,
                )
                await bot.call_action("send_forward_msg", **payload)
                logger.info(
                    "new_member_forwarder: confirmed recorded forward delivery via source node kind=%s to user %s in group %s.",
                    source_kind,
                    user_id,
                    group_id or "-",
                )
                return True
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "new_member_forwarder: source node send_forward_msg failed kind=%s id=%s user=%s group=%s: %s",
                    source_kind,
                    value,
                    user_id,
                    group_id or "-",
                    exc,
                )
        if last_exc:
            raise last_exc
        return False

    def _forward_status_checked_options(self) -> dict[str, Any]:
        if not self._get_bool("llbot_status_checked_forward", True):
            return {}
        options = {
            "source": self._string(self._get("forward_card_source", "聊天记录")).strip(),
            "summary": self._string(self._get("forward_card_summary", "查看转发消息")).strip(),
            "prompt": self._string(self._get("forward_card_prompt", "[聊天记录]")).strip(),
        }
        return {key: value for key, value in options.items() if value}

    async def _send_original_forward(
        self,
        bot: Any,
        user_id: str,
        forward_id: str,
        self_id: str,
        group_id: str,
    ) -> None:
        payload: dict[str, Any] = {
            "user_id": int(user_id),
            "message": [{"type": "forward", "data": {"id": forward_id}}],
        }
        payload.update(self._routing_kwargs(self_id))
        group_id = self._string(group_id)
        await self._call_private_action(
            bot,
            "send_private_msg",
            target_user_id=user_id,
            group_id=group_id,
            **payload,
        )

    async def _wait_private_context_ready(
        self,
        bot: Any,
        group_id: str,
        user_id: str,
        self_id: str = "",
    ) -> None:
        raise TempSessionNotReadyError("旧临时会话准备链路已停用")
        retry_delays = self._get_float_list("temp_session_retry_delays_seconds", [3.0, 8.0])
        for attempt in range(len(retry_delays) + 1):
            if await self._prepare_private_context(bot, group_id, user_id, self_id):
                return
            if attempt >= len(retry_delays):
                raise TempSessionNotReadyError(
                    f"LLBot has not recognized QQ {user_id} as a member of group {group_id}"
                )
            delay = max(0.0, retry_delays[attempt])
            logger.warning(
                "new_member_forwarder: user %s is not visible in LLBot group member list for group %s; "
                "retry prepare %s/%s after %.1f seconds.",
                user_id,
                group_id,
                attempt + 1,
                len(retry_delays),
                delay,
            )
            if delay:
                await asyncio.sleep(delay)

    async def _prepare_private_context(
        self,
        bot: Any,
        group_id: str,
        user_id: str,
        self_id: str = "",
    ) -> bool:
        return False
        if not self._get_bool("prepare_temp_session_before_send", True):
            return True
        group_id = self._string(group_id)
        user_id = self._string(user_id)
        if not group_id.isdigit() or not user_id.isdigit():
            return True
        human_enabled = self._get_bool("qq_human_group_warmup_enabled", False)
        member_info: Any = None
        try:
            member_info = await bot.call_action(
                "get_group_member_info",
                group_id=int(group_id),
                user_id=int(user_id),
                no_cache=True,
                **self._routing_kwargs(self_id),
            )
        except Exception as exc:
            logger.warning(
                "new_member_forwarder: failed to prepare temporary private context for user %s in group %s: %s",
                user_id,
                group_id,
                exc,
            )
            if not human_enabled:
                return False

        if not human_enabled:
            list_status = await self._check_group_member_list(bot, group_id, user_id, self_id)
            if list_status is False:
                return False
        else:
            list_status = await self._check_group_member_list(bot, group_id, user_id, self_id)
            if list_status is False:
                logger.info(
                    "new_member_forwarder: LLBot group member list does not contain user %s in group %s, "
                    "but human QQ member-search warmup is enabled; continue with QQ desktop search.",
                    user_id,
                    group_id,
                )
        member_name = self._member_display_name(member_info)
        group_name = await self._get_group_display_name(bot, group_id, self_id)
        human_sent = await self._send_qq_human_group_warmup_message(
            group_id,
            user_id,
            member_name,
            group_name,
        )
        if human_sent:
            return True
        if human_enabled and self._get_bool("qq_human_group_warmup_required", True):
            return False
        llbot_activated = await self._activate_llbot_temp_context(bot, group_id, user_id)
        opened = await self._open_qq_profile_context(group_id, user_id)
        if llbot_activated or opened:
            await self._send_qq_desktop_warmup_message(
                group_id,
                user_id,
                member_name,
                group_name,
            )
            return True
        return True

    async def _open_qq_profile_context(self, group_id: str, user_id: str) -> bool:
        return False
        if not self._get_bool("qq_protocol_profile_warmup_enabled", False):
            return False
        group_id = self._string(group_id)
        user_id = self._string(user_id)
        if not group_id.isdigit() or not user_id.isdigit():
            return False

        now = time.time()
        key = f"{group_id}:{user_id}"
        cooldown = max(0.0, self._get_float("qq_protocol_profile_warmup_cooldown_seconds", 30.0))
        last_at = self._qq_protocol_warmup_last_at.get(key, 0.0)
        if cooldown and now - last_at < cooldown:
            return False
        self._qq_protocol_warmup_last_at[key] = now

        urls = self._qq_protocol_profile_warmup_urls(group_id, user_id)
        if not urls:
            return False

        opened = False
        for url in urls:
            try:
                await asyncio.to_thread(os.startfile, url)  # type: ignore[attr-defined]
                opened = True
                logger.info(
                    "new_member_forwarder: opened QQ profile/chat protocol for user %s in group %s: %s",
                    user_id,
                    group_id,
                    url,
                )
                gap = max(0.0, self._get_float("qq_protocol_profile_warmup_url_gap_seconds", 0.4))
                if gap:
                    await asyncio.sleep(gap)
            except Exception as exc:
                logger.warning(
                    "new_member_forwarder: failed to open QQ profile/chat protocol for user %s in group %s: %s",
                    user_id,
                    group_id,
                    self._short_error(exc),
                )

        delay = max(0.0, self._get_float("qq_protocol_profile_warmup_delay_seconds", 2.0))
        if opened and delay:
            await asyncio.sleep(delay)
        return opened

    async def _send_qq_human_group_warmup_message(
        self,
        group_id: str,
        user_id: str,
        target_name: str = "",
        group_name: str = "",
        *,
        force: bool = False,
    ) -> bool:
        if not force and not self._get_bool("qq_human_group_warmup_enabled", False):
            return False
        text = self._string(self._get("forward_warmup_message_text", "欢迎进群")).strip()
        if not text:
            return False
        group_id = self._string(group_id)
        user_id = self._string(user_id)
        if not group_id.isdigit() or not user_id.isdigit():
            return False

        now = time.time()
        key = f"{group_id}:{user_id}"
        cooldown = max(0.0, self._get_float("qq_human_group_warmup_cooldown_seconds", 90.0))
        last_at = self._qq_human_group_warmup_last_at.get(key, 0.0)
        if not force and cooldown and now - last_at < cooldown:
            if self._recent_desktop_warmup_sent(group_id, user_id):
                return True
            last_result = self._qq_human_group_warmup_results.get(key) or {}
            if not (isinstance(last_result, dict) and last_result.get("ok") is False):
                return False
        self._qq_human_group_warmup_last_at[key] = now

        timeout = max(8.0, self._get_float("qq_human_group_warmup_timeout_seconds", 45.0))
        try:
            result = await asyncio.to_thread(
                self._run_qq_human_group_warmup_script,
                group_id,
                user_id,
                target_name,
                group_name,
                text,
                timeout,
            )
        except subprocess.TimeoutExpired as exc:
            self._qq_human_group_warmup_results[key] = {
                "ok": False,
                "stage": "timeout",
                "reason": f"powershell_timeout_after_{timeout:.1f}s",
            }
            logger.warning(
                "new_member_forwarder: human QQ group warmup timed out for user %s in group %s after %.1fs: %s",
                user_id,
                group_id,
                timeout,
                self._short_error(exc),
            )
            return False
        except Exception as exc:
            self._qq_human_group_warmup_results[key] = {
                "ok": False,
                "stage": "python",
                "reason": self._short_error(exc),
            }
            logger.warning(
                "new_member_forwarder: human QQ group warmup failed for user %s in group %s: %s",
                user_id,
                group_id,
                self._short_error(exc),
            )
            return False

        self._qq_human_group_warmup_results[key] = result if isinstance(result, dict) else {"ok": False, "reason": result}
        if result.get("ok"):
            self._qq_desktop_warmup_sent_at[key] = time.time()
            logger.info(
                "new_member_forwarder: sent human QQ group warmup for user %s in group %s: %s",
                user_id,
                group_id,
                result.get("reason") or "ok",
            )
            post_delay = max(0.0, self._get_float("qq_human_group_warmup_post_send_delay_seconds", 1.5))
            if post_delay:
                await asyncio.sleep(post_delay)
            return True

        logger.warning(
            "new_member_forwarder: human QQ group warmup did not send for user %s in group %s: %s",
            user_id,
            group_id,
            result.get("reason") or result,
        )
        return False

    async def _send_qq_human_group_warmup_message_queued(
        self,
        group_id: str,
        user_id: str,
        target_name: str = "",
        group_name: str = "",
        *,
        force: bool = False,
    ) -> bool:
        group_id = self._string(group_id)
        user_id = self._string(user_id)
        wait_started = time.time()
        if self._delivery_queue_lock.locked():
            logger.info(
                "new_member_forwarder: queued human warmup test for user %s in group %s because another delivery is running.",
                user_id,
                group_id or "-",
            )
        async with self._delivery_queue_lock:
            waited = time.time() - wait_started
            if waited >= 0.1:
                logger.info(
                    "new_member_forwarder: human warmup test queue turn started for user %s in group %s after %.1fs.",
                    user_id,
                    group_id or "-",
                    waited,
                )
            await self._sleep_before_next_delivery(user_id, group_id)
            try:
                return await self._send_qq_human_group_warmup_message(
                    group_id,
                    user_id,
                    target_name,
                    group_name,
                    force=force,
                )
            finally:
                self._last_delivery_finished_at = time.time()

    async def _send_qq_desktop_warmup_message(
        self,
        group_id: str,
        user_id: str,
        target_name: str = "",
        group_name: str = "",
    ) -> bool:
        return False
        if not self._get_bool("qq_desktop_warmup_enabled", False):
            return False
        text = self._string(self._get("forward_warmup_message_text", "欢迎进群")).strip()
        if not text:
            return False
        group_id = self._string(group_id)
        user_id = self._string(user_id)
        if not group_id.isdigit() or not user_id.isdigit():
            return False

        now = time.time()
        key = f"{group_id}:{user_id}"
        cooldown = max(0.0, self._get_float("qq_desktop_warmup_cooldown_seconds", 60.0))
        last_at = self._qq_desktop_warmup_last_at.get(key, 0.0)
        if cooldown and now - last_at < cooldown:
            return self._recent_desktop_warmup_sent(group_id, user_id)
        self._qq_desktop_warmup_last_at[key] = now

        timeout = max(3.0, self._get_float("qq_desktop_warmup_timeout_seconds", 18.0))
        try:
            result = await asyncio.to_thread(
                self._run_qq_desktop_warmup_script,
                group_id,
                user_id,
                target_name,
                group_name,
                text,
                timeout,
            )
        except Exception as exc:
            logger.warning(
                "new_member_forwarder: QQ desktop warmup failed for user %s in group %s: %s",
                user_id,
                group_id,
                self._short_error(exc),
            )
            return False

        if result.get("ok"):
            self._qq_desktop_warmup_sent_at[key] = time.time()
            logger.info(
                "new_member_forwarder: sent QQ desktop warmup for user %s in group %s: %s",
                user_id,
                group_id,
                result.get("reason") or "ok",
            )
            post_delay = max(0.0, self._get_float("qq_desktop_warmup_post_send_delay_seconds", 1.2))
            if post_delay:
                await asyncio.sleep(post_delay)
            return True

        logger.warning(
            "new_member_forwarder: QQ desktop warmup did not send for user %s in group %s: %s",
            user_id,
            group_id,
            result.get("reason") or result,
        )
        return False

    def _recent_desktop_warmup_sent(self, group_id: str, user_id: str) -> bool:
        group_id = self._string(group_id)
        user_id = self._string(user_id)
        if not group_id or not user_id:
            return False
        key = f"{group_id}:{user_id}"
        last_at = self._qq_desktop_warmup_sent_at.get(key, 0.0)
        window = max(3.0, self._get_float("qq_desktop_warmup_sent_window_seconds", 180.0))
        return bool(last_at and time.time() - last_at <= window)

    def _run_powershell_sta_script_file(
        self,
        script: str,
        env: dict[str, str],
        timeout: float,
        script_name: str,
    ) -> subprocess.CompletedProcess[str]:
        runtime_dir = self.data_dir / "runtime_scripts"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        script_path = runtime_dir / script_name
        script_path.write_text(script, encoding="utf-8-sig")
        command = [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-STA",
            "-File",
            str(script_path),
        ]
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    def _verified_qq_human_stable_script_path(self) -> Path | None:
        candidates = [
            self.plugin_dir / "qq_human_send_stable.ps1",
            Path("D:/")
            / "\u521b\u5efa\u6587\u4ef6"
            / "QQ\u7a97\u53e3\u81ea\u52a8\u5316\u6d4b\u8bd5"
            / "qq_human_send_stable.ps1",
        ]
        for path in candidates:
            try:
                if path.exists() and path.is_file():
                    return path
            except Exception:
                continue
        return None

    def _qq_human_debug_base(self) -> Path:
        default = (
            Path("D:/")
            / "\u521b\u5efa\u6587\u4ef6"
            / "QQ\u7a97\u53e3\u81ea\u52a8\u5316\u6d4b\u8bd5"
        )
        value = self._string(self._get("qq_human_group_warmup_debug_dir", str(default))).strip()
        return Path(value) if value else default

    def _json_from_powershell_stdout(self, stdout: str) -> dict[str, Any] | None:
        text = (stdout or "").strip()
        if not text:
            return None
        candidates = [text]
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            candidates.append(text[start : end + 1])
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if lines:
            candidates.append(lines[-1])
        for candidate in candidates:
            try:
                data = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                return data
        return None

    def _run_verified_qq_human_stable_script(
        self,
        script_path: Path,
        group_id: str,
        user_id: str,
        text: str,
        timeout: float,
    ) -> dict[str, Any]:
        runtime_dir = self.data_dir / "runtime_scripts"
        trace_path = runtime_dir / f"human_stable_warmup_{group_id}_{user_id}_{int(time.time() * 1000)}.trace.log"
        out_dir = self._qq_human_debug_base() / f"plugin-{time.strftime('%Y%m%d-%H%M%S')}-{group_id}-{user_id}"
        try:
            runtime_dir.mkdir(parents=True, exist_ok=True)
            out_dir.mkdir(parents=True, exist_ok=True)
            trace_path.write_text(f"external_script={script_path}\n", encoding="utf-8")
        except Exception:
            pass

        command = [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-STA",
            "-File",
            str(script_path),
            "-Mode",
            "send",
            "-GroupRow",
            str(max(1, int(self._get_float("qq_human_group_warmup_group_row", 1)))),
            "-GroupBaseY",
            str(max(1, int(self._get_float("qq_human_group_warmup_group_base_y", 150)))),
            "-SearchResultBaseY",
            str(max(1, int(self._get_float("qq_human_group_warmup_search_result_base_y", 322)))),
            "-TargetQQ",
            user_id,
            "-Message",
            text,
            "-WaitSeconds",
            str(max(3, int(self._get_float("qq_human_group_warmup_wait_seconds", 20.0)))),
            "-OutDir",
            str(out_dir),
            "-TraceFile",
            str(trace_path),
        ]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            stage = self._read_runtime_trace_tail(trace_path) or "external_timeout"
            return {
                "ok": False,
                "stage": stage,
                "reason": f"powershell_timeout_after_{timeout:.1f}s",
                "outDir": str(out_dir),
                "script": str(script_path),
            }

        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        result_path = out_dir / "result.json"
        data: dict[str, Any] | None = None
        if result_path.exists():
            try:
                parsed = json.loads(result_path.read_text(encoding="utf-8-sig"))
                if isinstance(parsed, dict):
                    data = parsed
            except Exception:
                data = None
        if data is None:
            data = self._json_from_powershell_stdout(stdout)
        if data is not None:
            sent = bool(data.get("sent") or data.get("ok"))
            return {
                "ok": sent,
                "stage": "sent" if sent else self._string(data.get("stage") or data.get("mode") or "not_sent"),
                "reason": "sent" if sent else self._short_error(RuntimeError(stderr or stdout or "not sent"), 300),
                "outDir": self._string(data.get("outDir")) or str(out_dir),
                "script": str(script_path),
                "steps": data.get("steps"),
                "shots": data.get("shots"),
                "profileOcr": data.get("profileOcr"),
                "returncode": completed.returncode,
            }

        return {
            "ok": False,
            "stage": "powershell",
            "reason": self._short_error(
                RuntimeError(stderr or stdout or f"powershell exited with {completed.returncode}"),
                300,
            ),
            "outDir": str(out_dir),
            "script": str(script_path),
            "returncode": completed.returncode,
        }

    def _run_qq_human_group_warmup_stable_script(
        self,
        group_id: str,
        user_id: str,
        target_name: str,
        group_name: str,
        text: str,
        timeout: float,
    ) -> dict[str, Any]:
        verified_script = self._verified_qq_human_stable_script_path()
        if verified_script is not None:
            return self._run_verified_qq_human_stable_script(verified_script, group_id, user_id, text, timeout)

        script = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.Windows.Forms

Add-Type -TypeDefinition @"
using System;
using System.Text;
using System.Runtime.InteropServices;
public class NmfStableHuman {
  public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left; public int Top; public int Right; public int Bottom; }
  [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc lpEnumFunc, IntPtr lParam);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool ShowWindowAsync(IntPtr hWnd, int nCmdShow);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool BringWindowToTop(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr hWnd, IntPtr hWndInsertAfter, int X, int Y, int cx, int cy, uint uFlags);
  [DllImport("user32.dll")] public static extern bool PostMessage(IntPtr hWnd, uint Msg, UIntPtr wParam, UIntPtr lParam);
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowText(IntPtr hWnd, StringBuilder text, int count);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetClassName(IntPtr hWnd, StringBuilder text, int count);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint processId);
  [DllImport("dwmapi.dll")] public static extern int DwmGetWindowAttribute(IntPtr hwnd, int attr, out RECT pvAttribute, int cbAttribute);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT rect);
  [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
  [DllImport("user32.dll")] public static extern void mouse_event(uint flags, uint dx, uint dy, uint data, UIntPtr extra);
  [DllImport("user32.dll")] public static extern void keybd_event(byte vk, byte scan, uint flags, UIntPtr extra);
}
"@

[NmfStableHuman]::SetProcessDPIAware() | Out-Null

$script:Stage = 'startup'
$script:Shots = New-Object System.Collections.ArrayList
$script:ProfileTitle = -join ([char[]](0x8d44, 0x6599, 0x5361))
$TargetQQ = ($env:NMF_TARGET_USER_ID + '').Trim()
$MessageText = $env:NMF_WARMUP_TEXT
$OutDir = $env:NMF_OUT_DIR
if (-not $OutDir) {
  $OutDir = Join-Path $env:TEMP ('nmf-human-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
}
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

function Set-Stage([string]$stage) {
  $script:Stage = $stage
  try {
    if ($env:NMF_TRACE_FILE) {
      Add-Content -LiteralPath $env:NMF_TRACE_FILE -Value $stage -Encoding UTF8
    }
  } catch {}
}

function Out-Result([bool]$ok, [string]$reason, [string]$stage, $extra) {
  $result = [ordered]@{
    ok = $ok
    reason = $reason
    stage = $stage
    outDir = $OutDir
    shots = @($script:Shots)
  }
  if ($extra -ne $null) {
    foreach ($key in $extra.Keys) {
      $result[$key] = $extra[$key]
    }
  }
  $result | ConvertTo-Json -Depth 8 -Compress
}

function Get-IntEnv([string]$name, [int]$defaultValue) {
  try {
    $value = [int]([Environment]::GetEnvironmentVariable($name))
    if ($value -gt 0) { return $value }
  } catch {}
  return $defaultValue
}

function Get-FloatEnv([string]$name, [double]$defaultValue) {
  try {
    $value = [double]([Environment]::GetEnvironmentVariable($name))
    if ($value -gt 0) { return $value }
  } catch {}
  return $defaultValue
}

function Get-Text([IntPtr]$Hwnd) {
  $sb = New-Object System.Text.StringBuilder 512
  [NmfStableHuman]::GetWindowText($Hwnd, $sb, $sb.Capacity) | Out-Null
  $sb.ToString()
}

function Get-Class([IntPtr]$Hwnd) {
  $sb = New-Object System.Text.StringBuilder 256
  [NmfStableHuman]::GetClassName($Hwnd, $sb, $sb.Capacity) | Out-Null
  $sb.ToString()
}

function Get-Frame([IntPtr]$Hwnd) {
  $r = New-Object NmfStableHuman+RECT
  $hr = [NmfStableHuman]::DwmGetWindowAttribute($Hwnd, 9, [ref]$r, [Runtime.InteropServices.Marshal]::SizeOf([type][NmfStableHuman+RECT]))
  if ($hr -ne 0 -or $r.Right -le $r.Left -or $r.Bottom -le $r.Top) {
    [NmfStableHuman]::GetWindowRect($Hwnd, [ref]$r) | Out-Null
  }
  [pscustomobject]@{
    Left = [int]$r.Left
    Top = [int]$r.Top
    Right = [int]$r.Right
    Bottom = [int]$r.Bottom
    Width = [int]($r.Right - $r.Left)
    Height = [int]($r.Bottom - $r.Top)
  }
}

function Find-QQWindows {
  $qqPids = @(Get-Process QQ -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Id)
  $rows = New-Object System.Collections.ArrayList
  $cb = [NmfStableHuman+EnumWindowsProc]{
    param([IntPtr]$hWnd, [IntPtr]$lParam)
    $procId = [uint32]0
    [NmfStableHuman]::GetWindowThreadProcessId($hWnd, [ref]$procId) | Out-Null
    if ($qqPids -contains [int]$procId) {
      $frame = Get-Frame $hWnd
      $className = Get-Class $hWnd
      $title = Get-Text $hWnd
      if ($className -eq 'Chrome_WidgetWin_1' -and $frame.Width -gt 20 -and $frame.Height -gt 20) {
        [void]$rows.Add([pscustomobject]@{
          Handle = $hWnd
          HandleValue = $hWnd.ToInt64()
          Title = $title
          Class = $className
          Visible = [NmfStableHuman]::IsWindowVisible($hWnd)
          Left = $frame.Left
          Top = $frame.Top
          Width = $frame.Width
          Height = $frame.Height
          Area = $frame.Width * $frame.Height
        })
      }
    }
    return $true
  }
  [NmfStableHuman]::EnumWindows($cb, [IntPtr]::Zero) | Out-Null
  @($rows | Sort-Object Area -Descending)
}

function Get-MainQQWindow {
  $windows = Find-QQWindows
  $main = $windows | Where-Object { $_.Title -eq 'QQ' -and $_.Width -ge 800 -and $_.Height -ge 600 } | Select-Object -First 1
  if (-not $main) {
    $main = $windows | Where-Object { $_.Width -ge 800 -and $_.Height -ge 600 } | Select-Object -First 1
  }
  if (-not $main) {
    throw 'main QQ window not found'
  }
  $main
}

function Focus-Maximized([IntPtr]$Hwnd) {
  [NmfStableHuman]::ShowWindowAsync($Hwnd, 3) | Out-Null
  Start-Sleep -Milliseconds 350
  [NmfStableHuman]::SetWindowPos($Hwnd, [IntPtr](-1), 0, 0, 0, 0, 0x0001 -bor 0x0002 -bor 0x0040) | Out-Null
  Start-Sleep -Milliseconds 80
  [NmfStableHuman]::SetWindowPos($Hwnd, [IntPtr](-2), 0, 0, 0, 0, 0x0001 -bor 0x0002 -bor 0x0040) | Out-Null
  [NmfStableHuman]::BringWindowToTop($Hwnd) | Out-Null
  [NmfStableHuman]::SetForegroundWindow($Hwnd) | Out-Null
  Start-Sleep -Milliseconds 450
}

function Click-At([int]$X, [int]$Y) {
  [NmfStableHuman]::SetCursorPos($X, $Y) | Out-Null
  Start-Sleep -Milliseconds 100
  [NmfStableHuman]::mouse_event(2, 0, 0, 0, [UIntPtr]::Zero)
  Start-Sleep -Milliseconds 70
  [NmfStableHuman]::mouse_event(4, 0, 0, 0, [UIntPtr]::Zero)
  Start-Sleep -Milliseconds 350
}

function DoubleClick-At([int]$X, [int]$Y) {
  [NmfStableHuman]::SetCursorPos($X, $Y) | Out-Null
  Start-Sleep -Milliseconds 100
  for ($i = 0; $i -lt 2; $i++) {
    [NmfStableHuman]::mouse_event(2, 0, 0, 0, [UIntPtr]::Zero)
    Start-Sleep -Milliseconds 45
    [NmfStableHuman]::mouse_event(4, 0, 0, 0, [UIntPtr]::Zero)
    Start-Sleep -Milliseconds 90
  }
  Start-Sleep -Milliseconds 500
}

function Press-Key([byte]$vk) {
  [NmfStableHuman]::keybd_event($vk, 0, 0, [UIntPtr]::Zero)
  Start-Sleep -Milliseconds 55
  [NmfStableHuman]::keybd_event($vk, 0, 2, [UIntPtr]::Zero)
  Start-Sleep -Milliseconds 160
}

function Press-CtrlA-Backspace {
  [NmfStableHuman]::keybd_event(0x11, 0, 0, [UIntPtr]::Zero)
  [NmfStableHuman]::keybd_event(0x41, 0, 0, [UIntPtr]::Zero)
  [NmfStableHuman]::keybd_event(0x41, 0, 2, [UIntPtr]::Zero)
  [NmfStableHuman]::keybd_event(0x11, 0, 2, [UIntPtr]::Zero)
  Start-Sleep -Milliseconds 80
  Press-Key 0x08
}

function Paste-Text([string]$Text) {
  [System.Windows.Forms.Clipboard]::SetText($Text)
  Start-Sleep -Milliseconds 120
  [NmfStableHuman]::keybd_event(0x11, 0, 0, [UIntPtr]::Zero)
  [NmfStableHuman]::keybd_event(0x56, 0, 0, [UIntPtr]::Zero)
  [NmfStableHuman]::keybd_event(0x56, 0, 2, [UIntPtr]::Zero)
  [NmfStableHuman]::keybd_event(0x11, 0, 2, [UIntPtr]::Zero)
  Start-Sleep -Milliseconds 300
}

function Save-Shot($Frame, [string]$Name) {
  $path = Join-Path $OutDir $Name
  $bmp = New-Object System.Drawing.Bitmap $Frame.Width, $Frame.Height
  $g = [System.Drawing.Graphics]::FromImage($bmp)
  $g.CopyFromScreen($Frame.Left, $Frame.Top, 0, 0, [System.Drawing.Size]::new($Frame.Width, $Frame.Height))
  $bmp.Save($path, [System.Drawing.Imaging.ImageFormat]::Png)
  $g.Dispose()
  $bmp.Dispose()
  [void]$script:Shots.Add($path)
  $path
}

function Get-GroupPanelScore($Frame) {
  $lineHeight = [Math]::Min(520, [Math]::Max(160, $Frame.Height - 250))
  $x = $Frame.Left + $Frame.Width - 275
  $y = $Frame.Top + 135
  $bmp = New-Object System.Drawing.Bitmap 1, $lineHeight
  $g = [System.Drawing.Graphics]::FromImage($bmp)
  $g.CopyFromScreen($x, $y, 0, 0, [System.Drawing.Size]::new(1, $lineHeight))
  $g.Dispose()
  $score = 0
  for ($i = 0; $i -lt $lineHeight; $i++) {
    $c = $bmp.GetPixel(0, $i)
    $b = ($c.R + $c.G + $c.B) / 3.0
    if ($b -ge 42 -and $b -le 70) { $score += 1 }
  }
  $bmp.Dispose()
  [pscustomobject]@{ Score = $score; Height = $lineHeight; Ratio = $score / [double]$lineHeight }
}

function Wait-ForGroupPanel($Frame, [int]$Seconds) {
  $deadline = (Get-Date).AddSeconds($Seconds)
  $last = $null
  while ((Get-Date) -lt $deadline) {
    $last = Get-GroupPanelScore $Frame
    if ($last.Ratio -gt 0.55) { return $last }
    Start-Sleep -Milliseconds 500
  }
  throw ('group member panel was not detected; score=' + ($last | ConvertTo-Json -Compress))
}

function Assert-PrivateChat($Frame) {
  $score = Get-GroupPanelScore $Frame
  if ($score.Ratio -gt 0.12) {
    throw ('private chat guard refused to send; group panel still visible; score=' + ($score | ConvertTo-Json -Compress))
  }
  $score
}

function Close-ProfilePopups {
  foreach ($popup in @(Find-QQWindows | Where-Object { $_.Title -eq $script:ProfileTitle })) {
    [NmfStableHuman]::PostMessage($popup.Handle, 0x0010, [UIntPtr]::Zero, [UIntPtr]::Zero) | Out-Null
  }
  Start-Sleep -Milliseconds 400
}

function Wait-ForProfile([int]$Seconds) {
  $deadline = (Get-Date).AddSeconds($Seconds)
  while ((Get-Date) -lt $deadline) {
    $profile = Find-QQWindows | Where-Object {
      $_.Title -eq $script:ProfileTitle -and $_.Visible -and $_.Left -ge 0 -and $_.Top -ge 0 -and $_.Width -gt 300 -and $_.Height -gt 300
    } | Select-Object -First 1
    if ($profile) { return $profile }
    Start-Sleep -Milliseconds 500
  }
  throw 'profile card was not detected'
}

function Try-WaitForProfile([int]$Seconds) {
  try { return Wait-ForProfile $Seconds } catch { return $null }
}

function Open-SearchResultProfile($MainFrame, [int]$BaseY) {
  $rowY = $MainFrame.Top + $BaseY
  $avatarX = $MainFrame.Left + $MainFrame.Width - 240
  $nameX = $MainFrame.Left + $MainFrame.Width - 185
  Click-At $avatarX $rowY
  $profile = Try-WaitForProfile 2
  if ($profile) { return $profile }
  Click-At $nameX $rowY
  $profile = Try-WaitForProfile 2
  if ($profile) { return $profile }
  DoubleClick-At $avatarX $rowY
  $profile = Try-WaitForProfile 2
  if ($profile) { return $profile }
  DoubleClick-At $nameX $rowY
  $profile = Try-WaitForProfile 2
  if ($profile) { return $profile }
  return $null
}

function Read-ImageText([string]$Path) {
  Add-Type -AssemblyName System.Runtime.WindowsRuntime
  $null = [Windows.Storage.StorageFile, Windows.Storage, ContentType=WindowsRuntime]
  $null = [Windows.Storage.FileAccessMode, Windows.Storage, ContentType=WindowsRuntime]
  $null = [Windows.Storage.Streams.IRandomAccessStream, Windows.Storage.Streams, ContentType=WindowsRuntime]
  $null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType=WindowsRuntime]
  $null = [Windows.Graphics.Imaging.SoftwareBitmap, Windows.Graphics.Imaging, ContentType=WindowsRuntime]
  $null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType=WindowsRuntime]
  $null = [Windows.Media.Ocr.OcrResult, Windows.Foundation, ContentType=WindowsRuntime]

  function Await-WinRtOperation($Operation, [type]$ResultType) {
    $method = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
      $_.Name -eq 'AsTask' -and
      $_.IsGenericMethod -and
      $_.GetParameters().Count -eq 1 -and
      $_.GetGenericArguments().Count -eq 1 -and
      $_.ToString().StartsWith('System.Threading.Tasks.Task`1')
    } | Select-Object -First 1
    $generic = $method.MakeGenericMethod($ResultType)
    $task = $generic.Invoke($null, @($Operation))
    $task.GetAwaiter().GetResult()
  }

  $file = Await-WinRtOperation ([Windows.Storage.StorageFile]::GetFileFromPathAsync($Path)) ([Windows.Storage.StorageFile])
  $stream = Await-WinRtOperation ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
  $decoder = Await-WinRtOperation ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
  $bitmap = Await-WinRtOperation ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
  $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
  if (-not $engine) { throw 'Windows OCR engine is not available' }
  $result = Await-WinRtOperation ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
  $result.Text
}

function Normalize-OcrToken([string]$token) {
  $normalized = ($token + '').ToUpperInvariant()
  $normalized = $normalized.Replace('O', '0').Replace('Q', '0')
  $normalized = $normalized.Replace('I', '1').Replace('L', '1')
  $normalized = $normalized.Replace('S', '5')
  $normalized = $normalized.Replace('B', '8')
  $normalized = $normalized.Replace('Z', '2')
  return $normalized
}

function Assert-ProfileQQ([string]$ShotPath, [string]$ExpectedQQ) {
  $ocrText = Read-ImageText $ShotPath
  $digits = ($ocrText -replace '[^0-9]', ' ')
  $parts = @($digits -split '\s+' | Where-Object { $_ })
  $tokens = @($ocrText -split '[^0-9A-Za-z]+' | Where-Object { $_ })
  $normalizedTokens = @()
  foreach ($token in $tokens) {
    $normalized = Normalize-OcrToken $token
    if ($normalized -match '^[0-9]+$') { $normalizedTokens += $normalized }
  }
  if (($parts -notcontains $ExpectedQQ) -and ($normalizedTokens -notcontains $ExpectedQQ)) {
    throw ('profile QQ mismatch; expected=' + $ExpectedQQ + '; ocr=' + $ocrText)
  }
  $ocrText
}

try {
  if (-not $TargetQQ -or $TargetQQ -notmatch '^[0-9]+$') {
    throw 'target QQ is empty or invalid'
  }
  if (-not $MessageText) {
    throw 'warmup message is empty'
  }

  $groupRow = Get-IntEnv 'NMF_GROUP_ROW' 1
  $groupBaseY = Get-IntEnv 'NMF_GROUP_BASE_Y' 150
  $searchResultBaseY = Get-IntEnv 'NMF_SEARCH_RESULT_BASE_Y' 322
  $waitSeconds = [int](Get-FloatEnv 'NMF_WAIT_SECONDS' 20.0)

  Set-Stage 'close_profile_popups'
  Close-ProfilePopups
  Set-Stage 'focus_main'
  $main = Get-MainQQWindow
  Focus-Maximized $main.Handle
  Press-Key 0x1B
  Press-Key 0x1B
  $main = Get-MainQQWindow
  Save-Shot $main '01-maximized-start.png' | Out-Null

  Set-Stage 'click_pinned_group'
  $groupX = $main.Left + [int]([Math]::Min(360, [Math]::Max(220, $main.Width * 0.16)))
  $groupY = $main.Top + $groupBaseY + (($groupRow - 1) * 95)
  Click-At $groupX $groupY
  Start-Sleep -Milliseconds 900
  $main = Get-MainQQWindow
  Save-Shot $main '02-after-group-click.png' | Out-Null

  Set-Stage 'wait_group_panel'
  $groupScore = Wait-ForGroupPanel $main $waitSeconds

  Set-Stage 'member_search'
  $searchIconX = $main.Left + $main.Width - 31
  $searchIconY = $main.Top + 264
  Click-At $searchIconX $searchIconY
  Start-Sleep -Milliseconds 500
  Press-CtrlA-Backspace
  Paste-Text $TargetQQ
  Start-Sleep -Seconds 1
  Save-Shot $main '03-member-search-result.png' | Out-Null

  Set-Stage 'open_profile'
  $profile = Open-SearchResultProfile $main $searchResultBaseY
  if (-not $profile) {
    Save-Shot $main '03b-after-result-clicks.png' | Out-Null
    $profile = Wait-ForProfile $waitSeconds
  }
  $profileShot = Save-Shot $profile '04-profile-card.png'

  Set-Stage 'ocr_profile'
  $profileOcr = Assert-ProfileQQ $profileShot $TargetQQ

  Set-Stage 'open_private_chat'
  $sendX = $profile.Left + [int]($profile.Width * 0.73)
  $sendY = $profile.Top + $profile.Height - 62
  Click-At $sendX $sendY
  Start-Sleep -Seconds 1
  $main = Get-MainQQWindow
  Focus-Maximized $main.Handle
  $main = Get-MainQQWindow
  Save-Shot $main '05-private-chat-before-send.png' | Out-Null

  Set-Stage 'private_guard'
  $privateScore = Assert-PrivateChat $main

  Set-Stage 'send_message'
  $privateEditorX = $main.Left + [int]($main.Width * 0.40)
  $privateEditorY = $main.Top + $main.Height - 235
  Click-At $privateEditorX $privateEditorY
  Press-CtrlA-Backspace
  Paste-Text $MessageText
  Press-Key 0x0D
  Start-Sleep -Seconds 1
  Save-Shot $main '06-private-chat-after-send.png' | Out-Null

  Out-Result $true 'sent' 'sent' @{
    targetQQ = $TargetQQ
    profileOcr = $profileOcr
    groupPanelRatio = $groupScore.Ratio
    privateGuardRatio = $privateScore.Ratio
    foreground = [NmfStableHuman]::GetForegroundWindow().ToInt64()
  }
} catch {
  Out-Result $false ($_.Exception.Message) $script:Stage @{
    targetQQ = $TargetQQ
  }
}
"""
        runtime_dir = self.data_dir / "runtime_scripts"
        trace_path = runtime_dir / f"human_stable_warmup_{group_id}_{user_id}_{int(time.time() * 1000)}.trace.log"
        debug_base = Path(
            self._string(
                self._get(
                    "qq_human_group_warmup_debug_dir",
                    r"D:\创建文件\QQ窗口自动化测试",
                )
            )
            or r"D:\创建文件\QQ窗口自动化测试"
        )
        out_dir = debug_base / f"plugin-{time.strftime('%Y%m%d-%H%M%S')}-{group_id}-{user_id}"
        try:
            runtime_dir.mkdir(parents=True, exist_ok=True)
            trace_path.write_text("", encoding="utf-8")
            out_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        env = os.environ.copy()
        env.update(
            {
                "NMF_GROUP_ID": group_id,
                "NMF_GROUP_NAME": group_name or "",
                "NMF_TARGET_USER_ID": user_id,
                "NMF_TARGET_NAME": target_name or "",
                "NMF_WARMUP_TEXT": text,
                "NMF_WAIT_SECONDS": str(max(3.0, self._get_float("qq_human_group_warmup_wait_seconds", 20.0))),
                "NMF_GROUP_ROW": str(max(1, int(self._get_float("qq_human_group_warmup_group_row", 1)))),
                "NMF_GROUP_BASE_Y": str(max(1, int(self._get_float("qq_human_group_warmup_group_base_y", 150)))),
                "NMF_SEARCH_RESULT_BASE_Y": str(
                    max(1, int(self._get_float("qq_human_group_warmup_search_result_base_y", 322)))
                ),
                "NMF_TRACE_FILE": str(trace_path),
                "NMF_OUT_DIR": str(out_dir),
            }
        )
        try:
            completed = self._run_powershell_sta_script_file(
                script,
                env,
                timeout,
                "human_stable_warmup.ps1",
            )
        except subprocess.TimeoutExpired:
            stage = self._read_runtime_trace_tail(trace_path) or "timeout"
            return {
                "ok": False,
                "stage": stage,
                "reason": f"powershell_timeout_after_{timeout:.1f}s",
                "outDir": str(out_dir),
            }
        stdout = (completed.stdout or "").strip()
        stderr = (completed.stderr or "").strip()
        last_line = stdout.splitlines()[-1].strip() if stdout else ""
        if last_line:
            try:
                result = json.loads(last_line)
                if isinstance(result, dict):
                    if completed.returncode and not result.get("ok"):
                        result["returncode"] = completed.returncode
                    return result
            except json.JSONDecodeError:
                pass
        return {
            "ok": False,
            "reason": self._short_error(
                RuntimeError(stderr or stdout or f"powershell exited with {completed.returncode}"),
                300,
            ),
            "returncode": completed.returncode,
            "outDir": str(out_dir),
        }

    def _run_qq_human_group_warmup_script(
        self,
        group_id: str,
        user_id: str,
        target_name: str,
        group_name: str,
        text: str,
        timeout: float,
    ) -> dict[str, Any]:
        return self._run_qq_human_group_warmup_stable_script(
            group_id,
            user_id,
            target_name,
            group_name,
            text,
            timeout,
        )
        script = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -AssemblyName System.Windows.Forms
Add-Type -TypeDefinition @"
using System;
using System.Text;
using System.Runtime.InteropServices;
public class NmfHumanWin32 {
  public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left; public int Top; public int Right; public int Bottom; }
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc lpEnumFunc, IntPtr lParam);
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint processId);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool ShowWindowAsync(IntPtr hWnd, int nCmdShow);
  [DllImport("user32.dll")] public static extern bool BringWindowToTop(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern IntPtr SetActiveWindow(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern IntPtr SetFocus(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr hWnd, IntPtr hWndInsertAfter, int X, int Y, int cx, int cy, uint uFlags);
  [DllImport("user32.dll")] public static extern void SwitchToThisWindow(IntPtr hWnd, bool fAltTab);
  [DllImport("user32.dll")] public static extern bool AttachThreadInput(uint idAttach, uint idAttachTo, bool fAttach);
  [DllImport("kernel32.dll")] public static extern uint GetCurrentThreadId();
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern int GetWindowTextLength(IntPtr hWnd);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowText(IntPtr hWnd, StringBuilder text, int count);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetClassName(IntPtr hWnd, StringBuilder text, int count);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT rect);
  [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
  [DllImport("user32.dll")] public static extern void mouse_event(uint flags, uint dx, uint dy, uint data, UIntPtr extra);
  [DllImport("user32.dll")] public static extern void keybd_event(byte vk, byte scan, uint flags, UIntPtr extra);
}
"@

function Out-Result([bool]$ok, [string]$reason, [string]$stage) {
  @{ ok = $ok; reason = $reason; stage = $stage } | ConvertTo-Json -Compress
}

function Is-True([string]$value) {
  $v = ($value + '').Trim().ToLowerInvariant()
  return @('1','true','yes','on') -contains $v
}

function Is-SingleQQMode {
  return Is-True $env:NMF_SINGLE_QQ_MODE
}

function Write-Stage([string]$stage) {
  try {
    if ($env:NMF_TRACE_FILE) {
      Add-Content -LiteralPath $env:NMF_TRACE_FILE -Value $stage -Encoding UTF8
    }
  } catch {}
}

function Get-ControlViewCondition {
  try {
    $condition = [System.Windows.Automation.Automation]::ControlViewCondition
    if ($condition -ne $null) { return $condition }
  } catch {}
  return [System.Windows.Automation.Condition]::TrueCondition
}

function Press-Key([byte]$vk) {
  [NmfHumanWin32]::keybd_event($vk, 0, 0, [UIntPtr]::Zero)
  Start-Sleep -Milliseconds 55
  [NmfHumanWin32]::keybd_event($vk, 0, 2, [UIntPtr]::Zero)
}

function Press-CtrlKey([byte]$vk) {
  [NmfHumanWin32]::keybd_event(0x11, 0, 0, [UIntPtr]::Zero)
  Start-Sleep -Milliseconds 30
  Press-Key $vk
  [NmfHumanWin32]::keybd_event(0x11, 0, 2, [UIntPtr]::Zero)
}

function Set-ClipboardTextSafe([string]$text) {
  for ($i = 0; $i -lt 12; $i++) {
    try {
      [System.Windows.Forms.Clipboard]::SetText($text)
      return $true
    } catch {
      Start-Sleep -Milliseconds 120
    }
  }
  return $false
}

function Paste-TextOnly([string]$text) {
  if (-not (Set-ClipboardTextSafe $text)) { return $false }
  Press-CtrlKey 0x56
  Start-Sleep -Milliseconds 260
  return $true
}

function Paste-And-Enter([string]$text) {
  $oldText = $null
  try {
    if ([System.Windows.Forms.Clipboard]::ContainsText()) {
      $oldText = [System.Windows.Forms.Clipboard]::GetText()
    }
  } catch {}
  if (-not (Paste-TextOnly $text)) { return $false }
  Start-Sleep -Milliseconds 220
  Press-Key 0x0D
  Start-Sleep -Milliseconds 450
  if ($oldText -ne $null) {
    try { [System.Windows.Forms.Clipboard]::SetText($oldText) } catch {}
  }
  return $true
}

function Get-ElementName($element) {
  try { return $element.Current.Name + '' } catch {}
  return ''
}

function Get-ElementTextBundle($element) {
  $parts = New-Object System.Collections.ArrayList
  foreach ($prop in @('Name','AutomationId','ClassName','HelpText')) {
    try {
      $value = $element.Current.$prop + ''
      if ($value) { [void]$parts.Add($value) }
    } catch {}
  }
  return ($parts -join "`n")
}

function Get-ValidHints([string[]]$hints) {
  return @($hints | Where-Object { ($_ + '').Trim().Length -gt 0 })
}

function Element-Has-Hint($element, [string[]]$hints) {
  $validHints = Get-ValidHints $hints
  if ($validHints.Count -eq 0) { return $false }
  try {
    $text = Get-ElementTextBundle $element
    foreach ($hint in $validHints) {
      if ($text.Contains($hint)) { return $true }
    }
    if (-not (Is-True $env:NMF_DEEP_HINT)) { return $false }
    Write-Stage 'deep_hint_scan'
    $all = $element.FindAll([System.Windows.Automation.TreeScope]::Descendants, (Get-ControlViewCondition))
    foreach ($item in $all) {
      $name = Get-ElementTextBundle $item
      if (-not $name) { continue }
      foreach ($hint in $validHints) {
        if ($name.Contains($hint)) { return $true }
      }
    }
  } catch {}
  return $false
}

function Get-RectScore($element) {
  try {
    $rect = $element.Current.BoundingRectangle
    if ($rect.Width -le 0 -or $rect.Height -le 0) { return -1000 }
    if ($rect.Width -gt 900 -or $rect.Height -gt 260) { return -5 }
    return 0
  } catch {}
  return -1000
}

function Invoke-Element($element, [bool]$doubleClick) {
  if (-not $doubleClick) {
    $pattern = $null
    try {
      if ($element.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern, [ref]$pattern)) {
        $pattern.Invoke()
        return $true
      }
    } catch {}
  }
  try {
    $rect = $element.Current.BoundingRectangle
    if ($rect.Width -le 0 -or $rect.Height -le 0) { return $false }
    $x = [int]($rect.Left + [Math]::Min([Math]::Max($rect.Width / 2, 12), $rect.Width - 8))
    $y = [int]($rect.Top + $rect.Height / 2)
    [NmfHumanWin32]::SetCursorPos($x, $y) | Out-Null
    Start-Sleep -Milliseconds 90
    $clicks = 1
    if ($doubleClick) { $clicks = 2 }
    for ($i = 0; $i -lt $clicks; $i++) {
      [NmfHumanWin32]::mouse_event(2, 0, 0, 0, [UIntPtr]::Zero)
      Start-Sleep -Milliseconds 75
      [NmfHumanWin32]::mouse_event(4, 0, 0, 0, [UIntPtr]::Zero)
      Start-Sleep -Milliseconds 110
    }
    return $true
  } catch {}
  return $false
}

function Click-NearLeftOfElement($element) {
  try {
    $rect = $element.Current.BoundingRectangle
    if ($rect.Width -le 0 -or $rect.Height -le 0) { return $false }
    $x = [int]($rect.Left - 34)
    if ($x -lt 0) { $x = [int]($rect.Left + 8) }
    $y = [int]($rect.Top + $rect.Height / 2)
    [NmfHumanWin32]::SetCursorPos($x, $y) | Out-Null
    Start-Sleep -Milliseconds 90
    [NmfHumanWin32]::mouse_event(2, 0, 0, 0, [UIntPtr]::Zero)
    Start-Sleep -Milliseconds 75
    [NmfHumanWin32]::mouse_event(4, 0, 0, 0, [UIntPtr]::Zero)
    Start-Sleep -Milliseconds 750
    return $true
  } catch {}
  return $false
}

function Get-WindowTextValue([IntPtr]$hWnd) {
  try {
    $len = [NmfHumanWin32]::GetWindowTextLength($hWnd)
    $sb = New-Object System.Text.StringBuilder ([Math]::Max(1, $len + 1))
    [NmfHumanWin32]::GetWindowText($hWnd, $sb, $sb.Capacity) | Out-Null
    return $sb.ToString()
  } catch {}
  return ''
}

function Get-WindowClassNameValue([IntPtr]$hWnd) {
  try {
    $sb = New-Object System.Text.StringBuilder 256
    [NmfHumanWin32]::GetClassName($hWnd, $sb, $sb.Capacity) | Out-Null
    return $sb.ToString()
  } catch {}
  return ''
}

function Get-WindowRectInfo([IntPtr]$hWnd) {
  try {
    $rect = New-Object NmfHumanWin32+RECT
    if ([NmfHumanWin32]::GetWindowRect($hWnd, [ref]$rect)) {
      return [pscustomobject]@{
        Left = $rect.Left
        Top = $rect.Top
        Width = $rect.Right - $rect.Left
        Height = $rect.Bottom - $rect.Top
      }
    }
  } catch {}
  return [pscustomobject]@{ Left = 0; Top = 0; Width = 0; Height = 0 }
}

function Test-UsableQQWindowHandle([IntPtr]$hWnd) {
  if ($hWnd -eq [IntPtr]::Zero) { return $false }
  $rect = Get-WindowRectInfo $hWnd
  if ($rect.Width -lt 260 -or $rect.Height -lt 220) { return $false }
  if ($rect.Left -lt -5000 -or $rect.Top -lt -5000) { return $false }
  $className = Get-WindowClassNameValue $hWnd
  if ($className -eq 'Chrome_WidgetWin_0') { return $false }
  if ($className -and $className -notmatch 'Chrome_WidgetWin_1') { return $false }
  return $true
}

function Add-QQWindowElement($result, $seen, [IntPtr]$hWnd) {
  if (-not (Test-UsableQQWindowHandle $hWnd)) { return }
  $key = $hWnd.ToInt64()
  if ($seen.Contains($key)) { return }
  try {
    $element = [System.Windows.Automation.AutomationElement]::FromHandle($hWnd)
    if ($element -eq $null) { return }
    $rect = $element.Current.BoundingRectangle
    if ($rect.Width -lt 260 -or $rect.Height -lt 220) { return }
    [void]$result.Add($element)
    [void]$seen.Add($key)
  } catch {}
}

function Get-QQWindowSortKey($win) {
  try {
    $rect = $win.Current.BoundingRectangle
    $area = [int]($rect.Width * $rect.Height)
    $name = Get-ElementName $win
    $bonus = 0
    if ($name -notmatch '资料卡|Profile') { $bonus += 100000000 }
    if ($rect.Width -ge 600 -and $rect.Height -ge 450) { $bonus += 50000000 }
    return $bonus + $area
  } catch {}
  return 0
}

function Force-ForegroundWindow([IntPtr]$hWnd) {
  try {
    [NmfHumanWin32]::ShowWindowAsync($hWnd, 9) | Out-Null
    Start-Sleep -Milliseconds 100
    [NmfHumanWin32]::keybd_event(0x12, 0, 0, [UIntPtr]::Zero)
    Start-Sleep -Milliseconds 35
    [NmfHumanWin32]::keybd_event(0x12, 0, 2, [UIntPtr]::Zero)
    $foreground = [NmfHumanWin32]::GetForegroundWindow()
    $foregroundPid = [uint32]0
    $targetPid = [uint32]0
    $foregroundThread = [NmfHumanWin32]::GetWindowThreadProcessId($foreground, [ref]$foregroundPid)
    $targetThread = [NmfHumanWin32]::GetWindowThreadProcessId($hWnd, [ref]$targetPid)
    $currentThread = [NmfHumanWin32]::GetCurrentThreadId()
    if ($foregroundThread -ne 0) { [NmfHumanWin32]::AttachThreadInput($currentThread, $foregroundThread, $true) | Out-Null }
    if ($targetThread -ne 0) { [NmfHumanWin32]::AttachThreadInput($currentThread, $targetThread, $true) | Out-Null }
    [NmfHumanWin32]::SetWindowPos($hWnd, [IntPtr](-1), 0, 0, 0, 0, 0x0001 -bor 0x0002 -bor 0x0040) | Out-Null
    Start-Sleep -Milliseconds 80
    [NmfHumanWin32]::SetWindowPos($hWnd, [IntPtr](-2), 0, 0, 0, 0, 0x0001 -bor 0x0002 -bor 0x0040) | Out-Null
    [NmfHumanWin32]::BringWindowToTop($hWnd) | Out-Null
    [NmfHumanWin32]::SetActiveWindow($hWnd) | Out-Null
    [NmfHumanWin32]::SetFocus($hWnd) | Out-Null
    [NmfHumanWin32]::SetForegroundWindow($hWnd) | Out-Null
    try { [NmfHumanWin32]::SwitchToThisWindow($hWnd, $true) } catch {}
    if ($targetThread -ne 0) { [NmfHumanWin32]::AttachThreadInput($currentThread, $targetThread, $false) | Out-Null }
    if ($foregroundThread -ne 0) { [NmfHumanWin32]::AttachThreadInput($currentThread, $foregroundThread, $false) | Out-Null }
    Start-Sleep -Milliseconds 280
    return $true
  } catch {}
  return $false
}

function Focus-Window($win) {
  try {
    $hWnd = [IntPtr]$win.Current.NativeWindowHandle
    if (-not (Force-ForegroundWindow $hWnd)) {
      [NmfHumanWin32]::ShowWindowAsync($hWnd, 9) | Out-Null
      [NmfHumanWin32]::SetForegroundWindow($hWnd) | Out-Null
    }
    Start-Sleep -Milliseconds 350
    return $true
  } catch {}
  return $false
}

function Get-QQWindows {
  $names = @('QQ')
  $allPids = New-Object System.Collections.ArrayList
  foreach ($name in $names) {
    try {
      foreach ($proc in @(Get-Process $name -ErrorAction SilentlyContinue)) {
        if ($proc.Id -and -not $allPids.Contains($proc.Id)) { [void]$allPids.Add($proc.Id) }
      }
    } catch {}
  }
  if ($allPids.Count -eq 0) { return @() }
  $result = New-Object System.Collections.ArrayList
  $seen = New-Object System.Collections.ArrayList
  try {
    $root = [System.Windows.Automation.AutomationElement]::RootElement
    $children = $root.FindAll([System.Windows.Automation.TreeScope]::Children, [System.Windows.Automation.Condition]::TrueCondition)
    foreach ($win in $children) {
      try {
        if ($allPids.Contains($win.Current.ProcessId) -and $win.Current.NativeWindowHandle -ne 0) {
          Add-QQWindowElement $result $seen ([IntPtr]$win.Current.NativeWindowHandle)
        }
      } catch {}
    }
  } catch {}
  foreach ($proc in @(Get-Process QQ -ErrorAction SilentlyContinue)) {
    try {
      if ($proc.MainWindowHandle -and $proc.MainWindowHandle -ne 0) {
        Add-QQWindowElement $result $seen ([IntPtr]$proc.MainWindowHandle)
      }
    } catch {}
  }
  $callback = [NmfHumanWin32+EnumWindowsProc]{
    param([IntPtr]$hWnd, [IntPtr]$lParam)
    $procId = [uint32]0
    [NmfHumanWin32]::GetWindowThreadProcessId($hWnd, [ref]$procId) | Out-Null
    if ($allPids.Contains([int]$procId)) {
      Add-QQWindowElement $result $seen $hWnd
    }
    return $true
  }
  try { [NmfHumanWin32]::EnumWindows($callback, [IntPtr]::Zero) | Out-Null } catch {}
  return @($result | Sort-Object { Get-QQWindowSortKey $_ } -Descending)
}

function Get-QQProcessCount {
  $count = 0
  foreach ($name in @('QQ')) {
    try { $count += @(Get-Process $name -ErrorAction SilentlyContinue).Count } catch {}
  }
  return $count
}

function Focus-FirstQQWindow {
  foreach ($win in (Get-QQWindows)) {
    if (Focus-Window $win) { return $true }
  }
  return $false
}

function Find-SearchEdit($root) {
  try {
    Write-Stage 'scan_search_edit'
    $all = $root.FindAll([System.Windows.Automation.TreeScope]::Descendants, (Get-ControlViewCondition))
    $best = $null
    $bestScore = -1
    foreach ($item in $all) {
      $text = Get-ElementTextBundle $item
      $score = 0
      try {
        $ctype = $item.Current.ControlType.ProgrammaticName + ''
        if ($ctype -match 'Edit|Document') { $score += 20 }
      } catch {}
      if ($text -match '搜索|查找|Search|search') { $score += 40 }
      if ($score -le 0) { continue }
      try {
        $rect = $item.Current.BoundingRectangle
        if ($rect.Width -le 0 -or $rect.Height -le 0) { continue }
        if ($rect.Width -gt 600 -or $rect.Height -gt 80) { $score -= 10 }
      } catch { continue }
      if ($score -gt $bestScore) {
        $best = $item
        $bestScore = $score
      }
    }
    return $best
  } catch {}
  return $null
}

function Find-NamedButton($root, [string]$regex) {
  try {
    Write-Stage 'scan_named_button'
    $all = $root.FindAll([System.Windows.Automation.TreeScope]::Descendants, (Get-ControlViewCondition))
    foreach ($item in $all) {
      $name = Get-ElementName $item
      if (-not $name) { continue }
      if ($name -match $regex) { return $item }
    }
  } catch {}
  return $null
}

function Close-UnsupportedQQDialog {
  $closed = $false
  foreach ($win in (Get-QQWindows)) {
    $isUnsupported = Element-Has-Hint $win @('QQ版本不支持', '不支持本功能', '请尝试下载最新版本', '手机端查看')
    if (-not $isUnsupported) { continue }
    Focus-Window $win | Out-Null
    $button = Find-NamedButton $win '确定|知道了|关闭|OK|Ok|ok'
    if ($button -ne $null) {
      if (Invoke-Element $button $false) {
        Start-Sleep -Milliseconds 400
        $closed = $true
        continue
      }
    }
    Press-Key 0x0D
    Start-Sleep -Milliseconds 400
    $closed = $true
  }
  return $closed
}

function Clear-And-TypeQuery([string]$query) {
  Press-CtrlKey 0x41
  Start-Sleep -Milliseconds 120
  Paste-TextOnly $query | Out-Null
  Start-Sleep -Milliseconds 350
}

function Try-OpenGroupFromQQSearch([string[]]$groupHints, [bool]$requireGroupHint) {
  if (-not (Is-True $env:NMF_GROUP_SEARCH)) { return $false }
  $query = $env:NMF_GROUP_NAME
  if (-not $query) { $query = $env:NMF_GROUP_ID }
  if (-not $query) { return $false }

  foreach ($win in (Get-QQWindows)) {
    Write-Stage 'group_search_keyboard'
    Focus-Window $win | Out-Null
    Press-CtrlKey 0x46
    Start-Sleep -Milliseconds 250
    Clear-And-TypeQuery $query
    Press-Key 0x0D
    Start-Sleep -Milliseconds 1300
    $verified = Find-GroupWindow $groupHints $true
    if ($verified -ne $null) { return $verified }
    if (Is-SingleQQMode) {
      Write-Stage 'group_search_single_qq_fallback'
      return $win
    }
    $visibleQQ = @(Get-QQWindows)
    if ($visibleQQ.Count -eq 1) {
      Write-Stage 'group_search_single_window_fallback'
      return $win
    }
    Press-Key 0x1B
    Start-Sleep -Milliseconds 300
  }
  return $null
}

function Open-GroupByProtocol([string]$groupId) {
  if (-not (Is-True $env:NMF_FORCE_OPEN_GROUP_PROTOCOL)) { return }
  foreach ($url in @(
    "mqqapi://im/chat?chat_type=group&uin=$groupId&version=1&src_type=web",
    "mqqapi://im/chat?chat_type=group&groupuin=$groupId&version=1&src_type=web"
  )) {
    try {
      Start-Process $url | Out-Null
      Start-Sleep -Milliseconds 900
      Close-UnsupportedQQDialog | Out-Null
    } catch {}
  }
}

function Find-GroupWindow([string[]]$groupHints, [bool]$requireGroupHint) {
  $windows = Get-QQWindows
  $best = $null
  $bestScore = -1
  foreach ($win in $windows) {
    $hasGroup = Element-Has-Hint $win $groupHints
    if ($requireGroupHint -and -not $hasGroup) { continue }
    $score = 1
    if ($hasGroup) { $score += 100 }
    $title = Get-ElementName $win
    foreach ($hint in (Get-ValidHints $groupHints)) {
      if ($title.Contains($hint)) { $score += 30 }
    }
    if ($score -gt $bestScore) {
      $best = $win
      $bestScore = $score
    }
  }
  return $best
}

function Find-TargetElement($root, [string[]]$targetHints) {
  $validHints = Get-ValidHints $targetHints
  if ($validHints.Count -eq 0) { return $null }
  $best = $null
  $bestScore = -1000
  try {
    foreach ($hint in $validHints) {
      try {
        $cond = New-Object System.Windows.Automation.PropertyCondition -ArgumentList ([System.Windows.Automation.AutomationElement]::NameProperty, $hint)
        $direct = $root.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $cond)
        if ($direct -ne $null) { return $direct }
      } catch {}
    }
    Write-Stage 'scan_target_controls'
    $all = $root.FindAll([System.Windows.Automation.TreeScope]::Descendants, (Get-ControlViewCondition))
    foreach ($item in $all) {
      $text = Get-ElementTextBundle $item
      if (-not $text) { continue }
      $score = Get-RectScore $item
      if ($score -le -1000) { continue }
      foreach ($hint in $validHints) {
        if ($text.Contains($hint)) {
          $score += 50
          if ((Get-ElementName $item) -eq $hint) { $score += 35 }
        }
      }
      if ($score -lt 45) { continue }
      try {
        $ctype = $item.Current.ControlType.ProgrammaticName + ''
        if ($ctype -match 'Button|Image|Hyperlink|ListItem|TreeItem|DataItem') { $score += 25 }
        elseif ($ctype -match 'Text') { $score += 8 }
      } catch {}
      if ($score -gt $bestScore) {
        $best = $item
        $bestScore = $score
      }
    }
  } catch {}
  return $best
}

function Window-HasTargetHint($root, [string[]]$targetHints) {
  if (Element-Has-Hint $root $targetHints) { return $true }
  $target = Find-TargetElement $root $targetHints
  return ($target -ne $null)
}

function Get-NativeWindowHandleValue($win) {
  try { return [int64]$win.Current.NativeWindowHandle } catch {}
  return 0
}

function Try-SearchTargetInGroup($groupWin, [string]$targetName, [string]$targetUserId) {
  if (-not (Is-True $env:NMF_MEMBER_SEARCH)) { return $false }
  $queries = New-Object System.Collections.ArrayList
  foreach ($query in @($targetName, $targetUserId)) {
    $value = ($query + '').Trim()
    if ($value -and -not $queries.Contains($value)) { [void]$queries.Add($value) }
  }
  if ($queries.Count -eq 0) { return $false }
  foreach ($query in $queries) {
    Focus-Window $groupWin | Out-Null
    Press-CtrlKey 0x46
    Start-Sleep -Milliseconds 300
    Clear-And-TypeQuery $query
    Write-Stage 'member_search_pasted'
    Start-Sleep -Milliseconds 700
    Press-Key 0x0D
    Write-Stage 'member_search_enter'
    Start-Sleep -Milliseconds 950
    Press-Key 0x0D
    Write-Stage 'member_search_enter_second'
    Start-Sleep -Milliseconds 650
  }
  return $true
}

function Find-SendButton($root, [string]$buttonRegex) {
  try {
    Write-Stage 'scan_send_button'
    foreach ($name in @('发消息', '发送消息', '聊天', '私聊')) {
      try {
        $cond = New-Object System.Windows.Automation.PropertyCondition -ArgumentList ([System.Windows.Automation.AutomationElement]::NameProperty, $name)
        $direct = $root.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $cond)
        if ($direct -ne $null) { return $direct }
      } catch {}
    }
    Write-Stage 'scan_send_button_typed'
    $buttonCond = New-Object System.Windows.Automation.PropertyCondition -ArgumentList ([System.Windows.Automation.AutomationElement]::ControlTypeProperty, [System.Windows.Automation.ControlType]::Button)
    $linkCond = New-Object System.Windows.Automation.PropertyCondition -ArgumentList ([System.Windows.Automation.AutomationElement]::ControlTypeProperty, [System.Windows.Automation.ControlType]::Hyperlink)
    $textCond = New-Object System.Windows.Automation.PropertyCondition -ArgumentList ([System.Windows.Automation.AutomationElement]::ControlTypeProperty, [System.Windows.Automation.ControlType]::Text)
    $condition = New-Object System.Windows.Automation.OrCondition -ArgumentList @($buttonCond, $linkCond, $textCond)
    $all = $root.FindAll([System.Windows.Automation.TreeScope]::Descendants, $condition)
    foreach ($item in $all) {
      $name = Get-ElementName $item
      if (-not $name) { continue }
      if ($name -match $buttonRegex) {
        return $item
      }
    }
  } catch {}
  return $null
}

function Try-ClickSendMessageButton([string[]]$targetHints, [string]$buttonRegex, [bool]$requireTargetHint) {
  foreach ($win in (Get-QQWindows)) {
    if ($requireTargetHint -and -not (Window-HasTargetHint $win $targetHints)) { continue }
    $button = Find-SendButton $win $buttonRegex
    if ($button -ne $null) {
      Focus-Window $win | Out-Null
      if (Invoke-Element $button $false) {
        Start-Sleep -Milliseconds 1000
        return $true
      }
    }
  }
  return $false
}

function Try-PasteIntoOpenedTargetChat([string[]]$targetHints, [string[]]$groupHints, [bool]$requireTargetHint, [string]$text, [int64]$excludeHandle) {
  foreach ($win in (Get-QQWindows)) {
    $handle = Get-NativeWindowHandleValue $win
    if ($excludeHandle -and $handle -eq $excludeHandle) { continue }
    $hasTarget = Window-HasTargetHint $win $targetHints
    if ($requireTargetHint -and -not $hasTarget) { continue }
    if (Element-Has-Hint $win $groupHints) { continue }
    Focus-Window $win | Out-Null
    if (Paste-And-Enter $text) { return $true }
  }
  return $false
}

function Click-At([int]$x, [int]$y) {
  try {
    [NmfHumanWin32]::SetCursorPos($x, $y) | Out-Null
    Start-Sleep -Milliseconds 80
    [NmfHumanWin32]::mouse_event(2, 0, 0, 0, [UIntPtr]::Zero)
    Start-Sleep -Milliseconds 70
    [NmfHumanWin32]::mouse_event(4, 0, 0, 0, [UIntPtr]::Zero)
    Start-Sleep -Milliseconds 180
    return $true
  } catch {}
  return $false
}

function Try-ClickProfileSendButtonByCoordinate([string]$text) {
  foreach ($win in (Get-QQWindows)) {
    $name = Get-ElementName $win
    if ($name -notmatch '资料卡|Profile') { continue }
    $profileHandle = Get-NativeWindowHandleValue $win
    try {
      $rect = $win.Current.BoundingRectangle
      if ($rect.Width -lt 260 -or $rect.Height -lt 260) { continue }
      Focus-Window $win | Out-Null
      foreach ($point in @(
        @{ X = 0.50; Bottom = 42 },
        @{ X = 0.50; Bottom = 64 },
        @{ X = 0.72; Bottom = 42 }
      )) {
        Write-Stage 'profile_card_coordinate_button'
        $x = [int]($rect.Left + ($rect.Width * [double]$point.X))
        $y = [int]($rect.Bottom - [double]$point.Bottom)
        if (-not (Click-At $x $y)) { continue }
        Start-Sleep -Milliseconds 900
        $foregroundHandle = [NmfHumanWin32]::GetForegroundWindow().ToInt64()
        if ($profileHandle -and $foregroundHandle -eq $profileHandle) { continue }
        Write-Stage 'paste_after_profile_card_coordinate'
        if (Paste-And-Enter $text) { return $true }
      }
    } catch {}
  }
  return $false
}

function Try-SendFromTargetContext([string[]]$targetHints, [string[]]$groupHints, [string]$buttonRegex, [bool]$requireTargetHint, [string]$text, $groupWin) {
  $groupHandle = Get-NativeWindowHandleValue $groupWin
  if (Is-SingleQQMode) {
    Write-Stage 'single_qq_context_button_first'
    if (Try-ClickProfileSendButtonByCoordinate $text) {
      return 'sent_after_profile_card_coordinate_button'
    }
    if (Try-ClickSendMessageButton $targetHints $buttonRegex $false) {
      Write-Stage 'paste_after_single_qq_context_button'
      if (Paste-And-Enter $text) { return 'sent_after_single_qq_context_button' }
    }
  }
  if (Try-ClickSendMessageButton $targetHints $buttonRegex $requireTargetHint) {
    Write-Stage 'paste_after_context_button'
    if (Paste-And-Enter $text) { return 'sent_after_context_button' }
  }
  if (Try-PasteIntoOpenedTargetChat $targetHints $groupHints $requireTargetHint $text $groupHandle) {
    return 'sent_after_context_chat'
  }
  return ''
}

$text = $env:NMF_WARMUP_TEXT
$groupId = $env:NMF_GROUP_ID
$groupName = $env:NMF_GROUP_NAME
$targetUserId = $env:NMF_TARGET_USER_ID
$targetName = $env:NMF_TARGET_NAME
$requireGroupHint = Is-True $env:NMF_REQUIRE_GROUP_HINT
$requireTargetHint = Is-True $env:NMF_REQUIRE_TARGET_HINT
$buttonRegex = $env:NMF_BUTTON_REGEX
if (-not $buttonRegex) { $buttonRegex = '\u53d1\u6d88\u606f|\u53d1\u9001\u6d88\u606f|\u804a\u5929|\u79c1\u804a' }
$waitSeconds = 20.0
try { $waitSeconds = [double]$env:NMF_WAIT_SECONDS } catch {}
$groupHints = @($groupName, $groupId)
$targetHints = @($targetUserId, $targetName)

Write-Stage 'startup'
if ((Get-QQProcessCount) -le 0) {
  Out-Result $false 'qq_process_not_found' 'startup'
  exit 0
}
Write-Stage 'focus_first_qq'
if (-not (Focus-FirstQQWindow)) {
  Out-Result $false 'visible_qq_window_not_found' 'startup'
  exit 0
}
Write-Stage 'close_unsupported_dialog'
Close-UnsupportedQQDialog | Out-Null

Write-Stage 'open_group_protocol'
Open-GroupByProtocol $groupId
$deadline = (Get-Date).AddSeconds($waitSeconds)
$groupWin = $null
$searchedGroup = $false
while ((Get-Date) -lt $deadline -and $groupWin -eq $null) {
  Write-Stage 'find_group_loop'
  Close-UnsupportedQQDialog | Out-Null
  $lookupRequiresHint = $requireGroupHint
  if (-not $searchedGroup -and (Is-True $env:NMF_GROUP_SEARCH)) {
    $lookupRequiresHint = $true
  }
  $groupWin = Find-GroupWindow $groupHints $lookupRequiresHint
  if ($groupWin -eq $null -and -not $searchedGroup) {
    Write-Stage 'group_search'
    $searchedGroup = $true
    $searchedGroupWin = Try-OpenGroupFromQQSearch $groupHints $requireGroupHint
    if ($searchedGroupWin -ne $null) { $groupWin = $searchedGroupWin }
  }
  if ($groupWin -eq $null) { Start-Sleep -Milliseconds 450 }
}
if ($groupWin -eq $null) {
  if (Is-SingleQQMode) {
    Write-Stage 'single_qq_group_window_fallback'
    $fallbackWindows = @(Get-QQWindows)
    if ($fallbackWindows.Count -ge 1) {
      $groupWin = $fallbackWindows[0]
    }
  }
}
if ($groupWin -eq $null) {
  Out-Result $false 'group_window_not_found_after_protocol_and_search' 'group'
  exit 0
}

Write-Stage 'group_found'
Focus-Window $groupWin | Out-Null
$searched = $false
if (Is-True $env:NMF_MEMBER_SEARCH) {
  Write-Stage 'member_search_initial'
  $searched = Try-SearchTargetInGroup $groupWin $targetName $targetUserId
  if ($searched) {
    Write-Stage 'after_member_search_context_probe'
    $sentReason = Try-SendFromTargetContext $targetHints $groupHints $buttonRegex $requireTargetHint $text $groupWin
    if ($sentReason) {
      Out-Result $true $sentReason 'send_button'
      exit 0
    }
    Focus-Window $groupWin | Out-Null
  }
}
while ((Get-Date) -lt $deadline) {
  Write-Stage 'find_target_loop'
  $target = Find-TargetElement $groupWin $targetHints
  if ($target -ne $null) {
    Write-Stage 'target_found'
    if (Click-NearLeftOfElement $target) {
      Write-Stage 'click_avatar_area'
      if (Try-ClickSendMessageButton $targetHints $buttonRegex $requireTargetHint) {
        Write-Stage 'paste_after_avatar_button'
        if (Paste-And-Enter $text) {
          Out-Result $true 'sent_after_group_member_avatar_area_button' 'send_button'
          exit 0
        }
      }
    }
    if (Invoke-Element $target $false) {
      Write-Stage 'click_target'
      Start-Sleep -Milliseconds 950
      if (Try-ClickSendMessageButton $targetHints $buttonRegex $requireTargetHint) {
        Write-Stage 'paste_after_profile_button'
        if (Paste-And-Enter $text) {
          Out-Result $true 'sent_after_group_member_profile_button' 'send_button'
          exit 0
        }
      }
    }
    if (Invoke-Element $target $true) {
      Write-Stage 'double_click_target'
      Start-Sleep -Milliseconds 1200
      if (Try-ClickSendMessageButton $targetHints $buttonRegex $requireTargetHint) {
        Write-Stage 'paste_after_double_click_button'
        if (Paste-And-Enter $text) {
          Out-Result $true 'sent_after_group_member_double_click_button' 'send_button'
          exit 0
        }
      }
      if (Try-PasteIntoOpenedTargetChat $targetHints $groupHints $requireTargetHint $text (Get-NativeWindowHandleValue $groupWin)) {
        Out-Result $true 'sent_after_group_member_double_click_chat' 'direct_chat'
        exit 0
      }
    }
  }
  if (-not $searched) {
    Write-Stage 'member_search_retry'
    $searched = Try-SearchTargetInGroup $groupWin $targetName $targetUserId
  }
  Start-Sleep -Milliseconds 650
}

Out-Result $false 'target_member_or_send_message_button_not_found' 'target'
"""
        runtime_dir = self.data_dir / "runtime_scripts"
        trace_path = runtime_dir / f"human_group_warmup_{group_id}_{user_id}_{int(time.time() * 1000)}.trace.log"
        try:
            runtime_dir.mkdir(parents=True, exist_ok=True)
            trace_path.write_text("", encoding="utf-8")
        except Exception:
            pass
        env = os.environ.copy()
        env.update(
            {
                "NMF_GROUP_ID": group_id,
                "NMF_GROUP_NAME": group_name or "",
                "NMF_TARGET_USER_ID": user_id,
                "NMF_TARGET_NAME": target_name or "",
                "NMF_WARMUP_TEXT": text,
                "NMF_WAIT_SECONDS": str(max(3.0, self._get_float("qq_human_group_warmup_wait_seconds", 20.0))),
                "NMF_REQUIRE_GROUP_HINT": "1"
                if self._get_bool("qq_human_group_warmup_require_group_hint", True)
                else "0",
                "NMF_REQUIRE_TARGET_HINT": "1"
                if self._get_bool("qq_human_group_warmup_require_target_hint", True)
                else "0",
                "NMF_MEMBER_SEARCH": "1"
                if self._get_bool("qq_human_group_warmup_member_search_enabled", True)
                else "0",
                "NMF_GROUP_SEARCH": "1"
                if self._get_bool("qq_human_group_warmup_group_search_enabled", True)
                else "0",
                "NMF_SINGLE_QQ_MODE": "1"
                if self._get_bool("qq_human_group_warmup_single_qq_mode", False)
                else "0",
                "NMF_FORCE_OPEN_GROUP_PROTOCOL": "1"
                if self._get_bool("qq_human_group_warmup_force_open_group_protocol_enabled", False)
                else "0",
                "NMF_DEEP_HINT": "1"
                if self._get_bool("qq_human_group_warmup_deep_hint_enabled", False)
                else "0",
                "NMF_TRACE_FILE": str(trace_path),
                "NMF_BUTTON_REGEX": self._string(
                    self._get(
                        "qq_human_group_warmup_button_regex",
                        r"\u53d1\u6d88\u606f|\u53d1\u9001\u6d88\u606f|\u804a\u5929|\u79c1\u804a",
                    )
                ),
            }
        )
        try:
            completed = self._run_powershell_sta_script_file(
                script,
                env,
                timeout,
                "human_group_warmup.ps1",
            )
        except subprocess.TimeoutExpired:
            stage = self._read_runtime_trace_tail(trace_path) or "timeout"
            return {
                "ok": False,
                "stage": stage,
                "reason": f"powershell_timeout_after_{timeout:.1f}s",
            }
        stdout = (completed.stdout or "").strip()
        stderr = (completed.stderr or "").strip()
        last_line = stdout.splitlines()[-1].strip() if stdout else ""
        if last_line:
            try:
                result = json.loads(last_line)
                if isinstance(result, dict):
                    if completed.returncode and not result.get("ok"):
                        result["returncode"] = completed.returncode
                    return result
            except json.JSONDecodeError:
                pass
        return {
            "ok": False,
            "reason": self._short_error(
                RuntimeError(stderr or stdout or f"powershell exited with {completed.returncode}"),
                300,
            ),
            "returncode": completed.returncode,
        }

    def _read_runtime_trace_tail(self, trace_path: Path) -> str:
        try:
            if not trace_path.exists():
                return ""
            lines = trace_path.read_text(encoding="utf-8", errors="replace").splitlines()
            return lines[-1].strip() if lines else ""
        except Exception:
            return ""

    def _run_qq_desktop_warmup_script(
        self,
        group_id: str,
        user_id: str,
        target_name: str,
        group_name: str,
        text: str,
        timeout: float,
    ) -> dict[str, Any]:
        script = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -AssemblyName System.Windows.Forms
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public class NmfWin32 {
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint processId);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool ShowWindowAsync(IntPtr hWnd, int nCmdShow);
  [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
  [DllImport("user32.dll")] public static extern void mouse_event(uint flags, uint dx, uint dy, uint data, UIntPtr extra);
  [DllImport("user32.dll")] public static extern void keybd_event(byte vk, byte scan, uint flags, UIntPtr extra);
}
"@

function Out-Result([bool]$ok, [string]$reason, [string]$stage) {
  @{ ok = $ok; reason = $reason; stage = $stage } | ConvertTo-Json -Compress
}

function Is-True([string]$value) {
  $v = ($value + '').Trim().ToLowerInvariant()
  return @('1','true','yes','on') -contains $v
}

function Press-Key([byte]$vk) {
  [NmfWin32]::keybd_event($vk, 0, 0, [UIntPtr]::Zero)
  Start-Sleep -Milliseconds 50
  [NmfWin32]::keybd_event($vk, 0, 2, [UIntPtr]::Zero)
}

function Paste-And-Enter([string]$text) {
  $oldText = $null
  try {
    if ([System.Windows.Forms.Clipboard]::ContainsText()) {
      $oldText = [System.Windows.Forms.Clipboard]::GetText()
    }
  } catch {}
  $set = $false
  for ($i = 0; $i -lt 10 -and -not $set; $i++) {
    try {
      [System.Windows.Forms.Clipboard]::SetText($text)
      $set = $true
    } catch {
      Start-Sleep -Milliseconds 120
    }
  }
  if (-not $set) { return $false }
  [NmfWin32]::keybd_event(0x11, 0, 0, [UIntPtr]::Zero)
  Press-Key 0x56
  [NmfWin32]::keybd_event(0x11, 0, 2, [UIntPtr]::Zero)
  Start-Sleep -Milliseconds 220
  Press-Key 0x0D
  Start-Sleep -Milliseconds 220
  if ($oldText -ne $null) {
    try { [System.Windows.Forms.Clipboard]::SetText($oldText) } catch {}
  }
  return $true
}

function Invoke-Element($element) {
  $pattern = $null
  try {
    if ($element.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern, [ref]$pattern)) {
      $pattern.Invoke()
      return $true
    }
  } catch {}
  try {
    $rect = $element.Current.BoundingRectangle
    if ($rect.Width -gt 0 -and $rect.Height -gt 0) {
      $x = [int]($rect.Left + $rect.Width / 2)
      $y = [int]($rect.Top + $rect.Height / 2)
      [NmfWin32]::SetCursorPos($x, $y) | Out-Null
      Start-Sleep -Milliseconds 80
      [NmfWin32]::mouse_event(2, 0, 0, 0, [UIntPtr]::Zero)
      Start-Sleep -Milliseconds 80
      [NmfWin32]::mouse_event(4, 0, 0, 0, [UIntPtr]::Zero)
      return $true
    }
  } catch {}
  return $false
}

function Element-Has-Hint($element, [string[]]$hints) {
  $validHints = @($hints | Where-Object { ($_ + '').Trim().Length -gt 0 })
  if ($validHints.Count -eq 0) { return $false }
  try {
    foreach ($hint in $validHints) {
      if (($element.Current.Name + '').Contains($hint)) { return $true }
    }
    if (-not (Is-True $env:NMF_DEEP_TARGET_HINT)) { return $false }
    $all = $element.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.Condition]::TrueCondition)
    foreach ($item in $all) {
      $name = ''
      try { $name = $item.Current.Name + '' } catch {}
      if (-not $name) { continue }
      foreach ($hint in $validHints) {
        if ($name.Contains($hint)) { return $true }
      }
    }
  } catch {}
  return $false
}

function Get-QQWindows {
  $llbotPids = @(Get-Process llbot -ErrorAction SilentlyContinue | ForEach-Object { $_.Id })
  $qqPids = @(Get-Process QQ -ErrorAction SilentlyContinue | ForEach-Object { $_.Id })
  $allPids = @($llbotPids + $qqPids)
  if ($allPids.Count -eq 0) { return @() }
  $fg = [NmfWin32]::GetForegroundWindow()
  if ($fg -ne [IntPtr]::Zero) {
    $fgPid = [uint32]0
    [void][NmfWin32]::GetWindowThreadProcessId($fg, [ref]$fgPid)
    if ($allPids -contains [int]$fgPid) {
      try { return @([System.Windows.Automation.AutomationElement]::FromHandle($fg)) } catch {}
    }
  }
  $root = [System.Windows.Automation.AutomationElement]::RootElement
  $children = $root.FindAll([System.Windows.Automation.TreeScope]::Children, [System.Windows.Automation.Condition]::TrueCondition)
  $result = New-Object System.Collections.ArrayList
  foreach ($win in $children) {
    try {
      if ($llbotPids -contains $win.Current.ProcessId -and $win.Current.NativeWindowHandle -ne 0) {
        [void]$result.Add($win)
        if ($result.Count -ge 5) { break }
      }
    } catch {}
  }
  foreach ($win in $children) {
    try {
      if ($qqPids -contains $win.Current.ProcessId -and $win.Current.NativeWindowHandle -ne 0) {
        [void]$result.Add($win)
        if ($result.Count -ge 5) { break }
      }
    } catch {}
  }
  return @($result)
}

$text = $env:NMF_WARMUP_TEXT
$targetUserId = $env:NMF_TARGET_USER_ID
$targetName = $env:NMF_TARGET_NAME
$requireHint = $true
$allowDirect = Is-True $env:NMF_ALLOW_DIRECT_PASTE
$clickButton = Is-True $env:NMF_CLICK_SEND_BUTTON
$buttonRegex = $env:NMF_BUTTON_REGEX
if (-not $buttonRegex) { $buttonRegex = '\u53d1\u6d88\u606f|\u53d1\u9001\u6d88\u606f|\u804a\u5929|\u79c1\u804a' }
$waitSeconds = 8.0
try { $waitSeconds = [double]$env:NMF_WAIT_SECONDS } catch {}
$deadline = (Get-Date).AddSeconds($waitSeconds)
$hints = @($targetUserId, $targetName)

while ((Get-Date) -lt $deadline) {
  $windows = Get-QQWindows
  foreach ($win in $windows) {
    $hasHint = Element-Has-Hint $win $hints
    if ($requireHint -and -not $hasHint) { continue }
    try {
      [NmfWin32]::ShowWindowAsync([IntPtr]$win.Current.NativeWindowHandle, 5) | Out-Null
      [NmfWin32]::SetForegroundWindow([IntPtr]$win.Current.NativeWindowHandle) | Out-Null
      Start-Sleep -Milliseconds 260
    } catch {}

    if ($clickButton) {
      try {
        $all = $win.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.Condition]::TrueCondition)
        foreach ($item in $all) {
          $name = ''
          try { $name = $item.Current.Name + '' } catch {}
          if (-not $name) { continue }
          if ($name -match $buttonRegex) {
            if (Invoke-Element $item) {
              Start-Sleep -Milliseconds 1200
              if (Paste-And-Enter $text) {
                Out-Result $true 'sent_after_button' 'button'
                exit 0
              }
            }
          }
        }
      } catch {}
    }

    if ($allowDirect -and $hasHint) {
      if (Paste-And-Enter $text) {
        Out-Result $true 'sent_by_direct_paste' 'direct'
        exit 0
      }
    }
  }
  Start-Sleep -Milliseconds 350
}

Out-Result $false 'target_qq_window_or_send_button_not_found' 'wait'
"""
        env = os.environ.copy()
        env.update(
            {
                "NMF_GROUP_ID": group_id,
                "NMF_GROUP_NAME": group_name or "",
                "NMF_TARGET_USER_ID": user_id,
                "NMF_TARGET_NAME": target_name or "",
                "NMF_WARMUP_TEXT": text,
                "NMF_WAIT_SECONDS": str(max(2.0, self._get_float("qq_desktop_warmup_wait_seconds", 8.0))),
                "NMF_REQUIRE_TARGET_HINT": "1"
                if self._get_bool("qq_desktop_warmup_require_target_hint", True)
                else "0",
                "NMF_DEEP_TARGET_HINT": "1"
                if self._get_bool("qq_desktop_warmup_deep_target_hint_enabled", True)
                else "0",
                "NMF_ALLOW_DIRECT_PASTE": "1"
                if self._get_bool("qq_desktop_warmup_direct_paste_enabled", True)
                else "0",
                "NMF_CLICK_SEND_BUTTON": "1"
                if self._get_bool("qq_desktop_warmup_click_send_button_enabled", True)
                else "0",
                "NMF_BUTTON_REGEX": self._string(
                    self._get(
                        "qq_desktop_warmup_button_regex",
                        r"\u53d1\u6d88\u606f|\u53d1\u9001\u6d88\u606f|\u804a\u5929|\u79c1\u804a",
                    )
                ),
            }
        )
        completed = self._run_powershell_sta_script_file(
            script,
            env,
            timeout,
            "desktop_warmup.ps1",
        )
        stdout = (completed.stdout or "").strip()
        stderr = (completed.stderr or "").strip()
        last_line = stdout.splitlines()[-1].strip() if stdout else ""
        if last_line:
            try:
                result = json.loads(last_line)
                if isinstance(result, dict):
                    if completed.returncode and not result.get("ok"):
                        result["returncode"] = completed.returncode
                    return result
            except json.JSONDecodeError:
                pass
        return {
            "ok": False,
            "reason": self._short_error(
                RuntimeError(stderr or stdout or f"powershell exited with {completed.returncode}"),
                300,
            ),
            "returncode": completed.returncode,
        }

    def _qq_protocol_profile_warmup_urls(self, group_id: str, user_id: str) -> list[str]:
        raw_urls = [self._string(item) for item in self._get_list("qq_protocol_profile_warmup_urls")]
        if not raw_urls:
            raw_urls = [
                "mqqapi://card/show_pslcard?src_type=internal&source=group&version=1&uin={user_id}&troopuin={group_id}",
                "mqqapi://card/show_pslcard?src_type=internal&source=group&version=1&uin={user_id}&groupcode={group_id}",
                "mqqapi://im/chat?chat_type=temp&uin={user_id}&groupuin={group_id}&version=1&src_type=web",
            ]
        urls: list[str] = []
        replacements = {
            "group_id": urllib.parse.quote(group_id, safe=""),
            "user_id": urllib.parse.quote(user_id, safe=""),
        }
        for item in raw_urls:
            if not item:
                continue
            try:
                url = item.format(**replacements)
            except Exception:
                url = item
            if url and url not in urls:
                urls.append(url)
        return urls

    async def _activate_llbot_temp_context(self, bot: Any, group_id: str, user_id: str) -> bool:
        return False
        if not self._get_bool("llbot_debug_temp_context_enabled", True):
            return False
        group_id = self._string(group_id)
        user_id = self._string(user_id)
        if not group_id.isdigit() or not user_id.isdigit():
            return False
        try:
            uid = await self._call_llbot_debug(
                bot,
                "ntUserApi",
                "getUidByUin",
                [user_id, group_id],
            )
        except Exception as exc:
            logger.warning(
                "new_member_forwarder: LLBot debug temp context cannot resolve uid for user %s in group %s: %s",
                user_id,
                group_id,
                self._short_error(exc),
            )
            return False

        uid = self._string(uid)
        if not uid:
            logger.warning(
                "new_member_forwarder: LLBot debug temp context got empty uid for user %s in group %s.",
                user_id,
                group_id,
            )
            return False

        peer = {"chatType": 100, "peerUid": uid, "guildId": group_id}
        topped = False
        calls: list[tuple[str, list[Any]]] = [
            ("setContactLocalTop", [peer, True]),
            ("activateChat", [peer]),
            ("activateChatAndGetHistory", [peer, 20]),
            ("getAioFirstViewLatestMsgs", [peer, 20]),
        ]
        if self._get_bool("llbot_debug_temp_input_status_enabled", True):
            event_type = max(0, int(self._get_float("llbot_debug_temp_input_status_event_type", 1)))
            calls.append(("sendShowInputStatusReq", [100, event_type, uid]))

        ok = False
        for method, args in calls:
            try:
                await self._call_llbot_debug(bot, "ntMsgApi", method, args)
                ok = True
                if method == "setContactLocalTop":
                    topped = True
            except Exception as exc:
                logger.info(
                    "new_member_forwarder: LLBot debug temp context step %s soft-failed for user %s in group %s: %s",
                    method,
                    user_id,
                    group_id,
                    self._short_error(exc),
                )
        if topped:
            try:
                await self._call_llbot_debug(bot, "ntMsgApi", "setContactLocalTop", [peer, False])
            except Exception:
                pass

        delay = max(0.0, self._get_float("llbot_debug_temp_context_delay_seconds", 0.8))
        if delay:
            await asyncio.sleep(delay)
        if ok:
            logger.info(
                "new_member_forwarder: activated LLBot temp private context for user %s in group %s via debug api.",
                user_id,
                group_id,
            )
        return ok

    async def _call_llbot_debug(self, bot: Any, api_class: str, method: str, args: list[Any]) -> Any:
        return await bot.call_action(
            "llonebot_debug",
            apiClass=api_class,
            method=method,
            args=args,
        )

    async def _check_group_member_list(
        self,
        bot: Any,
        group_id: str,
        user_id: str,
        self_id: str = "",
    ) -> bool | str:
        if not self._get_bool("validate_group_member_before_send", True):
            return True
        group_id = self._string(group_id)
        user_id = self._string(user_id)
        if not group_id.isdigit() or not user_id.isdigit():
            return True
        try:
            members = await bot.call_action(
                "get_group_member_list",
                group_id=int(group_id),
                no_cache=True,
                **self._routing_kwargs(self_id),
            )
        except Exception as exc:
            logger.warning(
                "new_member_forwarder: failed to validate group member list for user %s in group %s: %s",
                user_id,
                group_id,
                exc,
            )
            return self._short_error(exc)
        if not isinstance(members, list):
            return True
        return any(self._member_user_id(member) == user_id for member in members)

    async def _add_image_reply_asset_command(self, event: AstrMessageEvent, rule_kind: str):
        sender_id = self._string(event.get_sender_id())
        if not sender_id or not self._is_admin(sender_id):
            return
        if self._event_group_id(event):
            yield event.plain_result("请私聊我添加图片回复。")
            return

        image_segments = self._image_segments_from_event(event)
        if image_segments:
            asset = await self._extract_image_reply_asset(image_segments)
            if not asset:
                yield event.plain_result("这条图片保存失败，请重新发送图片。")
                return
            self._image_reply_recording_sessions.pop(sender_id, None)
            self._save_image_reply_asset(rule_kind, asset)
            yield event.plain_result(f"已添加{self._image_rule_label(rule_kind)}回复图片。")
            return

        self._image_reply_recording_sessions[sender_id] = {
            "rule_kind": rule_kind,
            "started_at": int(time.time()),
        }
        yield event.plain_result(
            f"开始添加{self._image_rule_label(rule_kind)}回复图片。请直接把图片发给我，发送“取消”可取消。"
        )

    async def _delete_image_reply_asset_command(self, event: AstrMessageEvent, rule_kind: str):
        sender_id = self._string(event.get_sender_id())
        if not sender_id or not self._is_admin(sender_id):
            return
        assets = self._load_image_reply_assets()
        existed = assets.pop(rule_kind, None) is not None
        self._save_image_reply_assets(assets)
        yield event.plain_result(
            f"已删除{self._image_rule_label(rule_kind)}回复图片。"
            if existed
            else f"当前没有已添加的{self._image_rule_label(rule_kind)}回复图片。"
        )

    async def _handle_private_images(
        self,
        bot: Any,
        user_id: str,
        image_count: int,
        group_id: str = "",
    ) -> None:
        if image_count <= 0:
            return
        wait_seconds = max(0.1, self._get_float("image_wait_seconds", 10.0))
        bucket = self._image_buckets.setdefault(
            user_id,
            {
                "count": 0,
                "started_at": time.time(),
                "group_id": group_id,
                "bot": bot,
            },
        )
        bucket["count"] = int(bucket.get("count") or 0) + image_count
        bucket["group_id"] = group_id
        bucket["bot"] = bot

        if int(bucket["count"]) >= 2:
            self._cancel_image_task(user_id)
            self._image_buckets.pop(user_id, None)
            await self._send_image_rule_reply(bot, user_id, "two", group_id=group_id)
            return

        task = self._image_tasks.get(user_id)
        if not task or task.done():
            self._image_tasks[user_id] = asyncio.create_task(
                self._image_timeout_worker(user_id, wait_seconds)
            )

    async def _image_timeout_worker(self, user_id: str, wait_seconds: float) -> None:
        try:
            await asyncio.sleep(wait_seconds)
            bucket = self._image_buckets.pop(user_id, None)
            if not bucket:
                return
            bot = bucket.get("bot")
            if not bot:
                return
            rule_kind = "two" if int(bucket.get("count") or 0) >= 2 else "one"
            await self._send_image_rule_reply(
                bot,
                user_id,
                rule_kind,
                group_id=self._string(bucket.get("group_id")),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("new_member_forwarder: image reply timeout worker failed: %s", exc)
        finally:
            if self._image_tasks.get(user_id) is asyncio.current_task():
                self._image_tasks.pop(user_id, None)

    def _cancel_image_task(self, user_id: str) -> None:
        task = self._image_tasks.pop(user_id, None)
        if task and not task.done():
            task.cancel()

    async def _send_image_rule_reply(
        self,
        bot: Any,
        user_id: str,
        rule_kind: str,
        *,
        group_id: str = "",
    ) -> None:
        items = self._image_rule_reply_items(rule_kind)
        if not items:
            return
        delay = max(0.0, self._get_float("image_reply_delay_seconds", 0.3))
        for index, (kind, value) in enumerate(items):
            if delay and index:
                await asyncio.sleep(delay)
            try:
                if kind == "text":
                    await self._send_private_segments(
                        bot,
                        user_id,
                        [{"type": "text", "data": {"text": value}}],
                        group_id=group_id,
                    )
                else:
                    await self._send_private_segments(
                        bot,
                        user_id,
                        [{"type": "image", "data": {"file": value}}],
                        group_id=group_id,
                    )
            except Exception as exc:
                logger.warning(
                    "new_member_forwarder: failed to send %s image reply %s to %s: %s",
                    rule_kind,
                    kind,
                    user_id,
                    exc,
                )

    async def _send_private_segments(
        self,
        bot: Any,
        user_id: str,
        segments: list[dict[str, Any]],
        *,
        group_id: str = "",
        self_id: str = "",
        allow_without_group_retry: bool = True,
    ) -> None:
        payload: dict[str, Any] = {
            "user_id": int(user_id),
            "message": segments,
        }
        payload.update(self._routing_kwargs(self_id))
        await self._call_private_action(
            bot,
            "send_private_msg",
            target_user_id=user_id,
            group_id=group_id,
            allow_without_group_retry=allow_without_group_retry,
            **payload,
        )

    async def _call_private_action(
        self,
        bot: Any,
        action: str,
        *,
        target_user_id: str,
        group_id: str = "",
        allow_without_group_retry: bool = True,
        **params: Any,
    ) -> Any:
        group_id = self._string(group_id)
        if group_id.isdigit():
            logger.info(
                "new_member_forwarder: %s private payload keys=%s user=%s group=%s",
                action,
                sorted([*params.keys(), "group_id"]),
                target_user_id,
                group_id,
            )
            return await bot.call_action(action, group_id=int(group_id), **params)
        logger.info(
            "new_member_forwarder: %s private payload keys=%s user=%s group=-",
            action,
            sorted(params.keys()),
            target_user_id,
        )
        return await bot.call_action(action, **params)

    def _is_friend_required_error(self, exc: Exception) -> bool:
        if isinstance(exc, TempSessionNotReadyError):
            return True
        text = f"{exc}"
        lower = text.lower()
        return (
            "发送失败，请先添加对方为好友" in text
            or "please add" in lower
            or "not a friend" in lower
            or "temporary session" in lower
            or "allow this temporary" in lower
            or (
                "getMsgService().sendMsg" in text
                and "chatType: 100" in text
                and "result: 1" in text
            )
        )

    def _member_user_id(self, member: Any) -> str:
        if not isinstance(member, dict):
            return ""
        for key in ("user_id", "userId", "uin", "qq"):
            value = self._string(member.get(key))
            if value:
                return value
        return ""

    def _member_display_name(self, member: Any) -> str:
        if not isinstance(member, dict):
            return ""
        for key in ("card_or_nickname", "card", "nickname", "nick", "name", "user_name"):
            value = self._string(member.get(key))
            if value:
                return value
        return ""

    async def _get_group_display_name(self, bot: Any, group_id: str, self_id: str = "") -> str:
        group_id = self._string(group_id)
        if not group_id.isdigit():
            return ""
        try:
            info = await bot.call_action(
                "get_group_info",
                group_id=int(group_id),
                no_cache=False,
                **self._routing_kwargs(self_id),
            )
        except Exception:
            return ""
        if not isinstance(info, dict):
            return ""
        for key in ("group_name", "groupName", "name"):
            value = self._string(info.get(key))
            if value:
                return value
        return ""

    def _short_error(self, exc: Exception, limit: int = 120) -> str:
        text = re.sub(r"\s+", " ", f"{exc}").strip()
        if len(text) <= limit:
            return text
        return text[: limit - 3] + "..."

    def _forward_id_from_segment(self, segment: dict[str, Any]) -> str:
        if not isinstance(segment, dict) or segment.get("type") != "forward":
            return ""
        data = segment.get("data") if isinstance(segment.get("data"), dict) else {}
        for key in ("id", "forward_id", "res_id", "message_id"):
            value = self._string(data.get(key))
            if value:
                return value
        return ""

    async def _extract_image_reply_asset(self, image_segments: list[dict[str, Any]]) -> str:
        for index, segment in enumerate(image_segments):
            data = segment.get("data") if isinstance(segment.get("data"), dict) else {}
            image_file = await self._persist_image_segment(data, index, force_save=True)
            if image_file:
                return image_file
        return ""

    def _image_rule_reply_items(self, rule_kind: str) -> list[tuple[str, str]]:
        prefix = "two_image_reply" if rule_kind == "two" else "one_image_reply"
        available: dict[str, str] = {}

        text = self._string(self._get(prefix, ""))
        if text:
            available["text"] = text

        image = self._stored_image_reply_asset(rule_kind)
        if not image and self._get_bool(f"{prefix}_image_enabled", False):
            image = self._first_config_file_value(self._get(f"{prefix}_image", []))
        image = self._config_file_send_source(image)
        if image:
            available["image"] = image

        return self._ordered_text_image_items(
            available,
            self._string(self._get(f"{prefix}_order", "text,image")),
            fallback_order=("text", "image"),
        )

    def _stored_image_reply_asset(self, rule_kind: str) -> str:
        assets = self._load_image_reply_assets()
        item = assets.get(rule_kind)
        if isinstance(item, dict):
            return self._string(item.get("file"))
        return ""

    def _save_image_reply_asset(self, rule_kind: str, asset: str) -> None:
        assets = self._load_image_reply_assets()
        assets[rule_kind] = {
            "file": self._string(asset),
            "updated_at": int(time.time()),
        }
        self._save_image_reply_assets(assets)

    def _load_image_reply_assets(self) -> dict[str, Any]:
        if not self.image_reply_file.exists():
            return {}
        try:
            data = json.loads(self.image_reply_file.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.error("new_member_forwarder: failed to read image reply assets: %s", exc)
            return {}
        if isinstance(data, dict) and isinstance(data.get("assets"), dict):
            return data["assets"]
        return data if isinstance(data, dict) else {}

    def _save_image_reply_assets(self, assets: dict[str, Any]) -> None:
        payload = {
            "version": 1,
            "updated_at": int(time.time()),
            "assets": assets,
        }
        self.image_reply_file.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _image_rule_label(self, rule_kind: str) -> str:
        return "两图" if rule_kind == "two" else "一图"

    def _ordered_text_image_items(
        self,
        available: dict[str, str],
        order_raw: str,
        *,
        fallback_order: tuple[str, str],
    ) -> list[tuple[str, str]]:
        aliases = {
            "text": "text",
            "文字": "text",
            "image": "image",
            "img": "image",
            "图片": "image",
        }
        ordered: list[tuple[str, str]] = []
        used: set[str] = set()
        for key in [item for item in re.split(r"[\s,，]+", order_raw.lower()) if item]:
            kind = aliases.get(key)
            if kind in available and kind not in used:
                ordered.append((kind, available[kind]))
                used.add(kind)
        for kind in fallback_order:
            if kind in available and kind not in used:
                ordered.append((kind, available[kind]))
        return ordered

    def _first_config_file_value(self, value: Any) -> str:
        if isinstance(value, list):
            for item in value:
                result = self._first_config_file_value(item)
                if result:
                    return result
            return ""
        if isinstance(value, dict):
            for key in ("path", "file", "url", "relative_path", "rel_path", "value", "name"):
                result = self._string(value.get(key))
                if result:
                    return result
            return ""
        return self._string(value)

    def _config_file_send_source(self, source: str) -> str:
        source = self._string(source)
        if not source:
            return ""
        if source.startswith(("http://", "https://", "file://", "base64://", "data:")):
            return source
        for path in self._config_file_path_candidates(source):
            if path.exists() and path.is_file():
                return path.resolve().as_uri()
        return source

    def _config_file_path_candidates(self, source: str) -> list[Path]:
        source = self._string(source).replace("\\", "/")
        if not source:
            return []
        raw_path = Path(source)
        if raw_path.is_absolute():
            return [raw_path]
        candidates = [
            self.plugin_dir / raw_path,
            self.data_dir / raw_path,
            self.media_dir / raw_path,
        ]
        if get_astrbot_data_path:
            try:
                data_path = Path(get_astrbot_data_path())
                candidates.append(data_path / "plugin_data" / PLUGIN_NAME / raw_path)
            except Exception:
                pass
        return candidates

    async def _persist_image_segment(self, data: dict[str, Any], index: int, *, force_save: bool = False) -> str:
        if not force_save and not self._get_bool("save_incoming_images", True):
            return self._string(data.get("url") or data.get("file"))

        url = self._string(data.get("url"))
        if url.startswith(("http://", "https://")):
            suffix = self._guess_suffix(url)
            file_path = self.media_dir / f"{int(time.time() * 1000)}_{index}{suffix}"
            try:
                await asyncio.to_thread(self._download_file, url, file_path)
                return file_path.resolve().as_uri()
            except Exception as exc:
                logger.warning("new_member_forwarder: image download failed, keep original url: %s", exc)
                return url

        file_value = self._string(data.get("file"))
        if file_value:
            return file_value
        return url

    def _download_file(self, url: str, file_path: Path) -> None:
        request = urllib.request.Request(url, headers={"User-Agent": "AstrBot-NewMemberForwarder/1.1"})
        with urllib.request.urlopen(request, timeout=30) as response:
            file_path.write_bytes(response.read())

    def _copy_segment(self, segment: Any) -> dict[str, Any] | None:
        if not isinstance(segment, dict):
            return None
        segment_type = self._string(segment.get("type")).lower()
        data = segment.get("data") if isinstance(segment.get("data"), dict) else {}
        if not segment_type or segment_type in {"reply", "forward"}:
            return None
        return {"type": segment_type, "data": dict(data)}

    def _load_recorded_payload(self) -> dict[str, Any]:
        if not self.record_file.exists():
            return {"version": 2, "items": []}
        try:
            data = json.loads(self.record_file.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.error("new_member_forwarder: failed to read recorded material: %s", exc)
            return {"version": 2, "items": []}
        if not isinstance(data, dict):
            return {"version": 2, "items": []}
        if not isinstance(data.get("items"), list):
            data["items"] = []
        return data

    def _save_recorded_payload(self, payload: dict[str, Any]) -> None:
        self.record_file.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _resolve_data_dir(self) -> Path:
        if get_astrbot_data_path:
            try:
                return Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME
            except Exception:
                pass
        return Path.cwd() / "data" / "plugin_data" / PLUGIN_NAME

    def _resolve_config_file(self) -> Path:
        data_path: Path | None = None
        if get_astrbot_data_path:
            try:
                data_path = Path(get_astrbot_data_path())
            except Exception:
                data_path = None
        if data_path is None:
            try:
                data_path = self.data_dir.parent.parent
            except Exception:
                data_path = Path.cwd() / "data"
        return data_path / "config" / f"{PLUGIN_NAME}_config.json"

    def _routing_kwargs(self, self_id: str) -> dict[str, Any]:
        if self_id and self_id.isdigit():
            return {"self_id": int(self_id)}
        return {}

    def _onebot_id_value(self, value: Any) -> int | str:
        text = self._string(value)
        return int(text) if text.isdigit() else text

    def _id_variants(self, value: Any) -> list[int | str]:
        text = self._string(value)
        if not text:
            return []
        values: list[int | str] = []

        def add(item: int | str) -> None:
            if item not in values:
                values.append(item)

        if text.isdigit():
            add(int(text))
        add(text)
        return values

    def _is_group_increase(self, raw: dict[str, Any]) -> bool:
        return raw.get("post_type") == "notice" and raw.get("notice_type") == "group_increase"

    def _is_group_allowed(self, group_id: str) -> bool:
        groups = [self._string(item) for item in self._get_list("allowed_groups")]
        groups = [item for item in groups if item]
        return not groups or group_id in groups

    def _is_duplicate(self, group_id: str, user_id: str) -> bool:
        now = time.time()
        window = max(1.0, self._get_float("dedupe_window_seconds", 30.0))
        key = f"{group_id}:{user_id}"
        last_at = self._recent_events.get(key, 0)
        self._recent_events[key] = now

        old_keys = [item_key for item_key, ts in self._recent_events.items() if now - ts > max(window, 300.0)]
        for item_key in old_keys:
            self._recent_events.pop(item_key, None)
        return now - last_at < window

    def _reserve_delivery_slot(self, group_id: str, user_id: str) -> str | None:
        if not self._get_bool("delivery_limit_enabled", True):
            return ""

        max_deliveries = self._get_int("max_deliveries_per_recipient", 2)
        if max_deliveries <= 0:
            return ""

        key = self._delivery_history_key(group_id, user_id)
        history = self._load_delivery_history()
        if self._cleanup_delivery_history(history):
            self._save_delivery_history(history)

        recipients = history.get("recipients") if isinstance(history.get("recipients"), dict) else {}
        item = recipients.get(key) if isinstance(recipients.get(key), dict) else {}
        sent_count = self._safe_int(item.get("count"), 0)
        pending_count = self._delivery_inflight.get(key, 0)
        if sent_count + pending_count >= max_deliveries:
            logger.info(
                "new_member_forwarder: skip delivery to %s in group %s because delivery limit %s reached; "
                "use /新人欢迎重置发送次数 %s %s for repeated tests.",
                user_id,
                group_id,
                max_deliveries,
                user_id,
                group_id,
            )
            return None

        self._delivery_inflight[key] = pending_count + 1
        return key

    def _release_delivery_slot(self, key: str) -> None:
        pending_count = self._delivery_inflight.get(key, 0)
        if pending_count <= 1:
            self._delivery_inflight.pop(key, None)
            return
        self._delivery_inflight[key] = pending_count - 1

    def _mark_delivery_success(self, group_id: str, user_id: str) -> None:
        history = self._load_delivery_history()
        self._cleanup_delivery_history(history)
        recipients = history.setdefault("recipients", {})
        if not isinstance(recipients, dict):
            recipients = {}
            history["recipients"] = recipients

        now = int(time.time())
        key = self._delivery_history_key(group_id, user_id)
        item = recipients.get(key) if isinstance(recipients.get(key), dict) else {}
        if "first_at" not in item:
            item["first_at"] = now
        item["count"] = self._safe_int(item.get("count"), 0) + 1
        item["updated_at"] = now
        item["last_group_id"] = group_id
        item["last_user_id"] = user_id
        recipients[key] = item
        history["updated_at"] = now
        self._save_delivery_history(history)

    async def _queue_pending_delivery(
        self,
        user_id: str,
        group_id: str,
        self_id: str = "",
        reason: str = "",
    ) -> bool:
        return False
        if not self._get_bool("pending_delivery_enabled", True):
            return False
        user_id = self._string(user_id)
        group_id = self._string(group_id)
        if not user_id:
            return False

        pending = self._load_pending_deliveries()
        self._cleanup_pending_deliveries(pending)
        recipients = pending.setdefault("recipients", {})
        if not isinstance(recipients, dict):
            recipients = {}
            pending["recipients"] = recipients

        now = int(time.time())
        old_item = recipients.get(user_id) if isinstance(recipients.get(user_id), dict) else {}
        recipients[user_id] = {
            "user_id": user_id,
            "group_id": group_id,
            "self_id": self._string(self_id),
            "reason": self._string(reason),
            "created_at": self._safe_int(old_item.get("created_at"), now),
            "updated_at": now,
            "attempts": self._safe_int(old_item.get("attempts"), 0),
        }
        self._save_pending_deliveries(pending)
        logger.info(
            "new_member_forwarder: queued pending private delivery for user %s in group %s.",
            user_id,
            group_id or "-",
        )
        return True

    async def _try_deliver_pending_private(self, bot: Any, user_id: str, self_id: str = "") -> bool:
        return False
        if not self._get_bool("pending_delivery_enabled", True):
            return False
        user_id = self._string(user_id)
        if not user_id:
            return False

        pending = self._load_pending_deliveries()
        changed = self._cleanup_pending_deliveries(pending)
        recipients = pending.get("recipients") if isinstance(pending.get("recipients"), dict) else {}
        item = recipients.get(user_id) if isinstance(recipients.get(user_id), dict) else None
        if not item:
            if changed:
                self._save_pending_deliveries(pending)
            return False

        payload = self._load_recorded_payload()
        items = payload.get("items") or []
        if not items:
            recipients.pop(user_id, None)
            self._save_pending_deliveries(pending)
            logger.warning("new_member_forwarder: removed pending delivery for user %s because no material is saved.", user_id)
            return False

        group_id = self._string(item.get("group_id"))
        recipients.pop(user_id, None)
        self._save_pending_deliveries(pending)

        delivery_slot_key = self._reserve_delivery_slot(group_id, user_id)
        if delivery_slot_key is None:
            logger.info(
                "new_member_forwarder: dropped pending delivery for user %s in group %s because delivery limit is reached.",
                user_id,
                group_id or "-",
            )
            return True

        try:
            # LLBot still needs the source group_id for non-friend temporary sessions.
            await self._deliver_private_with_retries(
                bot,
                user_id,
                items,
                self_id,
                group_id,
                skip_warmup=True,
            )
            self._mark_delivery_success(group_id, user_id)
            logger.info(
                "new_member_forwarder: sent pending private delivery to user %s from group %s.",
                user_id,
                group_id or "-",
            )
            return True
        except Exception as exc:
            self._requeue_pending_delivery_item(user_id, item, self_id, exc)
            logger.warning(
                "new_member_forwarder: pending private delivery to user %s failed and was requeued: %s",
                user_id,
                self._short_error(exc),
            )
            return False
        finally:
            if delivery_slot_key:
                self._release_delivery_slot(delivery_slot_key)

    async def _send_group_pending_notice(
        self,
        bot: Any,
        group_id: str,
        user_id: str,
        self_id: str = "",
    ) -> bool:
        return False
        if not self._get_bool("pending_delivery_notice_enabled", True):
            return False
        group_id = self._string(group_id)
        user_id = self._string(user_id)
        if not group_id or not user_id:
            return False

        text = self._string(
            self._get(
                "pending_delivery_notice_text",
                "欢迎进群，请先私聊我一下，我把资料发给你。",
            )
        )
        if not text:
            return False
        try:
            text = text.format(user_id=user_id, group_id=group_id)
        except Exception:
            pass

        message: list[dict[str, Any]] = []
        if self._get_bool("pending_delivery_notice_at_user", True):
            message.append({"type": "at", "data": {"qq": self._onebot_id_value(user_id)}})
            if not text.startswith((" ", "\n")):
                text = " " + text
        message.append({"type": "text", "data": {"text": text}})

        try:
            await bot.call_action(
                "send_group_msg",
                group_id=self._onebot_id_value(group_id),
                message=message,
                **self._routing_kwargs(self_id),
            )
            logger.info(
                "new_member_forwarder: sent pending notice to group %s for user %s.",
                group_id,
                user_id,
            )
            return True
        except Exception as exc:
            logger.warning(
                "new_member_forwarder: failed to send pending notice to group %s for user %s: %s",
                group_id,
                user_id,
                exc,
            )
            return False

    def _requeue_pending_delivery_item(
        self,
        user_id: str,
        item: dict[str, Any],
        self_id: str,
        exc: Exception,
    ) -> None:
        pending = self._load_pending_deliveries()
        self._cleanup_pending_deliveries(pending)
        recipients = pending.setdefault("recipients", {})
        if not isinstance(recipients, dict):
            recipients = {}
            pending["recipients"] = recipients

        next_item = dict(item)
        next_item["user_id"] = user_id
        next_item["self_id"] = self._string(self_id or next_item.get("self_id"))
        next_item["updated_at"] = int(time.time())
        next_item["attempts"] = self._safe_int(next_item.get("attempts"), 0) + 1
        next_item["last_error"] = self._short_error(exc, 200)
        recipients[user_id] = next_item
        self._save_pending_deliveries(pending)

    def _load_pending_deliveries(self) -> dict[str, Any]:
        if not self.pending_file.exists():
            return {"version": 1, "recipients": {}}
        try:
            data = json.loads(self.pending_file.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.error("new_member_forwarder: failed to read pending deliveries: %s", exc)
            return {"version": 1, "recipients": {}}
        if not isinstance(data, dict):
            return {"version": 1, "recipients": {}}
        if not isinstance(data.get("recipients"), dict):
            data["recipients"] = {}
        data.setdefault("version", 1)
        return data

    def _save_pending_deliveries(self, payload: dict[str, Any]) -> None:
        payload["version"] = 1
        payload["updated_at"] = int(time.time())
        self.pending_file.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _cleanup_pending_deliveries(self, payload: dict[str, Any]) -> bool:
        recipients = payload.get("recipients") if isinstance(payload.get("recipients"), dict) else {}
        if not recipients:
            return False
        expire_seconds = self._get_float("pending_expire_seconds", 86400.0)
        if expire_seconds <= 0:
            return False
        cutoff = time.time() - expire_seconds
        changed = False
        for user_id, item in list(recipients.items()):
            if not isinstance(item, dict):
                recipients.pop(user_id, None)
                changed = True
                continue
            created_at = self._safe_float(item.get("created_at"), 0.0)
            if created_at and created_at < cutoff:
                recipients.pop(user_id, None)
                changed = True
        return changed

    def _is_pending_private_consumed(self, user_id: str) -> bool:
        return False

    def _stop_event(self, event: AstrMessageEvent) -> None:
        stopper = getattr(event, "stop_event", None)
        if not callable(stopper):
            return
        try:
            stopper()
        except Exception:
            pass

    def _delivery_history_key(self, group_id: str, user_id: str) -> str:
        scope = self._string(self._get("delivery_limit_scope", "user")).lower()
        if scope in {"user_group", "group_user", "group", "群", "按群", "按群分别统计", "按qq和群分别统计", "按qq+群分别统计"}:
            return f"{group_id}:{user_id}"
        return user_id

    def _load_delivery_history(self) -> dict[str, Any]:
        if not self.delivery_history_file.exists():
            return {"version": 1, "recipients": {}}
        try:
            data = json.loads(self.delivery_history_file.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.error("new_member_forwarder: failed to read delivery history: %s", exc)
            return {"version": 1, "recipients": {}}
        if not isinstance(data, dict):
            return {"version": 1, "recipients": {}}
        if not isinstance(data.get("recipients"), dict):
            data["recipients"] = {}
        data.setdefault("version", 1)
        return data

    def _save_delivery_history(self, payload: dict[str, Any]) -> None:
        payload["version"] = 1
        payload["updated_at"] = int(time.time())
        payload.setdefault("recipients", {})
        self.delivery_history_file.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _reset_delivery_history_for(self, group_id: str, user_id: str) -> int:
        user_id = self._string(user_id)
        group_id = self._string(group_id)
        history = self._load_delivery_history()
        recipients = history.get("recipients") if isinstance(history.get("recipients"), dict) else {}
        if not user_id or not isinstance(recipients, dict):
            return 0

        keys = {user_id, self._delivery_history_key(group_id, user_id)}
        for key in list(recipients.keys()):
            if self._string(key).endswith(f":{user_id}"):
                keys.add(key)

        removed = 0
        for key in keys:
            if key in recipients:
                recipients.pop(key, None)
                removed += 1
        if removed:
            history["recipients"] = recipients
            self._save_delivery_history(history)
        return removed

    def _cleanup_delivery_history(self, payload: dict[str, Any]) -> bool:
        expire_days = self._get_float("delivery_history_expire_days", 0.0)
        if expire_days <= 0:
            return False
        recipients = payload.get("recipients")
        if not isinstance(recipients, dict):
            return False

        cutoff = time.time() - expire_days * 86400
        changed = False
        for key, item in list(recipients.items()):
            if not isinstance(item, dict):
                recipients.pop(key, None)
                changed = True
                continue
            updated_at = self._safe_float(item.get("updated_at"), 0.0)
            if updated_at and updated_at < cutoff:
                recipients.pop(key, None)
                changed = True
        return changed

    def _is_admin(self, user_id: str) -> bool:
        admins = [self._string(item) for item in self._get_list("admin_user_ids")]
        admins = [item for item in admins if item]
        return bool(user_id and user_id in admins)

    def _is_self_sender(self, event: AstrMessageEvent) -> bool:
        sender_id = self._string(event.get_sender_id())
        self_id = self._string(event.get_self_id())
        return bool(sender_id and self_id and sender_id == self_id)

    def _can_run_admin_or_self_command(self, event: AstrMessageEvent) -> bool:
        sender_id = self._string(event.get_sender_id())
        return self._is_admin(sender_id) or self._is_self_sender(event)

    def _parse_self_test_command(self, text: str) -> tuple[str, str] | None:
        text = self._normalize_control_text(text)
        command = "新人欢迎测试"
        if text == command:
            return "", ""
        if not text.startswith(command):
            return None
        remainder = text[len(command) :]
        if remainder and not remainder[0].isspace():
            return None
        args = [item for item in re.split(r"\s+", remainder.strip()) if item]
        target_qq = args[0] if len(args) >= 1 else ""
        source_group_id = args[1] if len(args) >= 2 else ""
        return target_qq, source_group_id

    def _result_plain_text(self, event: AstrMessageEvent) -> str:
        result = event.get_result()
        if result is None:
            return ""
        getter = getattr(result, "get_plain_text", None)
        if callable(getter):
            try:
                return self._string(getter()).strip()
            except Exception:
                pass

        parts: list[str] = []
        for component in getattr(result, "chain", []) or []:
            text = getattr(component, "text", None)
            if text:
                parts.append(self._string(text))
        return " ".join(parts).strip()

    def _raw_event(self, event: AstrMessageEvent) -> dict[str, Any]:
        raw = getattr(event.message_obj, "raw_message", None)
        return raw if isinstance(raw, dict) else {}

    def _message_id_from_event(self, event: AstrMessageEvent) -> str:
        message_obj = getattr(event, "message_obj", None)
        for obj in (message_obj, event):
            if obj is None:
                continue
            for key in ("message_id", "id"):
                value = getattr(obj, key, None)
                if value:
                    return self._string(value)
        raw = self._raw_event(event)
        for key in ("message_id", "message_seq", "real_id", "id"):
            value = raw.get(key)
            if value:
                return self._string(value)
        return ""

    def _event_group_id(self, event: AstrMessageEvent) -> str:
        try:
            group_id = event.get_group_id()
            if group_id:
                return self._string(group_id)
        except Exception:
            pass
        return self._string(self._raw_event(event).get("group_id"))

    def _event_segments(self, event: AstrMessageEvent) -> list[dict[str, Any]]:
        raw_segments = self._raw_event(event).get("message")
        if isinstance(raw_segments, list):
            return [segment for segment in raw_segments if isinstance(segment, dict)]

        segments: list[dict[str, Any]] = []
        for component in event.get_messages():
            segment: Any = None
            if hasattr(component, "toDict"):
                try:
                    segment = component.toDict()
                except Exception:
                    segment = None
            if not isinstance(segment, dict) and type(component).__name__.lower() == "image":
                data = {
                    "file": getattr(component, "file", ""),
                    "url": getattr(component, "url", ""),
                    "path": getattr(component, "path", ""),
                }
                segment = {"type": "image", "data": data}
            if isinstance(segment, dict):
                segments.append(segment)
        return segments

    def _image_segments_from_event(self, event: AstrMessageEvent) -> list[dict[str, Any]]:
        segments: list[dict[str, Any]] = []
        for segment in self._event_segments(event):
            if self._string(segment.get("type")).lower() != "image":
                continue
            data = segment.get("data") if isinstance(segment.get("data"), dict) else {}
            segments.append({"type": "image", "data": dict(data)})
        return segments

    def _is_private_control_text(self, text: str) -> bool:
        text = self._string(text)
        if not text:
            return False
        return (
            text in self._start_words()
            or text in self._end_words()
            or text in self._cancel_words()
            or text in self._status_words()
            or text in self._clear_words()
            or self._is_image_reply_command_text(text)
        )

    def _is_image_reply_command_text(self, text: str) -> bool:
        command = self._string(text).split(maxsplit=1)[0]
        return command in {
            "添加一图回复图片",
            "设置一图回复图片",
            "录入一图回复图片",
            "添加两图回复图片",
            "设置两图回复图片",
            "录入两图回复图片",
            "删除一图回复图片",
            "移除一图回复图片",
            "删除两图回复图片",
            "移除两图回复图片",
        }

    def _normalize_control_text(self, text: str) -> str:
        text = self._string(text)
        for prefix in ("/", "／", "#", "＃", "!", "！"):
            if text.startswith(prefix):
                text = text[len(prefix) :].strip()
        return text

    def _start_words(self) -> set[str]:
        return {"开始", "开始录制", "新人欢迎开始", "录制新人资料"}

    def _end_words(self) -> set[str]:
        return {"结束", "保存", "结束录制", "新人欢迎结束"}

    def _cancel_words(self) -> set[str]:
        return {"取消", "取消录制", "取消添加", "新人欢迎取消"}

    def _status_words(self) -> set[str]:
        return {"状态", "录制状态", "新人欢迎状态"}

    def _clear_words(self) -> set[str]:
        return {"清空", "清空资料", "新人欢迎清空"}

    def _guess_suffix(self, url: str) -> str:
        path = urllib.parse.urlparse(url).path
        suffix = Path(path).suffix.lower()
        if suffix in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}:
            return suffix
        return ".jpg"

    def _load_file_config(self) -> dict[str, Any]:
        try:
            stat = self.config_file.stat()
        except FileNotFoundError:
            self._config_file_cache = {}
            self._config_file_mtime = None
            return {}
        except Exception:
            return self._config_file_cache

        mtime = stat.st_mtime
        if self._config_file_mtime == mtime:
            return self._config_file_cache
        try:
            data = json.loads(self.config_file.read_text(encoding="utf-8-sig"))
        except Exception as exc:
            logger.warning("new_member_forwarder: failed to read config file %s: %s", self.config_file, exc)
            return self._config_file_cache
        if not isinstance(data, dict):
            data = {}
        self._config_file_cache = data
        self._config_file_mtime = mtime
        return data

    def _get(self, key: str, default: Any = None) -> Any:
        file_config = self._load_file_config()
        if key in file_config:
            return file_config.get(key, default)
        if hasattr(self.config, "get"):
            try:
                return self.config.get(key, default)
            except TypeError:
                pass
        try:
            return self.config[key]
        except Exception:
            return default

    def _get_bool(self, key: str, default: bool = False) -> bool:
        value = self._get(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on", "是", "开启"}
        return bool(value)

    def _get_float(self, key: str, default: float = 0.0) -> float:
        value = self._get(key, default)
        return self._safe_float(value, default)

    def _get_float_list(self, key: str, default: list[float]) -> list[float]:
        values = self._list_from_any(self._get(key, default))
        result: list[float] = []
        for value in values:
            if isinstance(value, str):
                parts = [part.strip() for part in re.split(r"[\s,，]+", value) if part.strip()]
            else:
                parts = [value]
            for part in parts:
                parsed = self._safe_float(part, -1.0)
                if parsed >= 0:
                    result.append(parsed)
        return result

    def _get_int(self, key: str, default: int = 0) -> int:
        value = self._get(key, default)
        return self._safe_int(value, default)

    def _safe_float(self, value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _safe_int(self, value: Any, default: int = 0) -> int:
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default

    def _get_list(self, key: str) -> list[Any]:
        return self._list_from_any(self._get(key, []))

    def _list_from_any(self, value: Any) -> list[Any]:
        if value is None:
            return []
        if isinstance(value, list):
            return value
        if isinstance(value, tuple):
            return list(value)
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return []
            try:
                parsed = json.loads(text)
                if isinstance(parsed, list):
                    return parsed
            except json.JSONDecodeError:
                pass
            return [line.strip() for line in text.splitlines() if line.strip()]
        return [value]

    def _string(self, value: Any) -> str:
        if value is None:
            return ""
        return str(value).strip()

    async def terminate(self):
        for task in self._image_tasks.values():
            task.cancel()
        self._image_tasks.clear()
        self._image_buckets.clear()
        logger.info("new_member_forwarder plugin terminated.")
