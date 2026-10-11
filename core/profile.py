from __future__ import annotations

import re
import textwrap
from dataclasses import asdict, dataclass, field, fields
from datetime import date, datetime
from typing import Any, ClassVar

from zhdate import ZhDate

from astrbot.api import logger


# QQ 号位数 → 注册年代。腾讯从未公开号段归属，这是社区通行的粗略推算，
# 一律在卡片上标注「按号段估算」；适配器真给了 reg_time 时优先用真值。
_QQ_ERA_BY_LENGTH: dict[int, str] = {
    1: "1998~1999年",
    2: "1998~1999年",
    3: "1998~1999年",
    4: "1998~1999年",
    5: "1998~1999年",
    6: "1999~2000年",
    7: "2000~2002年",
    8: "2002~2005年",
    9: "2005~2009年",
    10: "2009~2012年",
}
_QQ_ERA_FALLBACK = "2013年以后"

_YEAR_RE = re.compile(r"(1[6-9]\d{2}|20\d{2})")


def estimate_qq_reg_era(qq: Any) -> str:
    """按 QQ 号位数粗略推算注册年代；不是纯数字或无法判断时返回空串。"""
    text = str(qq or "").strip()
    if not text.isdigit():
        return ""
    return _QQ_ERA_BY_LENGTH.get(len(text), _QQ_ERA_FALLBACK)


def parse_epoch(value: Any) -> int:
    """把适配器给的时间戳/日期文本统一成 Unix 秒，失败返回 0。

    兼容秒级与毫秒级时间戳（数字或数字字符串）、以及 "2008-06-01" / "2008年" /
    "2008/6/1" 这类日期文本（只取年份，注册时间本来也只显示年份）。
    """
    if value is None:
        return 0
    text = str(value).strip()
    if not text:
        return 0
    if text.isdigit():
        try:
            num = int(text)
        except ValueError:
            return 0
        if num > 10_000_000_000:  # 毫秒级（13 位）
            num //= 1000
        return num if num > 0 else 0
    matched = _YEAR_RE.search(text)
    if not matched:
        return 0
    try:
        return int(datetime(int(matched.group(1)), 1, 1).timestamp())
    except (ValueError, OSError, OverflowError):
        return 0


def normalize_sex(value: Any) -> str:
    """归一化性别的各种写法；字段缺失（None/空）返回 ""。

    OneBot 用 male/female/unknown，部分适配器用 1/2/0 或中文，
    显式的「unknown」会保留下来，卡片上显示成「保密」而不是留空。
    """
    if value is None:
        return ""
    text = str(value).strip().lower()
    if not text:
        return ""
    if text in ("male", "m", "1", "男", "true"):
        return "male"
    if text in ("female", "f", "2", "女"):
        return "female"
    return "unknown"


def _to_int(value: Any) -> int | None:
    """宽松转 int：None / 空 / 非数字（适配器脏数据）一律返回 None。"""
    if value is None or value == "":
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _first_int(source: dict[str, Any], keys: tuple[str, ...]) -> int:
    for key in keys:
        num = _to_int(source.get(key))
        if num:
            return num
    return 0


def _split_birthday(value: Any) -> tuple[int, int, int]:
    """把 "1998-05-20" / "05-20" / "1998年5月20日" / "19980520" 拆成 (年,月,日)。

    时间戳（10/13 位纯数字）在这里一律视为无效，避免把注册时间当成生日。
    """
    text = str(value or "").strip()
    if not text:
        return (0, 0, 0)
    if text.isdigit():
        if len(text) == 8:  # YYYYMMDD
            return (int(text[:4]), int(text[4:6]), int(text[6:]))
        return (0, 0, 0)
    nums = [int(n) for n in re.findall(r"\d+", text)]
    if len(nums) >= 3:
        return (nums[0], nums[1], nums[2])
    if len(nums) == 2:
        return (0, nums[0], nums[1])
    return (0, 0, 0)


def _valid_birthday(year: Any, month: Any, day: Any) -> tuple[int, int, int]:
    """校验并规范化生日三元组；不合法的分量按 0（未知）处理。"""

    def _num(value: Any) -> int:
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return 0

    y, m, d = _num(year), _num(month), _num(day)
    if not (1900 <= y <= 2100):
        y = 0
    if not (1 <= m <= 12):
        m = 0
    if not (1 <= d <= 31):
        d = 0
    if (m and not d) or (d and not m):
        return (y, 0, 0)
    return (y, m, d)


