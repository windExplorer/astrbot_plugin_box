# config.py
from __future__ import annotations

import json
import shutil
from collections.abc import Mapping, MutableMapping
from pathlib import Path
from types import MappingProxyType, UnionType
from typing import Any, Union, get_args, get_origin, get_type_hints

from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.star.context import Context
from astrbot.core.utils.astrbot_path import (
    get_astrbot_plugin_data_path,
    get_astrbot_temp_path,
)


class ConfigNode:
    _SCHEMA_CACHE: dict[type, dict[str, type]] = {}
    _FIELDS_CACHE: dict[type, set[str]] = {}

    @classmethod
    def _schema(cls) -> dict[str, type]:
        return cls._SCHEMA_CACHE.setdefault(cls, get_type_hints(cls))

    @classmethod
    def _fields(cls) -> set[str]:
        return cls._FIELDS_CACHE.setdefault(
            cls,
            {k for k in cls._schema() if not k.startswith("_")},
        )

    @staticmethod
    def _is_optional(tp: type) -> bool:
        if get_origin(tp) in (Union, UnionType):
            return type(None) in get_args(tp)
        return False

    def __init__(self, data: MutableMapping[str, Any]):
        object.__setattr__(self, "_data", data)
        object.__setattr__(self, "_children", {})
        for key, tp in self._schema().items():
            if key.startswith("_"):
                continue
            if key in data:
                continue
            if hasattr(self.__class__, key):
                continue
            if self._is_optional(tp):
                continue
            logger.warning(f"[config:{self.__class__.__name__}] miss key: {key}")

    def __getattr__(self, key: str) -> Any:
        if key in self._fields():
            value = self._data.get(key)
            tp = self._schema().get(key)

            if isinstance(tp, type) and issubclass(tp, ConfigNode):
                children: dict[str, ConfigNode] = self.__dict__["_children"]
                if key not in children:
                    if not isinstance(value, MutableMapping):
                        raise TypeError(
                            f"[config:{self.__class__.__name__}] not a dict"
                        )
                    children[key] = tp(value)
                return children[key]

            return value

        if key in self.__dict__:
            return self.__dict__[key]

        raise AttributeError(key)

    def __setattr__(self, key: str, value: Any) -> None:
        if key in self._fields():
            self._data[key] = value
            return
        object.__setattr__(self, key, value)

    def raw_data(self) -> Mapping[str, Any]:
        return MappingProxyType(self._data)

    def save_config(self) -> None:

        if not isinstance(self._data, AstrBotConfig):
            raise RuntimeError(
                f"{self.__class__.__name__}.save_config() only support AstrBotConfig"
            )
        self._data.save_config()


class AutoBoxConfig(ConfigNode):
    white_groups: list[str]
    enter: bool
    exit: bool


class AIAnalysisConfig(ConfigNode):
    avatar_analysis: bool
    signature_analysis: bool
    overall_analysis: bool
    event_analysis: bool


class LLMModelsConfig(ConfigNode):
    """各 LLM 调用点的模型选择；面板「模型设置」下拉与本配置块读写同一份数据。"""

    fallback: str
    welcome: str
    avatar: str
    signature: str
    overall: str


