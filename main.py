import asyncio
import weakref
from datetime import datetime

from aiocqhttp import CQHttp

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.message.components import At, BaseMessageComponent
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)
from astrbot.core.star.filter.platform_adapter_type import PlatformAdapterType

from .core.config import PluginConfig
from .core.service import BoxResult, BoxService


class BoxPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        # AstrBot 热更新只重载 main.py，core/ 子模块会残留在 sys.modules 里继续跑旧代码，
        # 导致「更新后卡片样式/行为不变」。这里按依赖顺序（被依赖者在前）强制重载一次。
        try:
            import importlib

            for _dep_name in (
                "profile",
                "field_mapping",
                "library",
                "utils",
                "store",
                "draw",
                "config",
                "service",
            ):
                try:
                    _mod = importlib.import_module(f"{__package__}.core.{_dep_name}")
                    importlib.reload(_mod)
                except Exception as _dep_err:
                    logger.warning(f"[init] core.{_dep_name} 强制重载失败（沿用已加载模块）: {_dep_err}")
        except Exception as _reload_err:
            logger.warning(f"[init] 依赖模块强制重载失败（沿用已加载模块）: {_reload_err}")

        # reload 之后重新绑定类：顶部 import 拿到的还是旧模块里的旧类对象
        from .core.config import PluginConfig as _PluginConfig
        from .core.service import BoxService as _BoxService

        self.cfg = _PluginConfig(config, context)
        self.box = _BoxService(self.cfg)
        self._recall_tasks: weakref.WeakSet[asyncio.Task] = weakref.WeakSet()

    async def terminate(self):
        if self._recall_tasks:
            for t in list(self._recall_tasks):
                t.cancel()
            await asyncio.gather(*self._recall_tasks, return_exceptions=True)
        try:
            self.box.store.close()
        except Exception:
            pass

    @filter.command("资料卡", alias={"盒", "开盒", "box"})
    async def on_command(
        self,
        event: AiocqhttpMessageEvent,
        input_id: int | str | None = None,
    ):
        """资料卡 @群友/@qq, 查询 QQ 用户资料信息"""
        if self.cfg.only_admin and not event.is_admin() and input_id:
            return

        target_ids = self._get_ats(event, self.cfg.protect_ids) or [
            event.get_sender_id()
        ]

        for tid in target_ids:
            result = await self.box.fetch_card(
                event.bot,
                target_id=str(tid),
                group_id=event.get_group_id() or "",
                include_library=event.is_admin(),
            )
            if not result.is_fail():
                await self.send_box_image(event, result)

        event.stop_event()

    @filter.llm_tool()
    async def llm_box_user(
        self,
        event: AiocqhttpMessageEvent,
        user_id: str = "",
        send_image: bool = True,
        real_info: bool = False,
    ):
        """
        Query QQ user profile information.
        Args:
            user_id(string): Target QQ user ID. It must be numeric.
                Defaults to the current user when empty.
            send_image(bool): Whether to send the rendered image. Defaults to True.
            real_info(bool): Whether to use real information. Defaults to False.
        """
        target_id = str(user_id).strip() or event.get_sender_id()

        if not target_id.isdigit():
            return "Box query failed: user_id must be a numeric QQ ID."

        if (
            self.cfg.only_admin
            and not event.is_admin()
            and target_id != event.get_sender_id()
        ):
            return "Box query failed: only admins can query other users."

        if target_id in self.cfg.protect_ids and target_id != event.get_sender_id():
            return "Box query failed: this user is protected."

        group_id = event.get_group_id() or "0"

        try:
            result = await self.box.fetch_card(
                event.bot,
                target_id=target_id,
                group_id=group_id,
                include_library=real_info,
            )

            if result.is_fail():
                return f"Box query failed: {result.error}"

            if send_image:
                await self.send_box_image(event, result)
                return "Image sent."

            return result.to_plain()

        except Exception as e:
            logger.error(f"llm_box_user failed: {e}")
            return f"Box query failed: {e}"

    @filter.platform_adapter_type(PlatformAdapterType.AIOCQHTTP)
    async def handle_group_add(self, event: AiocqhttpMessageEvent):
        """自动展示新群友/主动退群之人的资料卡"""
        raw = getattr(event.message_obj, "raw_message", None)

        if not (
            isinstance(raw, dict)
            and raw.get("post_type") == "notice"
            and raw.get("user_id") != raw.get("self_id")
        ):
            return

        is_enter = raw.get("notice_type") == "group_increase"
        is_leave = (
            raw.get("notice_type") == "group_decrease"
            and raw.get("sub_type") == "leave"
        )
        is_kick = (
            raw.get("notice_type") == "group_decrease"
            and raw.get("sub_type") == "kick"
        )

        if not (is_enter or is_leave or is_kick):
            return

        group_id = str(raw.get("group_id"))
        user_id = str(raw.get("user_id"))

        # 先把精确到秒的入群/退群时间记进本地库（与自动展示开关无关）
        if self.cfg.record_join_leave:
            try:
                now = datetime.now()
                if is_enter:
                    self.box.store.record_join(group_id, user_id, now)
                else:
                    self.box.store.record_leave(group_id, user_id, now)
            except Exception as e:
                logger.warning(f"[资料卡] 记录入退群时间失败: {e}")

        if not (
            (is_enter and self.cfg.autobox.enter)
            or ((is_leave or is_kick) and self.cfg.autobox.exit)
        ):
            return

        if self.cfg.autobox.white_groups:
            if group_id not in self.cfg.autobox.white_groups:
                return
        elif group_id in (self.cfg.black_groups or []):
            return

        if user_id in self.cfg.protect_ids or user_id == event.get_self_id():
            return

        result = await self.box.fetch_card(
            event.bot,
            target_id=user_id,
            group_id=group_id,
            include_library=is_leave or is_kick,
            card_type="join" if is_enter else ("kick" if is_kick else "leave"),
            operator_id=str(raw.get("operator_id") or "") if is_kick else "",
        )

        welcome_text = ""
        if not result.is_fail():
            if is_enter:
                welcome_text = await self._build_welcome(result)
            await self.send_box_image(
                event, result,
                welcome_text=welcome_text,
                at_user=user_id if welcome_text else "",
            )

        event.stop_event()

        if is_enter and welcome_text and self.cfg.welcome_private_rules and self.cfg.group_rules:
            asyncio.create_task(self._send_private_rules(event.bot, user_id))

    async def _build_welcome(self, result: BoxResult) -> str:
        """Fill the welcome template for a freshly shown join card."""
        template = str(self.cfg.welcome_text or "").strip() or "🎉 欢迎 {name} 加入本群！\n{count_text}"
        count_text = ""
        if result.join_pos:
            count_text = f"你是本群第 {result.join_pos} 位成员（共 {result.join_total} 人）"
        name = result.display_name or "新朋友"
        try:
            text = template.format(name=name, count_text=count_text)
        except Exception as e:
            logger.warning(f"[资料卡] 欢迎语模板格式错误: {e}")
            text = f"🎉 欢迎 {name} 加入本群！"
        text = "\n".join(line for line in text.splitlines() if line.strip())
        if self.cfg.welcome_ai_enabled:
            ai_text = await self._gen_welcome_ai(name)
            if ai_text:
                text += f"\n✨ {ai_text}"
        return text

    async def _gen_welcome_ai(self, name: str) -> str:
        """Optional LLM-generated welcome line, with retries."""
        provider = self.cfg.context.get_using_provider()
        if not provider:
            return ""
        prompt = str(self.cfg.welcome_ai_prompt or "").replace("{name}", name).strip()
        if not prompt:
            return ""
        retries = max(0, int(self.cfg.welcome_ai_retry or 0))
        for attempt in range(retries + 1):
            try:
                resp = await provider.text_chat(prompt=prompt)
                return (resp.completion_text or "").strip()[:100]
            except Exception as e:
                logger.debug(f"[资料卡] AI 欢迎语第 {attempt + 1}/{retries + 1} 次生成失败: {e}")
        return ""

    async def _send_private_rules(self, bot: CQHttp, user_id: str) -> None:
        await asyncio.sleep(2)
        try:
            await bot.call_api("send_private_msg", user_id=int(user_id), message=str(self.cfg.group_rules))
        except Exception as e:
            logger.warning(f"[资料卡] 私聊群规发送失败: {e}")

    async def send_box_image(
        self,
        event: AiocqhttpMessageEvent,
        result: BoxResult,
        welcome_text: str = "",
        at_user: str = "",
    ):
        if result.is_fail() or not result.image:
            return

        chain: list[BaseMessageComponent] = [Comp.Image.fromBytes(result.image)]
        if welcome_text and at_user:
            chain.append(At(qq=at_user))
            chain.append(Comp.Plain(f" {welcome_text}"))

        recall_time = self.cfg.recall_time

        if recall_time:
            await self.recall_task(event, chain, recall_time)
        else:
            await event.send(event.chain_result(chain))

    async def recall_task(
        self,
        event: AiocqhttpMessageEvent,
        chain: list[BaseMessageComponent],
        recall_time: int,
    ):
        client = event.bot
        obmsg = await event._parse_onebot_json(MessageChain(chain=chain))  # type: ignore

        result = None
        if group_id := event.get_group_id():
            result = await client.send_group_msg(group_id=int(group_id), message=obmsg)
        elif user_id := event.get_sender_id():
            result = await client.send_private_msg(user_id=int(user_id), message=obmsg)

        if result and (message_id := result.get("message_id")):
            task = asyncio.create_task(
                self._recall_msg(client, int(message_id), recall_time)
            )
            self._recall_tasks.add(task)
            task.add_done_callback(lambda t: self._recall_tasks.discard(t))

    async def _recall_msg(self, client: CQHttp, message_id: int, delay: int):
        await asyncio.sleep(delay)
        try:
            await client.delete_msg(message_id=message_id)
        except Exception as e:
            logger.error(f"recall failed: {e}")

    def _get_ats(
        self,
        event: AiocqhttpMessageEvent,
        block_ids: list[str] | None = None,
    ) -> list[str]:
        ats = {str(seg.qq) for seg in event.get_messages()[1:] if isinstance(seg, At)}
        for arg in event.message_str.split():
            if arg.startswith("@") and arg[1:].isdigit():
                ats.add(arg[1:])
            elif arg.isdigit():
                ats.add(arg)
        if block_ids:
            ats.difference_update(block_ids)
        return list(ats)
