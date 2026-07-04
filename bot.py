import os
import subprocess
import sys

try:
    import atexit
    import asyncio
    import json
    import re
    import tempfile
    from datetime import datetime, timedelta, timezone

    import aiohttp
    import discord
    import feedparser
    from discord import app_commands
    from discord.ext import commands, tasks
    from dotenv import load_dotenv
    from gtts import gTTS
except ImportError as import_error:
    print("필수 패키지를 불러오지 못했습니다. 필요한 라이브러리를 설치한 뒤 다시 실행해 주세요.")
    print(f"상세 오류: {import_error}")
    input("엔터를 누르면 종료합니다...")
    raise SystemExit(1)

access_token = os.environ["BOT_TOKEN"]
DISCORD_TOKEN = "access_token"
BOT_VERSION = "3.0.0-py"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
PID_FILE = os.path.join(DATA_DIR, "bot.pid")

PANEL_COLOR = discord.Color(0x6ECB3F)
ENDING_SOON_DAYS = 7
NOTICE_REFRESH: dict[str, asyncio.Task] = {}
verify_state: dict[str, dict] = {}
panel_state: dict[str, dict] = {}
spec_state: dict[str, dict] = {}

SPEC_TABS = [
    ("summary", "요약"),
    ("equip", "장비"),
    ("hexa", "헥사"),
    ("stat", "스탯"),
    ("union", "유니온"),
    ("symbol", "심볼"),
    ("set", "세트"),
]

CARD_PRESET_LABELS = {
    "equip": "착용 장비",
    "1": "프리셋 1",
    "2": "프리셋 2",
    "3": "프리셋 3",
}

GUILD_SETTINGS_FILE = os.path.join(DATA_DIR, "guild-settings.json")
USER_STORE_FILE = os.path.join(DATA_DIR, "verified-users.json")
YOUTUBE_STORE_FILE = os.path.join(DATA_DIR, "youtube-watch.json")

BASE_URL = "https://open.api.nexon.com"
WORLDS = ["스카니아", "루나", "엘리시움", "크로아", "유니온", "제니스", "아케인"]
CHARACTER_TYPES = {
    "기본": "/maplestorym/v1/character/basic",
    "스탯": "/maplestorym/v1/character/stat",
    "길드": "/maplestorym/v1/character/guild",
    "장비": "/maplestorym/v1/character/item-equipment",
    "심볼": "/maplestorym/v1/character/symbol",
    "세트효과": "/maplestorym/v1/character/set-effect",
    "헥사스킬": "/maplestorym/v1/character/hexamatrix-skill",
    "헥사스탯": "/maplestorym/v1/character/hexamatrix-stat",
}
RANKING_TYPES = {
    "레벨": "/maplestorym/v1/ranking/level",
    "무릉": "/maplestorym/v1/ranking/dojang",
    "유니온": "/maplestorym/v1/ranking/union",
    "전투력": "/maplestorym/v1/ranking/combat-power",
}
NOTICE_TYPES = {
    "공지목록": "/maplestorym/v1/notice",
    "패치목록": "/maplestorym/v1/notice-patch",
    "이벤트목록": "/maplestorym/v1/notice-event",
}


class NexonApiError(Exception):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


