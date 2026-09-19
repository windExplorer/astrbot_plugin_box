# config.py
from __future__ import annotations

import json
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


class PluginConfig(ConfigNode):
    only_admin: bool
    protect_ids: list[str]
    autobox: AutoBoxConfig
    display_options: list[str]
    recall_time: int
    desensitize: bool
    ai_analysis: AIAnalysisConfig
    record_join_leave: bool
    init_backfill: bool
    cache_cooldown: int
    max_concurrent: int
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

    _plugin_name: str = "astrbot_plugin_box"

    def __init__(self, cfg: AstrBotConfig, context: Context):
        super().__init__(cfg)
        self.context = context
        self.admins_id: list[str] = context.get_config().get("admins_id", [])
        self.temp_dir = Path(get_astrbot_temp_path()) / self._plugin_name / "box_cards"
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir = Path(get_astrbot_plugin_data_path()) / self._plugin_name
        self._fill_schema_defaults()
        self._normalize_protect_ids()

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

    def _normalize_protect_ids(self):
        if not self.admins_id:
            return
        for admin_id in self.admins_id:
            if admin_id not in self.protect_ids:
                self.protect_ids.append(admin_id)
        self.save_config()

    @property
    def library_switch(self) -> bool:
        return bool(self.mystery_url) and self.mystery_url.startswith("https://")
