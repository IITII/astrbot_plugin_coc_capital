from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger

import httpx
import asyncio
import time
import unicodedata
from math import ceil
from typing import Dict, Optional
from datetime import datetime, timedelta


def clean_tag(tag):
    return tag.replace("#", "").upper()


def encode_tag(tag):
    return "%23" + clean_tag(tag)

    # 时间转换


def parse_time(t: str):
    dt = datetime.strptime(t, "%Y%m%dT%H%M%S.%fZ")
    dt = dt + timedelta(hours=8)
    return dt.strftime("%Y/%m/%d %H:%M")


def cache_set(cache, key, value):
    cache[key] = {
        "time": time.time(),
        "data": value
    }


def cache_get(cache, key, ttl):
    item = cache.get(key)
    if not item:
        return None
    if time.time() - item["time"] > ttl:
        del cache[key]
        return None
    return item["data"]


def _str_width(s: str) -> int:
    """计算字符串显示宽度（支持中文）"""
    w = 0
    for c in str(s):
        if unicodedata.east_asian_width(c) in ("F", "W"):
            w += 2
        else:
            w += 1
    return w


def _pad(s, width, align="left"):
    """按显示宽度补齐"""
    s = str(s)
    w = _str_width(s)
    pad = width - w
    if pad <= 0:
        return s

    if align == "right":
        return " " * pad + s
    return s + " " * pad


def json_to_table(
        data,
        headers: dict = None,
        sep="  ",
        sort_by=None,
        reverse=True,
        add_rank=False,
):
    """
    JSON(list[dict]) 转文本表格
    headers: dict, key:字段名, value:列标题, dict顺序即输出顺序
    """
    if not data:
        return ""

    data = list(data)

    # 排序
    if sort_by:
        data.sort(key=lambda x: x.get(sort_by, 0), reverse=reverse)

    # 自动排名
    if add_rank:
        for i, d in enumerate(data, 1):
            d["rank"] = i
        # 将 rank 插入到 headers 第一列
        headers = {"rank": "排名", **headers}

    if headers is None:
        # 默认取第一个 dict 的 key 顺序
        headers = {k: k for k in data[0].keys()}

    keys = list(headers.keys())
    titles = list(headers.values())

    rows = []
    rows.append(titles)

    for item in data:
        rows.append([item.get(k, "") for k in keys])

    # 计算列宽
    widths = []
    for col in range(len(keys)):
        widths.append(max(_str_width(row[col]) for row in rows))

    lines = []

    for i, row in enumerate(rows):
        parts = []
        for col, val in enumerate(row):
            align = "right" if isinstance(val, (int, float)) else "left"
            parts.append(_pad(val, widths[col], align))
        lines.append(sep.join(parts))

        # 表头分隔
        if i == 0:
            lines.append(sep.join("-" * w for w in widths))

    return "\n".join(lines)


def single_attack_medals(log, perk_medals, sub_medals):
    medals = []

    for d in log.get("districts", []):
        if d.get("stars") == 3:
            # Capital Peak
            if d.get("id") == 70000000 or d.get("name") == "Capital Peak":
                medals.append(perk_medals.get(d.get("districtHallLevel"), 0))
            else:
                medals.append(sub_medals.get(d.get("districtHallLevel"), 0))
    return medals


def attack_medals(season):
    perk_medals = {
        1: 45, 2: 180, 3: 360, 4: 585, 5: 810,
        6: 1115, 7: 1240, 8: 1260, 9: 1375, 10: 1450
    }

    sub_medals = {
        1: 135, 2: 225, 3: 350, 4: 405, 5: 460
    }
    total_medals = 0
    total_attacks = 0

    for log in season.get("attackLog", []):
        total_medals += sum(single_attack_medals(log, perk_medals, sub_medals))
        total_attacks += log.get("attackCount", 0)

    if total_attacks == 0:
        return 0
    return ceil(total_medals / total_attacks) * 6


