"""成员数据面板的 Web API 与回填任务。

路由前缀 ``/astrbot_plugin_box``（AstrBot 约定：带插件名），前端通过
``window.AstrBotPluginPage.apiGet/apiPost`` 调用，Dashboard 转发。

返回信封遵循 AstrBot 桥接约定：
  - 成功 → ``{"status": "ok", "data": ...}``
  - 失败 → ``{"status": "error", "message": "..."}``

成员数据回填（MemberBackfiller）：拉取机器人所在群的成员列表，
把群昵称/头衔/身份/等级写入 member_info，把 join_time 回填进 member_times，
使退群/被踢卡的信息行不依赖「该成员曾被查询过」。

成员变动自动刷新（GroupRefresher）：QQ 不会推送「群成员列表变了」这样的事件，
只有人来人走的 notice；拿到 notice 后按群去抖、限频地重拉一次该群成员列表入库，
面板的「群人数 / 记录覆盖率」不再要等到下一次全量回填（最长 12 小时）才更新。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from astrbot.api import logger

try:  # quart 是 AstrBot 的运行期依赖
    from quart import request
except Exception:  # pragma: no cover
    request = None  # type: ignore

ROUTE_PREFIX = "/astrbot_plugin_box"

# 面板可读写的插件配置键白名单（键 -> 类型）
_CONFIG_FIELDS = {
    "only_admin": "bool",
    "protect_ids": "strlist",
    "record_join_leave": "bool",
    "init_backfill": "bool",
    "cache_cooldown": "int",
    "max_concurrent": "int",
    "desensitize": "bool",
    "avatar_shape": "str",
    "recall_time": "int",
    "welcome_enabled": "bool",
    "welcome_text": "str",
    "welcome_images": "strlist",
    "welcome_ai_enabled": "bool",
    "welcome_ai_prompt": "str",
    "welcome_ai_retry": "int",
    "welcome_private_rules": "bool",
    "group_rules": "str",
    "black_groups": "strlist",
    "show_reg_time": "bool",
    "display_options": "strlist",
    "autobox.enter": "bool",
    "autobox.exit": "bool",
    "autobox.white_groups": "strlist",
    "ai_analysis.avatar_analysis": "bool",
    "ai_analysis.signature_analysis": "bool",
    "ai_analysis.overall_analysis": "bool",
    "ai_analysis.event_analysis": "bool",
}


def _coerce(value: Any, kind: str) -> Any:
    if kind == "bool":
        return bool(value)
    if kind == "int":
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0
    if kind == "str":
        return str(value if value is not None else "")
    if kind == "strlist":
        if isinstance(value, list):
            return [str(v).strip() for v in value if str(v).strip()]
        return []
    return value
FMT = "%Y-%m-%d %H:%M:%S"
GROUP_SLEEP_SECONDS = 1  # 群与群之间稍作停歇，降低接口压力


def _ok(data: Any = None) -> dict:
    return {"status": "ok", "data": data}


def _err(message: str) -> dict:
    return {"status": "error", "message": message}


def _query(name: str, default: str = "") -> str:
    try:
        if request is not None:
            return (request.args.get(name) or default).strip()
    except Exception:
        pass
    return default


async def _body() -> dict:
    try:
        if request is not None:
            return await request.get_json(silent=True) or {}
    except Exception:
        pass
    return {}


async def fetch_member_list(bot, group_id: str) -> list:
    """取群成员列表：优先 no_cache（适配器默认会回吐自身缓存，人数会滞后）。"""
    try:
        return await bot.get_group_member_list(group_id=int(group_id), no_cache=True)
    except Exception as e:
        logger.debug(f"[资料卡] 群 {group_id} 成员列表不支持 no_cache，退回默认取数: {e}")
    return await bot.get_group_member_list(group_id=int(group_id))


def ingest_group_members(store, group_id: str, members: list) -> int:
    """把一次实时成员列表落库：群内信息 / 入群时间 / 当前名册快照。

    名册快照（``group_roster``）整体替换，是「该群此刻实际有哪些人」的权威副本：
    面板的覆盖率统计以它为准，而不是以 member_times 里累计的历史记录为准——否则
    「退群时没观测到退群事件」的幽灵记录会让各项计数虚高、对不上实际群人数。
    """
    seen_at = datetime.now().strftime(FMT)
    info_rows: list[tuple] = []
    join_rows: list[tuple] = []
    present: list[str] = []
    for m in members or []:
        uid = str(m.get("user_id") or "")
        if not uid:
            continue
        present.append(uid)
        info_rows.append(
            (
                group_id,
                uid,
                str(m.get("card") or ""),
                str(m.get("title") or ""),
                str(m.get("role") or ""),
                str(m.get("level") or ""),
            )
        )
        try:
            jt = int(m.get("join_time") or 0)
        except (TypeError, ValueError):
            jt = 0
        if jt > 0:
            try:
                join_rows.append((group_id, uid, datetime.fromtimestamp(jt).strftime(FMT)))
            except (OSError, OverflowError, ValueError):
                pass
    store.put_member_info_many(info_rows)
    store.ensure_join_many(join_rows)
    store.replace_roster(group_id, present, seen_at)
    # 记录该群成员列表的实际长度：群报告人数(member_count)经常滞后 1~2 人，
    # 覆盖率统计以列表数为准，否则会出现"幽灵缺口"
    try:
        listed = json.loads(store.get_meta("group_listed") or "{}")
        listed[group_id] = len(present)
        store.set_meta("group_listed", json.dumps(listed, ensure_ascii=False))
    except Exception:
        pass
    return len(present)


class MemberBackfiller:
    """后台任务：遍历机器人所在群，批量入库成员群内信息与入群时间。"""

    def __init__(self, cfg, store, get_bot):
        self.cfg = cfg
        self.store = store
        self.get_bot = get_bot
        self._task: asyncio.Task | None = None
        self.state: dict[str, Any] = self._fresh_state()

    @staticmethod
    def _fresh_state() -> dict[str, Any]:
        return {
            "running": False,
            "cancel": False,
            "total_groups": 0,
            "done_groups": 0,
            "current_group": "",
            "total_members": 0,
            "recorded_members": 0,
            "errors": 0,
            "last_error": "",
            "started_at": "",
            "finished_at": "",
        }

    MODEL_KEYS = ("welcome", "avatar", "signature", "overall", "fallback")

    def status(self) -> dict[str, Any]:
        return dict(self.state)

    def start(self, only_group: str = "") -> bool:
        if self.state["running"]:
            return False
        self.state = self._fresh_state()
        self.state["running"] = True
        self._task = asyncio.create_task(self._run(only_group))
        return True

    def cancel(self) -> None:
        self.state["cancel"] = True

    async def _run(self, only_group: str = "") -> None:
        try:
            bot = self.get_bot()
            if not bot:
                self.state["last_error"] = "未找到 OneBot 适配器"
                return
            groups = await bot.get_group_list()
            if only_group:
                groups = [g for g in groups if str(g.get("group_id")) == only_group]
            self.state["total_groups"] = len(groups)
            for g in groups:
                if self.state["cancel"]:
                    break
                gid = str(g.get("group_id") or "")
                self.state["current_group"] = f"{g.get('group_name') or ''}({gid})"
                try:
                    members = await fetch_member_list(bot, gid)
                except Exception as e:
                    self.state["errors"] += 1
                    self.state["last_error"] = f"群 {gid} 成员列表获取失败: {e}"
                    self.state["done_groups"] += 1
                    continue
                try:
                    count = ingest_group_members(self.store, gid, members)
                    self.state["recorded_members"] += count
                except Exception as e:
                    self.state["errors"] += 1
                    self.state["last_error"] = f"群 {gid} 入库失败: {e}"
                    count = 0
                self.state["total_members"] += count
                self.state["done_groups"] += 1
                await asyncio.sleep(GROUP_SLEEP_SECONDS)
        except Exception as e:
            self.state["errors"] += 1
            self.state["last_error"] = str(e)
        finally:
            self.state["running"] = False
            self.state["finished_at"] = datetime.now().strftime(FMT)
            self.state["current_group"] = ""


class GroupRefresher:
    """成员变动（入群/退群/被踢）后，按群去抖 + 限频地刷新该群成员数据。

    同一个群里连续几个人进出只拉一次成员列表（去抖窗口内的事件合并），且同一群两次
    刷新之间有最小间隔，避免活跃群把接口打满。刷新期间又发生变动的群会被标记为
    「脏」，本轮结束后再补一轮（间隔限制仍然生效）。
    """

    DEBOUNCE_SECONDS = 20
    MIN_INTERVAL_SECONDS = 90

    def __init__(self, store, get_bot):
        self.store = store
        self.get_bot = get_bot
        self._pending: dict[str, asyncio.Task] = {}
        self._dirty: set[str] = set()
        self._last_run: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self.stats: dict[str, Any] = {
            "refreshed": 0,
            "recorded": 0,
            "errors": 0,
            "last_error": "",
            "last_group": "",
            "last_at": "",
        }

    def status(self) -> dict[str, Any]:
        out = dict(self.stats)
        out["pending"] = len(self._pending)
        return out

    def schedule(self, group_id: str) -> bool:
        """登记一次刷新（去抖）。返回是否新建了任务。"""
        gid = str(group_id or "").strip()
        if not gid or not gid.isdigit():
            return False
        self._dirty.add(gid)
        if gid in self._pending:
            return False
        try:
            self._pending[gid] = asyncio.create_task(self._loop(gid))
        except RuntimeError as e:  # 没有事件循环（例如同步上下文里被调用）
            self._dirty.discard(gid)
            logger.warning(f"[资料卡] 群 {gid} 成员数据自动刷新无法启动: {e}")
            return False
        return True

    async def _loop(self, gid: str) -> None:
        try:
            while True:
                self._dirty.discard(gid)
                await asyncio.sleep(self.DEBOUNCE_SECONDS)
                wait = self.MIN_INTERVAL_SECONDS - (time.monotonic() - self._last_run.get(gid, 0.0))
                if wait > 0:
                    await asyncio.sleep(wait)
                await self.refresh(gid)
                if gid not in self._dirty:
                    break
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.stats["errors"] += 1
            self.stats["last_error"] = f"群 {gid} 刷新失败: {e}"
            logger.warning(f"[资料卡] 群 {gid} 成员数据自动刷新失败: {e}")
        finally:
            self._pending.pop(gid, None)
            self._dirty.discard(gid)

    async def refresh(self, gid: str) -> int:
        """立即刷新一个群，返回入库的名册人数。"""
        bot = self.get_bot()
        if not bot:
            raise RuntimeError("未找到 OneBot 适配器")
        lock = self._locks.setdefault(gid, asyncio.Lock())
        async with lock:
            members = await fetch_member_list(bot, gid)
            count = ingest_group_members(self.store, gid, members)
        self._last_run[gid] = time.monotonic()
        self.stats["refreshed"] += 1
        self.stats["recorded"] += count
        self.stats["last_group"] = gid
        self.stats["last_at"] = datetime.now().strftime(FMT)
        logger.info(f"[资料卡] 群 {gid} 成员变动后已自动刷新：名册 {count} 人")
        return count

    def cancel(self) -> None:
        for task in list(self._pending.values()):
            task.cancel()
        self._pending.clear()
        self._dirty.clear()


def register_apis(plugin, backfiller: MemberBackfiller, refresher: GroupRefresher | None = None) -> None:
    """把面板路由注册到 AstrBot（在插件 initialize() 中调用）。"""

    async def h_groups(*_args, **_kwargs) -> dict:
        bot = backfiller.get_bot()
        if not bot:
            return _err("未找到 OneBot 适配器")
        try:
            groups = await bot.get_group_list()
        except Exception as e:
            return _err(f"获取群列表失败: {e}")
        store = plugin.box.store
        try:
            info_counts = store.info_counts()
            join_counts = store.join_counts()
            listed_map = json.loads(store.get_meta("group_listed") or "{}")
            roster = store.roster_stats()
        except Exception as e:
            return _err(f"读取统计失败: {e}")
        out = []
        for g in groups:
            gid = str(g.get("group_id") or "")
            member_count = int(g.get("member_count") or 0)
            rs = roster.get(gid) or {}
            present = int(rs.get("present") or 0)
            if present:
                # 有名册快照：覆盖率以「现在还在群里的人」为准，历史离群记录不计入分子分母
                info_present = max(present - int(rs.get("missing_info") or 0), 0)
                join_present = max(present - int(rs.get("missing_join") or 0), 0)
                unrec = int(rs.get("missing_any") or 0)
            else:
                # 从未回填/刷新过该群：退回旧的粗估（列表数取上次回填值或群报告值）
                present = int(listed_map.get(gid) or member_count or 0)
                info_present = min(info_counts.get(gid, 0), present)
                join_present = min(join_counts.get(gid, 0), present)
                unrec = max(present - info_counts.get(gid, 0), 0)
            out.append(
                {
                    "group_id": gid,
                    "group_name": str(g.get("group_name") or ""),
                    "member_count": member_count,
                    "listed": present,
                    "has_roster": bool(rs),
                    "info_recorded": info_present,
                    "join_recorded": join_present,
                    "info_recorded_all": info_counts.get(gid, 0),
                    "join_recorded_all": join_counts.get(gid, 0),
                    "departed_recorded": max(join_counts.get(gid, 0) - join_present, 0),
                    "unrecorded": unrec,
                }
            )
        return _ok(
            {
                "groups": out,
                "backfill": backfiller.status(),
                "refresher": refresher.status() if refresher else {},
            }
        )

    async def h_backfill_status(*_args, **_kwargs) -> dict:
        return _ok(backfiller.status())

    async def h_backfill_start(*_args, **_kwargs) -> dict:
        body = await _body()
        group_id = str(body.get("group_id") or _query("group_id") or "")
        started = backfiller.start(group_id)
        if not started:
            return _err("已有读取任务在进行中")
        return _ok({"started": True, "group_id": group_id})

    async def h_backfill_cancel(*_args, **_kwargs) -> dict:
        backfiller.cancel()
        return _ok({"cancel": True})

    async def h_group_members(*_args, **_kwargs) -> dict:
        gid = _query("group_id")
        if not gid.isdigit():
            return _err("缺少或非法的 group_id")
        bot = backfiller.get_bot()
        if not bot:
            return _err("未找到 OneBot 适配器")
        try:
            members = await bot.get_group_member_list(group_id=int(gid))
        except Exception as e:
            return _err(f"获取成员列表失败: {e}")
        try:
            known_info = plugin.box.store.member_info_for_group(gid)
            known_join = plugin.box.store.join_times_for_group(gid)
        except Exception as e:
            return _err(f"读取记录失败: {e}")
        out = []
        for m in members:
            uid = str(m.get("user_id") or "")
            if not uid:
                continue
            info = known_info.get(uid) or {}
            jt = known_join.get(uid) or ""
            if not jt:
                try:
                    api_jt = int(m.get("join_time") or 0)
                    if api_jt > 0:
                        jt = datetime.fromtimestamp(api_jt).strftime("%Y-%m-%d")
                except (TypeError, ValueError, OSError, OverflowError):
                    jt = ""
            out.append(
                {
                    "user_id": uid,
                    "name": str(m.get("card") or m.get("nickname") or uid),
                    "title": info.get("title") or str(m.get("title") or ""),
                    "level": info.get("level") or str(m.get("level") or ""),
                    "role": info.get("role") or str(m.get("role") or ""),
                    "join_recorded": bool(known_join.get(uid)),
                    "join_time": jt,
                    "info_recorded": uid in known_info,
                }
            )
        unrecorded = sum(1 for m in out if not m["info_recorded"] or not m["join_recorded"])
        return _ok({"group_id": gid, "total": len(out), "unrecorded": unrecorded, "members": out})

    async def h_llm_models(*_args, **_kwargs) -> dict:
        """列出可用的对话模型提供商（模型选择以「提供商」为单位）。

        AstrBot 里一个提供商就绑定一个模型，下拉里一项 = 一个提供商，显示成
        「名称 · 模型」；只列已加载的——AstrBot 遇到不存在的提供商 id 会直接
        放弃本次 LLM 请求，所以必须让前端只能从这份名单里选。
        """
        default_id = ""
        try:
            prov = await plugin.context.get_using_provider_async()
            default_id = str((getattr(prov, "provider_config", {}) or {}).get("id") or "")
        except Exception:
            default_id = ""
        items: list[dict] = []
        try:
            for p in plugin.context.get_all_providers() or []:
                cfg_p = getattr(p, "provider_config", {}) or {}
                pid = str(cfg_p.get("id") or "")
                if not pid or not hasattr(p, "text_chat"):
                    continue
                name = str(
                    cfg_p.get("provider_source_id") or cfg_p.get("name") or cfg_p.get("provider") or pid
                )
                try:
                    model = str(p.get_model() or "")
                except Exception:
                    model = ""
                if not model:
                    model = str(cfg_p.get("model") or cfg_p.get("default_model") or "")
                label = f"{name} · {model}" if name and model and name != model else (name or model or pid)
                items.append(
                    {
                        "id": pid,
                        "name": name,
                        "model": model,
                        "label": label,
                        "type": str(cfg_p.get("type") or ""),
                        "modalities": list(cfg_p.get("modalities") or []),
                        "is_default": bool(default_id and pid == default_id),
                    }
                )
        except Exception as e:
            logger.warning(f"[资料卡] 读取提供商列表失败: {e}")
        items.sort(key=lambda it: (not it["is_default"], it["label"]))
        return _ok(
            {
                "items": items,
                "default_id": default_id,
                "selection": plugin.box.get_model_selection(),
            }
        )

    async def h_llm_models_set(*_args, **_kwargs) -> dict:
        body = await _body()
        selection = body.get("selection")
        if not isinstance(selection, dict):
            return _err("缺少 selection 字段")
        try:
            plugin.box.set_model_selection(selection)
        except Exception as e:
            return _err(f"保存失败: {e}")
        logger.info(f"[资料卡] 模型选择已更新: {plugin.box.get_model_selection()}")
        return _ok({"selection": plugin.box.get_model_selection()})

    async def h_info(*_args, **_kwargs) -> dict:
        """面板头部信息：插件版本 + 可选展示字段（单一来源 = 仓库里的配置文件）。

        「信息显示选项」的候选字段以前在前端硬编码了一份，改 schema 后两边容易对不上，
        这里直接从 _conf_schema.json 读出来发给面板。
        """
        root = Path(__file__).resolve().parent.parent
        version = ""
        fields: list[str] = []
        try:
            raw = (root / "metadata.yaml").read_text(encoding="utf-8")
            matched = re.search(r"(?m)^\s*version:\s*v?([0-9][^\s#]*)", raw)
            version = matched.group(1) if matched else ""
        except Exception as e:
            logger.debug(f"[资料卡] 读取插件版本失败: {e}")
        try:
            schema = json.loads((root / "_conf_schema.json").read_text(encoding="utf-8"))
            item = schema.get("display_options")
            if isinstance(item, dict):
                fields = [str(x) for x in item.get("options") or []]
        except Exception as e:
            logger.debug(f"[资料卡] 读取展示字段清单失败: {e}")
        return _ok(
            {
                "version": version,
                "display_fields": fields,
                "show_reg_time": bool(plugin.cfg.show_reg_time),
            }
        )

    # ------------------------------------------------------ 插件配置读写
    async def h_config_get(*_args, **_kwargs) -> dict:
        raw = plugin.cfg._data
        autobox = raw.get("autobox") if isinstance(raw.get("autobox"), dict) else {}
        ai_sub = raw.get("ai_analysis") if isinstance(raw.get("ai_analysis"), dict) else {}
        # 前端 cfgGet 是按点号逐层下钻的，故嵌套项必须还回成 {"autobox": {...}}
        # 而不是扁平的 "autobox.enter"；POST /config 一侧仍收扁平点号键（白名单如此）。
        values: dict[str, Any] = {}
        for key, kind in _CONFIG_FIELDS.items():
            if "." in key:
                head, leaf = key.split(".", 1)
                node = autobox if head == "autobox" else ai_sub
                current = node.get(leaf) if isinstance(node, dict) else None
                values.setdefault(head, {})[leaf] = _coerce(current, kind)
            else:
                values[key] = _coerce(raw.get(key), kind)
        return _ok({"values": values})

    async def h_config_set(*_args, **_kwargs) -> dict:
        body = await _body()
        values = body.get("values")
        if not isinstance(values, dict):
            return _err("缺少 values 字段")
        fields = _CONFIG_FIELDS
        changed = []
        for key, value in values.items():
            kind = fields.get(key)
            if not kind:
                continue  # 白名单外的键直接忽略
            coerced = _coerce(value, kind)
            if "." in key:
                head, leaf = key.split(".", 1)
                node = getattr(plugin.cfg, head)
                setattr(node, leaf, coerced)
            else:
                setattr(plugin.cfg, key, coerced)
            changed.append(key)
        if changed:
            plugin.cfg.save_config()
        logger.info(f"[资料卡] 面板已更新配置: {changed}")
        return _ok({"changed": changed})

    routes = [
        ("/info", h_info, ["GET"]),
        ("/groups", h_groups, ["GET"]),
        ("/backfill/status", h_backfill_status, ["GET"]),
        ("/backfill", h_backfill_start, ["POST"]),
        ("/backfill/cancel", h_backfill_cancel, ["POST"]),
        ("/group/members", h_group_members, ["GET"]),
        ("/llm/models", h_llm_models, ["GET"]),
        ("/llm/models/set", h_llm_models_set, ["POST"]),
        ("/config", h_config_get, ["GET"]),
        ("/config", h_config_set, ["POST"]),
    ]
    for path, fn, methods in routes:
        plugin.context.register_web_api(
            f"{ROUTE_PREFIX}{path}", fn, methods, f"MoeCard {path}"
        )
    logger.info(f"[资料卡] 已注册 {len(routes)} 条成员数据面板路由（前缀 {ROUTE_PREFIX}）")
