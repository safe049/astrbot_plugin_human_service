import re
import asyncio
from typing import Dict, List, Optional
from astrbot.api.event import filter
from astrbot.api.star import Context, Star, register
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.message.components import Reply, Node, Plain
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)
from astrbot.api import logger


@register(
    "astrbot_plugin_human_service",
    "Zhalslar",
    "人工客服插件",
    "1.0.4",
    "https://github.com/Zhalslar/astrbot_plugin_human_service",
)
class HumanServicePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.servicers_id: List[str] = config.get("servicers_id", "")
        self.customer_group: List[str] = config.get("customer_group", [])  
        self.response_timeout = config.get("response_timeout", 30)  
        self.total_timeout = config.get("total_timeout", 300) 

        if not self.servicers_id:
            for admin_id in context.get_config()["admins_id"]:
                if admin_id.isdigit():
                    self.servicers_id.append(admin_id)

        self.session_map = {}
        self.pending_requests: Dict[str, Dict] = {} 
        self.customer_mode_users: Dict[str, str] = {}  

    async def get_group_history(self, group_id: int, count: int = 15) -> List[dict]:
        try:
            platform = self.context.get_platform(filter.PlatformAdapterType.AIOCQHTTP)
            result = await platform.get_client().get_group_msg_history(
                group_id=group_id, count=count
            )
            messages = result.get("messages", [])
            nodes = []
            for msg in messages[-count:]:
                nodes.append({
                    "type": "node",
                    "data": {
                        "name": msg["sender"]["nickname"],
                        "uin": msg["sender"]["user_id"],
                        "content": msg["message"]
                    }
                })
            return nodes
        except Exception as e:
            logger.error(f"获取群历史消息失败: {e}")
            return []

    async def notify_customer_group(self, user_name: str, user_id: str, src_group: str):
        msg = f"{user_name}({user_id}) 在群{src_group}请求转人工"
        platform = self.context.get_platform(filter.PlatformAdapterType.AIOCQHTTP)

        for gid in self.customer_group:
            try:
                await platform.get_client().send_group_msg(
                    group_id=int(gid),
                    message=f"[CQ:at,qq=all] {msg}"
                )
            except Exception:
                at_seg = "".join([f"[CQ:at,qq={sid}]" for sid in self.servicers_id])
                try:
                    await platform.get_client().send_group_msg(
                        group_id=int(gid),
                        message=f"{at_seg} {msg}"
                    )
                except Exception as e:
                    logger.error(f"客服群({gid})通知失败: {e}")

    async def send_private_history(self, servicer_id: str, user_id: str, group_id: str,send_name):
        nodes = await self.get_group_history(int(group_id))
        if not nodes:
            return
        platform = self.context.get_platform(filter.PlatformAdapterType.AIOCQHTTP)
        await platform.get_client().send_private_forward_msg(
            user_id=int(servicer_id),
            messages=nodes
        )
        await platform.get_client().send_private_msg(
            user_id=int(servicer_id),
            message=f"{send_name}({user_id}) 在申请前的聊天记录已给出，接下来我将转发你的消息给对方，请开始对话："
        )

    @filter.command("转人工", priority=1)
    async def transfer_to_human(self, event: AiocqhttpMessageEvent):
        sender_id = event.get_sender_id()
        send_name = event.get_sender_name()
        group_id = event.get_group_id() or "0"

        if sender_id in self.session_map:
            yield event.plain_result("⚠ 您已在等待接入或正在对话")
            return

        self.pending_requests[sender_id] = {
            "group_id": group_id,
            "timestamp": asyncio.get_event_loop().time(),
            "user_name": send_name 
        }

        await self.notify_customer_group(send_name, sender_id, group_id)

        asyncio.create_task(self._handle_timeout(sender_id, group_id))

        yield event.plain_result("正在等待客服👤接入...")

    async def _handle_timeout(self, user_id: str, group_id: str):
        await asyncio.sleep(self.response_timeout)
        
        if user_id not in self.pending_requests:
            return
            
        for servicer_id in self.servicers_id:
            try:
                platform = self.context.get_platform(filter.PlatformAdapterType.AIOCQHTTP)
                await platform.get_client().send_private_msg(
                    user_id=int(servicer_id),
                    message=f"用户{user_id}的转人工请求即将超时，可回复此消息接入"
                )
            except Exception as e:
                logger.error(f"私聊通知失败: {e}")

        await asyncio.sleep(self.total_timeout - self.response_timeout)
        if user_id in self.pending_requests:
            self.pending_requests.pop(user_id, None)
            await self.send(
                event=None,
                message="⚠人工服务因无人应答而结束\n诶呀……看来客服都在休息呢",
                group_id=group_id,
                user_id=user_id
            )

    @filter.command("转人机", priority=1)
    async def transfer_to_bot(self, event: AiocqhttpMessageEvent):
        sender_id = event.get_sender_id()
        sender_name = event.get_sender_name()
        session = self.session_map.get(sender_id)

        if session and session["status"] == "connected":
            await self.send(
                event,
                message=f"❗{sender_name} 已取消人工请求",
                user_id=session["servicer_id"],
            )
            del self.session_map[sender_id]
            self.customer_mode_users.pop(session["servicer_id"], None)
            yield event.plain_result("好的，我现在是人机啦！")

    @filter.command("接入对话", priority=1)
    async def accept_conversation(
        self, event: AiocqhttpMessageEvent, target_id: str | int | None = None
    ):
        sender_id = event.get_sender_id()
        if sender_id not in self.servicers_id:
            return

        if reply_seg := next(
            (seg for seg in event.get_messages() if isinstance(seg, Reply)), None
        ):
            if text := reply_seg.message_str:
                matches = re.findall(r"\((\d+)\)", text)
                if matches:
                    target_id = matches[-1]

        if str(target_id) not in self.pending_requests:
            yield event.plain_result(f"用户({target_id})未请求人工")
            return

        request_info = self.pending_requests.pop(str(target_id))

        self.session_map[str(target_id)] = {
            "servicer_id": sender_id,
            "status": "connected",
            "group_id": request_info["group_id"],
            "is_private": True  
        }
        self.customer_mode_users[sender_id] = str(target_id)

        for group_id in self.customer_group:
            try:
                platform = self.context.get_platform(filter.PlatformAdapterType.AIOCQHTTP)
                await platform.get_client().send_group_msg(
                    group_id=int(group_id),
                    message=f"用户{target_id}的请求现在由客服{sender_id}接手"
                )
            except Exception as e:
                logger.error(f"客服群通知失败: {e}")

        send_name = request_info["user_name"]
        await self.send_private_history(sender_id, str(target_id),  request_info["group_id"],send_name)

        yield event.plain_result(f"{sender_id}，用户{target_id}将在私聊展开对话")
        event.stop_event()

    @filter.command("结束对话")
    async def end_conversation(self, event: AiocqhttpMessageEvent):
        sender_id = event.get_sender_id()
        
        if sender_id in self.servicers_id:
            for uid, session in self.session_map.items():
                if session["servicer_id"] == sender_id:
                    await self.send(
                        event,
                        message="客服👤已结束对话",
                        group_id=session["group_id"],
                        user_id=uid,
                    )
                    del self.session_map[uid]
                    self.customer_mode_users.pop(sender_id, None)
                    yield event.plain_result(f"已结束与用户 {uid} 的对话")
                    return
        else:
            session = self.session_map.get(sender_id)
            if session:
                await self.send(
                    event,
                    message="用户已结束对话",
                    user_id=session["servicer_id"],
                )
                self.customer_mode_users.pop(session["servicer_id"], None)
                del self.session_map[sender_id]
                yield event.plain_result("已结束对话")

    async def send(
        self,
        event: Optional[AiocqhttpMessageEvent],
        message,
        group_id: int | str | None = None,
        user_id: int | str | None = None,
    ):
        try:
            platform = self.context.get_platform(filter.PlatformAdapterType.AIOCQHTTP)
            if group_id and str(group_id) != "0":
                await platform.get_client().send_group_msg(group_id=int(group_id), message=message)
            elif user_id:
                await platform.get_client().send_private_msg(user_id=int(user_id), message=message)
        except Exception as e:
            logger.error(f"发送消息失败: {e}")

    async def send_ob(
        self,
        event: AiocqhttpMessageEvent,
        group_id: int | str | None = None,
        user_id: int | str | None = None,
    ):
        try:
            platform = self.context.get_platform(filter.PlatformAdapterType.AIOCQHTTP)
            ob_message = await event._parse_onebot_json(
                MessageChain(chain=event.message_obj.message)
            )
            if group_id and str(group_id) != "0":
                await platform.get_client().send_group_msg(group_id=int(group_id), message=ob_message)
            elif user_id:
                await platform.get_client().send_private_msg(user_id=int(user_id), message=ob_message)
        except Exception as e:
            logger.error(f"发送OB消息失败: {e}")

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def handle_match(self, event: AiocqhttpMessageEvent):
        chain = event.get_messages()
        if not chain or any(isinstance(seg, Reply) for seg in chain):
            return

        sender_id = event.get_sender_id()

        if sender_id in self.customer_mode_users:
            target_user = self.customer_mode_users[sender_id]
            session = self.session_map.get(target_user)
            if session and event.is_private_chat():
                group_id = session["group_id"]
                message = f"[CQ:at,qq={target_user}] " + event.message_str
                await self.send(
                    event=None,
                    message=message,
                    group_id=group_id
                )
                event.stop_event()
            return

        session = self.session_map.get(sender_id)
        if session and session.get("status") == "connected" and not event.is_private_chat():
            servicer_id = session["servicer_id"]
            sender_name = event.get_sender_name()
            message = f"{event.message_str}"
            await self.send(
                event=None,
                message=message,
                user_id=servicer_id
            )
            event.stop_event()