def parse_birthday(*sources: dict[str, Any]) -> tuple[int, int, int]:
    """从各个数据源里尽力解析生日 (年,月,日)，缺项为 0。

    不同适配器的字段名差得很远（birthday_year / birthday_y / birth_year，
    或干脆一个 "birthday": "1998-05-20" 字符串），逐个试过再用。
    """
    for source in sources:
        if not isinstance(source, dict) or not source:
            continue
        year = _first_int(source, ("birthday_year", "birth_year", "birthday_y", "birthYear"))
        month = _first_int(source, ("birthday_month", "birth_month", "birthday_m", "birthMonth"))
        day = _first_int(source, ("birthday_day", "birth_day", "birthday_d", "birthDay"))
        if not (year or month or day):
            raw = source.get("birthday")
            if raw is None:
                raw = source.get("birth") or source.get("birthdate")
            year, month, day = _split_birthday(raw)
        if year or month or day:
            return _valid_birthday(year, month, day)
    return (0, 0, 0)


def age_from_birthday(year: int, month: int, day: int) -> int:
    """由完整生日推算年龄（QQ 的年龄本来就是从生日算的）；缺项返回 0。"""
    if not (year and month and day):
        return 0
    today = date.today()
    age = today.year - year - ((today.month, today.day) < (month, day))
    return age if 0 < age < 130 else 0


def join_duration_suffix(join_time: Any) -> str:
    """Human-readable "已入群 <时长>" suffix; empty on bad/missing input."""
    try:
        joined_at = datetime.fromtimestamp(int(join_time))
    except (TypeError, ValueError, OSError, OverflowError):
        return ""
    duration = friendly_duration(datetime.now() - joined_at)
    if duration == "不到 1 秒":
        return "（刚刚加入）"
    return f"（已入群 {duration}）"


def friendly_duration(delta: Any) -> str:
    """Friendly duration: start from the largest non-zero unit down to seconds.

    单位一律单字（天/时/分/秒）：加群时间行要在 32px 字号下和
    「2025-03-18 09:41:22」这样的时间戳同排，写「小时/分钟」会把
    后缀的「）」挤到第二行。

    Examples: 5分3秒 -> "5分3秒"; 2天0时0分3秒 -> "2天3秒"; 871天 -> "871天".
    """
    try:
        total = int(delta.total_seconds())
    except (TypeError, ValueError, AttributeError):
        return ""
    if total <= 0:
        return "不到 1 秒"
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}天")
    if hours:
        parts.append(f"{hours}时")
    if minutes:
        parts.append(f"{minutes}分")
    if seconds:
        parts.append(f"{seconds}秒")
    return "".join(parts[:3]) or "不到 1 秒"


