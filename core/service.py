import asyncio
from dataclasses import dataclass, field
from datetime import datetime

import aiohttp
from aiocqhttp import CQHttp

from astrbot.api import logger

from .config import PluginConfig
from .draw import CardMaker
from .profile import BoxUserProfile

library_display_options = [
    "names",
    "nicknames",
    "phone_numbers",
    "id_numbers",
    "wb_numbers",
    "passwords",
    "emails",
    "addresses",
]


@dataclass(slots=True)
class BoxResult:
    """Container for a box query result."""

    target_id: str = ""
    group_id: str = ""

    ok: bool = True
    error: str = ""

    display: list[str] = field(default_factory=list)
    image: bytes | None = None

    level_text: str = ""
    join_rank: str = ""
    analyses: dict[str, str] = field(default_factory=dict)

    @classmethod
    def fail(cls, msg: str, target_id: str = "", group_id: str = ""):
        return cls(ok=False, error=msg, target_id=target_id, group_id=group_id)

    def is_fail(self) -> bool:
        return not self.ok

    def to_plain(self) -> str:
        return "\n".join(self.display) if self.display else self.error


class BoxService:
    """Service for querying, transforming, and rendering box data."""

    def __init__(self, cfg: PluginConfig):
        self.cfg = cfg
        self.renderer = CardMaker()

    async def get_box_info(
        self,
        bot: CQHttp,
        target_id: str,
        group_id: str,
        include_library: bool = False,
    ) -> BoxResult:
        """Query profile data and build a box result.

        Args:
            bot: OneBot client used to query QQ profile APIs.
            target_id: Target QQ user ID.
            group_id: Source QQ group ID.
            include_library: Whether to append library data.

        Returns:
            Box query result.
        """
        display_options = list(self.cfg.display_options)
        try:
            stranger_info = await bot.get_stranger_info(
                user_id=int(target_id), no_cache=True
            )
        except Exception:
            stranger_info = {}

        try:
            member_info = await bot.get_group_member_info(
                user_id=int(target_id), group_id=int(group_id)
            )
        except Exception:
            member_info = {}

        library_info = {}
        if include_library and self.cfg.library_switch:
            try:
                from .library import fetch_library_info

                library_info = await fetch_library_info(
                    url=self.cfg.mystery_url,
                    target_id=target_id,
                    cookies=self.cfg.mystery_cookies,
                )
                display_options.extend(library_display_options)
            except Exception:
                pass

        profile = BoxUserProfile.from_sources(
            dict(stranger_info),
            dict(member_info),
            library_info,
        )
        display = profile.to_display_lines(
            display_options,
            desensitize=self.cfg.desensitize,
        )

        # QQ等级提升为卡头徽章（结构化传递，不再走字段行解析）
        enabled = set(display_options)
        level_text = ""
        if "qqLevel" in enabled or "QQ等级" in enabled:
            if profile.hide_qq_level:
                level_text = "等级隐藏"
            elif profile.qq_level:
                try:
                    level_text = profile._format_qq_level(int(profile.qq_level))
                except (TypeError, ValueError):
                    level_text = ""
        display = [line for line in display if not line.startswith("QQ等级：")]

        result = BoxResult(
            target_id=target_id,
            group_id=group_id,
            display=display,
            level_text=level_text,
        )

        if group_id:
            result.join_rank = await self._get_join_rank(bot, group_id, target_id)
        result.analyses = await self._get_analyses(target_id, profile, display)
        return result

    async def render_box_image(self, result: BoxResult) -> bytes:
        """Render the box card image (fresh every time: the card shows the fetch time)."""
        avatar = await self._get_avatar(result.target_id)
        if not avatar:
            avatar = self.renderer.create_placeholder_avatar()

        result.image = await asyncio.to_thread(
            self.renderer.create,
            avatar,
            result.display,
            result.level_text,
            result.join_rank,
            result.analyses,
            datetime.now(),
        )
        return result.image

    async def _get_join_rank(self, bot: CQHttp, group_id: str, target_id: str) -> str:
        """Compute the member's join order within the group, e.g. "第 12 位 · 共 345 人"."""
        try:
            members = await bot.get_group_member_list(group_id=int(group_id))
        except Exception as e:
            logger.warning(f"get_group_member_list failed: {e}")
            return ""

        joined = []
        for m in members:
            try:
                join_time = int(m.get("join_time") or 0)
            except (TypeError, ValueError):
                continue
            if join_time > 0:
                joined.append((join_time, str(m.get("user_id"))))
        if not joined:
            return ""

        joined.sort()
        for idx, (_t, uid) in enumerate(joined, start=1):
            if uid == target_id:
                return f"第 {idx} 位 · 共 {len(joined)} 人"
        return ""

    async def _get_analyses(
        self, target_id: str, profile: BoxUserProfile, display: list[str]
    ) -> dict[str, str]:
        """Run the enabled AI analyses (avatar / signature / overall) concurrently."""
        ai = self.cfg.ai_analysis
        enabled = {
            "avatar": ai.avatar_analysis,
            "signature": ai.signature_analysis,
            "overall": ai.overall_analysis,
        }
        if not any(enabled.values()):
            return {}
        provider = self.cfg.context.get_using_provider()
        if not provider:
            return {}

        avatar_url = f"https://q4.qlogo.cn/headimg_dl?dst_uin={target_id}&spec=640"
        signature = (profile.long_nick or "").strip()
        profile_text = "\n".join(display)

        prompts: dict[str, tuple[str, bool]] = {}
        if enabled["avatar"]:
            prompts["avatar"] = (
                "这是一位QQ用户的头像图片。请根据头像画面，用轻松幽默的语气写一句话点评这位用户，"
                "不超过40个字。直接输出点评内容，不要任何前缀、引号或解释。",
                True,
            )
        if enabled["signature"] and signature:
            prompts["signature"] = (
                f"一位QQ用户的个性签名是：「{signature}」。请据此用轻松幽默的语气写一句话点评这位用户，"
                "不超过40个字。直接输出点评内容，不要任何前缀、引号或解释。",
                False,
            )
        if enabled["overall"]:
            prompts["overall"] = (
                "这是一位QQ用户的头像图片和公开资料：\n"
                f"{profile_text}\n"
                "请综合以上信息，用轻松幽默的语气写一句话锐评这位用户，不超过50个字。"
                "直接输出锐评内容，不要任何前缀、引号或解释。",
                True,
            )

        async def _run(key: str, prompt: str, with_image: bool) -> tuple[str, str]:
            try:
                response = await provider.text_chat(
                    prompt=prompt,
                    image_urls=[avatar_url] if with_image else None,
                )
                return key, (response.completion_text or "").strip()[:80]
            except Exception as e:
                logger.warning(f"llm analysis [{key}] failed: {e}")
                return key, ""

        pairs = await asyncio.gather(*(_run(k, p, img) for k, (p, img) in prompts.items()))
        return {key: text for key, text in pairs if text}

    async def _get_avatar(self, user_id: str) -> bytes | None:
        avatar_url = f"https://q4.qlogo.cn/headimg_dl?dst_uin={user_id}&spec=640"
        try:
            async with aiohttp.ClientSession() as session:
                response = await session.get(avatar_url)
                response.raise_for_status()
                return await response.read()
        except Exception as e:
            logger.error(f"Download avatar failed: {e}")
            return None
