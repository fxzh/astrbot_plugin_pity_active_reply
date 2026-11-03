import random
import time

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Json, Plain
from astrbot.api.star import Context, Star, register

try:
    from astrbot.api import logger
except Exception:
    import logging

    logger = logging.getLogger("astrbot")

try:  # 兼容不同版本的 AstrBot
    from astrbot.api import AstrBotConfig
except ImportError:
    try:
        from astrbot.api.config import AstrBotConfig
    except ImportError:
        AstrBotConfig = None


@register(
    "astrbot_plugin_pity_active_reply",
    "fengxue",
    "保底递增式主动回复：连续未插话时概率逐条升高，一旦主动回复或被 @ 被动回复即重置，"
    "避免原生纯概率「某段时间一直接话、某段时间从不接话」的两个极端",
    "1.0.1",
)
class PityActiveReply(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config
        # umo -> {"misses": 连续未插话条数, "last_ts": 上条消息时间, "warn_ts": 上次警告时间}
        self._state: dict[str, dict] = {}

    # ---------- 配置读取（带默认值，插件配置未注入时也能工作） ----------

    def _cfg(self, key, default):
        try:
            if self.config is not None:
                v = self.config.get(key, None)
                if v is not None:
                    return v
        except Exception:
            pass
        return default

    def _probability(self, misses: int) -> float:
        """当前抽签概率：p = min(base + increment * misses, max_prob)，硬保底时为 1.0"""
        base = float(self._cfg("base_prob", 0.05))
        increment = float(self._cfg("increment", 0.03))
        cap = float(self._cfg("max_prob", 0.4))
        hard = int(self._cfg("hard_pity_count", 0) or 0)
        if hard > 0 and misses >= hard:
            return 1.0
        # 钳制到 [0, 1]：防止误配置（如 base_prob 填成 5）导致 p>1 每条必回
        return min(max(base + increment * misses, 0.0), max(cap, base), 1.0)

    def _get_state(self, umo: str) -> dict:
        st = self._state.get(umo)
        if st is None:
            st = {"misses": 0, "last_ts": 0.0, "warn_ts": 0.0}
            self._state[umo] = st
        return st

    def _native_active_reply_enabled(self, event: AstrMessageEvent) -> bool:
        """原生「主动回复」是否开启。开着会导致双重回复，必须关闭（本插件完全接管）。"""
        try:
            cfg = self.context.get_config(umo=event.unified_msg_origin)
            active_reply = cfg.get("provider_ltm_settings", {}).get("active_reply", {})
            return bool(active_reply.get("enable", False))
        except Exception:
            return False

    # ---------- 主逻辑 ----------

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        """保底递增式主动回复。默认优先级(0)低于内置群聊上下文记录(maxsize-2)，
        保证抽中后发出的 LLM 请求能带上内置「群聊上下文感知」注入的历史记录。"""
        # 忽略 bot 自身消息（如适配器开启了上报自身消息）
        if str(event.get_sender_id() or "") == str(event.get_self_id() or ""):
            return

        if not bool(self._cfg("enable", True)):
            return

        umo = event.unified_msg_origin

        # 白名单：留空 = 所有群生效；支持完整 umo 或群号/群 openid
        whitelist = self._cfg("whitelist", []) or []
        if whitelist:
            group_id = str(event.get_group_id() or "")
            if umo not in whitelist and group_id not in whitelist:
                return

        # 原生主动回复开着会双重触发 → 警告并暂停本插件
        if self._native_active_reply_enabled(event):
            st = self._get_state(umo)
            now = time.time()
            if now - st.get("warn_ts", 0.0) > 600:
                st["warn_ts"] = now
                logger.warning(
                    "[pity_active_reply] 检测到原生「主动回复」仍开启，为避免同一条消息双重回复，"
                    "本插件已暂停工作。请到 WebUI → 配置 → 其他配置 → 群聊上下文感知 → 主动回复 → 关闭"
                    "（「群聊上下文感知」本身的开关保持开启，本插件依赖它记录群聊上下文）。"
                )
            return

        st = self._get_state(umo)
        now = time.time()

        # 可选冷清清零：两条消息间隔超过 decay_minutes 则保底计数归零
        decay_minutes = int(self._cfg("decay_minutes", 0) or 0)
        if (
            decay_minutes > 0
            and st["last_ts"] > 0
            and now - st["last_ts"] > decay_minutes * 60
        ):
            st["misses"] = 0
        st["last_ts"] = now

        # 被 @ / 唤醒词 / 命令 / 按钮交互 → 被动回复路径，重置保底计数后放行
        if event.is_at_or_wake_command or event.is_button_interaction():
            if st["misses"]:
                logger.debug(
                    f"[pity_active_reply] {umo} 发生直接交互，保底计数重置({st['misses']}→0)"
                )
                st["misses"] = 0
            return

        # 无实际内容（纯表情/纯@等）不参与抽签，与内置主动回复口径一致
        if not any(
            isinstance(comp, (Plain, Image, Json)) for comp in event.get_messages()
        ):
            return

        # 抽签
        p = self._probability(st["misses"])
        if random.random() >= p:
            st["misses"] += 1
            return

        # 抽中：重置计数并触发主动回复（复用与原生主动回复相同的链路）
        missed = st["misses"]
        st["misses"] = 0
        logger.info(
            f"[pity_active_reply] {umo} 主动回复触发(本次p={p:.2f}，此前连续未插话{missed}条)"
        )

        provider = await self.context.get_using_provider_async(umo)
        if not provider:
            logger.error("[pity_active_reply] 未找到 LLM 提供商，无法主动回复")
            return
        curr_cid = await self.context.conversation_manager.get_curr_conversation_id(umo)
        if not curr_cid:
            logger.error(
                "[pity_active_reply] 当前会话没有活跃对话，无法主动回复。"
                "请先在该群 @ 机器人发送 /new 创建对话"
            )
            return
        conv = await self.context.conversation_manager.get_conversation(umo, curr_cid)
        if not conv:
            logger.error("[pity_active_reply] 未找到对话，无法主动回复")
            return

        image_urls = []
        for comp in event.get_messages():
            if isinstance(comp, Image):
                try:
                    image_urls.append(await comp.convert_to_file_path())
                except Exception:
                    logger.exception("[pity_active_reply] 主动回复处理图片失败")

        yield event.request_llm(
            prompt=event.message_str,
            session_id=event.session_id,
            image_urls=image_urls,
            conversation=conv,
        )
