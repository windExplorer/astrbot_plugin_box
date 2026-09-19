import asyncio
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import aiohttp
from aiocqhttp import CQHttp

from astrbot.api import logger

from .config import PluginConfig
from .draw import CardMaker
from .profile import BoxUserProfile, friendly_duration, join_duration_suffix
from .store import MemberStore

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
    card_type: str = ""  # "" 查询 / "join" 入群 / "leave" 退群 / "kick" 被踢
    from_cache: bool = False

    display_name: str = ""  # 群昵称或昵称，欢迎语等场景使用
    join_pos: int = 0
    join_total: int = 0

    welcome_text: str = ""  # 入群卡内嵌欢迎语
    welcome_image: bytes | None = None  # 入群卡内嵌欢迎图片

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
        self.store = MemberStore(cfg.data_dir / "member_times.db")
        self._semaphore = asyncio.Semaphore(max(1, int(cfg.max_concurrent or 1)))

    async def get_box_info(
        self,
        bot: CQHttp,
        target_id: str,
        group_id: str,
        include_library: bool = False,
        card_type: str = "",
        operator_id: str = "",
    ) -> BoxResult:
        """Query profile data and build a box result.

        Args:
            bot: OneBot client used to query QQ profile APIs.
            target_id: Target QQ user ID.
            group_id: Source QQ group ID.
            include_library: Whether to append library data.
            card_type: "" / "join" / "leave" / "kick" — controls extra rows.
            operator_id: For kick cards, the admin who performed the kick.

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

        # 名片获赞数：NapCat 扩展接口，不可用时静默跳过
        like_count = None
        try:
            resp = await bot.call_api("get_profile_like", user_id=int(target_id))
            if isinstance(resp, dict):
                for key in ("total_vote_count", "like_count", "count", "total"):
                    value = resp.get(key)
                    if isinstance(value, (int, str)) and str(value).isdigit() and int(value) > 0:
                        like_count = int(value)
                        break
        except Exception:
            like_count = None

        profile = BoxUserProfile.from_sources(
            dict(stranger_info),
            dict(member_info),
            library_info,
            like_count=like_count,
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
            else:
                logger.info(
                    "[资料卡] 陌生人接口未返回有效的 QQ 等级数据（qqLevel/level 缺失或为 0），"
                    f"本次返回字段: {sorted(stranger_info.keys())}"
                )
        display = [line for line in display if not line.startswith("QQ等级：")]

        # 入群/退群精确时间：优先使用本地数据库记录（退群成员接口查不到，只能靠库）
        show_join = "join_time" in enabled or "加群时间" in enabled
        force_times = card_type in ("leave", "kick")
        db_join = ""
        db_leave = ""
        if group_id and self.cfg.record_join_leave:
            try:
                if member_info:
                    # 最后已知的群内信息（头衔/等级等），退群/被踢后接口查不到，靠这份缓存展示
                    self.store.put_member_info(
                        group_id,
                        target_id,
                        card=str(member_info.get("card") or ""),
                        title=str(member_info.get("title") or ""),
                        role=str(member_info.get("role") or ""),
                        level=str(member_info.get("level") or ""),
                    )
                if member_info.get("join_time"):
                    self.store.ensure_join(
                        group_id,
                        target_id,
                        datetime.fromtimestamp(int(member_info["join_time"])),
                    )
                record = self.store.get(group_id, target_id)
                if record:
                    db_join, db_leave = record
            except Exception as e:
                logger.warning(f"[资料卡] 读取本地时间库失败: {e}")

        if force_times:
            # 事件卡：适配器对已离群成员可能返回残留/半空数据（不报错），
            # 先剔除 API 侧同类行，再按固定顺序重组（DB 精确值优先，接口数据兜底）
            labels = ("加群时间：", "退群时间：", "被踢时间：", "在群时长：", "群等级：", "群头衔：")
            display = [line for line in display if not line.startswith(labels)]
            if db_join:
                display.append(f"加群时间：{db_join}")
            elif member_info.get("join_time"):
                try:
                    display.append(
                        "加群时间："
                        + datetime.fromtimestamp(int(member_info["join_time"])).strftime("%Y-%m-%d %H:%M:%S")
                    )
                except (TypeError, ValueError, OSError, OverflowError):
                    pass
            if db_leave:
                display.append(f"{'被踢时间' if card_type == 'kick' else '退群时间'}：{db_leave}")
            if db_join and db_leave:
                try:
                    join_dt = datetime.strptime(db_join, "%Y-%m-%d %H:%M:%S")
                    leave_dt = datetime.strptime(db_leave, "%Y-%m-%d %H:%M:%S")
                    display.append(f"在群时长：{friendly_duration(leave_dt - join_dt)}")
                except ValueError:
                    pass
            # 群等级/群头衔：接口有就用接口的，否则取最后已知缓存值
            known = {}
            try:
                known = self.store.get_member_info(group_id, target_id) or {}
            except Exception as e:
                logger.warning(f"[资料卡] 读取成员信息缓存失败: {e}")
            level_value = member_info.get("level") or known.get("level")
            try:
                if level_value and int(level_value) > 0:
                    display.append(f"群等级：{int(level_value)}级")
            except (TypeError, ValueError):
                pass
            title_value = member_info.get("title") or known.get("title")
            if title_value:
                display.append(f"群头衔：{title_value}")
        else:
            if db_join and show_join:
                replaced = False
                for i, line in enumerate(display):
                    if line.startswith("加群时间："):
                        display[i] = f"加群时间：{db_join}{join_duration_suffix(member_info.get('join_time'))}"
                        replaced = True
                        break
                if not replaced and not member_info:
                    display.append(f"加群时间：{db_join}")
            if db_leave and not member_info:
                display.append(f"退群时间：{db_leave}")
        if card_type == "kick" and operator_id:
            operator_name = ""
            try:
                op_info = await bot.get_stranger_info(user_id=int(operator_id))
                operator_name = str((op_info or {}).get("nickname") or "")
            except Exception:
                operator_name = ""
            display.append(
                f"操作管理员：{operator_name}（{operator_id}）"
                if operator_name
                else f"操作管理员：{operator_id}"
            )

        result = BoxResult(
            target_id=target_id,
            group_id=group_id,
            display=display,
            level_text=level_text,
            card_type=card_type,
            display_name=profile.card or profile.nickname or target_id,
        )

        if group_id:
            result.join_rank, result.join_pos, result.join_total = await self._get_join_rank(
                bot, group_id, target_id
            )
        result.analyses = await self._get_analyses(target_id, profile, display, card_type)
        if card_type == "join" and self.cfg.welcome_enabled:
            result.welcome_text = await self._build_welcome(result)
            pool = [u for u in (self.cfg.welcome_images or []) if str(u).strip()]
            if pool:
                result.welcome_image = await self._download_image(str(random.choice(pool)))
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
            result.card_type,
            result.welcome_text,
            result.welcome_image,
        )
        return result.image

    # ------------------------------------------------------------ 缓存与队列
    def _cache_lookup(self, group_id: str, user_id: str) -> BoxResult | None:
        """冷却期内的缓存卡片；过期/缺失/关闭缓存时返回 None。"""
        cooldown = int(self.cfg.cache_cooldown or 0)
        if cooldown <= 0:
            return None
        try:
            cached = self.store.get_card_cache(group_id, user_id)
        except Exception as e:
            logger.warning(f"[资料卡] 读取卡片缓存失败: {e}")
            return None
        if not cached:
            return None
        if cached["card_type"]:
            # 事件卡（入群/退群/被踢）不属于普通查询：老版本写入的残留直接视为未命中，
            # 让本次实时获取的正常卡覆盖它
            return None
        try:
            fetched_at = datetime.strptime(cached["fetched_at"], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
        if datetime.now() - fetched_at >= timedelta(minutes=cooldown):
            return None
        return BoxResult(
            target_id=user_id,
            group_id=group_id,
            display=list(cached["display"]),
            image=cached["image"],
            level_text=cached["level_text"],
            join_rank=cached["join_rank"],
            analyses=dict(cached["analyses"]),
            card_type=cached["card_type"],
            from_cache=True,
        )

    def _cache_store(self, group_id: str, user_id: str, result: BoxResult) -> None:
        cooldown = int(self.cfg.cache_cooldown or 0)
        if cooldown <= 0 or result.is_fail() or result.image is None:
            return
        now = datetime.now()
        try:
            self.store.put_card_cache(
                group_id,
                user_id,
                now.strftime("%Y-%m-%d %H:%M:%S"),
                result.display,
                result.level_text,
                result.join_rank,
                result.analyses,
                result.card_type,
                result.image,
                stale_before=(now - timedelta(minutes=cooldown)).strftime("%Y-%m-%d %H:%M:%S"),
            )
        except Exception as e:
            logger.warning(f"[资料卡] 写入卡片缓存失败: {e}")

    async def fetch_card(
        self,
        bot: CQHttp,
        target_id: str,
        group_id: str,
        include_library: bool = False,
        card_type: str = "",
        operator_id: str = "",
    ) -> BoxResult:
        """获取并渲染一张卡片：冷却期内直接返回缓存（不排队），否则排队实时获取。

        事件卡（入群/退群/被踢）不受冷却限制，始终实时获取并刷新缓存。
        """
        if card_type == "":
            cached = self._cache_lookup(group_id, target_id)
            if cached:
                return cached
        async with self._semaphore:
            if card_type == "":
                # 排队期间其他任务可能已刷新过同一目标，再查一次
                cached = self._cache_lookup(group_id, target_id)
                if cached:
                    return cached
            result = await self.get_box_info(
                bot,
                target_id,
                group_id,
                include_library=include_library,
                card_type=card_type,
                operator_id=operator_id,
            )
            if result.is_fail():
                return result
            await self.render_box_image(result)
            # 只有普通查询的结果进缓存：事件卡（入群/退群/被踢）是特定场景的
            # 一次性卡片，写进缓存会让冷却期内的手动查询端出一张"新朋友"卡
            if card_type == "":
                self._cache_store(group_id, target_id, result)
            return result

    # ------------------------------------------------------------ 欢迎语
    def _resolve_llm(self, site_model: str) -> tuple[Any, str | None]:
        """解析 LLM 调用目标：站点模型 > 兜底模型 > 系统默认。

        模型格式：``提供商ID/模型名``（跨提供商路由）或 ``模型名``（用系统默认提供商）。
        返回 (provider, model_name)；model_name 为 None 时用该提供商的默认模型。
        """
        context = self.cfg.context
        model = (site_model or "").strip()
        if not model:
            model = (self.cfg.llm_fallback_model or "").strip()
        if not model:
            return context.get_using_provider(), None
        if "/" in model:
            provider_id, _, model_name = model.partition("/")
            provider = context.get_provider_by_id(provider_id)
            if provider:
                return provider, model_name or None
            logger.warning(f"[资料卡] 未找到 LLM 提供商 {provider_id}，回退系统默认")
            return context.get_using_provider(), model_name or None
        return context.get_using_provider(), model

    async def _build_welcome(self, result: BoxResult) -> str:
        """Fill the welcome text for a join card (rendered inside the card).

        AI 开启时正文由 LLM 生成（替代固定模板，避免两者重复堆叠），
        排位信息行保留；AI 失败时回退固定模板。
        """
        name = result.display_name or "新朋友"
        count_text = ""
        if result.join_pos:
            count_text = f"你是本群第 {result.join_pos} 位成员（共 {result.join_total} 人）"

        if self.cfg.welcome_ai_enabled:
            ai_text = await self._gen_welcome_ai(name, count_text)
            if ai_text:
                parts = ([count_text] if count_text else []) + [f"✨ {ai_text}"]
                return "\n".join(parts)
            # AI 生成失败 → 回退固定模板

        template = str(self.cfg.welcome_text or "").strip() or "🎉 欢迎 {name} 加入本群！\n{count_text}"
        try:
            text = template.format(name=name, count_text=count_text)
        except Exception as e:
            logger.warning(f"[资料卡] 欢迎语模板格式错误: {e}")
            text = f"🎉 欢迎 {name} 加入本群！"
        text = "\n".join(line for line in text.splitlines() if line.strip())
        return text

    async def _gen_welcome_ai(self, name: str, count_text: str = "") -> str:
        """Optional LLM-generated welcome line, with retries."""
        provider, model = self._resolve_llm(self.cfg.welcome_ai_model)
        if not provider:
            return ""
        prompt = (
            str(self.cfg.welcome_ai_prompt or "")
            .replace("{name}", name)
            .replace("{count_text}", count_text)
            .strip()
        )
        if not prompt:
            return ""
        retries = max(0, int(self.cfg.welcome_ai_retry or 0))
        for attempt in range(retries + 1):
            try:
                resp = await provider.text_chat(prompt=prompt, model=model)
                return (resp.completion_text or "").strip()[:100]
            except Exception as e:
                logger.debug(f"[资料卡] AI 欢迎语第 {attempt + 1}/{retries + 1} 次生成失败: {e}")
        return ""

    async def _download_image(self, url: str) -> bytes | None:
        if not url.startswith(("http://", "https://")):
            return None
        try:
            async with aiohttp.ClientSession() as session:
                response = await session.get(url)
                response.raise_for_status()
                return await response.read()
        except Exception as e:
            logger.warning(f"[资料卡] 欢迎图片下载失败: {e}")
            return None

    async def _get_join_rank(self, bot: CQHttp, group_id: str, target_id: str) -> tuple[str, int, int]:
        """Compute the member's join order within the group.

        Returns (display_text, position, total); text is "" when unavailable.
        """
        try:
            members = await bot.get_group_member_list(group_id=int(group_id))
        except Exception as e:
            logger.warning(f"get_group_member_list failed: {e}")
            return "", 0, 0

        joined = []
        for m in members:
            try:
                join_time = int(m.get("join_time") or 0)
            except (TypeError, ValueError):
                continue
            if join_time > 0:
                joined.append((join_time, str(m.get("user_id"))))
        if not joined:
            return "", 0, 0

        joined.sort()
        for idx, (_t, uid) in enumerate(joined, start=1):
            if uid == target_id:
                return f"第 {idx} 位 · 共 {len(joined)} 人", idx, len(joined)
        # 目标已不在群里（退群/被踢）：排位与总数都把已知的离群成员算进去——
        # 排位 = 早于其入群的现有成员数 + 早于其入群的已知离群者数 + 1；
        # 总数 = 现有成员数 + 已知离群者数
        try:
            record = self.store.get(group_id, target_id)
        except Exception:
            record = None
        if record and record[0]:
            try:
                join_dt = datetime.strptime(record[0], "%Y-%m-%d %H:%M:%S")
                departed = self.store.departed_joins_for_group(group_id)
                before = sum(1 for t, _u in joined if datetime.fromtimestamp(t) < join_dt)
                for uid, jt in departed.items():
                    if uid == target_id or not jt:
                        continue
                    try:
                        if datetime.strptime(jt, "%Y-%m-%d %H:%M:%S") < join_dt:
                            before += 1
                    except ValueError:
                        continue
                pos = 1 + before
                total = len(joined) + len(departed)
                return f"第 {pos} 位 · 共 {total} 人", pos, total
            except ValueError:
                pass
        return "", 0, 0

    async def _get_analyses(
        self, target_id: str, profile: BoxUserProfile, display: list[str], card_type: str = ""
    ) -> dict[str, str]:
        """Run the enabled AI analyses (avatar / signature / overall) concurrently."""
        ai = self.cfg.ai_analysis
        # 退群/被踢卡默认不调用 LLM，除非显式开启 event_analysis
        if card_type in ("leave", "kick") and not ai.event_analysis:
            return {}
        avatar_url = f"https://q4.qlogo.cn/headimg_dl?dst_uin={target_id}&spec=640"
        signature = (profile.long_nick or "").strip()
        profile_text = "\n".join(display)

        # (key, (provider, model), (prompt, with_image))
        specs: list[tuple[str, tuple[Any, str | None], tuple[str, bool]]] = []
        if ai.avatar_analysis:
            specs.append(
                ("avatar", self._resolve_llm(ai.avatar_model),
                 ("这是一位QQ用户的头像图片。请根据头像画面，用轻松幽默的语气写一句话点评这位用户，"
                  "不超过40个字。直接输出点评内容，不要任何前缀、引号或解释。", True))
            )
        if ai.signature_analysis and signature:
            specs.append(
                ("signature", self._resolve_llm(ai.signature_model),
                 (f"一位QQ用户的个性签名是：「{signature}」。请据此用轻松幽默的语气写一句话点评这位用户，"
                  "不超过40个字。直接输出点评内容，不要任何前缀、引号或解释。", False))
            )
        if ai.overall_analysis:
            specs.append(
                ("overall", self._resolve_llm(ai.overall_model),
                 ("这是一位QQ用户的头像图片和公开资料：\n"
                  f"{profile_text}\n"
                  "请综合以上信息，用轻松幽默的语气写一句话锐评这位用户，不超过50个字。"
                  "直接输出锐评内容，不要任何前缀、引号或解释。", True))
            )
        if not specs:
            return {}

        async def _run(key: str, provider_model: tuple, prompt_info: tuple[str, bool]) -> tuple[str, str]:
            prompt, with_image = prompt_info
            provider, model = provider_model
            if not provider:
                return key, ""
            try:
                response = await provider.text_chat(
                    prompt=prompt,
                    image_urls=[avatar_url] if with_image else None,
                    model=model,
                )
                return key, (response.completion_text or "").strip()[:80]
            except Exception as e:
                logger.warning(f"llm analysis [{key}] failed: {e}")
                return key, ""

        pairs = await asyncio.gather(*(_run(k, pm, pi) for k, pm, pi in specs))
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