@register("astrbot_plugin_coc_capital", "COC都城", "查询多个部落的突袭币, 感谢 warreport 提供数据支持", "1.3.0")
class CocCapitalPlugin(Star):

    def __init__(self, context: Context, config: Optional[Dict] = None):
        super().__init__(context)

        self.config = config if config else {}

        self.apiKey = self.config.get("apiKey", None)
        self.ua = self.config.get("ua")

        self.max_tags: int = self.config.get("max_tags")

        self.clan_cache_ttl: int = self.config.get("clan_cache_ttl")
        self.raid_cache_ttl: int = self.config.get("raid_cache_ttl")

        # 新增：并发控制
        self.concurrency: int = self.config.get("concurrency")
        self.semaphore = asyncio.Semaphore(self.concurrency)

        self.tagCache = {}
        self.raidCache = {}

        # apiKey 未配置直接禁用
        if not self.apiKey:
            logger.error("COC都城插件未配置 apiKey，插件不会启用")
            self.disabled = True
        else:
            logger.info("COC都城插件已配置 apiKey，启用ing")
            self.disabled = False

    async def initialize(self):
        if self.disabled:
            return
        logger.info("COC都城插件已启动")

    def get_headers(self):
        return {
            "User-Agent": self.ua,
            "apikey": self.apiKey,
            "origin": "https://www.warreport.app",
            "Accept": "application/json"
        }

    # 获取部落信息
    async def fetch_clan(self, client, tag):

        cached = cache_get(self.tagCache, tag, self.clan_cache_ttl)
        if cached:
            return cached

        url = f"https://clashapi.colinschmale.dev/v1/clans/{encode_tag(tag)}"

        async with self.semaphore:
            try:
                resp = await client.get(url, headers=self.get_headers())
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                logger.error(f"{tag} clan 查询失败: {e}")
                return {}

        cache_set(self.tagCache, tag, data)
        return data

    # 查询 raid
    async def fetch_raid_raw(self, client, tag):
        cached = cache_get(self.raidCache, tag, self.raid_cache_ttl)
        if cached:
            return cached

        url = f"https://clashapi.colinschmale.dev/v1/clans/{encode_tag(tag)}/capitalraidseasons?limit=1"

        async with self.semaphore:
            try:
                resp = await client.get(url, headers=self.get_headers())
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                logger.error(f"{tag} raid 查询失败: {e}")
                return None

        if "items" not in data or not data["items"]:
            return None
        cache_set(self.raidCache, tag, data)
        return data

    async def fetch_raid(self, client, tag):

        data = await self.fetch_raid_raw(client, tag)
        season = data["items"][0]

        clan_info = await self.fetch_clan(client, tag)
        clan_name = clan_info.get("name", "未知")

        raidIsEnded = season.get("state") == "ended"
        offensive = season.get("offensiveReward", 0) * 6 if raidIsEnded else attack_medals(season)
        defensive = season.get("defensiveReward", 0) if raidIsEnded else 0
        total = offensive + defensive

        start = parse_time(season["startTime"])
        end = parse_time(season["endTime"])

        result = {
            "tag": f"#{tag}",
            "name": clan_name,
            "offensive": offensive,
            "defensive": defensive,
            "total": total,
            "start": start,
            "end": end
        }

        return result

    async def fetch_def_clan(self, client, log):
        tag = log["attacker"]["tag"]
        name = log["attacker"]["name"]
        attack_cnt = log["attackCount"]
        defeated = all(d.get("destructionPercent", 0) == 100 for d in log["districts"])
        total_attackers = list({
            attack.get("attacker", {}).get("tag")
            for d in log["districts"]
            if d.get("attacks")
            for attack in d["attacks"]
        })
        clan_info = await self.fetch_clan(client, clean_tag(tag))
        logger.debug(clan_info)
        is_open = clan_info["type"] == "open"
        return {
            "tag": tag,
            "name": name,
            "attack_cnt": attack_cnt,
            "defeated": "是" if defeated else "否",
            "is_open": "是" if is_open else "否",
            "total_attackers": len(total_attackers),
        }

    @filter.command("防守")
    async def defense_detail(self, event: AstrMessageEvent):
        """ 用法: 防守 #部落标签 示例: 都城 #222 """

        msg = event.message_str.strip()
        parts = msg.split()
        if len(parts) != 2:
            yield event.plain_result("查单个部落都城防守详情\n用法: 防守 #部落标签\n例如: 都城 #222")
            return
        clan_tag = clean_tag(parts[1])

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await self.fetch_raid_raw(client, clan_tag)
            raid_info = resp["items"][0]
            def_log = raid_info["defenseLog"]
            tasks = [self.fetch_def_clan(client, log) for log in def_log]
            results = await asyncio.gather(*tasks)

        results = [r for r in results if r]

        start_time = parse_time(raid_info["startTime"])
        end_time = parse_time(raid_info["endTime"])

        msg_lines = [
            "🛡️ 突袭周末防守战绩",
            f"📅 开始时间: {start_time}",
            f"📅 结束时间: {end_time}",
            ""
        ]
        tb_header = {
            "tag": "标签",
            "name": "部落名称",
            "defeated": "已被击败",
            "attack_cnt": "总进攻刀数",
            "is_open": "部落开门",
            "total_attackers": "已参加突袭人数",
        }
        tb_lines = json_to_table(results, tb_header, " ", "is_open")
        msg_lines.append(tb_lines)

        yield event.plain_result("\n".join(msg_lines))

    @filter.command("都城")
    async def predict_offensive(self, event: AstrMessageEvent):

        if self.disabled:
            yield event.plain_result("插件未配置 apiKey")
            return

        msg = event.message_str.strip()
        parts = msg.split()

        if len(parts) < 2:
            yield event.plain_result(
                "查多个部落都城进攻详情\n用法: 都城 #部落标签 #部落标签\n例如: 都城 #222 #333"
            )
            return

        tags = [clean_tag(p) for p in parts[1:]]

        # 去重
        tags = list(dict.fromkeys(tags))

        if len(tags) > self.max_tags:
            yield event.plain_result(f"最多支持查询 {self.max_tags} 个部落")
            return

        async with httpx.AsyncClient(timeout=15) as client:

            tasks = [self.fetch_raid(client, tag) for tag in tags]

            results = await asyncio.gather(*tasks)

        results = [r for r in results if r]

        if not results:
            yield event.plain_result("没有查询到数据")
            return

        start_time = results[0]["start"]
        end_time = results[0]["end"]

        msg_lines = [
            "⚔️ 突袭周末进攻战绩",
            f"📅 开始时间: {start_time}",
            f"📅 结束时间: {end_time}",
            ""
        ]

        tb_header = {
            "tag": "标签",
            "name": "部落名称",
            "offensive": "进攻",
            "defensive": "防守",
            "total": "总奖励"
        }
        tb_lines = json_to_table(results, tb_header, " ", "total")
        msg_lines.append(tb_lines)

        yield event.plain_result("\n".join(msg_lines))

    async def terminate(self):
        self.tagCache = {}
        self.raidCache = {}
        logger.info("COC都城插件已卸载")