def _read_json_file(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as file:
            return json.load(file)
    except (json.JSONDecodeError, OSError):
        return {}


def _write_json_file(path: str, data: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)


def _is_process_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def _cleanup_pid_file() -> None:
    try:
        if not os.path.exists(PID_FILE):
            return
        with open(PID_FILE, "r", encoding="utf-8") as file:
            pid = int((file.read() or "0").strip())
        if pid == os.getpid():
            os.remove(PID_FILE)
    except Exception:
        pass


def _stop_process(pid: int) -> None:
    if pid <= 0 or pid == os.getpid():
        return
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
    else:
        import signal
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass


def ensure_single_instance() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    if os.path.exists(PID_FILE):
        try:
            with open(PID_FILE, "r", encoding="utf-8") as file:
                existing_pid = int((file.read() or "0").strip())
            if existing_pid and _is_process_running(existing_pid):
                print(f"기존 봇 종료 중: PID {existing_pid}")
                _stop_process(existing_pid)
        except ValueError:
            pass
    with open(PID_FILE, "w", encoding="utf-8") as file:
        file.write(str(os.getpid()))
    atexit.register(_cleanup_pid_file)


def _patch_guild_settings(guild_id: str, patch: dict) -> dict:
    db = _read_json_file(GUILD_SETTINGS_FILE)
    gid = str(guild_id)
    db[gid] = {**db.get(gid, {}), **patch, "updatedAt": datetime.now(timezone.utc).isoformat()}
    _write_json_file(GUILD_SETTINGS_FILE, db)
    return db[gid]


def get_guild_settings(guild_id: str) -> dict:
    return _read_json_file(GUILD_SETTINGS_FILE).get(str(guild_id), {})


def set_verify_review_channel(guild_id: str, channel_id: str) -> dict:
    return _patch_guild_settings(guild_id, {"verifyReviewChannelId": str(channel_id)})


def get_verify_review_channel_id(guild_id: str) -> str | None:
    return get_guild_settings(guild_id).get("verifyReviewChannelId")


def set_tts_channel(guild_id: str, channel_id: str) -> dict:
    return _patch_guild_settings(guild_id, {"ttsChannelId": str(channel_id)})


def get_tts_channel_id(guild_id: str) -> str | None:
    return get_guild_settings(guild_id).get("ttsChannelId")


def set_server_log_channel(guild_id: str, channel_id: str) -> dict:
    return _patch_guild_settings(guild_id, {"serverLogChannelId": str(channel_id)})


def get_server_log_channel_id(guild_id: str) -> str | None:
    return get_guild_settings(guild_id).get("serverLogChannelId")


def set_member_log_channel(guild_id: str, channel_id: str) -> dict:
    return _patch_guild_settings(guild_id, {"memberLogChannelId": str(channel_id)})


def get_member_log_channel_id(guild_id: str) -> str | None:
    return get_guild_settings(guild_id).get("memberLogChannelId")


def set_music_channel(guild_id: str, channel_id: str) -> dict:
    return _patch_guild_settings(guild_id, {"musicChannelId": str(channel_id)})


def get_music_channel_id(guild_id: str) -> str | None:
    return get_guild_settings(guild_id).get("musicChannelId")


def set_youtube_notify_channel(guild_id: str, channel_id: str) -> dict:
    return _patch_guild_settings(guild_id, {"youtubeNotifyChannelId": str(channel_id)})


def get_youtube_notify_channel_id(guild_id: str) -> str | None:
    return get_guild_settings(guild_id).get("youtubeNotifyChannelId")


def set_notice_panel(guild_id: str, channel_id: int, message_id: int) -> dict:
    return _patch_guild_settings(
        guild_id,
        {"noticePanelChannelId": str(channel_id), "noticePanelMessageId": str(message_id)},
    )


def get_notice_panel(guild_id: str) -> dict | None:
    settings = get_guild_settings(guild_id)
    channel_id = settings.get("noticePanelChannelId")
    message_id = settings.get("noticePanelMessageId")
    if channel_id and message_id:
        return {"channelId": channel_id, "messageId": message_id}
    return None


def set_feature_channel(guild_id, key: str, channel_id: int) -> dict:
    return _patch_guild_settings(guild_id, {key: str(channel_id)})


def get_feature_channel_id(guild_id, key: str) -> str | None:
    return get_guild_settings(guild_id).get(key)


def set_verify_channel(guild_id, channel_id: int) -> dict:
    return set_feature_channel(guild_id, "verifyChannelId", channel_id)


def get_verify_channel_id(guild_id) -> str | None:
    return get_feature_channel_id(guild_id, "verifyChannelId") or os.getenv("VERIFY_CHANNEL_ID")


def save_verified_user(record: dict) -> dict:
    db = _read_json_file(USER_STORE_FILE)
    discord_id = str(record["discordId"])
    db[discord_id] = {**record, "updatedAt": datetime.now(timezone.utc).isoformat()}
    _write_json_file(USER_STORE_FILE, db)
    return db[discord_id]


def get_verified_user(discord_id: str) -> dict | None:
    return _read_json_file(USER_STORE_FILE).get(str(discord_id)) or None


def get_all_verified_users() -> list:
    return list(_read_json_file(USER_STORE_FILE).values())


def get_guild_watch(guild_id: str) -> dict:
    return _read_json_file(YOUTUBE_STORE_FILE).get(str(guild_id), {"channels": []})


def save_guild_watch(guild_id: str, watch: dict) -> dict:
    db = _read_json_file(YOUTUBE_STORE_FILE)
    gid = str(guild_id)
    db[gid] = {**watch, "updatedAt": datetime.now(timezone.utc).isoformat()}
    _write_json_file(YOUTUBE_STORE_FILE, db)
    return db[gid]


def get_all_watches() -> dict:
    return _read_json_file(YOUTUBE_STORE_FILE)


def add_youtube_channel(guild_id: str, channel: dict) -> dict:
    watch = get_guild_watch(guild_id)
    if any(item["channelId"] == channel["channelId"] for item in watch["channels"]):
        return {"error": "이미 등록된 YouTube 채널입니다."}
    watch["channels"].append(channel)
    save_guild_watch(guild_id, watch)
    return {"watch": watch["channels"][-1]}


def remove_youtube_channel(guild_id: str, channel_id: str) -> dict:
    watch = get_guild_watch(guild_id)
    before = len(watch["channels"])
    watch["channels"] = [item for item in watch["channels"] if item["channelId"] != channel_id]
    if len(watch["channels"]) == before:
        return {"error": "등록된 YouTube 채널을 찾을 수 없습니다."}
    save_guild_watch(guild_id, watch)
    return {"watch": watch}


def update_youtube_channel(guild_id: str, channel_id: str, patch: dict):
    watch = get_guild_watch(guild_id)
    target = next((item for item in watch["channels"] if item["channelId"] == channel_id), None)
    if not target:
        return None
    target.update(patch)
    save_guild_watch(guild_id, watch)
    return target


def truncate(text, max_len: int = 1024) -> str:
    value = str(text or "")
    return value if len(value) <= max_len else value[: max_len - 3] + "..."


def _kst_now() -> datetime:
    return datetime.now(timezone(timedelta(hours=9)))


def _parse_period(title: str, year: int):
    match = re.search(r"\((\d{1,2})/(\d{1,2})\s*~\s*(\d{1,2})/(\d{1,2})\)\s*$", title)
    if not match:
        return None
    sm, sd, em, ed = map(int, match.groups())
    end_year = year + 1 if em < sm else year
    start = datetime(year, sm, sd, tzinfo=timezone(timedelta(hours=9)))
    end = datetime(end_year, em, ed, 23, 59, 59, tzinfo=timezone(timedelta(hours=9)))
    return start, end


def _is_ongoing(item: dict) -> bool:
    title = item.get("title", "")
    now = _kst_now()
    year = int(str(item.get("date", now.year))[:4]) if item.get("date") else now.year
    period = _parse_period(title, year)
    if period:
        return period[0] <= now <= period[1]
    month_match = re.search(r"(\d{1,2})월", title)
    return bool(month_match and int(month_match.group(1)) == now.month)


def _is_ending_soon(item: dict) -> bool:
    title = item.get("title", "")
    now = _kst_now()
    year = int(str(item.get("date", now.year))[:4]) if item.get("date") else now.year
    period = _parse_period(title, year)
    if not period:
        return False
    start, end = period
    if not (start <= now <= end):
        return False
    return (end - now).total_seconds() / 86400 <= ENDING_SOON_DAYS


def _format_notice_title(item: dict) -> str:
    title = item.get("title", "제목 없음")
    match = re.search(r"\((\d{1,2}/\d{1,2}\s*~\s*\d{1,2}/\d{1,2})\)\s*$", title)
    if match:
        period = re.sub(r"\s+", " ", match.group(1))
        clean = re.sub(r"\s*\(\d{1,2}/\d{1,2}\s*~\s*\d{1,2}/\d{1,2}\)\s*$", "", title).strip()
        return f"{clean} ({period})"
    return title.strip()


def format_notice_list(data: dict) -> str:
    items = data.get("notice") or data.get("event_notice") or data.get("patch_notice") or []
    ongoing = [item for item in items if _is_ongoing(item)]
    if not ongoing:
        return "현재 진행 중인 항목이 없습니다."
    lines = []
    for item in ongoing:
        text = _format_notice_title(item)
        lines.append(f"🔴 {text}" if _is_ending_soon(item) else text)
    return "\n".join(lines)


def build_all_notices_embed(notice_data, patch_data, event_data) -> discord.Embed:
    now = _kst_now().strftime("%Y-%m-%d %H:%M:%S KST")
    return (
        discord.Embed(
            title="메이플M 진행 중",
            description=f"기준: {now}\n🔴 7일 이내 종료 · ※ 1분마다 자동 갱신됩니다",
            color=PANEL_COLOR,
        )
        .add_field(name="공지", value=truncate(format_notice_list(notice_data)), inline=False)
        .add_field(name="패치", value=truncate(format_notice_list(patch_data)), inline=False)
        .add_field(name="이벤트", value=truncate(format_notice_list(event_data)), inline=False)
        .set_footer(text="Nexon Open API · 1분마다 자동 갱신")
    )


def build_character_embed(type_name: str, data: dict, *, character_name: str = "", world_name: str = "", guild_name: str = "") -> discord.Embed:
    embed = discord.Embed(
        title=f"{character_name or data.get('character_name', '캐릭터')} - {type_name}",
        color=PANEL_COLOR,
    )
    if world_name:
        embed.description = f"**{world_name}** 서버"
    if data.get("character_image"):
        embed.set_thumbnail(url=data["character_image"])
    if type_name == "기본":
        embed.add_field(name="레벨", value=str(data.get("character_level", "-")), inline=True)
        embed.add_field(name="직업", value=str(data.get("character_class", "-")), inline=True)
        embed.add_field(name="길드", value=guild_name or "없음", inline=True)
    return embed.set_footer(text="Nexon Open API")


def build_ranking_embed(type_name: str, data: dict, *, date: str = "", world_name: str = "") -> discord.Embed:
    rows = data.get("ranking") or data.get("dojang_ranking") or data.get("union_ranking") or []
    lines = []
    for item in rows[:10]:
        rank = item.get("ranking") or item.get("rank") or "-"
        name = item.get("character_name") or item.get("guild_name") or "이름 없음"
        world = f" ({item['world_name']})" if item.get("world_name") else ""
        level = f" Lv.{item['character_level']}" if item.get("character_level") else ""
        lines.append(f"**{rank}위** {name}{world}{level}")
    return (
        discord.Embed(
            title=f"메이플M {type_name} 랭킹",
            description=f"기준일: **{date or data.get('date', '-')}**",
            color=PANEL_COLOR,
        )
        .add_field(name="TOP 10", value=truncate("\n".join(lines) or "랭킹 정보 없음"))
        .set_footer(text="Nexon Open API")
    )


def _item_slot_name(item: dict) -> str:
    return item.get("item_equipment_slot_name") or item.get("item_equipment_slot") or "슬롯"


def _item_grade(item: dict) -> str:
    return item.get("item_grade") or item.get("potential_option_grade") or "없음"


def _item_starforce(item: dict) -> int:
    value = item.get("starforce_upgrade") or item.get("starforce") or 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _equipment_list(data: dict | None) -> list:
    return (data or {}).get("item_equipment") or []


def _format_options(options: list | None) -> str:
    if not options:
        return "없음"
    return "\n".join(f"• {opt.get('option_name', '-')}: **{opt.get('option_value', '-')}**" for opt in options)


def _build_stat_embed(stat_data: dict | None) -> discord.Embed:
    stats = (stat_data or {}).get("stat") or (stat_data or {}).get("final_stat") or []
    lines = [f"**{s.get('stat_name', '-')}**: {s.get('stat_value', '-')}" for s in stats]
    return discord.Embed(title="종합 스탯", color=PANEL_COLOR, description=truncate("\n".join(lines) or "스탯 정보 없음", 4000))


def _build_hexa_embed(hexa_skill: dict | None, hexa_stat: dict | None) -> discord.Embed:
    skills = (hexa_skill or {}).get("hexamatrix_skill") or []
    skill_lines = [
        f"**{s.get('skill_name', '-')}** Lv.{s.get('slot_level', '-')} · {s.get('skill_type', '-')}"
        for s in skills
    ]
    stat_lines = []
    for block in (hexa_stat or {}).get("hexamatrix_stat") or []:
        for info in block.get("stat_info") or []:
            if str(info.get("activate_flag")) == "1":
                stat_lines.append(f"**코어 {block.get('stat_core_slot', '-')}** · {info.get('main_stat', '-')} Lv.{info.get('main_stat_level', '-')}")
    embed = discord.Embed(title="헥사 매트릭스", color=PANEL_COLOR)
    embed.add_field(name="HEXA 스킬", value=truncate("\n".join(skill_lines) or "정보 없음"), inline=False)
    embed.add_field(name="HEXA 스탯", value=truncate("\n".join(stat_lines) or "정보 없음"), inline=False)
    return embed


def _build_set_embed(set_data: dict | None) -> discord.Embed:
    items = (set_data or {}).get("set_info") or (set_data or {}).get("set_effect") or []
    lines = []
    for item in items:
        name = item.get("set_name") or item.get("set_effect_name") or "세트"
        count = item.get("set_count") or item.get("set_effect_count") or "-"
        option = item.get("set_option") or item.get("set_effect_option") or item.get("set_effect_description") or ""
        lines.append(f"**{name}** · {count}셋\n{option}".strip())
    return discord.Embed(title="세트 효과", color=PANEL_COLOR, description=truncate("\n\n".join(lines) or "세트 효과 없음", 4000))


def _build_symbol_embed(symbol_data: dict | None) -> discord.Embed:
    lines = []
    for key, label in (("arcane_symbol", "아케인"), ("authentic_symbol", "어센틱")):
        for symbol in (symbol_data or {}).get(key) or []:
            name = str(symbol.get("symbol_name", "심볼")).replace("아케인심볼 : ", "").replace("어센틱심볼 : ", "")
            lines.append(f"**[{label}] {name}** Lv.{symbol.get('symbol_level', '-')}")
    return discord.Embed(title="심볼 정보", color=PANEL_COLOR, description=truncate("\n".join(lines) or "심볼 정보 없음", 4000))


def _build_summary_embed(world: str, basic: dict, guild: dict | None, stat: dict | None) -> discord.Embed:
    embed = discord.Embed(
        title=f"{basic.get('character_name', '-')} Lv.{basic.get('character_level', '-')}",
        description=f"**{basic.get('character_class', '-')}** · {world}\n길드: **{(guild or {}).get('guild_name', '없음')}**",
        color=PANEL_COLOR,
    )
    if basic.get("character_image"):
        embed.set_thumbnail(url=basic["character_image"])
    embed.add_field(name="경험치", value=str(basic.get("character_exp_rate", "-")), inline=True)
    stats = (stat or {}).get("stat") or (stat or {}).get("final_stat") or []
    if stats:
        main_stats = "\n".join(f"**{s.get('stat_name', '-')}**: {s.get('stat_value', '-')}" for s in stats[:8])
        embed.add_field(name="주요 스탯", value=truncate(main_stats), inline=False)
    embed.set_footer(text="탭을 눌러 상세 정보를 확인하세요")
    return embed


def _build_equipment_embed(equipment: list) -> discord.Embed:
    lines = []
    for item in equipment:
        star = f"⭐{_item_starforce(item)}" if _item_starforce(item) else ""
        grade = f"[{_item_grade(item)}]" if _item_grade(item) != "없음" else ""
        lines.append(f"**{_item_slot_name(item)}** {item.get('item_name', '-')} {star} {grade}".strip())
    return discord.Embed(title="장착 장비", color=PANEL_COLOR, description=truncate("\n".join(lines) or "장비 없음", 4000))


def _build_item_detail_embeds(item: dict) -> list[discord.Embed]:
    grade = _item_grade(item)
    star = _item_starforce(item)
    icon = item.get("item_icon") or item.get("item_shape_icon")
    if icon and not str(icon).startswith("http"):
        icon = f"https://{str(icon).lstrip('/')}"
    image_embed = discord.Embed(
        title=f"{item.get('item_name', '-')} · {_item_slot_name(item)}",
        description=f"**{grade}** · ⭐ **{star}**",
        color=PANEL_COLOR,
    )
    if icon:
        image_embed.set_image(url=icon)
    detail = discord.Embed(title=f"{item.get('item_name', '-')} 상세 옵션", color=PANEL_COLOR)
    detail.add_field(name="기본 옵션", value=truncate(_format_options(item.get("item_basic_option"))), inline=False)
    detail.add_field(name="추가 옵션", value=truncate(_format_options(item.get("item_additional_option"))), inline=False)
    if item.get("item_option"):
        detail.add_field(name="요약", value=truncate(item.get("item_option")), inline=False)
    return [image_embed, detail]


def _get_spec_state(message_id: int | str) -> dict:
    return spec_state.setdefault(str(message_id), {})


def _set_spec_state(message_id: int | str, **kwargs) -> dict:
    state = _get_spec_state(message_id)
    state.update(kwargs)
    return state


def _preset_equipment(data: dict | None, preset: str) -> list:
    if not data:
        return []
    if preset == "equip":
        return data.get("item_equipment") or []
    try:
        preset_no = int(preset)
    except (TypeError, ValueError):
        return []
    for entry in data.get("equipment_preset") or []:
        if entry.get("preset_no") == preset_no:
            return entry.get("item_equipment") or []
    return []


def _active_preset_no(data: dict | None) -> str | None:
    try:
        value = int((data or {}).get("use_preset_no") or 0)
    except (TypeError, ValueError):
        return None
    return str(value) if value > 0 else None


def _build_union_embed(union_data: dict | None) -> discord.Embed:
    union = union_data or {}
    embed = discord.Embed(title="유니온 정보", color=PANEL_COLOR)
    embed.add_field(name="유니온 레벨", value=str(union.get("union_level", "-")), inline=True)
    embed.add_field(name="유니온 등급", value=str(union.get("union_grade", "-")), inline=True)
    embed.add_field(
        name="공격대 점수",
        value=str(union.get("union_artifact_level") or union.get("union_power") or "-"),
        inline=True,
    )
    return embed


async def _safe_character_data(type_name: str, ocid: str):
    try:
        return await get_character_data(type_name, ocid)
    except NexonApiError:
        return None


async def _fetch_spec_tab_data(tab: str, ocid: str) -> dict:
    if tab == "summary":
        basic, guild, stat = await asyncio.gather(
            get_character_data("기본", ocid),
            get_character_data("길드", ocid),
            _safe_character_data("스탯", ocid),
        )
        return {"basic": basic, "guild": guild, "stat": stat}
    if tab == "equip":
        return {"equipment": await get_character_data("장비", ocid)}
    if tab == "hexa":
        hexa_skill, hexa_stat = await asyncio.gather(
            _safe_character_data("헥사스킬", ocid),
            _safe_character_data("헥사스탯", ocid),
        )
        return {"hexaSkill": hexa_skill, "hexaStat": hexa_stat}
    if tab == "stat":
        return {"stat": await get_character_data("스탯", ocid)}
    if tab == "union":
        return {"union": await get_union_data("정보", ocid)}
    if tab == "symbol":
        return {"symbol": await _safe_character_data("심볼", ocid)}
    if tab == "set":
        return {"setEffect": await _safe_character_data("세트효과", ocid)}
    return {}


def _build_spec_tab_view(
    active_tab: str,
    equipment: list | None = None,
    *,
    show_equip_back: bool = False,
    preset: str = "equip",
) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for tab_id, label in SPEC_TABS[:4]:
        style = discord.ButtonStyle.primary if tab_id == active_tab else discord.ButtonStyle.secondary
        view.add_item(discord.ui.Button(label=label, style=style, custom_id=f"spec:tab:{tab_id}"))
    for tab_id, label in SPEC_TABS[4:]:
        style = discord.ButtonStyle.primary if tab_id == active_tab else discord.ButtonStyle.secondary
        view.add_item(discord.ui.Button(label=label, style=style, custom_id=f"spec:tab:{tab_id}"))
    if show_equip_back:
        view.add_item(discord.ui.Button(label="장비 목록으로", style=discord.ButtonStyle.secondary, custom_id="spec:equip:back"))
    if active_tab == "equip" and not show_equip_back:
        preset_options = [
            discord.SelectOption(label=label, value=preset_id, default=(preset_id == preset))
            for preset_id, label in CARD_PRESET_LABELS.items()
        ]
        view.add_item(
            discord.ui.Select(
                placeholder="장비 프리셋 선택",
                options=preset_options,
                custom_id="spec:preset:select",
            )
        )
    if active_tab == "equip" and equipment and not show_equip_back:
        options = [
            discord.SelectOption(
                label=truncate(f"{_item_slot_name(item)} {item.get('item_name', '-')}", 100),
                value=str(index),
            )
            for index, item in enumerate(equipment[:25])
        ]
        view.add_item(discord.ui.Select(placeholder="장비 선택 → 상세 정보", options=options, custom_id="spec:equip:select"))
    return view


async def _render_spec_view(state: dict, tab: str = "summary", item_index: str | None = None, *, refresh_equip: bool = True):
    ocid = state.get("ocid")
    message_id = state.get("messageId")
    world_name = state.get("worldName", "")
    preset = state.get("preset", "equip")

    if tab == "equip" and (refresh_equip or not state.get("equipmentData")):
        data = await _fetch_spec_tab_data(tab, ocid)
    elif tab == "equip":
        data = {"equipment": state.get("equipmentData")}
    else:
        data = await _fetch_spec_tab_data(tab, ocid)

    if tab == "equip" and item_index is not None:
        equipment_data = state.get("equipmentData") or data.get("equipment")
        equipment = _preset_equipment(equipment_data, preset)
        item = equipment[int(item_index)]
        embeds = _build_item_detail_embeds(item)
        view = _build_spec_tab_view(tab, equipment, show_equip_back=True, preset=preset)
        _set_spec_state(message_id, activeTab=tab, equipment=equipment, itemIndex=item_index, equipmentData=equipment_data, preset=preset)
        return embeds, view

    equipment = []
    show_equip_select = False

    if tab == "summary":
        basic = state.get("basic") or data.get("basic") or {}
        guild = state.get("guild") if "guild" in state else data.get("guild")
        stat = data.get("stat")
        if not stat:
            stat = await _safe_character_data("스탯", ocid)
        embed = _build_summary_embed(world_name, basic, guild, stat)
    elif tab == "equip":
        equipment_data = data.get("equipment")
        equipment = _preset_equipment(equipment_data, preset)
        preset_label = CARD_PRESET_LABELS.get(preset, preset)
        if preset == "equip":
            active = _active_preset_no(equipment_data)
            if active:
                preset_label = f"{preset_label} (프리셋 {active} 적용 중)"
        embed = _build_equipment_embed(equipment)
        embed.set_footer(text=f"프리셋: {preset_label} · 아래 메뉴에서 장비를 선택하세요")
        show_equip_select = True
        _set_spec_state(message_id, equipment=equipment, itemIndex=None, equipmentData=equipment_data, preset=preset)
    elif tab == "hexa":
        embed = _build_hexa_embed(data.get("hexaSkill"), data.get("hexaStat"))
    elif tab == "stat":
        embed = _build_stat_embed(data.get("stat"))
    elif tab == "union":
        embed = _build_union_embed(data.get("union"))
    elif tab == "symbol":
        embed = _build_symbol_embed(data.get("symbol"))
    elif tab == "set":
        embed = _build_set_embed(data.get("setEffect"))
    else:
        embed = _build_summary_embed(world_name, state.get("basic") or {}, state.get("guild"), None)

    _set_spec_state(message_id, activeTab=tab)
    view = _build_spec_tab_view(tab, equipment if show_equip_select else None, preset=preset if tab == "equip" else "equip")
    return [embed], view


def get_default_ranking_date() -> str:
    kst = datetime.now(timezone(timedelta(hours=9)))
    if kst.hour < 9:
        kst -= timedelta(days=1)
    return kst.strftime("%Y-%m-%d")


async def nexon_fetch(path: str, params: dict | None = None, *, allow_404: bool = False, not_found_message: str = "데이터를 찾을 수 없습니다."):
    api_key = os.getenv("NEXON_API_KEY", "")
    if not api_key or api_key == "your_nexon_api_key_here":
        raise NexonApiError("넥슨 API 키가 설정되지 않았습니다. .env 파일의 NEXON_API_KEY를 설정해 주세요.")

    query = {k: v for k, v in (params or {}).items() if v not in (None, "")}
    headers = {"x-nxopen-api-key": api_key}
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{BASE_URL}{path}", params=query, headers=headers) as response:
            if response.status == 200:
                return await response.json()
            body = await response.text()
            if response.status == 404:
                if allow_404:
                    return None
                raise NexonApiError(not_found_message, 404)
            if response.status == 429:
                raise NexonApiError("API 호출 한도를 초과했습니다. 잠시 후 다시 시도해 주세요.", 429)
            if response.status in (400, 401) or "apikey is not valid" in body:
                raise NexonApiError("넥슨 API 키가 유효하지 않습니다.", response.status)
            raise NexonApiError(f"API 오류 ({response.status}): {body[:200]}", response.status)


async def get_character_ocid(character_name: str, world_name: str) -> dict:
    return await nexon_fetch(
        "/maplestorym/v1/id",
        {"character_name": character_name, "world_name": world_name},
        not_found_message="캐릭터를 찾을 수 없습니다. 닉네임과 월드를 확인해 주세요.",
    )


async def get_character_data(type_name: str, ocid: str, date: str | None = None) -> dict | None:
    path = CHARACTER_TYPES.get(type_name)
    if not path:
        raise NexonApiError("지원하지 않는 캐릭터 조회 종류입니다.")
    return await nexon_fetch(
        path,
        {"ocid": ocid, "date": date},
        allow_404=type_name == "길드",
        not_found_message="캐릭터 정보를 찾을 수 없습니다.",
    )


async def get_union_data(type_name: str, ocid: str, date: str | None = None) -> dict:
    path = "/maplestorym/v1/user/union-raider" if type_name == "공격대" else "/maplestorym/v1/user/union"
    return await nexon_fetch(path, {"ocid": ocid, "date": date}, not_found_message="유니온 정보를 찾을 수 없습니다.")


async def get_guild_id(guild_name: str, world_name: str) -> dict:
    return await nexon_fetch(
        "/maplestorym/v1/guild/id",
        {"guild_name": guild_name, "world_name": world_name},
        not_found_message="길드를 찾을 수 없습니다.",
    )


async def get_guild_basic(oguild_id: str, date: str | None = None) -> dict:
    return await nexon_fetch(
        "/maplestorym/v1/guild/basic",
        {"oguild_id": oguild_id, "date": date},
        not_found_message="길드 정보를 찾을 수 없습니다.",
    )


async def get_ranking_data(type_name: str, *, date: str | None = None, world_name: str | None = None, page: int | None = None) -> dict:
    path = RANKING_TYPES.get(type_name)
    if not path:
        raise NexonApiError("지원하지 않는 랭킹 종류입니다.")
    return await nexon_fetch(
        path,
        {"date": date or get_default_ranking_date(), "world_name": world_name, "page": page},
        not_found_message="랭킹 정보를 찾을 수 없습니다.",
    )


async def get_notice_data(type_name: str) -> dict:
    path = NOTICE_TYPES.get(type_name)
    if not path:
        raise NexonApiError("지원하지 않는 공지 종류입니다.")
    return await nexon_fetch(path, {}, not_found_message="공지 정보를 찾을 수 없습니다.")


def _verified_role(guild: discord.Guild) -> discord.Role | None:
    role_id = os.getenv("VERIFIED_ROLE_ID")
    role_name = os.getenv("VERIFIED_ROLE_NAME", "인증완료")
    if role_id:
        return guild.get_role(int(role_id))
    return discord.utils.get(guild.roles, name=role_name)


def _has_verified_role(member: discord.Member) -> bool:
    role = _verified_role(member.guild)
    return bool(role and role in member.roles)


def _build_nickname(world: str, character_name: str) -> str:
    nickname = f"({world} / {character_name})"
    if len(nickname) <= 32:
        return nickname
    prefix = f"({world} / "
    max_len = 32 - len(prefix) - 1
    return f"{prefix}{character_name[:max_len]})"


class VerifyModal(discord.ui.Modal, title="캐릭터 닉네임 입력"):
    nickname = discord.ui.TextInput(label="닉네임", placeholder="메이플M 캐릭터 닉네임", max_length=50)

    def __init__(self, cog: "VerificationCog", user_id: int):
        super().__init__()
        self.cog = cog
        self.user_id = user_id

    async def on_submit(self, interaction: discord.Interaction):
        state = verify_state.get(str(self.user_id), {})
        if not state.get("world"):
            await interaction.response.send_message("❌ 먼저 서버(월드)를 선택해 주세요.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            profile = await self.cog.fetch_profile(state["world"], str(self.nickname.value).strip())
            state.update({**profile, "nickname": str(self.nickname.value).strip(), "pending": True})
            verify_state[str(self.user_id)] = state
            await self.cog.send_review_request(interaction, self.user_id, state)
            embed = discord.Embed(title="인증 요청 접수", description="관리자 승인 대기 중입니다.", color=PANEL_COLOR)
            embed.add_field(name="서버", value=state["world"], inline=True)
            embed.add_field(name="닉네임", value=state["nickname"], inline=True)
            await interaction.edit_original_response(embed=embed, view=None)
        except NexonApiError as error:
            verify_state.pop(str(self.user_id), None)
            await interaction.edit_original_response(content=f"❌ {error}")
        except Exception as error:
            verify_state.pop(str(self.user_id), None)
            await interaction.edit_original_response(content=f"❌ {error}")


class VerifyView(discord.ui.View):
    def __init__(self, cog: "VerificationCog", user_id: int):
        super().__init__(timeout=600)
        self.cog = cog
        self.user_id = user_id
        options = [discord.SelectOption(label=world, value=world) for world in WORLDS]
        self.add_item(WorldSelect(cog, user_id, options))
        self.add_item(NicknameButton(cog, user_id))


class WorldSelect(discord.ui.Select):
    def __init__(self, cog, user_id, options):
        super().__init__(placeholder="서버(월드) 선택", options=options, custom_id="verify:world")
        self.cog = cog
        self.user_id = user_id

    async def callback(self, interaction: discord.Interaction):
        state = verify_state.setdefault(str(self.user_id), {})
        state["world"] = self.values[0]
        await _defer_component(interaction)


class NicknameButton(discord.ui.Button):
    def __init__(self, cog, user_id):
        super().__init__(label="닉네임 입력", style=discord.ButtonStyle.primary, custom_id="verify:nickname")
        self.cog = cog
        self.user_id = user_id

    async def callback(self, interaction: discord.Interaction):
        state = verify_state.get(str(self.user_id), {})
        if not state.get("world"):
            await interaction.response.send_message("❌ 먼저 서버(월드)를 선택해 주세요.", ephemeral=True)
            return
        await interaction.response.send_modal(VerifyModal(self.cog, self.user_id))


class ReviewView(discord.ui.View):
    def __init__(self, cog: "VerificationCog", applicant_id: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.applicant_id = applicant_id
        self.add_item(discord.ui.Button(label="거절", style=discord.ButtonStyle.danger, custom_id=f"verify:reject:{applicant_id}"))
        self.add_item(discord.ui.Button(label="수락", style=discord.ButtonStyle.success, custom_id=f"verify:accept:{applicant_id}"))


class VerificationCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def fetch_profile(self, world: str, nickname: str) -> dict:
        ocid_data = await get_character_ocid(nickname, world)
        ocid = ocid_data["ocid"]
        basic = await get_character_data("기본", ocid)
        guild = await get_character_data("길드", ocid)
        return {"ocid": ocid, "basic": basic, "guild": guild}

    async def restore_member(self, member: discord.Member):
        existing = get_verified_user(str(member.id))
        if not existing:
            return None
        role = _verified_role(member.guild)
        if role:
            await member.add_roles(role, reason="인증 복구")
        nickname = _build_nickname(existing["world"], existing["characterName"])
        try:
            await member.edit(nick=nickname, reason="인증 복구")
        except discord.Forbidden:
            pass
        return existing

    async def send_review_request(self, interaction: discord.Interaction, user_id: int, state: dict):
        channel_id = (
            get_verify_review_channel_id(interaction.guild_id)
            or os.getenv("VERIFY_REVIEW_CHANNEL_ID")
            or os.getenv("VERIFY_CHANNEL_ID")
        )
        if not channel_id:
            raise RuntimeError("인증 검토 채널이 설정되지 않았습니다.")
        channel = interaction.guild.get_channel(int(channel_id))
        if not channel:
            channel = await interaction.guild.fetch_channel(int(channel_id))
        basic = state["basic"]
        embed = discord.Embed(title="인증 요청", description=f"{interaction.user.mention} 님의 요청", color=PANEL_COLOR)
        embed.add_field(name="서버", value=state["world"], inline=True)
        embed.add_field(name="닉네임", value=basic.get("character_name", state["nickname"]), inline=True)
        embed.add_field(name="레벨", value=str(basic.get("character_level", "-")), inline=True)
        embed.add_field(name="직업", value=str(basic.get("character_class", "-")), inline=True)
        guild_info = state.get("guild") or {}
        embed.add_field(name="길드", value=guild_info.get("guild_name", "없음"), inline=True)
        await channel.send(embed=embed, view=ReviewView(self, user_id))

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        existing = get_verified_user(str(member.id))
        if existing:
            try:
                await self.restore_member(member)
                channel_id = get_verify_channel_id(member.guild.id)
                if channel_id:
                    channel = member.guild.get_channel(int(channel_id))
                    if channel:
                        await channel.send(f"{member.mention} 님, 다시 오신 것을 환영합니다! 인증이 복구되었습니다.")
                return
            except Exception as error:
                print("재입장 복구 실패:", error)
        channel_id = get_verify_channel_id(member.guild.id)
        if not channel_id:
            return
        channel = member.guild.get_channel(int(channel_id))
        if channel:
            embed = discord.Embed(title="서버 입장을 환영합니다", description="캐릭터 인증이 필요합니다.", color=PANEL_COLOR)
            view = discord.ui.View()
            view.add_item(discord.ui.Button(label="인증 시작", style=discord.ButtonStyle.success, custom_id="verify:start"))
            await channel.send(member.mention, embed=embed, view=view)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if before.nick == after.nick:
            return
        user = get_verified_user(str(after.id))
        if not user:
            return
        expected = _build_nickname(user["world"], user["characterName"])
        if after.nick == expected:
            return
        try:
            await after.edit(nick=expected, reason="인증 닉네임 유지")
        except discord.Forbidden:
            pass

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type != discord.InteractionType.component:
            return
        custom_id = interaction.data.get("custom_id", "")
        if not custom_id.startswith("verify:"):
            return
        if custom_id == "verify:start":
            await self.start_verification(interaction)
        elif custom_id.startswith("verify:accept:") or custom_id.startswith("verify:reject:"):
            await self.handle_review(interaction, custom_id)

    async def start_verification(self, interaction: discord.Interaction):
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if not member:
            await interaction.response.send_message("❌ 서버에서만 사용할 수 있습니다.", ephemeral=True)
            return
        existing = get_verified_user(str(member.id))
        if existing and _has_verified_role(member):
            await interaction.response.send_message(
                f"이미 인증된 계정입니다. (**{existing['world']}** · **{existing['characterName']}**)",
                ephemeral=True,
            )
            return
        if existing and not _has_verified_role(member):
            restored = await self.restore_member(member)
            if restored:
                await interaction.response.send_message("재입장 환영합니다! 인증 정보가 복구되었습니다.", ephemeral=True)
                return
        verify_state.pop(str(member.id), None)
        embed = discord.Embed(title="캐릭터 인증", description="서버(월드) 선택 후 닉네임을 입력해 주세요.", color=PANEL_COLOR)
        await interaction.response.send_message(embed=embed, view=VerifyView(self, member.id), ephemeral=True)

    async def handle_review(self, interaction: discord.Interaction, custom_id: str):
        can_manage = interaction.user.guild_permissions.manage_guild or interaction.user.guild_permissions.administrator
        if not can_manage:
            await interaction.response.send_message("❌ 관리자만 수락/거절할 수 있습니다.", ephemeral=True)
            return
        applicant_id = int(custom_id.split(":")[2])
        state = verify_state.get(str(applicant_id))
        if not state or not state.get("basic"):
            await interaction.response.edit_message(content="❌ 만료되었거나 처리된 요청입니다.", embed=None, view=None)
            return
        await interaction.response.defer()
        if custom_id.startswith("verify:reject:"):
            verify_state.pop(str(applicant_id), None)
            await interaction.edit_original_response(content=f"❌ 인증 거절 · <@{applicant_id}>", view=None)
            return
        member = interaction.guild.get_member(applicant_id)
        if not member:
            verify_state.pop(str(applicant_id), None)
            await interaction.edit_original_response(content="❌ 유저가 서버에 없습니다.", view=None)
            return
        role = _verified_role(interaction.guild)
        if role:
            await member.add_roles(role, reason="캐릭터 인증")
        basic = state["basic"]
        char_name = basic.get("character_name", state["nickname"])
        nickname = _build_nickname(state["world"], char_name)
        try:
            await member.edit(nick=nickname, reason="캐릭터 인증")
        except discord.Forbidden:
            pass
        save_verified_user(
            {
                "discordId": str(applicant_id),
                "discordTag": str(member),
                "world": state["world"],
                "characterName": char_name,
                "displayNickname": nickname,
                "ocid": state["ocid"],
                "characterLevel": basic.get("character_level"),
                "characterClass": basic.get("character_class"),
                "guildName": (state.get("guild") or {}).get("guild_name"),
                "verifiedAt": discord.utils.utcnow().isoformat(),
            }
        )
        verify_state.pop(str(applicant_id), None)
        await interaction.edit_original_response(content=f"✅ 인증 완료 · <@{applicant_id}>", view=None)

    verify_group = app_commands.Group(name="인증", description="인증 채널 및 패널 설정")

    @verify_group.command(name="지정채널", description="인증 패널 채널 지정")
    @app_commands.describe(채널="인증 채널 (비우면 현재 설정 조회)")
    @app_commands.default_permissions(manage_guild=True)
    async def verify_set_channel(self, interaction: discord.Interaction, 채널: discord.TextChannel | None = None):
        if 채널:
            set_verify_channel(interaction.guild_id, 채널.id)
            await interaction.response.send_message(f"✅ 인증 채널: {채널.mention}", ephemeral=True)
        else:
            channel_id = get_verify_channel_id(interaction.guild_id)
            await interaction.response.send_message(
                f"현재: {f'<#{channel_id}>' if channel_id else '미설정'}",
                ephemeral=True,
            )

    @verify_group.command(name="패널", description="인증 패널 등록")
    @app_commands.default_permissions(manage_guild=True)
    async def verify_panel(self, interaction: discord.Interaction):
        channel_id = get_verify_channel_id(interaction.guild_id)
        if not channel_id:
            await interaction.response.send_message("❌ 먼저 `/인증 지정채널`을 설정해 주세요.", ephemeral=True)
            return
        channel = interaction.guild.get_channel(int(channel_id))
        if not channel:
            await interaction.response.send_message("❌ 인증 채널을 찾을 수 없습니다.", ephemeral=True)
            return
        embed = discord.Embed(title="서버 입장을 환영합니다", description="캐릭터 인증이 필요합니다.", color=PANEL_COLOR)
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(label="인증 시작", style=discord.ButtonStyle.success, custom_id="verify:start"))
        await channel.send(embed=embed, view=view)
        await interaction.response.send_message(f"✅ {channel.mention} 에 패널을 등록했습니다.", ephemeral=True)

    @verify_group.command(name="시작", description="캐릭터 인증을 시작합니다")
    async def verify_start(self, interaction: discord.Interaction):
        await self.start_verification(interaction)

    @app_commands.command(name="유저", description="내 인증 캐릭터 정보를 조회합니다")
    async def user_info(self, interaction: discord.Interaction):
        user = get_verified_user(str(interaction.user.id))
        if not user:
            await interaction.response.send_message("아직 인증되지 않았습니다.", ephemeral=True)
            return
        embed = discord.Embed(title="유저 정보", color=PANEL_COLOR)
        embed.add_field(name="서버", value=user.get("world", "-"), inline=True)
        embed.add_field(name="닉네임", value=user.get("characterName", "-"), inline=True)
        embed.add_field(name="레벨", value=str(user.get("characterLevel", "-")), inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="유저정보", description="인증 검토 채널을 설정하거나 조회합니다")
    @app_commands.describe(검토채널="수락/거절 검토 채널")
    @app_commands.default_permissions(manage_guild=True)
    async def user_settings(self, interaction: discord.Interaction, 검토채널: discord.TextChannel | None = None):
        if 검토채널:
            set_verify_review_channel(interaction.guild_id, 검토채널.id)
            await interaction.response.send_message(f"✅ 검토 채널: {검토채널.mention}", ephemeral=True)
            return
        channel_id = get_verify_review_channel_id(interaction.guild_id)
        await interaction.response.send_message(f"현재 검토 채널: {f'<#{channel_id}>' if channel_id else '미설정'}", ephemeral=True)


class MapleModal(discord.ui.Modal):
    def __init__(self, title: str, callback):
        super().__init__(title=title)
        self.callback_fn = callback
        self.world = discord.ui.TextInput(label="월드", placeholder="예: 엘리시움", max_length=20)
        self.nickname = discord.ui.TextInput(label="닉네임", max_length=30)
        self.add_item(self.world)
        self.add_item(self.nickname)

    async def on_submit(self, interaction: discord.Interaction):
        await self.callback_fn(interaction, self.world.value.strip(), self.nickname.value.strip())


def _world_select_options() -> list[discord.SelectOption]:
    return [discord.SelectOption(label=world, value=world) for world in WORLDS]


def _panel_state_key(interaction: discord.Interaction) -> str:
    return f"{interaction.message.id}:{interaction.user.id}"


def _get_panel_state(interaction: discord.Interaction) -> dict:
    return panel_state.setdefault(_panel_state_key(interaction), {})


class NicknameModal(discord.ui.Modal):
    def __init__(self, title: str, on_submit_cb):
        super().__init__(title=title)
        self.on_submit_cb = on_submit_cb
        self.nickname = discord.ui.TextInput(label="닉네임", placeholder="캐릭터 닉네임", max_length=30)
        self.add_item(self.nickname)

    async def on_submit(self, interaction: discord.Interaction):
        await self.on_submit_cb(interaction, str(self.nickname.value).strip())


class GuildNameModal(discord.ui.Modal):
    def __init__(self, on_submit_cb):
        super().__init__(title="길드 조회")
        self.on_submit_cb = on_submit_cb
        self.guild_name = discord.ui.TextInput(label="길드명", placeholder="길드 이름", max_length=30)
        self.add_item(self.guild_name)

    async def on_submit(self, interaction: discord.Interaction):
        await self.on_submit_cb(interaction, str(self.guild_name.value).strip())


def _build_world_select(custom_id: str, placeholder: str = "서버(월드) 선택") -> discord.ui.Select:
    return discord.ui.Select(placeholder=placeholder, options=_world_select_options(), custom_id=custom_id)


def _build_ranking_world_select() -> discord.ui.Select:
    options = [discord.SelectOption(label="전체", value="all"), *_world_select_options()]
    return discord.ui.Select(placeholder="서버(월드) 선택 (선택)", options=options, custom_id="panel:ranking:world")


def _modal_text_value(interaction: discord.Interaction, field_id: str = "nickname") -> str:
    for row in interaction.data.get("components", []):
        for component in row.get("components", []):
            if component.get("custom_id") == field_id:
                return str(component.get("value", "")).strip()
    return ""


QUERY_PANEL_PREFIXES = (
    "panel:character:",
    "panel:union:",
    "panel:guild:",
    "panel:ranking:",
)


def _is_query_panel(custom_id: str) -> bool:
    return custom_id.startswith(QUERY_PANEL_PREFIXES)


async def _defer_component(interaction: discord.Interaction) -> None:
    if not interaction.response.is_done():
        await interaction.response.defer()


async def _defer_ephemeral_query(interaction: discord.Interaction) -> None:
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True, thinking=True)


async def _edit_ephemeral_query(
    interaction: discord.Interaction,
    *,
    content: str | None = None,
    embed: discord.Embed | None = None,
    embeds: list[discord.Embed] | None = None,
    view: discord.ui.View | None = None,
) -> None:
    kwargs: dict = {"content": content, "view": view}
    if embeds is not None:
        kwargs["embeds"] = embeds
    elif embed is not None:
        kwargs["embed"] = embed
    else:
        kwargs["embed"] = None
        kwargs["embeds"] = []
    await interaction.edit_original_response(**kwargs)


async def _reply_ephemeral(interaction: discord.Interaction, content: str) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(content, ephemeral=True)
    else:
        await interaction.response.send_message(content, ephemeral=True)


async def _reply_interaction_error(interaction: discord.Interaction, content: str) -> None:
    try:
        if interaction.response.is_done():
            await interaction.edit_original_response(content=content, embeds=[], view=None)
        else:
            await interaction.response.send_message(content, ephemeral=True)
    except discord.HTTPException:
        await _reply_ephemeral(interaction, content)


class FeaturesCog(commands.Cog):
    log_group = app_commands.Group(name="서버로그", description="서버 로그 채널 설정")
    member_log_group = app_commands.Group(name="서버입퇴장로그", description="입퇴장 로그 채널 설정")
    tts_group = app_commands.Group(name="tts", description="TTS 명령")
    music_group = app_commands.Group(name="노래", description="노래 패널 설정")
    yt_group = app_commands.Group(name="유튜브", description="YouTube 알림")
    notice_group = app_commands.Group(name="공지", description="공지 패널 설정")
    character_group = app_commands.Group(name="캐릭터정보", description="캐릭터 조회 패널 설정")
    union_group = app_commands.Group(name="유니온", description="유니온 조회 패널 설정")
    guild_group = app_commands.Group(name="길드", description="길드 조회 패널 설정")
    ranking_group = app_commands.Group(name="랭킹", description="랭킹 조회 패널 설정")

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.tts_sessions: dict[int, dict] = {}
        self.music_sessions: dict[int, dict] = {}
        self._notice_panels_restored = False
        self.youtube_poll.start()

    def cog_unload(self):
        self.youtube_poll.cancel()
        for task in NOTICE_REFRESH.values():
            task.cancel()

    @commands.Cog.listener()
    async def on_ready(self):
        if self._notice_panels_restored:
            return
        self._notice_panels_restored = True
        for guild in self.bot.guilds:
            await self._restore_notice_panel(str(guild.id))

    async def _fetch_notice_embed(self) -> discord.Embed:
        notice_data, patch_data, event_data = await asyncio.gather(
            get_notice_data("공지목록"),
            get_notice_data("패치목록"),
            get_notice_data("이벤트목록"),
        )
        return build_all_notices_embed(notice_data, patch_data, event_data)

    def _stop_notice_refresh(self, guild_id: str) -> None:
        task = NOTICE_REFRESH.pop(guild_id, None)
        if task:
            task.cancel()

    async def _restore_notice_panel(self, guild_id: str) -> None:
        panel = get_notice_panel(guild_id)
        if not panel:
            return
        guild = self.bot.get_guild(int(guild_id))
        if not guild:
            return
        channel = guild.get_channel(int(panel["channelId"]))
        if not channel:
            return
        try:
            await channel.fetch_message(int(panel["messageId"]))
        except discord.NotFound:
            return
        self._stop_notice_refresh(guild_id)
        NOTICE_REFRESH[guild_id] = asyncio.create_task(
            self._refresh_notice(guild_id, int(panel["channelId"]), int(panel["messageId"]))
        )

    async def _refresh_notice(self, guild_id: str, channel_id: int, message_id: int) -> None:
        try:
            while True:
                await asyncio.sleep(60)
                guild = self.bot.get_guild(int(guild_id))
                if not guild:
                    break
                channel = guild.get_channel(channel_id)
                if not channel:
                    break
                try:
                    message = await channel.fetch_message(message_id)
                except discord.NotFound:
                    break
                embed = await self._fetch_notice_embed()
                await message.edit(embed=embed)
        except asyncio.CancelledError:
            pass
        except Exception as error:
            print("공지 갱신 실패:", error)
        finally:
            NOTICE_REFRESH.pop(guild_id, None)

    async def _reply_channel_setting(
        self,
        interaction: discord.Interaction,
        key: str,
        label: str,
        channel: discord.TextChannel | None,
    ) -> None:
        if channel:
            set_feature_channel(interaction.guild_id, key, channel.id)
            await interaction.response.send_message(f"✅ {label}: {channel.mention}", ephemeral=True)
        else:
            channel_id = get_feature_channel_id(interaction.guild_id, key)
            await interaction.response.send_message(
                f"현재: {f'<#{channel_id}>' if channel_id else '미설정'}",
                ephemeral=True,
            )

    def _get_configured_channel(self, interaction: discord.Interaction, key: str) -> discord.TextChannel | None:
        channel_id = get_feature_channel_id(interaction.guild_id, key)
        if not channel_id:
            return None
        channel = interaction.guild.get_channel(int(channel_id))
        return channel if isinstance(channel, discord.TextChannel) else None

    async def _post_notice_panel(self, interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        guild_id = str(interaction.guild_id)
        embed = await self._fetch_notice_embed()
        self._stop_notice_refresh(guild_id)

        panel = get_notice_panel(guild_id)
        message = None
        if panel:
            old_channel = interaction.guild.get_channel(int(panel["channelId"]))
            if old_channel:
                try:
                    old_message = await old_channel.fetch_message(int(panel["messageId"]))
                    if old_channel.id == channel.id:
                        message = old_message
                        await message.edit(embed=embed)
                    else:
                        try:
                            await old_message.unpin()
                        except (discord.Forbidden, discord.HTTPException):
                            pass
                        try:
                            await old_message.delete()
                        except discord.HTTPException:
                            pass
                except discord.NotFound:
                    pass

        if not message:
            message = await channel.send(embed=embed)
            try:
                await message.pin()
            except (discord.Forbidden, discord.HTTPException):
                pass

        set_notice_panel(guild_id, channel.id, message.id)
        NOTICE_REFRESH[guild_id] = asyncio.create_task(
            self._refresh_notice(guild_id, channel.id, message.id)
        )

    @notice_group.command(name="지정채널", description="공지 패널 채널 지정")
    @app_commands.describe(채널="공지 채널 (비우면 현재 설정 조회)")
    @app_commands.default_permissions(manage_guild=True)
    async def notice_set_channel(self, interaction: discord.Interaction, 채널: discord.TextChannel | None = None):
        await self._reply_channel_setting(interaction, "noticeChannelId", "공지 채널", 채널)

    @notice_group.command(name="패널", description="공지 패널 등록")
    @app_commands.default_permissions(manage_guild=True)
    async def notice_panel(self, interaction: discord.Interaction):
        channel = self._get_configured_channel(interaction, "noticeChannelId")
        if not channel:
            await interaction.response.send_message("❌ 먼저 `/공지 지정채널`을 설정해 주세요.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await self._post_notice_panel(interaction, channel)
        await interaction.followup.send(
            f"✅ {channel.mention} 에 공지 패널을 고정했습니다. (1분마다 자동 갱신)",
            ephemeral=True,
        )

    @character_group.command(name="지정채널", description="캐릭터 조회 패널 채널 지정")
    @app_commands.describe(채널="캐릭터 조회 채널 (비우면 현재 설정 조회)")
    @app_commands.default_permissions(manage_guild=True)
    async def character_set_channel(self, interaction: discord.Interaction, 채널: discord.TextChannel | None = None):
        await self._reply_channel_setting(interaction, "characterChannelId", "캐릭터 조회 채널", 채널)

    @character_group.command(name="패널", description="캐릭터 조회 패널 등록")
    @app_commands.default_permissions(manage_guild=True)
    async def character_panel(self, interaction: discord.Interaction):
        channel = self._get_configured_channel(interaction, "characterChannelId")
        if not channel:
            await interaction.response.send_message("❌ 먼저 `/캐릭터정보 지정채널`을 설정해 주세요.", ephemeral=True)
            return
        embed = discord.Embed(
            title="캐릭터정보 조회",
            description="**1.** 서버(월드) 선택\n**2.** `조회하기` 버튼으로 닉네임 입력\n\n탭: 요약 · 장비(프리셋) · 헥사 · 스탯 · 유니온 · 심볼 · 세트",
            color=PANEL_COLOR,
        )
        embed.set_footer(text="캐릭터 상세 정보를 확인합니다")
        view = discord.ui.View(timeout=None)
        view.add_item(_build_world_select("spec:world"))
        view.add_item(discord.ui.Button(label="조회하기", style=discord.ButtonStyle.primary, custom_id="spec:search"))
        await channel.send(embed=embed, view=view)
        await interaction.response.send_message(f"✅ {channel.mention} 에 패널을 등록했습니다.", ephemeral=True)

    @union_group.command(name="지정채널", description="유니온 조회 패널 채널 지정")
    @app_commands.describe(채널="유니온 조회 채널 (비우면 현재 설정 조회)")
    @app_commands.default_permissions(manage_guild=True)
    async def union_set_channel(self, interaction: discord.Interaction, 채널: discord.TextChannel | None = None):
        await self._reply_channel_setting(interaction, "unionChannelId", "유니온 조회 채널", 채널)

    @union_group.command(name="패널", description="유니온 조회 패널 등록")
    @app_commands.default_permissions(manage_guild=True)
    async def union_panel(self, interaction: discord.Interaction):
        channel = self._get_configured_channel(interaction, "unionChannelId")
        if not channel:
            await interaction.response.send_message("❌ 먼저 `/유니온 지정채널`을 설정해 주세요.", ephemeral=True)
            return
        embed = discord.Embed(
            title="유니온 조회 패널",
            description="**1.** 조회 종류 선택\n**2.** 서버(월드) 선택\n**3.** `조회하기` 버튼으로 닉네임 입력",
            color=PANEL_COLOR,
        )
        union_options = [
            discord.SelectOption(label="정보", value="정보"),
            discord.SelectOption(label="공격대", value="공격대"),
        ]
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Select(placeholder="조회 종류 선택", options=union_options, custom_id="panel:union:type"))
        view.add_item(_build_world_select("panel:union:world"))
        view.add_item(discord.ui.Button(label="조회하기", style=discord.ButtonStyle.primary, custom_id="panel:union:search"))
        await channel.send(embed=embed, view=view)
        await interaction.response.send_message(f"✅ {channel.mention} 에 패널을 등록했습니다.", ephemeral=True)

    @guild_group.command(name="지정채널", description="길드 조회 패널 채널 지정")
    @app_commands.describe(채널="길드 조회 채널 (비우면 현재 설정 조회)")
    @app_commands.default_permissions(manage_guild=True)
    async def guild_set_channel(self, interaction: discord.Interaction, 채널: discord.TextChannel | None = None):
        await self._reply_channel_setting(interaction, "guildChannelId", "길드 조회 채널", 채널)

    @guild_group.command(name="패널", description="길드 조회 패널 등록")
    @app_commands.default_permissions(manage_guild=True)
    async def guild_panel(self, interaction: discord.Interaction):
        channel = self._get_configured_channel(interaction, "guildChannelId")
        if not channel:
            await interaction.response.send_message("❌ 먼저 `/길드 지정채널`을 설정해 주세요.", ephemeral=True)
            return
        embed = discord.Embed(
            title="길드 조회 패널",
            description="**1.** 서버(월드) 선택\n**2.** `조회하기` 버튼으로 길드명 입력",
            color=PANEL_COLOR,
        )
        view = discord.ui.View(timeout=None)
        view.add_item(_build_world_select("panel:guild:world"))
        view.add_item(discord.ui.Button(label="조회하기", style=discord.ButtonStyle.primary, custom_id="panel:guild:search"))
        await channel.send(embed=embed, view=view)
        await interaction.response.send_message(f"✅ {channel.mention} 에 패널을 등록했습니다.", ephemeral=True)

    @ranking_group.command(name="지정채널", description="랭킹 조회 패널 채널 지정")
    @app_commands.describe(채널="랭킹 조회 채널 (비우면 현재 설정 조회)")
    @app_commands.default_permissions(manage_guild=True)
    async def ranking_set_channel(self, interaction: discord.Interaction, 채널: discord.TextChannel | None = None):
        await self._reply_channel_setting(interaction, "rankingChannelId", "랭킹 조회 채널", 채널)

    @ranking_group.command(name="패널", description="랭킹 조회 패널 등록")
    @app_commands.default_permissions(manage_guild=True)
    async def ranking_panel(self, interaction: discord.Interaction):
        channel = self._get_configured_channel(interaction, "rankingChannelId")
        if not channel:
            await interaction.response.send_message("❌ 먼저 `/랭킹 지정채널`을 설정해 주세요.", ephemeral=True)
            return
        embed = discord.Embed(
            title="랭킹 조회 패널",
            description="**1.** 랭킹 종류 선택\n**2.** 서버(월드) 선택 (선택)\n**3.** `조회하기` 버튼 클릭",
            color=PANEL_COLOR,
        )
        options = [discord.SelectOption(label=key, value=key) for key in RANKING_TYPES]
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Select(placeholder="랭킹 종류 선택", options=options, custom_id="panel:ranking:type"))
        view.add_item(_build_ranking_world_select())
        view.add_item(discord.ui.Button(label="조회하기", style=discord.ButtonStyle.primary, custom_id="panel:ranking:search"))
        await channel.send(embed=embed, view=view)
        await interaction.response.send_message(f"✅ {channel.mention} 에 패널을 등록했습니다.", ephemeral=True)

    async def _open_spec_card(self, interaction: discord.Interaction, character_name: str, world_name: str) -> None:
        await _defer_ephemeral_query(interaction)
        try:
            ocid = (await get_character_ocid(character_name, world_name))["ocid"]
            summary = await _fetch_spec_tab_data("summary", ocid)
            message = await interaction.original_response()
            state = _set_spec_state(
                message.id,
                ocid=ocid,
                characterName=summary["basic"].get("character_name") or character_name,
                worldName=world_name,
                basic=summary["basic"],
                guild=summary.get("guild"),
                activeTab="summary",
                preset="equip",
                messageId=message.id,
            )
            embeds, view = await _render_spec_view(state, "summary")
            await _edit_ephemeral_query(interaction, embeds=embeds, view=view)
        except NexonApiError as error:
            await _edit_ephemeral_query(interaction, content=f"❌ {error}")

    async def _run_character_query(self, interaction: discord.Interaction, world: str, nick: str) -> None:
        await self._open_spec_card(interaction, nick, world)

    async def _run_union_query(self, interaction: discord.Interaction, world: str, nick: str, union_type: str = "정보") -> None:
        await _defer_ephemeral_query(interaction)
        try:
            ocid = (await get_character_ocid(nick, world))["ocid"]
            data = await get_union_data(union_type, ocid)
            embed = discord.Embed(title=f"{nick} - 유니온 ({union_type})", color=PANEL_COLOR)
            if union_type == "공격대":
                embed.add_field(name="유니온 공격대", value="조회 완료", inline=False)
            else:
                embed.add_field(name="유니온 레벨", value=str(data.get("union_level", "-")), inline=True)
            await _edit_ephemeral_query(interaction, embed=embed)
        except NexonApiError as error:
            await _edit_ephemeral_query(interaction, content=f"❌ {error}")

    async def _run_guild_query(self, interaction: discord.Interaction, world: str, guild_name: str) -> None:
        await _defer_ephemeral_query(interaction)
        try:
            guild_id = await get_guild_id(guild_name, world)
            data = await get_guild_basic(guild_id["oguild_id"])
            embed = discord.Embed(title=f"{data.get('guild_name', '-')} - 길드", color=PANEL_COLOR)
            embed.add_field(name="서버", value=world, inline=True)
            embed.add_field(name="레벨", value=str(data.get("guild_level", "-")), inline=True)
            await _edit_ephemeral_query(interaction, embed=embed)
        except NexonApiError as error:
            await _edit_ephemeral_query(interaction, content=f"❌ {error}")

    async def _run_ranking_query(self, interaction: discord.Interaction, rank_type: str, world: str | None) -> None:
        await _defer_ephemeral_query(interaction)
        try:
            data = await get_ranking_data(rank_type, world_name=world)
            embed = build_ranking_embed(rank_type, data, world_name=world or "")
            await _edit_ephemeral_query(interaction, embed=embed)
        except NexonApiError as error:
            await _edit_ephemeral_query(interaction, content=f"❌ {error}")

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        try:
            await self._handle_interaction(interaction)
        except Exception as error:
            custom_id = (interaction.data or {}).get("custom_id", "")
            print(f"[Interaction 오류] {custom_id}: {error}")
            try:
                await _reply_interaction_error(interaction, "❌ 처리 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.")
            except Exception:
                pass

    async def _handle_interaction(self, interaction: discord.Interaction):
        if interaction.type == discord.InteractionType.modal_submit:
            custom_id = interaction.data.get("custom_id", "")
            if custom_id == "spec:modal" or custom_id.startswith("card:modal:"):
                panel_id = interaction.message.id if interaction.message else None
                if custom_id.startswith("card:modal:"):
                    panel_id = custom_id.split(":", 2)[2]
                if not panel_id:
                    await _reply_ephemeral(interaction, "❌ 패널 메시지를 찾을 수 없습니다.")
                    return
                world = _get_spec_state(panel_id).get("world")
                if not world:
                    await _reply_ephemeral(interaction, "❌ 먼저 서버(월드)를 선택해 주세요.")
                    return
                nickname = _modal_text_value(interaction)
                if not nickname:
                    await _reply_ephemeral(interaction, "❌ 닉네임을 입력해 주세요.")
                    return
                await self._open_spec_card(interaction, nickname, world)
            return

        if interaction.type != discord.InteractionType.component:
            return

        custom_id = interaction.data.get("custom_id", "")

        if custom_id.startswith("card:"):
            card_map = {
                "card:world": "spec:world",
                "card:search": "spec:search",
                "card:stat": "spec:tab:stat",
                "card:hexa": "spec:tab:hexa",
                "card:equip:select": "spec:equip:select",
                "card:equip:back": "spec:equip:back",
            }
            if custom_id in card_map:
                custom_id = card_map[custom_id]
            elif custom_id.startswith("card:preset:"):
                custom_id = custom_id.replace("card:preset:", "spec:preset:", 1)

        if custom_id.startswith("spec:"):
            await self._handle_spec_interaction(interaction, custom_id)
            return

        if custom_id.startswith("panel:") and _is_query_panel(custom_id):
            await self._handle_panel_interaction(interaction, custom_id)

    async def _handle_spec_interaction(self, interaction: discord.Interaction, custom_id: str) -> None:
        message_id = interaction.message.id
        state = _get_spec_state(message_id)
        state["messageId"] = message_id

        if custom_id == "spec:world":
            world = interaction.data.get("values", [""])[0]
            _set_spec_state(message_id, world=world)
            await _reply_ephemeral(interaction, f"✅ 서버: **{world}**")
            return

        if custom_id == "spec:search":
            entry = _get_spec_state(message_id)
            if not entry.get("world"):
                await _reply_ephemeral(interaction, "❌ 먼저 서버(월드)를 선택해 주세요.")
                return
            modal = discord.ui.Modal(title="캐릭터정보 조회", custom_id="spec:modal")
            modal.add_item(
                discord.ui.TextInput(label="닉네임", custom_id="nickname", placeholder="캐릭터 닉네임", max_length=30, required=True)
            )
            await interaction.response.send_modal(modal)
            return

        if not state.get("ocid"):
            await _reply_ephemeral(interaction, "❌ 조회 세션이 만료되었습니다. 패널에서 다시 조회해 주세요.")
            return

        await _defer_component(interaction)
        try:
            if custom_id.startswith("spec:tab:"):
                tab = custom_id.split(":")[2]
                embeds, view = await _render_spec_view(state, tab)
            elif custom_id.startswith("spec:preset:"):
                preset = custom_id.split(":")[2]
                if preset == "select":
                    preset = interaction.data.get("values", ["equip"])[0]
                _set_spec_state(message_id, preset=preset)
                state["preset"] = preset
                embeds, view = await _render_spec_view(state, "equip", refresh_equip=False)
            elif custom_id == "spec:equip:select":
                embeds, view = await _render_spec_view(state, "equip", interaction.data.get("values", ["0"])[0])
            elif custom_id == "spec:equip:back":
                embeds, view = await _render_spec_view(state, "equip")
            else:
                await _reply_interaction_error(interaction, "❌ 알 수 없는 버튼입니다.")
                return
            await interaction.edit_original_response(content=None, embeds=embeds, view=view)
        except NexonApiError as error:
            await _reply_interaction_error(interaction, f"❌ {error}")
        except (IndexError, ValueError):
            await _reply_interaction_error(interaction, "❌ 장비를 찾을 수 없습니다.")
        except discord.HTTPException as error:
            print(f"[Spec UI 오류] {custom_id}: {error}")
            await _reply_interaction_error(interaction, "❌ 화면을 갱신하지 못했습니다. 다시 조회해 주세요.")

    async def _handle_panel_interaction(self, interaction: discord.Interaction, custom_id: str) -> None:
        state = _get_panel_state(interaction)
        handled = True

        if custom_id == "panel:character:world":
            state["world"] = interaction.data.get("values", [""])[0]
            await _reply_ephemeral(interaction, f"✅ 서버: **{state['world']}**")
        elif custom_id == "panel:union:type":
            state["union_type"] = interaction.data.get("values", [""])[0]
            await _reply_ephemeral(interaction, f"✅ 조회 종류: **{state['union_type']}**")
        elif custom_id == "panel:union:world":
            state["world"] = interaction.data.get("values", [""])[0]
            await _reply_ephemeral(interaction, f"✅ 서버: **{state['world']}**")
        elif custom_id == "panel:guild:world":
            state["world"] = interaction.data.get("values", [""])[0]
            await _reply_ephemeral(interaction, f"✅ 서버: **{state['world']}**")
        elif custom_id == "panel:ranking:type":
            state["rank_type"] = interaction.data.get("values", [""])[0]
            await _reply_ephemeral(interaction, f"✅ 랭킹: **{state['rank_type']}**")
        elif custom_id == "panel:ranking:world":
            state["world"] = interaction.data.get("values", [""])[0]
            label = "전체" if state["world"] == "all" else state["world"]
            await _reply_ephemeral(interaction, f"✅ 서버: **{label}**")
        elif custom_id == "panel:character:search":
            if not state.get("world"):
                await _reply_ephemeral(interaction, "❌ 먼저 서버(월드)를 선택해 주세요.")
                return
            world = state["world"]
            await interaction.response.send_modal(
                NicknameModal("캐릭터 조회", lambda inter, nick: self._run_character_query(inter, world, nick))
            )
        elif custom_id == "panel:union:search":
            if not state.get("union_type"):
                await _reply_ephemeral(interaction, "❌ 먼저 조회 종류를 선택해 주세요.")
                return
            if not state.get("world"):
                await _reply_ephemeral(interaction, "❌ 먼저 서버(월드)를 선택해 주세요.")
                return
            world = state["world"]
            union_type = state["union_type"]
            await interaction.response.send_modal(
                NicknameModal("유니온 조회", lambda inter, nick: self._run_union_query(inter, world, nick, union_type))
            )
        elif custom_id == "panel:guild:search":
            if not state.get("world"):
                await _reply_ephemeral(interaction, "❌ 먼저 서버(월드)를 선택해 주세요.")
                return
            world = state["world"]
            await interaction.response.send_modal(
                GuildNameModal(lambda inter, guild_name: self._run_guild_query(inter, world, guild_name))
            )
        elif custom_id == "panel:ranking:search":
            if not state.get("rank_type"):
                await _reply_ephemeral(interaction, "❌ 먼저 랭킹 종류를 선택해 주세요.")
                return
            world_value = state.get("world", "all")
            world = None if world_value == "all" else world_value
            await self._run_ranking_query(interaction, state["rank_type"], world)
        else:
            handled = False

        if not handled and not interaction.response.is_done():
            await _reply_ephemeral(interaction, "❌ 알 수 없는 패널 버튼입니다. 패널을 다시 등록해 주세요.")

    @app_commands.command(name="청소", description="채널 메시지를 삭제합니다")
    @app_commands.describe(개수="삭제할 메시지 수 (1~100)", 유저="특정 유저 메시지만 삭제")
    @app_commands.default_permissions(manage_messages=True)
    async def cleanup(
        self,
        interaction: discord.Interaction,
        개수: app_commands.Range[int, 1, 100],
        유저: discord.Member | None = None,
    ):
        await interaction.response.defer(ephemeral=True)
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.followup.send("❌ 텍스트 채널에서만 가능합니다.", ephemeral=True)
            return
        if 유저:
            messages = [m async for m in channel.history(limit=100) if m.author.id == 유저.id][:개수]
            await channel.delete_messages(messages)
            deleted = len(messages)
        else:
            deleted = len(await channel.purge(limit=개수))
        await interaction.followup.send(f"✅ {deleted}개 삭제했습니다.", ephemeral=True)
        await self._server_log(
            interaction.guild,
            discord.Embed(title="청소 명령 실행", color=PANEL_COLOR, description=f"{interaction.user} · {deleted}개"),
        )

    @log_group.command(name="지정채널", description="서버 로그 채널 지정")
    @app_commands.describe(채널="로그 채널 (비우면 현재 설정 조회)")
    @app_commands.default_permissions(manage_guild=True)
    async def server_log_channel(self, interaction: discord.Interaction, 채널: discord.TextChannel | None = None):
        if 채널:
            set_server_log_channel(interaction.guild_id, 채널.id)
            await interaction.response.send_message(f"✅ 서버 로그: {채널.mention}", ephemeral=True)
        else:
            channel_id = get_server_log_channel_id(interaction.guild_id)
            await interaction.response.send_message(f"현재: {f'<#{channel_id}>' if channel_id else '미설정'}", ephemeral=True)

    @member_log_group.command(name="지정채널", description="입퇴장 로그 채널 지정")
    @app_commands.describe(채널="로그 채널 (비우면 현재 설정 조회)")
    @app_commands.default_permissions(manage_guild=True)
    async def member_log_channel(self, interaction: discord.Interaction, 채널: discord.TextChannel | None = None):
        if 채널:
            set_member_log_channel(interaction.guild_id, 채널.id)
            await interaction.response.send_message(f"✅ 입퇴장 로그: {채널.mention}", ephemeral=True)
        else:
            channel_id = get_member_log_channel_id(interaction.guild_id)
            await interaction.response.send_message(f"현재: {f'<#{channel_id}>' if channel_id else '미설정'}", ephemeral=True)

    async def _server_log(self, guild: discord.Guild, embed: discord.Embed):
        channel_id = get_server_log_channel_id(guild.id)
        if not channel_id:
            return
        channel = guild.get_channel(int(channel_id))
        if channel:
            await channel.send(embed=embed)

    async def _member_log(self, guild: discord.Guild, embed: discord.Embed):
        channel_id = get_member_log_channel_id(guild.id)
        if not channel_id:
            return
        channel = guild.get_channel(int(channel_id))
        if channel:
            await channel.send(embed=embed)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        embed = discord.Embed(title="서버 퇴장", color=discord.Color.red())
        embed.add_field(name="유저", value=str(member), inline=False)
        await self._member_log(member.guild, embed)

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message):
        if not message.guild or message.author.bot:
            return
        embed = discord.Embed(title="메시지 삭제", color=discord.Color.orange())
        embed.add_field(name="채널", value=message.channel.mention, inline=True)
        embed.add_field(name="작성자", value=str(message.author), inline=True)
        embed.add_field(name="내용", value=(message.content or "(없음)")[:1000], inline=False)
        await self._server_log(message.guild, embed)

    @tts_group.command(name="지정채널", description="TTS 지정 채널 설정")
    @app_commands.describe(채널="TTS 채널 (비우면 현재 설정 조회)")
    @app_commands.default_permissions(manage_guild=True)
    async def tts_channel(self, interaction: discord.Interaction, 채널: discord.TextChannel | None = None):
        if 채널:
            set_tts_channel(interaction.guild_id, 채널.id)
            await interaction.response.send_message(f"✅ TTS 채널: {채널.mention}", ephemeral=True)
        else:
            channel_id = get_tts_channel_id(interaction.guild_id)
            await interaction.response.send_message(f"현재: {f'<#{channel_id}>' if channel_id else '미설정'}", ephemeral=True)

    @tts_group.command(name="재생", description="TTS 재생")
    async def tts_play(self, interaction: discord.Interaction, 내용: str):
        channel_id = get_tts_channel_id(interaction.guild_id)
        if channel_id and interaction.channel_id != int(channel_id):
            await interaction.response.send_message(f"❌ <#{channel_id}> 채널에서만 사용 가능합니다.", ephemeral=True)
            return
        if not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.response.send_message("❌ 음성 채널에 참여해 주세요.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await self._play_tts(interaction.guild, interaction.user.voice.channel, interaction.user.id, 내용[:200])
        await interaction.followup.send("🔊 TTS 재생 중", ephemeral=True)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        channel_id = get_tts_channel_id(message.guild.id)
        if not channel_id or message.channel.id != int(channel_id):
            return
        text = (message.content or "").strip()
        if not text or text.startswith("/"):
            return
        member = message.author
        if not member.voice or not member.voice.channel:
            return
        await self._play_tts(message.guild, member.voice.channel, member.id, text[:200])

    async def _play_tts(self, guild: discord.Guild, voice_channel, user_id: int, text: str):
        session = self.tts_sessions.setdefault(guild.id, {"users": set(), "vc": None})
        session["users"].add(user_id)
        if not guild.voice_client or guild.voice_client.channel != voice_channel:
            if guild.voice_client:
                await guild.voice_client.disconnect(force=True)
            await voice_channel.connect()
        session["vc"] = voice_channel
        tts = gTTS(text=text, lang="ko")
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as temp_file:
            path = temp_file.name
        tts.save(path)
        source = discord.FFmpegPCMAudio(path)
        guild.voice_client.play(source)
        while guild.voice_client and guild.voice_client.is_playing():
            await asyncio.sleep(0.5)
        try:
            os.remove(path)
        except OSError:
            pass

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        session = self.tts_sessions.get(member.guild.id)
        if not session:
            return
        if before.channel and before.channel != after.channel:
            session["users"].discard(member.id)
        if session["users"]:
            return
        if member.guild.voice_client:
            await member.guild.voice_client.disconnect(force=True)
        self.tts_sessions.pop(member.guild.id, None)

    @music_group.command(name="지정채널", description="노래 패널 채널 지정")
    @app_commands.describe(채널="노래 채널 (비우면 현재 설정 조회)")
    @app_commands.default_permissions(manage_guild=True)
    async def music_channel(self, interaction: discord.Interaction, 채널: discord.TextChannel | None = None):
        if 채널:
            set_music_channel(interaction.guild_id, 채널.id)
            await interaction.response.send_message(f"✅ 노래 채널: {채널.mention}", ephemeral=True)
        else:
            channel_id = get_music_channel_id(interaction.guild_id)
            await interaction.response.send_message(f"현재: {f'<#{channel_id}>' if channel_id else '미설정'}", ephemeral=True)

    @music_group.command(name="패널", description="노래 패널 등록")
    @app_commands.default_permissions(manage_guild=True)
    async def music_panel(self, interaction: discord.Interaction):
        channel_id = get_music_channel_id(interaction.guild_id)
        if not channel_id:
            await interaction.response.send_message("❌ 먼저 `/노래 지정채널`을 설정해 주세요.", ephemeral=True)
            return
        channel = interaction.guild.get_channel(int(channel_id))
        embed = discord.Embed(
            title="노래 패널",
            description="음성 채널 입장 후 `/tts 재생` 또는 추후 재생 기능을 사용하세요.",
            color=PANEL_COLOR,
        )
        await channel.send(embed=embed)
        await interaction.response.send_message(f"✅ {channel.mention} 에 패널 등록", ephemeral=True)

    @yt_group.command(name="지정채널", description="YouTube 알림 채널")
    @app_commands.describe(채널="알림 채널 (비우면 현재 설정 조회)")
    @app_commands.default_permissions(manage_guild=True)
    async def yt_notify_channel(self, interaction: discord.Interaction, 채널: discord.TextChannel | None = None):
        if 채널:
            set_youtube_notify_channel(interaction.guild_id, 채널.id)
            await interaction.response.send_message(f"✅ YouTube 알림: {채널.mention}", ephemeral=True)
        else:
            channel_id = get_youtube_notify_channel_id(interaction.guild_id)
            await interaction.response.send_message(f"현재: {f'<#{channel_id}>' if channel_id else '미설정'}", ephemeral=True)

    @yt_group.command(name="추가", description="YouTube 채널 등록")
    @app_commands.default_permissions(manage_guild=True)
    async def yt_add(self, interaction: discord.Interaction, 채널: str, 생방: bool = True, 영상: bool = True):
        await interaction.response.defer(ephemeral=True)
        try:
            channel_id = await self._resolve_yt_channel(채널)
            feed = feedparser.parse(f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}")
            latest_id = feed.entries[0].yt_videoid if feed.entries and hasattr(feed.entries[0], "yt_videoid") else None
            name = feed.feed.get("title", channel_id)
            result = add_youtube_channel(
                interaction.guild_id,
                {
                    "channelId": channel_id,
                    "name": name,
                    "notifyLive": 생방,
                    "notifyVideo": 영상,
                    "lastVideoId": latest_id,
                    "lastLiveVideoId": None,
                    "isLive": False,
                },
            )
            if result.get("error"):
                await interaction.followup.send(f"❌ {result['error']}", ephemeral=True)
            else:
                await interaction.followup.send(f"✅ 등록: **{name}**", ephemeral=True)
        except Exception as error:
            await interaction.followup.send(f"❌ {error}", ephemeral=True)

    @yt_group.command(name="삭제", description="YouTube 채널 삭제")
    @app_commands.default_permissions(manage_guild=True)
    async def yt_remove(self, interaction: discord.Interaction, 채널: str):
        watch = get_guild_watch(interaction.guild_id)
        target = next((channel for channel in watch["channels"] if channel["channelId"] == 채널 or 채널 in channel["name"]), None)
        if not target:
            await interaction.response.send_message("❌ 등록된 채널이 없습니다.", ephemeral=True)
            return
        remove_youtube_channel(interaction.guild_id, target["channelId"])
        await interaction.response.send_message("✅ 삭제했습니다.", ephemeral=True)

    @yt_group.command(name="목록", description="등록 목록")
    async def yt_list(self, interaction: discord.Interaction):
        watch = get_guild_watch(interaction.guild_id)
        lines = [f"• **{channel['name']}** (`{channel['channelId']}`)" for channel in watch["channels"]] or ["없음"]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    async def _resolve_yt_channel(self, raw: str) -> str:
        raw = raw.strip()
        if re.fullmatch(r"UC[\w-]{22}", raw):
            return raw
        url = raw if raw.startswith("http") else f"https://www.youtube.com/{raw if raw.startswith('@') else '@' + raw}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers={"User-Agent": "Mozilla/5.0"}) as response:
                html = await response.text()
        match = re.search(r'"channelId":"(UC[^"]+)"', html)
        if not match:
            raise RuntimeError("YouTube 채널 ID를 찾을 수 없습니다.")
        return match.group(1)

    @tasks.loop(minutes=3)
    async def youtube_poll(self):
        await self.bot.wait_until_ready()
        for guild_id, watch in get_all_watches().items():
            notify_id = get_youtube_notify_channel_id(guild_id)
            if not notify_id:
                continue
            guild = self.bot.get_guild(int(guild_id))
            if not guild:
                continue
            channel = guild.get_channel(int(notify_id))
            if not channel:
                continue
            for item in watch.get("channels", []):
                try:
                    feed = feedparser.parse(f"https://www.youtube.com/feeds/videos.xml?channel_id={item['channelId']}")
                    if not feed.entries:
                        continue
                    entry = feed.entries[0]
                    video_id = getattr(entry, "yt_videoid", None) or entry.id.split(":")[-1]
                    if item.get("notifyVideo") and item.get("lastVideoId") and video_id != item["lastVideoId"]:
                        embed = discord.Embed(title="📺 YouTube 새 영상", description=entry.title, color=PANEL_COLOR, url=entry.link)
                        await channel.send(embed=embed)
                        update_youtube_channel(guild_id, item["channelId"], {"lastVideoId": video_id})
                    elif not item.get("lastVideoId"):
                        update_youtube_channel(guild_id, item["channelId"], {"lastVideoId": video_id})
                except Exception as error:
                    print("YT poll error:", error)

    @youtube_poll.before_loop
    async def before_yt(self):
        await self.bot.wait_until_ready()


class MapleBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        await self.add_cog(VerificationCog(self))
        await self.add_cog(FeaturesCog(self))
        synced = await self.tree.sync()
        print(f"[MapleBot {BOT_VERSION}] Slash command synced: {len(synced)}")

    async def on_ready(self):
        print(f"[MapleBot {BOT_VERSION}] 로그인 완료: {self.user} ({self.user.id})")


def main():
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
    token = os.getenv("DISCORD_TOKEN") or DISCORD_TOKEN
    if not token:
        raise RuntimeError("DISCORD_TOKEN이 설정되지 않았습니다.")
    ensure_single_instance()
    bot = MapleBot()
    bot.run(token, log_handler=None)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("사용자 중단으로 종료합니다.")
    except Exception as error:
        import traceback
        traceback.print_exc()
        print(f"오류로 종료되었습니다: {error}")
        input("엔터를 누르면 종료합니다...")
