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
MAX_IMAGE_DOWNLOAD_BYTES = 20 * 1024 * 1024


@register(
    PLUGIN_NAME,
    "Codex",
    "管理员私聊录制新人入群资料，新人进群时自动私聊转发文字、图片和聊天记录。",
    "1.4.62",
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
        self._qq_human_group_warmup_sent_at: dict[str, float] = {}
        self._qq_human_group_warmup_last_at: dict[str, float] = {}
        self._qq_human_group_warmup_results: dict[str, dict[str, Any]] = {}
        self._test_delivery_running_until = 0.0
        self.data_dir = self._resolve_data_dir()
        self.media_dir = self.data_dir / "media"
        self.record_file = self.data_dir / "recorded_material.json"
        self.image_reply_file = self.data_dir / "image_reply_assets.json"
        self.delivery_history_file = self.data_dir / "delivery_history.json"
        self.human_calibration_file = self.data_dir / "qq_human_calibration.json"
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
            yield event.plain_result("真人开路消息为空，请先在后台设置“真人开路文字”。")
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
    @filter.command("新人欢迎校准", alias={"新人欢迎QQ校准", "新人欢迎坐标校准"})
    async def calibrate_human_group_warmup(self, event: AstrMessageEvent, target_qq: str = ""):
        if not self._is_admin(self._string(event.get_sender_id())):
            return

        user_id = self._string(target_qq)
        if not user_id.isdigit():
            yield event.plain_result(
                "用法：/新人欢迎校准 QQ号\n"
                "先把 QQ 打开到目标群页面，然后按屏幕提示把鼠标移到指定位置并按 F8。"
            )
            return

        yield event.plain_result(
            "开始 QQ 坐标校准。\n"
            "请看电脑桌面的校准提示窗：把鼠标放到目标位置后按 F8，按 ESC 可取消。"
        )
        timeout = max(60.0, self._get_float("qq_human_group_warmup_calibration_timeout_seconds", 300.0))
        try:
            result = await asyncio.to_thread(self._run_qq_human_group_warmup_calibration_script, user_id, timeout)
        except Exception as exc:
            logger.exception("new_member_forwarder: human warmup calibration failed: %s", exc)
            yield event.plain_result(f"校准失败：{self._short_error(exc, 220)}")
            return

        if result.get("ok"):
            yield event.plain_result(
                "校准完成。\n"
                f"文件：{self.human_calibration_file}\n"
                "之后真人开路会优先使用校准坐标。"
            )
            return
        yield event.plain_result(
            f"校准未完成：stage={self._string(result.get('stage')) or '-'}，"
            f"reason={self._string(result.get('reason')) or 'unknown'}"
        )

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
            if self._recent_human_group_warmup_sent(group_id, user_id):
                return True
            last_result = self._qq_human_group_warmup_results.get(key) or {}
            if not (isinstance(last_result, dict) and last_result.get("ok") is False):
                return False
        self._qq_human_group_warmup_last_at[key] = now

        timeout = max(65.0, self._get_float("qq_human_group_warmup_timeout_seconds", 65.0))
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
            self._qq_human_group_warmup_sent_at[key] = time.time()
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


    def _recent_human_group_warmup_sent(self, group_id: str, user_id: str) -> bool:
        group_id = self._string(group_id)
        user_id = self._string(user_id)
        if not group_id or not user_id:
            return False
        key = f"{group_id}:{user_id}"
        last_at = self._qq_human_group_warmup_sent_at.get(key, 0.0)
        return bool(last_at and time.time() - last_at <= 180.0)


    def _verified_qq_human_stable_script_path(self) -> Path | None:
        candidates = [self.plugin_dir / "qq_human_send_stable.ps1"]
        for path in candidates:
            try:
                if path.exists() and path.is_file():
                    return path
            except Exception:
                continue
        return None

    def _qq_human_debug_base(self) -> Path:
        default = self.data_dir / "debug" / "qq_human_warmup"
        value = self._string(self._get("qq_human_group_warmup_debug_dir", "")).strip()
        if not value:
            return default
        path = Path(os.path.expandvars(value)).expanduser()
        drive = path.drive
        if drive and not Path(drive + "\\").exists():
            logger.warning(
                "new_member_forwarder: configured human warmup debug drive does not exist, use plugin data dir: %s",
                value,
            )
            return default
        return path

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
            "-CalibrationFile",
            str(self.human_calibration_file),
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
            sent = completed.returncode == 0 and bool(data.get("sent") or data.get("ok"))
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

    def _run_qq_human_group_warmup_calibration_script(self, user_id: str, timeout: float) -> dict[str, Any]:
        script_path = self._verified_qq_human_stable_script_path()
        if not script_path:
            return {"ok": False, "stage": "script", "reason": "qq_human_send_stable.ps1 not found"}

        runtime_dir = self.data_dir / "runtime_scripts"
        trace_path = runtime_dir / f"human_calibration_{user_id}_{int(time.time() * 1000)}.trace.log"
        out_dir = self._qq_human_debug_base() / f"calibration-{time.strftime('%Y%m%d-%H%M%S')}-{user_id}"
        try:
            runtime_dir.mkdir(parents=True, exist_ok=True)
            out_dir.mkdir(parents=True, exist_ok=True)
            trace_path.write_text(f"calibration_script={script_path}\n", encoding="utf-8")
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
            "calibrate",
            "-TargetQQ",
            user_id,
            "-WaitSeconds",
            str(max(3, int(self._get_float("qq_human_group_warmup_wait_seconds", 20.0)))),
            "-OutDir",
            str(out_dir),
            "-TraceFile",
            str(trace_path),
            "-CalibrationFile",
            str(self.human_calibration_file),
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
            stage = self._read_runtime_trace_tail(trace_path) or "calibration_timeout"
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
            ok = completed.returncode == 0 and bool(data.get("ok"))
            return {
                "ok": ok,
                "stage": "calibrated" if ok else self._string(data.get("stage") or data.get("mode") or "not_calibrated"),
                "reason": "calibrated" if ok else self._short_error(RuntimeError(stderr or stdout or "not calibrated"), 300),
                "outDir": self._string(data.get("outDir")) or str(out_dir),
                "script": str(script_path),
                "calibrationFile": self._string(data.get("calibrationFile")) or str(self.human_calibration_file),
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
        if verified_script is None:
            return {
                "ok": False,
                "stage": "script",
                "reason": "qq_human_send_stable.ps1 not found in plugin directory",
            }
        return self._run_verified_qq_human_stable_script(verified_script, group_id, user_id, text, timeout)


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


    def _read_runtime_trace_tail(self, trace_path: Path) -> str:
        try:
            if not trace_path.exists():
                return ""
            lines = trace_path.read_text(encoding="utf-8", errors="replace").splitlines()
            return lines[-1].strip() if lines else ""
        except Exception:
            return ""






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
            content_length = response.headers.get("Content-Length")
            if content_length and self._safe_int(content_length, 0) > MAX_IMAGE_DOWNLOAD_BYTES:
                raise ValueError(f"image is larger than {MAX_IMAGE_DOWNLOAD_BYTES} bytes")

            tmp_path = file_path.with_name(file_path.name + ".part")
            total = 0
            try:
                with tmp_path.open("wb") as fp:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > MAX_IMAGE_DOWNLOAD_BYTES:
                            raise ValueError(f"image is larger than {MAX_IMAGE_DOWNLOAD_BYTES} bytes")
                        fp.write(chunk)
                tmp_path.replace(file_path)
            finally:
                try:
                    if tmp_path.exists():
                        tmp_path.unlink()
                except Exception:
                    pass

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
