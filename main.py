from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger

import httpx
import asyncio
import time
import unicodedata
from typing import Dict, Optional
from datetime import datetime, timedelta


@register("astrbot_plugin_coc_capital", "COC都城", "查询多个部落的突袭币, 感谢 warreport 提供数据支持", "1.3.0")
class CocCapitalPlugin(Star):
    # === 类常量 ===
    PERK_MEDALS = {
        1: 45, 2: 180, 3: 360, 4: 585, 5: 810,
        6: 1115, 7: 1240, 8: 1260, 9: 1375, 10: 1450
    }

    SUB_MEDALS = {
        1: 135, 2: 225, 3: 350, 4: 405, 5: 460
    }

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
            self.disabled = False

    async def initialize(self):
        if self.disabled:
            return
        logger.info("COC都城插件已启动")

    # 时间转换
    def parse_time(self, t: str):
        dt = datetime.strptime(t, "%Y%m%dT%H%M%S.%fZ")
        dt = dt + timedelta(hours=8)
        return dt.strftime("%Y/%m/%d %H:%M")

    def get_headers(self):
        return {
            "User-Agent": self.ua,
            "apikey": self.apiKey,
            "origin": "https://www.warreport.app",
            "Accept": "application/json"
        }

    def cache_get(self, cache, key, ttl):
        item = cache.get(key)
        if not item:
            return None
        if time.time() - item["time"] > ttl:
            del cache[key]
            return None
        return item["data"]

    def cache_set(self, cache, key, value):
        cache[key] = {
            "time": time.time(),
            "data": value
        }

    def _str_width(self, s: str) -> int:
        """计算字符串显示宽度（支持中文）"""
        w = 0
        for c in str(s):
            if unicodedata.east_asian_width(c) in ("F", "W"):
                w += 2
            else:
                w += 1
        return w

    def _pad(self, s, width, align="left"):
        """按显示宽度补齐"""
        s = str(s)
        w = self._str_width(s)
        pad = width - w
        if pad <= 0:
            return s

        if align == "right":
            return " " * pad + s
        return s + " " * pad

    def json_to_table(
            self,
            data, headers=None, sep="  ",
            sort_by=None, reverse=True, add_rank=False,
    ):
        """
        JSON(list[dict]) 转文本表格

        data: list[dict]
        headers: [(key, title)]
        sep: 分隔符
        sort_by: 按某字段排序
        reverse: 是否倒序
        add_rank: 是否自动增加排名列
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
            headers = [("rank", "排名")] + headers

        if headers is None:
            headers = [(k, k) for k in data[0].keys()]

        keys = [k for k, _ in headers]
        titles = [t for _, t in headers]

        rows = []
        rows.append(titles)

        for item in data:
            rows.append([item.get(k, "") for k in keys])

        # 计算列宽
        widths = []
        for col in range(len(keys)):
            widths.append(max(self._str_width(row[col]) for row in rows))

        lines = []

        for i, row in enumerate(rows):
            parts = []
            for col, val in enumerate(row):
                align = "right" if isinstance(val, (int, float)) else "left"
                parts.append(self._pad(val, widths[col], align))
            lines.append(sep.join(parts))

            # 表头分隔
            if i == 0:
                lines.append(sep.join("-" * w for w in widths))

        return "\n".join(lines)


    def single_attack_medals(self, log):
        medals = []

        for d in log.get("districts", []):
            if d.get("stars") == 3:
                # Capital Peak
                if d.get("id") == 70000000 or d.get("name") == "Capital Peak":
                    medals.append(self.PERK_MEDALS.get(d.get("districtHallLevel"), 0))
                else:
                    medals.append(self.SUB_MEDALS.get(d.get("districtHallLevel"), 0))
        return medals

    def attack_medals(self, season):
        total_medals = 0
        total_attacks = 0

        for log in season.get("attackLog", []):
            total_medals += sum(self.single_attack_medals(log))
            total_attacks += log.get("attackCount", 0)

        if total_attacks == 0:
            return 0
        return round((total_medals / total_attacks) * 6)

    # 获取部落信息
    async def fetch_clan(self, client, tag):

        cached = self.cache_get(self.tagCache, tag, self.clan_cache_ttl)
        if cached:
            return cached

        encoded_tag = "%23" + tag
        url = f"https://clashapi.colinschmale.dev/v1/clans/{encoded_tag}"

        async with self.semaphore:
            try:
                resp = await client.get(url, headers=self.get_headers())
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                logger.error(f"{tag} clan 查询失败: {e}")
                return {}

        self.cache_set(self.tagCache, tag, data)
        return data

    # 查询 raid
    async def fetch_raid(self, client, tag):

        cached = self.cache_get(self.raidCache, tag, self.raid_cache_ttl)
        if cached:
            return cached

        encoded_tag = "%23" + tag
        url = f"https://clashapi.colinschmale.dev/v1/clans/{encoded_tag}/capitalraidseasons?limit=1"

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

        season = data["items"][0]

        clan_info = await self.fetch_clan(client, tag)
        clan_name = clan_info.get("name", "未知")

        raidIsEnded = season.get("state") == "ended"
        offensive = season.get("offensiveReward", 0) * 6 if raidIsEnded else self.attack_medals(season)
        defensive = season.get("defensiveReward", 0) if raidIsEnded else 0
        total = offensive + defensive

        start = self.parse_time(season["startTime"])
        end = self.parse_time(season["endTime"])

        result = {
            "tag": f"#{tag}",
            "name": clan_name,
            "offensive": offensive,
            "defensive": defensive,
            "total": total,
            "start": start,
            "end": end
        }

        self.cache_set(self.raidCache, tag, result)

        return result

    @filter.command("都城", need_at=False)
    async def capital(self, event: AstrMessageEvent):

        if self.disabled:
            yield event.plain_result("插件未配置 apiKey")
            return

        msg = event.message_str.strip()
        parts = msg.split()

        if len(parts) < 2:
            yield event.plain_result(
                "用法:\n都城 #部落标签 #部落标签\n例如:\n都城 #222 #333"
            )
            return

        tags = [p.upper().replace("#", "") for p in parts[1:]]

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
            "🏰 突袭周末战绩",
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
        tb_lines = self.json_to_table(results, tb_header, " ", "total")
        msg_lines.append(tb_lines)

        yield event.plain_result("\n".join(msg_lines))

    async def terminate(self):
        self.tagCache = {}
        self.raidCache = {}
        logger.info("COC都城插件已卸载")