from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger

import httpx
import asyncio
import time
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

    def str_width(self, s):
        w = 0
        for c in str(s):
            w += 2 if ord(c) > 127 else 1
        return w

    def pad(self, s, width, align="left"):
        s = str(s)
        diff = width - self.str_width(s)
        if diff <= 0:
            return s

        if align == "right":
            return " " * diff + s
        else:
            return s + " " * diff

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
        # 按奖励排序
        results.sort(key=lambda x: x["total"], reverse=True)

        start_time = results[0]["start"]
        end_time = results[0]["end"]

        # 插入表头
        results.insert(0, {
            "tag": "标签",
            "name": "部落名称",
            "offensive": "进攻",
            "defensive": "防守",
            "total": "总奖励"
        })

        # 计算列宽
        tag_width = max(self.str_width(r["tag"]) for r in results)
        total_width = max(self.str_width(r["total"]) for r in results)
        off_width = max(self.str_width(r["offensive"]) for r in results)
        def_width = max(self.str_width(r["defensive"]) for r in results)
        name_width = max(self.str_width(r["name"]) for r in results)

        msg_lines = [
            "🏰 突袭周末战绩",
            f"📅 开始时间: {start_time}",
            f"📅 结束时间: {end_time}",
            ""
        ]

        for i, r in enumerate(results):

            rank = "排名" if i == 0 else f"{i}"

            line = (
                f"{self.pad(rank, 3, 'right')} "
                f"{self.pad(r['tag'], tag_width)}  "
                f"{self.pad(r['total'], total_width, 'right')}  "
                f"{self.pad(r['offensive'], off_width, 'right')}  "
                f"{self.pad(r['defensive'], def_width, 'right')}  "
                f"{self.pad(r['name'], name_width)}"
            )

            msg_lines.append(line)

            if i == 0:
                msg_lines.append("-" * (tag_width + total_width + off_width + def_width + name_width + 15))

        yield event.plain_result("\n".join(msg_lines))

    async def terminate(self):
        self.tagCache = {}
        self.raidCache = {}
        logger.info("COC都城插件已卸载")