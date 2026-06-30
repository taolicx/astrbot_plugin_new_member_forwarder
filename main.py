import asyncio
import base64
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
    "1.4.20",
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
        self._pending_private_consumed_until: dict[str, float] = {}
        self._qq_protocol_warmup_last_at: dict[str, float] = {}
        self._qq_desktop_warmup_last_at: dict[str, float] = {}
        self._qq_desktop_warmup_sent_at: dict[str, float] = {}
        self._test_delivery_running_until = 0.0
        self.data_dir = self._resolve_data_dir()
        self.media_dir = self.data_dir / "media"
        self.record_file = self.data_dir / "recorded_material.json"
        self.image_reply_file = self.data_dir / "image_reply_assets.json"
        self.delivery_history_file = self.data_dir / "delivery_history.json"
        self.pending_file = self.data_dir / "pending_deliveries.json"
        self.warmup_source_file = self.data_dir / "warmup_source.json"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.media_dir.mkdir(parents=True, exist_ok=True)

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
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE, priority=1000)
    async def private_pending_delivery(self, event: AstrMessageEvent):
        if not self._get_bool("enabled", True):
            return

        sender_id = self._string(event.get_sender_id())
        if not sender_id:
            return

        text = self._normalize_control_text(event.get_message_str())
        if self._is_admin(sender_id) and (
            sender_id in self._recording_sessions
            or sender_id in self._image_reply_recording_sessions
            or self._is_private_control_text(text)
        ):
            return

        bot = getattr(event, "bot", None)
        if not bot:
            logger.warning("new_member_forwarder: pending private delivery skipped because event has no bot instance.")
            return

        if await self._try_deliver_pending_private(bot, sender_id, self._string(event.get_self_id())):
            self._pending_private_consumed_until[sender_id] = time.time() + 10.0
            self._stop_event(event)

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
                if isinstance(exc, TempSessionNotReadyError):
                    logger.warning(
                        "new_member_forwarder: LLBot has not recognized user %s as a member of group %s; "
                        "private delivery was not counted.",
                        user_id,
                        group_id,
                    )
                    if await self._queue_pending_delivery(user_id, group_id, self_id, "temp_session_not_ready"):
                        await self._send_group_pending_notice(bot, group_id, user_id, self_id)
                    return
                if self._is_friend_required_error(exc):
                    logger.warning(
                        "new_member_forwarder: QQ refused private delivery to %s in group %s: "
                        "temporary private session was rejected by QQ/LLBot. "
                        "Original forward card was not rebuilt, and this delivery was not counted.",
                        user_id,
                        group_id,
                    )
                    if await self._queue_pending_delivery(user_id, group_id, self_id, "temp_session_rejected"):
                        await self._send_group_pending_notice(bot, group_id, user_id, self_id)
                else:
                    logger.exception("new_member_forwarder: failed to deliver welcome material: %s", exc)
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

    async def _run_test_delivery(self, event: AstrMessageEvent, target_qq: str = "", source_group_id: str = "") -> str:
        user_id = self._string(target_qq or event.get_sender_id())
        group_id = self._string(source_group_id or event.get_group_id())
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
            if isinstance(exc, TempSessionNotReadyError):
                return (
                    f"测试发送失败：LLBot 当前没有在群 {group_id or '未知'} 的成员列表里确认 QQ {user_id}。\n"
                    "处理：确认这是机器人所在的来源群，或等 QQ/LLBot 群成员同步后再测。"
                )
            if self._is_friend_required_error(exc):
                logger.warning(
                    "new_member_forwarder: QQ refused test private delivery to %s from group %s: temporary private session rejected.",
                    user_id,
                    group_id,
                )
                return (
                    f"测试发送失败：QQ/LLBot 拒绝给 QQ {user_id} 发群临时私聊。\n"
                    "处理：确认命令里带的是机器人和对方共同所在的来源群号，并确认对方允许群临时会话。"
                )
            logger.exception("new_member_forwarder: test delivery failed: %s", exc)
            return f"测试发送失败：{exc}"
        finally:
            self._test_delivery_running_until = time.time() + 5.0

        return f"已私聊 QQ {user_id} 执行一次测试发送。"

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.command("新人欢迎开路测试")
    async def test_warmup_delivery(self, event: AstrMessageEvent, target_qq: str = "", source_group_id: str = ""):
        if not self._is_admin(self._string(event.get_sender_id())):
            return

        user_id = self._string(target_qq or event.get_sender_id())
        group_id = self._string(source_group_id or event.get_group_id())
        text = self._string(self._get("forward_warmup_message_text", "欢迎进群")).strip()
        if not text:
            yield event.plain_result("开路消息为空，请先在后台设置 forward_warmup_message_text。")
            return
        if not user_id.isdigit() or not group_id.isdigit():
            yield event.plain_result("用法：/新人欢迎开路测试 QQ号 来源群号")
            return

        bot = getattr(event, "bot", None)
        if not bot:
            yield event.plain_result("当前事件没有 OneBot bot 实例，无法测试发送。")
            return

        self_id = self._string(event.get_self_id())
        try:
            await self._wait_private_context_ready(bot, group_id, user_id, self_id)
            await self._send_plain_warmup_message_with_retries(bot, user_id, self_id, group_id)
        except Exception as exc:
            if isinstance(exc, TempSessionNotReadyError):
                yield event.plain_result(
                    f"开路消息失败：LLBot 当前没有在群 {group_id} 的成员列表里确认 QQ {user_id}。"
                )
                return
            if self._is_friend_required_error(exc):
                yield event.plain_result(
                    f"开路消息失败：QQ/LLBot 拒绝给 QQ {user_id} 发群临时私聊。"
                )
                return
            logger.exception("new_member_forwarder: warmup-only test failed: %s", exc)
            yield event.plain_result(f"开路消息失败：{self._short_error(exc)}")
            return

        yield event.plain_result(f"已发送开路消息给 QQ {user_id}，来源群 {group_id}。")

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.command("新人欢迎诊断")
    async def diagnose_target(self, event: AstrMessageEvent, target_qq: str = "", source_group_id: str = ""):
        if not self._is_admin(self._string(event.get_sender_id())):
            return

        user_id = self._string(target_qq or event.get_sender_id())
        group_id = self._string(source_group_id or event.get_group_id())
        bot = getattr(event, "bot", None)
        if not bot:
            yield event.plain_result("当前事件没有 OneBot bot 实例，无法诊断。")
            return
        if not user_id.isdigit() or not group_id.isdigit():
            yield event.plain_result("用法：/新人欢迎诊断 QQ号 来源群号")
            return

        self_id = self._string(event.get_self_id())
        lines = [f"诊断 QQ：{user_id}", f"来源群：{group_id}"]

        try:
            member_info = await bot.call_action(
                "get_group_member_info",
                group_id=int(group_id),
                user_id=int(user_id),
                no_cache=True,
                **self._routing_kwargs(self_id),
            )
            info_user_id = self._member_user_id(member_info)
            lines.append(f"成员详情：成功{f'，返回 QQ {info_user_id}' if info_user_id else ''}")
        except Exception as exc:
            lines.append(f"成员详情：失败，{self._short_error(exc)}")

        list_status = await self._check_group_member_list(bot, group_id, user_id, self_id)
        if list_status is True:
            lines.append("成员列表：已找到该 QQ")
            lines.append("结论：LLBot 已认到群成员；如果仍发不出，就是 QQ/LLBot 临时会话发送被底层拒绝。")
        elif list_status is False:
            lines.append("成员列表：没有找到该 QQ")
            lines.append("结论：LLBot 当前不认为这个 QQ 是该来源群成员，群临时私聊会被拒绝。")
        else:
            lines.append(f"成员列表：检查失败，{list_status}")
            lines.append("结论：无法确认成员列表；可以先按原链路测试发送。")

        yield event.plain_result("\n".join(lines))

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
        gap = max(0.0, self._get_float("message_gap_seconds", 0.8))
        for item in items:
            if not isinstance(item, dict):
                continue
            kind = self._string(item.get("kind"))
            if kind in {"forward_id", "forward"} and not self._forward_item_has_reference(item):
                raise RuntimeError("recorded forward item is old format and has no source_message_id; please re-record it")

        if self._should_send_warmup_message(items):
            await self._send_plain_warmup_message(bot, user_id, self_id, group_id)

        for item in items:
            if not isinstance(item, dict):
                continue
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
                    await asyncio.sleep(gap)
            elif kind in {"forward_id", "forward"}:
                if self._forward_item_has_reference(item):
                    await self._send_recorded_forward(bot, user_id, item, self_id, group_id)
                    await asyncio.sleep(gap)

    async def _deliver_private_with_retries(
        self,
        bot: Any,
        user_id: str,
        items: list[dict[str, Any]],
        self_id: str = "",
        group_id: str = "",
        *,
        skip_warmup: bool = False,
    ) -> None:
        gap = max(0.0, self._get_float("message_gap_seconds", 0.8))
        for item in items:
            if not isinstance(item, dict):
                continue
            kind = self._string(item.get("kind"))
            if kind in {"forward_id", "forward"} and not self._forward_item_has_reference(item):
                raise RuntimeError("recorded forward item is old format and has no source_message_id; please re-record it")

        if self._string(group_id):
            await self._wait_private_context_ready(bot, group_id, user_id, self_id)
        desktop_warmup_sent = self._recent_desktop_warmup_sent(group_id, user_id)
        if not skip_warmup and self._should_send_warmup_message(items) and not desktop_warmup_sent:
            try:
                await self._send_plain_warmup_message(bot, user_id, self_id, group_id)
            except Exception as exc:
                logger.warning(
                    "new_member_forwarder: warmup message failed once for user %s in group %s; "
                    "continue to recorded material: %s",
                    user_id,
                    group_id or "-",
                    self._short_error(exc),
                )
        elif desktop_warmup_sent:
            delay = max(0.0, self._get_float("forward_warmup_delay_seconds", 1.0))
            if delay:
                await asyncio.sleep(delay)

        retry_delays = self._get_float_list("temp_session_retry_delays_seconds", [3.0, 8.0])
        for item in items:
            if not isinstance(item, dict):
                continue
            attempt = 0
            while True:
                try:
                    sent = await self._deliver_private_item(bot, user_id, item, self_id, group_id)
                    if sent:
                        await asyncio.sleep(gap)
                    break
                except Exception as exc:
                    if not self._is_friend_required_error(exc) or attempt >= len(retry_delays):
                        raise

                    delay = max(0.0, retry_delays[attempt])
                    attempt += 1
                    logger.warning(
                        "new_member_forwarder: temporary private session for user %s in group %s is not ready; "
                        "retry current item %s/%s after %.1f seconds.",
                        user_id,
                        group_id,
                        attempt,
                        len(retry_delays),
                        delay,
                    )
                    if delay:
                        await asyncio.sleep(delay)
                    await self._prepare_private_context(bot, group_id, user_id, self_id)

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
            if self._forward_item_has_reference(item):
                await self._send_recorded_forward(bot, user_id, item, self_id, group_id)
                return True
        return False

    def _forward_item_has_reference(self, item: dict[str, Any]) -> bool:
        return bool(self._forward_item_source_message_id(item))

    def _forward_item_source_message_id(self, item: dict[str, Any]) -> str:
        return self._string(item.get("source_message_id") or item.get("message_id"))

    async def _send_recorded_forward(
        self,
        bot: Any,
        user_id: str,
        item: dict[str, Any],
        self_id: str,
        group_id: str,
    ) -> None:
        source_message_id = self._forward_item_source_message_id(item)
        if not source_message_id:
            raise RuntimeError("recorded forward item has no source_message_id; please re-record it")

        await self._send_source_node_forward(
            bot,
            user_id,
            source_message_id,
            self_id,
            group_id,
            source_kind="source_message_id",
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
        if not self._get_bool("prepare_temp_session_before_send", True):
            return True
        group_id = self._string(group_id)
        user_id = self._string(user_id)
        if not group_id.isdigit() or not user_id.isdigit():
            return True
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
            return False

        list_status = await self._check_group_member_list(bot, group_id, user_id, self_id)
        if list_status is False:
            return False
        llbot_activated = await self._activate_llbot_temp_context(bot, group_id, user_id)
        opened = await self._open_qq_profile_context(group_id, user_id)
        if llbot_activated or opened:
            await self._send_qq_desktop_warmup_message(
                group_id,
                user_id,
                self._member_display_name(member_info),
            )
        return True

    async def _open_qq_profile_context(self, group_id: str, user_id: str) -> bool:
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

    async def _send_qq_desktop_warmup_message(
        self,
        group_id: str,
        user_id: str,
        target_name: str = "",
    ) -> bool:
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

    def _run_qq_desktop_warmup_script(
        self,
        group_id: str,
        user_id: str,
        target_name: str,
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
        encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
        command = [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-STA",
            "-EncodedCommand",
            encoded,
        ]
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
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
            try:
                logger.info(
                    "new_member_forwarder: %s private payload keys=%s user=%s group=%s",
                    action,
                    sorted([*params.keys(), "group_id"]),
                    target_user_id,
                    group_id,
                )
                return await bot.call_action(action, group_id=int(group_id), **params)
            except Exception as exc:
                if self._is_friend_required_error(exc):
                    raise
                if not allow_without_group_retry:
                    logger.warning(
                        "new_member_forwarder: %s with source group failed and fallback without group_id is disabled: %s",
                        action,
                        self._short_error(exc),
                    )
                    raise
                logger.info(
                    "new_member_forwarder: %s with source group failed; retrying without group_id: %s",
                    action,
                    exc,
                )
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
                "new_member_forwarder: skip delivery to %s in group %s because limit %s reached.",
                user_id,
                group_id,
                max_deliveries,
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
        now = time.time()
        for key, until in list(self._pending_private_consumed_until.items()):
            if until <= now:
                self._pending_private_consumed_until.pop(key, None)
        return self._pending_private_consumed_until.get(user_id, 0.0) > now

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
        if scope in {"user_group", "group_user", "group", "群", "按群"}:
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
        return not admins or user_id in admins

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
        for prefix in ("/", "／", "!", "！"):
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

    def _get(self, key: str, default: Any = None) -> Any:
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
