from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger

from typing import Dict, Optional

from .coc import CocAPI

@register("astrbot_plugin_coc_capital", "COC都城", "查询多个部落的突袭币, 感谢 warreport 提供数据支持", "1.4.0")
class CocCapitalPlugin(Star):

    def __init__(self, context: Context, config: Optional[Dict] = None):
        super().__init__(context)

        self.config = config if config else {}
        logger.info(f"astrbot_plugin_coc_capital config: {config} -> {self.config}")

        self.apiKey = self.config.get("apiKey", "")
        self.ua = self.config.get("ua", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36")

        self.max_tags: int = self.config.get("max_tags", 25)

        self.clan_cache_ttl: int = self.config.get("clan_cache_ttl",600)
        self.raid_cache_ttl: int = self.config.get("raid_cache_ttl", 10)

        # 新增：并发控制
        self.concurrency: int = self.config.get("concurrency", 10)

        # apiKey 未配置直接禁用
        if not self.apiKey:
            logger.error("COC都城插件未配置 apiKey，插件不会启用")
            self.disabled = True
        else:
            logger.info("COC都城插件已配置 apiKey，启用ing")
            self.disabled = False
            self.coc = CocAPI(logger, self.apiKey, self.ua, self.clan_cache_ttl, self.raid_cache_ttl, self.concurrency)

    async def initialize(self):
        if self.disabled:
            return
        logger.info("COC都城插件已启动")


    @filter.command("防守")
    async def defense_detail(self, event: AstrMessageEvent):
        """ 用法: 防守 #部落标签 示例: 都城 #222 """

        msg = event.message_str.strip()
        parts = msg.split()
        if len(parts) != 2:
            yield event.plain_result("查单个部落都城防守详情\n用法: 防守 #部落标签\n例如: 都城 #222")
            return
        clan_tag = parts[1]
        yield event.plain_result(await self.coc.defense_detail(clan_tag))

    @filter.command("都城")
    async def predict_offensive(self, event: AstrMessageEvent):

        if self.disabled:
            yield event.plain_result("插件未配置 apiKey")
            return

        msg = event.message_str.strip()
        parts = msg.split()

        if len(parts) < 2:
            yield event.plain_result(
                "查多个部落都城进攻详情\n用法: 都城 #部落标签 #部落标签\n例如: 都城 #222 #333\n\n单个部落防守大于等于 44 刀必定拿满防守"
            )
            return

        # 去重
        tags = list(dict.fromkeys(parts[1:]))

        if len(tags) > self.max_tags:
            yield event.plain_result(f"最多支持查询 {self.max_tags} 个部落")
            return

        yield event.plain_result(await self.coc.predict_offensive(tags))

    async def terminate(self):
        if not self.disabled:
            self.coc.clean_cache()
        logger.info("COC都城插件已卸载")