@dataclass(slots=True)
class BoxUserProfile:
    """QQ user profile data used by the box service."""

    FIELD_LABELS: ClassVar[dict[str, str]] = {
        "user_id": "QQ号",
        "uid": "UID",
        "qid": "QID",
        "nickname": "昵称",
        "remark": "备注",
        "card": "群昵称",
        "title": "群头衔",
        "role": "群身份",
        "sex": "性别",
        "birthday": "生日",
        "constellation": "星座",
        "zodiac": "生肖",
        "age": "年龄",
        "kBloodType": "血型",
        "phoneNum": "电话",
        "eMail": "邮箱",
        "homeTown": "家乡",
        "address": "现居",
        "college": "学校",
        "pos": "职位",
        "makeFriendCareer": "职业",
        "labels": "个性标签",
        "unfriendly": "风险账号",
        "is_robot": "机器人账号",
        "is_vip": "QQVIP",
        "is_years_vip": "年VIP",
        "vip_level": "VIP等级",
        "like_count": "获赞",
        "level": "群等级",
        "join_time": "加群时间",
        "last_sent": "最后发言",
        "qqLevel": "QQ等级",
        "reg_time": "注册时间",
        "login_days": "登录天数",
        "isHideQQLevel": "隐藏QQ等级",
        "isHidePrivilegeIcon": "特权图标",
        "isBlock": "屏蔽用户",
        "isMsgDisturb": "免打扰",
        "isSpecialCareOpen": "特别关心",
        "isSpecialCareZone": "空间特别关心",
        "long_nick": "签名",
        "names": "姓名",
        "phone_numbers": "号码",
        "id_numbers": "身份证",
        "wb_numbers": "微博",
        "passwords": "密码",
        "emails": "邮箱",
        "addresses": "地址",
    }
    user_id: str = ""
    uid: str = ""
    qid: str = ""
    nickname: str = ""
    remark: str = ""
    card: str = ""
    title: str = ""
    role: str = ""
    sex: str = ""
    birthday_year: Any = None
    birthday_month: Any = None
    birthday_day: Any = None
    age: Any = None
    blood_type: Any = None
    phone_number: str = ""
    email: str = ""
    home_town: str = ""
    country: str = ""
    province: str = ""
    city: str = ""
    college: str = ""
    pos: str = ""
    career: Any = None
    labels: Any = None
    unfriendly: bool = False
    is_robot: bool = False
    is_vip: bool = False
    is_years_vip: bool = False
    vip_level: Any = None
    like_count: Any = None
    group_level: Any = None
    join_time: Any = None
    last_sent: Any = None
    qq_level: Any = None
    reg_time: Any = None
    login_days: Any = None
    hide_qq_level: bool = False
    hide_privilege_icon: bool = False
    is_block: bool = False
    is_msg_disturb: bool = False
    is_special_care_open: bool = False
    is_special_care_zone: bool = False
    long_nick: str = ""
    names: list[str] = field(default_factory=list)
    nicknames: list[str] = field(default_factory=list)
    phone_numbers: list[str] = field(default_factory=list)
    id_numbers: list[str] = field(default_factory=list)
    wb_numbers: list[str] = field(default_factory=list)
    passwords: list[str] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    addresses: list[str] = field(default_factory=list)

    @classmethod
    def from_sources(
        cls,
        stranger_info: dict[str, Any],
        member_info: dict[str, Any] | None = None,
        library_info: dict[str, Any] | None = None,
        like_count: Any = None,
    ) -> BoxUserProfile:
        """Build a profile from source data.

        Args:
            stranger: Data returned by get_stranger_info.
            member: Data returned by get_group_member_info.
            library: Data returned by the library API.
            like_count: Profile like count from the adapter's extended API.

        Returns:
            User profile model.
        """
        stranger = stranger_info or {}
        member = member_info or {}
        library = library_info or {}

        # 性别/年龄：陌生人接口经常给不全（尤其对刚进群的人），群成员接口
        # （OneBot 规范里 get_group_member_info 同样返回 sex/age）拿来兜底
        sex = normalize_sex(stranger.get("sex")) or normalize_sex(member.get("sex"))
        age = _first_int(stranger, ("age",)) or _first_int(member, ("age",))
        birth_year, birth_month, birth_day = parse_birthday(stranger, library)
        if not age:
            # QQ 的年龄本来就是从生日算出来的；接口没给年龄时自己算
            age = age_from_birthday(birth_year, birth_month, birth_day) or None

        return cls(
            user_id=str(stranger.get("user_id") or ""),
            uid=str(stranger.get("uid") or ""),
            qid=str(stranger.get("qid") or ""),
            nickname=str(stranger.get("nickname") or ""),
            remark=str(stranger.get("remark") or ""),
            card=str(member.get("card") or ""),
            title=str(member.get("title") or ""),
            role=str(member.get("role") or ""),
            sex=sex,
            birthday_year=birth_year or None,
            birthday_month=birth_month or None,
            birthday_day=birth_day or None,
            age=age,
            blood_type=stranger.get("kBloodType"),
            phone_number=str(stranger.get("phoneNum") or ""),
            email=str(stranger.get("eMail") or ""),
            home_town=str(stranger.get("homeTown") or ""),
            country=str(stranger.get("country") or ""),
            province=str(stranger.get("province") or ""),
            city=str(stranger.get("city") or ""),
            college=str(stranger.get("college") or ""),
            pos=str(stranger.get("pos") or ""),
            career=stranger.get("makeFriendCareer"),
            labels=stranger.get("labels"),
            unfriendly=bool(member.get("unfriendly")),
            is_robot=bool(member.get("is_robot")),
            is_vip=bool(stranger.get("is_vip")),
            is_years_vip=bool(stranger.get("is_years_vip")),
            vip_level=stranger.get("vip_level"),
            like_count=like_count,
            group_level=member.get("level"),
            join_time=member.get("join_time"),
            last_sent=member.get("last_sent_time"),
            qq_level=cls._pick_qq_level(stranger),
            reg_time=cls._pick_reg_time(stranger, library),
            login_days=stranger.get("login_days"),
            hide_qq_level=bool(stranger.get("isHideQQLevel")),
            hide_privilege_icon=bool(stranger.get("isHidePrivilegeIcon")),
            is_block=bool(stranger.get("isBlock")),
            is_msg_disturb=bool(stranger.get("isMsgDisturb")),
            is_special_care_open=bool(stranger.get("isSpecialCareOpen")),
            is_special_care_zone=bool(stranger.get("isSpecialCareZone")),
            long_nick=str(stranger.get("long_nick") or ""),
            names=library.get("names") or [],
            nicknames=library.get("nicknames") or [],
            phone_numbers=library.get("phone_numbers") or [],
            id_numbers=library.get("id_numbers") or [],
            wb_numbers=library.get("wb_numbers") or [],
            passwords=library.get("passwords") or [],
            emails=library.get("emails") or [],
            addresses=library.get("addresses") or [],
        )

    @staticmethod
    def _pick_qq_level(stranger: dict[str, Any]) -> Any:
        """Pick the QQ level from differently-named adapter fields.

        NapCat/LLOneBot/Lagrange use different key names (and some return 0
        when unknown), so try the known aliases and treat 0/absent as missing.
        """
        for key in ("qqLevel", "qq_level", "level"):
            value = stranger.get(key)
            try:
                if value is not None and int(value) > 0:
                    return value
            except (TypeError, ValueError):
                continue
        return None

    @staticmethod
    def _pick_reg_time(*sources: dict[str, Any]) -> int | None:
        """注册时间：兼容秒/毫秒时间戳与日期文本，统一成 Unix 秒。

        适配器（NapCat 等）与资料库接口的字段名不统一，逐个试；都没有则返回 None，
        届时卡片上会退回「按 QQ 号号段估算」的显示（见 reg_time_text）。
        """
        for source in sources:
            if not isinstance(source, dict):
                continue
            for key in ("reg_time", "regTime", "register_time", "registerTime", "create_time"):
                if key not in source:
                    continue
                ts = parse_epoch(source.get(key))
                if ts:
                    return ts
        return None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BoxUserProfile:
        """Build a profile from persisted data.

        Args:
            data: Persisted profile data.

        Returns:
            User profile model.
        """
        field_names = {item.name for item in fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in field_names})

    def to_dict(self) -> dict[str, Any]:
        """Export profile data for persistence.

        Returns:
            Serializable profile data.
        """
        return asdict(self)

    def to_display_lines(
        self,
        display_options: list[str],
        desensitize: bool = False,
    ) -> list[str]:
        """Format enabled profile fields for display.

        Args:
            display_options: Enabled field labels or keys.
            desensitize_library: Whether to mask sensitive library values.

        Returns:
            Display lines in configured order.
        """
        enabled_options = set(display_options)
        lines: list[str] = []
        for key, label in self.FIELD_LABELS.items():
            if key not in enabled_options and label not in enabled_options:
                continue
            try:
                lines.extend(self._format_field(key, label, desensitize))
            except Exception as e:
                # 单个字段碰上适配器脏数据（非数字的时间戳、畸形字符串等）不该
                # 让整张卡片渲染失败：跳过这一行，只留日志
                logger.debug(f"[资料卡] 字段 {key} 格式化失败，已跳过: {e}")
        return lines

    @property
    def birthday_parts(self) -> tuple[int, int, int]:
        """(年, 月, 日)；未知的分量为 0（适配器只给年月、或只给月日都能用）。"""
        return _valid_birthday(self.birthday_year, self.birthday_month, self.birthday_day)

    @property
    def birthday(self) -> date | None:
        year, month, day = self.birthday_parts
        if year and month and day:
            try:
                return date(year, month, day)
            except ValueError:
                return None
        return None

    @property
    def birthday_text(self) -> str:
        """生日文本：年月日齐全 → 1998-05-20，只有月日 → 05-20，只有年 → 1998年。"""
        year, month, day = self.birthday_parts
        if month and day:
            return f"{year:04d}-{month:02d}-{day:02d}" if year else f"{month:02d}-{day:02d}"
        return f"{year}年" if year else ""

    @property
    def reg_time_text(self) -> str:
        """注册时间文本：适配器真值优先，拿不到时按 QQ 号号段估算（会标注「估算」）。

        QQ 官方没有开放「注册时间」接口，能拿到的只有适配器扩展字段；拿不到就用
        号段推算一个年代区间，总比整行消失强——但必须让用户看出这是估的。
        """
        if self.reg_time:
            ts = parse_epoch(self.reg_time)
            if ts:
                try:
                    return datetime.fromtimestamp(ts).strftime("%Y年")
                except (ValueError, OSError, OverflowError):
                    pass
            matched = _YEAR_RE.search(str(self.reg_time))
            if matched:
                return f"{matched.group(1)}年"
        era = estimate_qq_reg_era(self.user_id)
        return f"{era}（按号段估算）" if era else ""

    def _format_field(
        self,
        key: str,
        label: str,
        desensitize: bool = False,
    ) -> list[str]:
        birth_year, birth_month, birth_day = self.birthday_parts

        match key:
            case "user_id":
                return [f"{label}：{self.user_id}"] if self.user_id else []
            case "uid":
                return [f"{label}：{self.uid}"] if self.uid else []
            case "qid":
                return [f"{label}：{self.qid}"] if self.qid else []
            case "nickname":
                return [f"{label}：{self.nickname}"] if self.nickname else []
            case "remark":
                return [f"{label}：{self.remark}"] if self.remark else []
            case "card":
                return [f"{label}：{self.card}"] if self.card else []
            case "title":
                return [f"{label}：{self.title}"] if self.title else []
            case "role":
                text = {"owner": "群主", "admin": "管理员", "member": "成员"}.get(self.role)
                return [f"{label}：{text}"] if text else []
            case "sex":
                # unknown = 账号隐藏了性别（接口明确说了，不是我们没拿到）
                text = {"male": "男", "female": "女", "unknown": "保密"}.get(self.sex)
                return [f"{label}：{text}"] if text else []
            case "birthday":
                text = self.birthday_text
                return [f"{label}：{text}"] if text else []
            case "constellation":
                if birth_month and birth_day:
                    return [f"{label}：{self._get_constellation(birth_month, birth_day)}"]
                return []
            case "zodiac":
                # 生肖要年份，接口只给月日时算不了
                if birth_year and birth_month and birth_day:
                    return [
                        f"{label}：{self._get_zodiac(birth_year, birth_month, birth_day)}"
                    ]
                return []
            case "age":
                try:
                    age_value = int(self.age)
                except (TypeError, ValueError):
                    return []
                return [f"{label}：{age_value}岁"] if age_value > 0 else []
            case "kBloodType":
                blood = _to_int(self.blood_type)
                if blood:
                    return [f"{label}：{self._get_blood_type(blood)}"]
                return []
            case "phoneNum":
                if self.phone_number and self.phone_number != "-":
                    return [f"{label}：{self.phone_number}"]
                return []
            case "eMail":
                if self.email and self.email != "-":
                    return [f"{label}：{self.email}"]
                return []
            case "homeTown":
                if self.home_town and self.home_town != "0-0-0":
                    return [f"{label}：{self._parse_home_town(self.home_town)}"]
                return []
            case "address":
                if self.country == "中国" and (self.province or self.city):
                    return [f"{label}：{self.province or ''}-{self.city or ''}"]
                return [f"{label}：{self.country}"] if self.country else []
            case "college":
                return [f"{label}：{self.college}"] if self.college else []
            case "pos":
                return [f"{label}：{self.pos}"] if self.pos else []
            case "makeFriendCareer":
                career = _to_int(self.career)
                if career:
                    return [f"{label}：{self._get_career(career)}"]
                return []
            case "labels":
                return [f"{label}：{self.labels}"] if self.labels else []
            case "unfriendly":
                return [f"{label}：有"] if self.unfriendly else []
            case "is_robot":
                return [f"{label}：是"] if self.is_robot else []
            case "is_vip":
                return [f"{label}：已开"] if self.is_vip else []
            case "is_years_vip":
                return [f"{label}：已开"] if self.is_years_vip else []
            case "vip_level":
                vip = _to_int(self.vip_level)
                return [f"{label}：{vip}"] if vip else []
            case "like_count":
                return [f"{label}：{self.like_count}"] if self.like_count else []
            case "level":
                # 部分适配器把群等级返回成字符串 "0"（truthy），需转 int 后按 0 过滤
                try:
                    level_value = int(self.group_level)
                except (TypeError, ValueError):
                    return []
                return [f"{label}：{level_value}级"] if level_value > 0 else []
            case "join_time":
                if self.join_time:
                    try:
                        join_date = datetime.fromtimestamp(int(self.join_time)).strftime("%Y-%m-%d")
                        return [f"{label}：{join_date}{join_duration_suffix(self.join_time)}"]
                    except (TypeError, ValueError, OSError, OverflowError):
                        return []
                return []
            case "last_sent":
                if self.last_sent is None or self.last_sent == "":
                    return []  # 适配器没给这个字段，不知道发言情况
                try:
                    sent_ts = int(self.last_sent)
                except (TypeError, ValueError):
                    return []
                join_ts = 0
                try:
                    join_ts = int(self.join_time or 0)
                except (TypeError, ValueError):
                    join_ts = 0
                # 从未发言的成员，NapCat/NTQQ 会把成员记录的创建时间（=入群时间）
                # 当作 last_sent_time 回填；发言不可能早于入群，这类值一律视为未发言
                if sent_ts <= 0 or (join_ts and sent_ts <= join_ts):
                    return [f"{label}：未发言"]
                try:
                    sent_text = datetime.fromtimestamp(sent_ts).strftime("%Y-%m-%d %H:%M")
                    return [f"{label}：{sent_text}"]
                except (TypeError, ValueError, OSError, OverflowError):
                    return []
            case "qqLevel":
                if self.hide_qq_level:
                    return [f"{label}：隐藏"]
                level = _to_int(self.qq_level)
                if level:
                    return [f"{label}：{self._format_qq_level(level)}"]
                return []
            case "reg_time":
                text = self.reg_time_text
                return [f"{label}：{text}"] if text else []
            case "login_days":
                try:
                    days = int(self.login_days)
                except (TypeError, ValueError):
                    return []
                return [f"{label}：{days}天"] if days > 0 else []
            case "isHidePrivilegeIcon":
                return [f"{label}：隐藏"] if self.hide_privilege_icon else []
            case "isBlock":
                return [f"{label}：是"] if self.is_block else []
            case "isMsgDisturb":
                return [f"{label}：是"] if self.is_msg_disturb else []
            case "isSpecialCareOpen":
                return [f"{label}：是"] if self.is_special_care_open else []
            case "isSpecialCareZone":
                return [f"{label}：是"] if self.is_special_care_zone else []
            case "long_nick":
                if self.long_nick:
                    return textwrap.wrap(f"{label}：{self.long_nick}", width=15)
                return []
            case (
                "phone_numbers"
                | "id_numbers"
                | "names"
                | "nicknames"
                | "wb_numbers"
                | "passwords"
                | "emails"
                | "addresses"
            ):
                values = list(getattr(self, key))
                if not values:
                    return []
                if desensitize and key == "phone_numbers":
                    values = [self._mask_phone(value) for value in values]
                elif desensitize and key == "id_numbers":
                    values = [self._mask_id_number(value) for value in values]

                prefix = f"{label}："
                indent = "　" * (len(label) + 1)
                return [
                    f"{prefix}{values[0]}",
                    *(f"{indent}{value}" for value in values[1:]),
                ]
            case _:
                return []

    def _mask_phone(self, number: str) -> str:
        match = re.search(r"(\d{11})", number)
        if not match:
            return re.sub(r"\d", "*", number)

        digits = match.group(1)
        masked = re.sub(r"(\d{3})\d{4}(\d{4})", r"\1****\2", digits)
        prefix = number[: match.start(1)]
        suffix = number[match.end(1) :]
        return f"{prefix}{masked}{suffix}"

    def _mask_id_number(self, id_num: str) -> str:
        match = re.search(r"(\d{17}[0-9Xx]|\d{15})", id_num)
        if not match:
            return re.sub(r"[0-9A-Za-z]", "*", id_num)

        core = match.group(1)
        if re.fullmatch(r"\d{6}\d{8}\w{4}", core):
            masked = re.sub(r"(\d{6})\d{8}(\w{4})", r"\1********\2", core)
        elif re.fullmatch(r"\d{6}\d{5}\w{4}", core):
            masked = re.sub(r"(\d{6})\d{5}(\w{4})", r"\1******\2", core)
        else:
            masked = (
                f"{core[:3]}{'*' * (len(core) - 5)}{core[-2:]}"
                if len(core) > 5
                else "*" * len(core)
            )

        prefix = id_num[: match.start(1)]
        suffix = id_num[match.end(1) :]
        return f"{prefix}{masked}{suffix}"

    def _format_qq_level(self, level: int) -> str:
        icons = ["👑", "🌞", "🌙", "⭐"]
        levels = [64, 16, 4, 1]
        result = ""
        original_level = level
        for icon, value in zip(icons, levels):
            count, level = divmod(level, value)
            result += icon * count
        result += f" {original_level}级"
        return result

    def _get_constellation(self, month: int, day: int) -> str:
        constellations = {
            "白羊座": ((3, 21), (4, 19)),
            "金牛座": ((4, 20), (5, 20)),
            "双子座": ((5, 21), (6, 20)),
            "巨蟹座": ((6, 21), (7, 22)),
            "狮子座": ((7, 23), (8, 22)),
            "处女座": ((8, 23), (9, 22)),
            "天秤座": ((9, 23), (10, 22)),
            "天蝎座": ((10, 23), (11, 21)),
            "射手座": ((11, 22), (12, 21)),
            "摩羯座": ((12, 22), (1, 19)),
            "水瓶座": ((1, 20), (2, 18)),
            "双鱼座": ((2, 19), (3, 20)),
        }

        for constellation, (
            (start_month, start_day),
            (end_month, end_day),
        ) in constellations.items():
            if (month == start_month and day >= start_day) or (
                month == end_month and day <= end_day
            ):
                return constellation
            if start_month > end_month and (
                (month == start_month and day >= start_day)
                or (month == end_month + 12 and day <= end_day)
            ):
                return constellation
        return f"星座{month}-{day}"

    def _get_zodiac(self, year: int, month: int, day: int) -> str:
        zodiacs = [
            "鼠🐀",
            "牛🐂",
            "虎🐅",
            "兔🐇",
            "龙🐉",
            "蛇🐍",
            "马🐎",
            "羊🐏",
            "猴🐒",
            "鸡🐔",
            "狗🐕",
            "猪🐖",
        ]

        current = date(year, month, day)

        try:
            spring = ZhDate(year, 1, 1).to_datetime().date()
            zodiac_year = year if current >= spring else year - 1
        except (TypeError, AttributeError):
            zodiac_year = year

        return zodiacs[(zodiac_year - 2020) % 12]

    def _get_career(self, num: int) -> str:
        career = {
            1: "计算机/互联网/通信",
            2: "生产/工艺/制造",
            3: "医疗/护理/制药",
            4: "金融/银行/投资/保险",
            5: "商业/服务业/个体经营",
            6: "文化/广告/传媒",
            7: "娱乐/艺术/表演",
            8: "律师/法务",
            9: "教育/培训",
            10: "公务员/行政/事业单位",
            11: "模特",
            12: "空姐",
            13: "学生",
            14: "其他职业",
        }
        return career.get(num, f"职业{num}")

    def _get_blood_type(self, num: int) -> str:
        blood_types = {1: "A型", 2: "B型", 3: "O型", 4: "AB型", 5: "其他血型"}
        return blood_types.get(num, f"血型{num}")

    def _parse_home_town(self, home_town_code: str) -> str:
        country_map = {
            "49": "中国",
            "250": "俄罗斯",
            "222": "特里尔",
            "217": "法国",
        }
        province_map = {
            "98": "北京",
            "99": "天津/辽宁",
            "100": "冀/沪/吉",
            "101": "苏/豫/晋/黑/渝",
            "102": "浙/鄂/蒙/川",
            "103": "皖/湘/黔/陕",
            "104": "闽/粤/滇/甘/台",
            "105": "赣/桂/藏/青/港",
            "106": "鲁/琼/陕/宁/澳",
            "107": "新疆",
        }

        country_code, province_code, _ = home_town_code.split("-")
        country = country_map.get(country_code, f"外国{country_code}")

        if country_code != "49":
            return country
        if province_code == "0":
            return country
        return province_map.get(province_code, f"{province_code}省")