class PluginConfig(ConfigNode):
    only_admin: bool
    protect_ids: list[str]
    autobox: AutoBoxConfig
    show_reg_time: bool
    display_options: list[str]
    recall_time: int
    desensitize: bool
    avatar_shape: str
    ai_analysis: AIAnalysisConfig
    record_join_leave: bool
    init_backfill: bool
    cache_cooldown: int
    max_concurrent: int
    llm_models: LLMModelsConfig
    welcome_enabled: bool
    welcome_text: str
    welcome_images: list
    welcome_ai_enabled: bool
    welcome_ai_prompt: str
    welcome_ai_retry: int
    welcome_private_rules: bool
    group_rules: str
    black_groups: list
    mystery_url: str
    mystery_cookies: str

    _plugin_name: str = "astrbot_plugin_moe_box"
    _old_plugin_name: str = "astrbot_plugin_box"

    def __init__(self, cfg: AstrBotConfig, context: Context):
        super().__init__(cfg)
        self.context = context
        self.admins_id: list[str] = context.get_config().get("admins_id", [])
        self.temp_dir = Path(get_astrbot_temp_path()) / self._plugin_name / "box_cards"
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir = Path(get_astrbot_plugin_data_path()) / self._plugin_name
        self._migrate_renamed_plugin()
        self._migrate_reg_time_switch()
        self._fill_schema_defaults()
        self._strip_admin_ids()

    def _migrate_renamed_plugin(self) -> None:
        """v0.12.22：插件从 ``astrbot_plugin_box`` 改名为 ``astrbot_plugin_moe_box``。

        AstrBot 用「目录名 / 插件名」当身份，改名后等于一个全新插件：
        - 配置文件是 ``data/config/<插件名>_config.json``，新文件里只有 schema 默认值；
        - 数据目录是 ``data/plugin_data/<插件名>``，本地库（入群时间/名册/卡片缓存）会被孤立。

        两者都做一次性搬迁，**旧文件保留不动**（便于回退，也便于之后卸载旧插件）：
        - 数据目录：新目录不存在而旧目录存在 → 整目录复制（含 member_times.db 与 -wal/-shm）；
        - 配置：仅当 AstrBot 这次刚创建新配置（``first_deploy``，内容还是 schema 默认值）
          且旧配置文件存在时，把旧值整体搬进来。之后每次启动 ``first_deploy`` 都是 False，
          不会把用户新改的配置覆盖回去。
        """
        # —— 数据目录 ——
        old_data = self.data_dir.parent / self._old_plugin_name
        if old_data.is_dir() and not self.data_dir.exists():
            try:
                shutil.copytree(old_data, self.data_dir)
                logger.info(
                    f"[config:{self.__class__.__name__}] 插件改名迁移：数据目录 "
                    f"{old_data} → {self.data_dir}"
                )
            except Exception as e:
                logger.warning(
                    f"[config:{self.__class__.__name__}] 数据目录迁移失败（沿用旧目录）: {e}"
                )

        # —— 配置文件 ——
        try:
            config_path = str(getattr(self._data, "config_path", "") or "")
            if not config_path:
                return
            # first_deploy=True 表示 AstrBot 这次刚按 schema 默认值创建了新配置文件，
            # 里面还没有任何用户数据——这正是搬迁旧配置的安全时机。
            old_cfg = Path(config_path).parent / f"{self._old_plugin_name}_config.json"
            if not (bool(getattr(self._data, "first_deploy", False)) and old_cfg.is_file()):
                return
            old_values = json.loads(old_cfg.read_text(encoding="utf-8-sig"))
            if not isinstance(old_values, dict) or not old_values:
                return
            self._data.update(old_values)
            self.save_config()
            logger.info(
                f"[config:{self.__class__.__name__}] 插件改名迁移：已从 {old_cfg.name} "
                f"搬入 {len(old_values)} 个配置键"
            )
        except Exception as e:
            logger.warning(
                f"[config:{self.__class__.__name__}] 旧配置迁移失败（使用默认配置）: {e}"
            )

    def _migrate_reg_time_switch(self) -> None:
        """v0.12.20：注册时间从「信息显示选项」独立成开关 ``show_reg_time``。

        老存档只能靠 ``display_options`` 里有没有「注册时间」判断用户想要什么：
        有 → 开关开；没有 → 开关关（尊重之前的选择）。判断完就把该项从列表里摘掉，
        免得同一件事有两个入口。全新安装时 ``display_options`` 还不存在，这里直接
        返回，交给 ``_fill_schema_defaults`` 用 schema 默认值（开）。

        迁移后必须立刻落盘：否则下次启动时列表里已经没有「注册时间」，会被误判成
        「用户把它关掉了」。
        """
        if "show_reg_time" in self._data:
            return
        options = self._data.get("display_options")
        if not isinstance(options, list):
            return
        self._data["show_reg_time"] = "注册时间" in options
        self._data["display_options"] = [item for item in options if item != "注册时间"]
        logger.info(
            f"[config:{self.__class__.__name__}] 「注册时间」已迁移为独立开关 "
            f"show_reg_time={self._data['show_reg_time']}"
        )
        try:
            self.save_config()
        except Exception as e:
            logger.warning(f"[config:{self.__class__.__name__}] 迁移注册时间开关后落盘失败: {e}")

    @property
    def protected_ids(self) -> set[str]:
        """实际生效的保护名单：手动配置 ∪ Bot 管理员。

        管理员不写进 protect_ids（历史版本会并表写盘，导致面板里冒出
        用户没填过的 QQ 号），改为在读取时取并集；统一成 str 也顺手修掉
        配置里存数字 ID 时 `target_id in protect_ids` 判不等的问题。
        """
        ids = list(self.protect_ids or []) + list(self.admins_id or [])
        return {str(x).strip() for x in ids if str(x).strip()}

    def _fill_schema_defaults(self) -> None:
        """旧版本保存的配置可能缺少新增键（AstrBot 不一定合并）：按 _conf_schema.json 的 default 补齐。

        否则新增配置在旧存档上读取为 None 甚至 AttributeError（未声明字段）。
        """
        try:
            schema_path = Path(__file__).resolve().parent.parent / "_conf_schema.json"
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"[config:{self.__class__.__name__}] 读取 _conf_schema.json 失败: {e}")
            return

        changed = False
        for key, item in schema.items():
            if not isinstance(item, dict):
                continue
            if item.get("type") == "object":
                sub = self._data.get(key)
                if not isinstance(sub, MutableMapping):
                    default = item.get("default")
                    sub = dict(default) if isinstance(default, MutableMapping) else {}
                    self._data[key] = sub
                    changed = True
                if isinstance(item.get("items"), dict):
                    for sub_key, sub_item in item["items"].items():
                        if isinstance(sub_item, dict) and sub_key not in sub and "default" in sub_item:
                            sub[sub_key] = sub_item["default"]
                            changed = True
            elif key not in self._data and "default" in item:
                self._data[key] = item["default"]
                changed = True
        if changed:
            logger.info(f"[config:{self.__class__.__name__}] 已按 schema 补齐缺失的配置键")
            try:
                self.save_config()
            except Exception:
                pass

    def _strip_admin_ids(self):
        """一次性清掉旧版自动并入 protect_ids 的管理员 ID。

        保护语义不变（管理员仍受保护，见 protected_ids）；清掉后配置里
        只剩用户自己填的 QQ 号，面板回显也不再有多余条目。
        """
        admins = {str(x).strip() for x in self.admins_id or []}
        if not admins:
            return
        current = list(self.protect_ids or [])
        kept = [x for x in current if str(x).strip() not in admins]
        if len(kept) != len(current):
            self.protect_ids = kept
            self.save_config()
            logger.info(
                f"[config:{self.__class__.__name__}] 已从 protect_ids 清掉 "
                f"{len(current) - len(kept)} 个自动并入的管理员 ID"
            )

    @property
    def library_switch(self) -> bool:
        return bool(self.mystery_url) and self.mystery_url.startswith("https://")
