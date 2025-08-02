import re
import asyncio
import random
import string
import json
import os
from typing import Dict, List, Optional, Tuple
from datetime import datetime, time, timedelta
from astrbot.api.event import filter
from astrbot.api.star import Context, Star, register
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.message.components import Reply, Plain
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

        self.plugin_data_dir = "data/plugins/astrbot_plugin_human_service/data"
        self.config_file_path = os.path.join(
            self.plugin_data_dir, "servicer_config.json"
        )
        self.config = config
        self.servicers_id: List[str] = config.get("servicers_id", [])

        if not isinstance(self.servicers_id, list):
            self.servicers_id = [self.servicers_id] if self.servicers_id else []
        self.customer_group: List[str] = config.get("customer_group", [])
        self.response_timeout = config.get("response_timeout", 30)
        self.total_timeout = config.get("total_timeout", 300)

        if not self.servicers_id:
            for admin_id in context.get_config().get("admins_id", []):
                if admin_id.isdigit():
                    self.servicers_id.append(admin_id)
            logger.info(f"未配置客服ID，已自动添加管理员ID: {self.servicers_id}")

        self.session_map: Dict[str, Dict] = {}

        self.pending_requests: Dict[str, Dict] = {}

        self.customer_mode_users: Dict[str, str] = {}

        self.servicer_config: Dict[str, Dict] = {}

        self.waiting_queue: List[str] = []

        self.user_temp_nicknames: Dict[str, Dict[str, str]] = {}

        self.transfer_requests: Dict[str, Dict] = {}

        self.transfer_response_timeout = self.response_timeout

        self.user_servicer_chat_history: Dict[str, List[Dict]] = {}

        self.user_pre_request_history_for_transfer: Dict[str, List[Dict]] = {}

        self._load_persistent_config()

        self._initialize_servicer_status_from_config()

    def _load_persistent_config(self):

        if not os.path.exists(self.config_file_path):
            logger.info(
                f"客服配置文件 {self.config_file_path} 不存在，将使用默认配置。"
            )

            for sid in self.servicers_id:
                if sid not in self.servicer_config:
                    self.servicer_config[sid] = {
                        "nickname": "",
                        "online_schedule": [],
                        "use_nickname": False,
                        "status": "idle",
                    }
            return
        try:
            with open(self.config_file_path, "r", encoding="utf-8") as f:
                loaded_config = json.load(f)

            self.servicer_config = {}
            for sid, config in loaded_config.items():
                config_copy = config.copy()
                schedule_data = config_copy.get("online_schedule")
                if schedule_data and isinstance(schedule_data, list):
                    deserialized_schedule = []
                    for time_range_str in schedule_data:

                        if (
                            isinstance(time_range_str, (list, tuple))
                            and len(time_range_str) == 2
                            and isinstance(time_range_str[0], str)
                            and isinstance(time_range_str[1], str)
                        ):
                            try:
                                start_time = datetime.strptime(
                                    time_range_str[0], "%H:%M"
                                ).time()
                                end_time = datetime.strptime(
                                    time_range_str[1], "%H:%M"
                                ).time()
                                deserialized_schedule.append((start_time, end_time))
                            except ValueError as ve:
                                logger.warning(
                                    f"解析客服 {sid} 的时间范围 {time_range_str} 失败: {ve}，将跳过该项。"
                                )
                        else:
                            logger.warning(
                                f"客服 {sid} 的时间范围格式不正确: {time_range_str}，将跳过该项。"
                            )
                    config_copy["online_schedule"] = deserialized_schedule
                self.servicer_config[sid] = config_copy

            for sid in self.servicers_id:
                if sid not in self.servicer_config:
                    logger.info(f"配置文件中缺少客服 {sid}，添加默认配置。")
                    self.servicer_config[sid] = {
                        "nickname": "",
                        "online_schedule": [],
                        "use_nickname": False,
                        "status": "idle",
                    }
            logger.info(f"已从 {self.config_file_path} 加载并处理客服配置。")
        except json.JSONDecodeError as e:
            logger.error(
                f"解析客服配置文件 {self.config_file_path} 失败: {e}。将使用默认配置。"
            )

            for sid in self.servicers_id:
                if sid not in self.servicer_config:
                    self.servicer_config[sid] = {
                        "nickname": "",
                        "online_schedule": [],
                        "use_nickname": False,
                        "status": "idle",
                    }
        except Exception as e:
            logger.error(
                f"加载客服配置文件 {self.config_file_path} 时发生未知错误: {e}。将使用默认配置。"
            )
            for sid in self.servicers_id:
                if sid not in self.servicer_config:
                    self.servicer_config[sid] = {
                        "nickname": "",
                        "online_schedule": [],
                        "use_nickname": False,
                        "status": "idle",
                    }

    def _save_persistent_config(self):

        try:

            os.makedirs(self.plugin_data_dir, exist_ok=True)

            config_to_save = {}
            for sid, config in self.servicer_config.items():
                config_copy = config.copy()

                schedule = config_copy.get("online_schedule")
                if schedule and isinstance(schedule, list):

                    serialized_schedule = []
                    for time_range in schedule:
                        if (
                            isinstance(time_range, (tuple, list))
                            and len(time_range) == 2
                        ):
                            start_time, end_time = time_range
                            if isinstance(start_time, time) and isinstance(
                                end_time, time
                            ):
                                serialized_schedule.append(
                                    (
                                        start_time.strftime("%H:%M"),
                                        end_time.strftime("%H:%M"),
                                    )
                                )
                            else:

                                serialized_schedule.append(time_range)
                        else:

                            serialized_schedule.append(time_range)
                    config_copy["online_schedule"] = serialized_schedule
                config_to_save[sid] = config_copy
            with open(self.config_file_path, "w", encoding="utf-8") as f:
                json.dump(config_to_save, f, indent=4, ensure_ascii=False)
            logger.debug(f"客服配置已保存到 {self.config_file_path}")
        except Exception as e:
            logger.error(f"保存客服配置到 {self.config_file_path} 失败: {e}")

    def _initialize_servicer_status_from_config(self):

        for sid in self.servicers_id:

            if sid not in self.servicer_config:
                self.servicer_config[sid] = {
                    "nickname": "",
                    "online_schedule": [],
                    "use_nickname": False,
                    "status": "idle",
                }

            self._update_servicer_status(sid)
        logger.info("客服状态已根据配置和运行时情况初始化完成。")

    def _initialize_servicer_status(self):

        for sid in self.servicers_id:
            if sid not in self.servicer_config:
                self.servicer_config[sid] = {
                    "nickname": "",
                    "online_schedule": [],
                    "use_nickname": False,
                    "status": "idle",
                }
            else:

                pass
        logger.info("客服状态初始化完成。")

    def _is_servicer_online(self, servicer_id: str) -> bool:

        config = self.servicer_config.get(servicer_id, {})
        schedule = config.get("online_schedule", [])

        if not schedule:
            return True
        now = datetime.now().time()
        for start_time, end_time in schedule:

            if start_time > end_time:
                if now >= start_time or now <= end_time:
                    return True
            else:
                if start_time <= now <= end_time:
                    return True
        return False

    def _get_earliest_online_time(self) -> Optional[time]:

        all_times = []
        now = datetime.now()
        for sid in self.servicers_id:
            config = self.servicer_config.get(sid, {})
            schedule = config.get("online_schedule", [])
            if not schedule:
                return None
            for start_time, _ in schedule:

                candidate_time = datetime.combine(now.date(), start_time)
                if candidate_time.time() < now.time():
                    candidate_time += timedelta(days=1)
                all_times.append(candidate_time)
        if all_times:
            return min(all_times).time()
        return None

    def _update_servicer_status(self, servicer_id: str):

        if servicer_id not in self.servicer_config:
            return

        config = self.servicer_config[servicer_id]
        manual_override = config.get("manual_status_override")
        is_busy = servicer_id in self.customer_mode_users
        if is_busy:
            config["status"] = "busy"
            return
        if manual_override in ["idle", "offline"]:
            config["status"] = manual_override
            return
        is_online = self._is_servicer_online(servicer_id)
        if not is_online:
            config["status"] = "offline"
        else:
            config["status"] = "idle"

    def _get_servicer_display_name(
        self, servicer_id: str, for_user: bool = False, user_id: Optional[str] = None
    ) -> str:

        config = self.servicer_config.get(servicer_id, {})
        use_nickname = config.get("use_nickname", False)
        nickname = config.get("nickname", "")
        if for_user:

            if use_nickname and nickname:
                return f"客服{nickname}"
            else:

                if user_id and servicer_id in self.user_temp_nicknames.get(user_id, {}):
                    return f"客服{self.user_temp_nicknames[user_id][servicer_id]}"
                else:

                    temp_name = "".join(
                        random.choices(string.ascii_uppercase + string.digits, k=2)
                    )
                    if user_id:
                        if user_id not in self.user_temp_nicknames:
                            self.user_temp_nicknames[user_id] = {}
                        self.user_temp_nicknames[user_id][servicer_id] = temp_name
                    return f"客服{temp_name}"
        else:

            if use_nickname and nickname:
                return f"客服{nickname}({servicer_id})"
            else:
                return f"客服{servicer_id}"

    def _get_idle_servicers(self) -> List[str]:

        idle_servicers = []
        for sid in self.servicers_id:
            self._update_servicer_status(sid)
            if self.servicer_config[sid]["status"] == "idle":
                idle_servicers.append(sid)
        return idle_servicers

    def _get_online_servicers(self) -> List[str]:

        online_servicers = []
        for sid in self.servicers_id:
            self._update_servicer_status(sid)
            if self.servicer_config[sid]["status"] != "offline":
                online_servicers.append(sid)
        return online_servicers

    def _get_user_queue_position(self, user_id: str) -> int:

        try:
            return self.waiting_queue.index(user_id) + 1
        except ValueError:
            return -1

    async def _refresh_queue_notifications(self):

        tasks = []
        platform = self.context.get_platform(filter.PlatformAdapterType.AIOCQHTTP)
        for i, user_id in enumerate(self.waiting_queue):
            position = i + 1
            request_info = self.pending_requests.get(user_id)
            if request_info:
                is_private = request_info.get("is_private", False)
                group_id = request_info.get("group_id", "0")
                user_name = request_info.get("user_name", user_id)

                msg = f"@{user_name} 正在等待客服接入...\n当前排队位次：{position}"

                if is_private:
                    tasks.append(
                        platform.get_client().send_private_msg(
                            user_id=int(user_id), message=msg
                        )
                    )
                else:
                    tasks.append(
                        platform.get_client().send_group_msg(
                            group_id=int(group_id), message=msg
                        )
                    )
        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    logger.error(
                        f"刷新队列通知给用户 {self.waiting_queue[i]} 时出错: {result}"
                    )

    async def get_formatted_history(
        self, target_id: int, is_group: bool, count: int = 15
    ) -> List[dict]:

        nodes = []
        try:
            platform = self.context.get_platform(filter.PlatformAdapterType.AIOCQHTTP)
            client = platform.get_client()
            result = None
            if is_group:
                result = await client.get_group_msg_history(
                    group_id=target_id, count=count
                )
            else:

                result = await client.get_friend_msg_history(
                    user_id=target_id, count=count
                )
            if not result:
                logger.warning(
                    f"获取 {'群' if is_group else '私聊'} {target_id} 历史记录返回空结果。"
                )
                return nodes
            messages = result.get("messages", [])
            if not messages:
                logger.info(
                    f"{'群' if is_group else '私聊'} {target_id} 历史记录中无消息。"
                )
                return nodes

            recent_messages = messages[-count:] if len(messages) > count else messages
            for msg in recent_messages:

                sender_info = msg.get("sender", {})

                sender_name = (
                    sender_info.get("card")
                    or sender_info.get("nickname")
                    or str(sender_info.get("user_id", "未知用户"))
                )
                sender_uin = sender_info.get("user_id", "0")
                nodes.append(
                    {
                        "type": "node",
                        "data": {
                            "name": sender_name,
                            "uin": str(sender_uin),
                            "content": msg["message"],
                        },
                    }
                )
            return nodes
        except Exception as e:
            logger.error(
                f"获取 {'群' if is_group else '私聊'} {target_id} 历史消息失败: {e}",
                exc_info=True,
            )
            return nodes

    async def notify_customer_group(
        self, user_name: str, user_id: str, src_group: str, is_private: bool = False
    ):
        
        if is_private:
            msg = f"[客服插件]\n{user_name}({user_id})在私聊中请求转人工"
        else:
            platform = self.context.get_platform(filter.PlatformAdapterType.AIOCQHTTP)
            client = platform.get_client()
            try:
                group_info = await client.get_group_info(group_id=int(src_group))
                group_name = group_info.get("group_name", src_group)
            except Exception as e:
                logger.warning(f"获取群名 {src_group} 失败: {e}")
                group_name = src_group
            msg = f"[客服插件]\n{user_name}({user_id})在群{group_name}({src_group})请求转人工"
        group_notification_failed = False
        for sid in self.servicers_id:
            self._update_servicer_status(sid)

        online_servicers = self._get_online_servicers()
        idle_servicers = self._get_idle_servicers()
        should_at_all = False
        servicers_to_at = []
        if self.customer_group:
            if online_servicers:
                if len(online_servicers) == len(idle_servicers) and len(idle_servicers) > 0:
                    should_at_all = True
                else:
                    servicers_to_at = idle_servicers
            else:
                logger.info("无在线客服，通知消息将发送至客服群但不艾特任何人。")
        else:
            logger.info("未配置客服群。")
        if self.customer_group:
            notification_sent_to_any_group = False
            for gid in self.customer_group:
                try:
                    at_all_failed = False
                    if should_at_all:
                        try:
                            await platform.get_client().send_group_msg(
                                group_id=int(gid), message=f"[CQ:at,qq=all] {msg}"
                            )
                            logger.info(f"已向客服群({gid})发送艾特全体通知。")
                        except Exception as e:
                            logger.warning(f"向客服群({gid})艾特全体失败: {e}，将回退至艾特空闲客服。")
                            at_all_failed = True
                    if should_at_all and not at_all_failed:
                        pass
                    elif (should_at_all and at_all_failed) or (not should_at_all and servicers_to_at):
                        servicers_to_at_final = servicers_to_at if not should_at_all else idle_servicers
                        if servicers_to_at_final:
                            at_seg = "".join([f"[CQ:at,qq={sid}]" for sid in servicers_to_at_final])
                            await platform.get_client().send_group_msg(
                                group_id=int(gid), message=f"{at_seg} \n {msg}"
                            )
                            logger.info(f"已向客服群({gid})发送艾特空闲客服通知。")
                        else:
                            await platform.get_client().send_group_msg(
                                group_id=int(gid), message=msg
                            )
                            logger.info(f"无空闲客服，已向客服群({gid})发送普通通知。")
                    else:
                         await platform.get_client().send_group_msg(
                            group_id=int(gid), message=msg
                        )
                         logger.info(f"无空闲客服，已向客服群({gid})发送普通通知（不艾特）。")

                    notification_sent_to_any_group = True
                    logger.info(f"已向客服群({gid})发送转人工通知。")
                except Exception as e:
                    logger.warning(f"向客服群({gid})发送通知失败: {e}")
                    group_notification_failed = True
            if not notification_sent_to_any_group and self.customer_group:
                 group_notification_failed = True
        else:
            logger.info("未配置客服群。")
        if not self.customer_group or group_notification_failed:
            servicers_to_notify = self._get_online_servicers()
            if servicers_to_notify:
                suffix = "\n附：客服群消息发送失败" if self.customer_group and group_notification_failed else ""
                full_msg = f"{msg}{suffix}"

                for servicer_id in servicers_to_notify:
                    try:
                        await platform.get_client().send_private_msg(
                            user_id=int(servicer_id), message=full_msg
                        )
                        logger.info(f"已向客服({servicer_id})私信转人工通知。")
                    except Exception as e:
                        logger.error(f"私信客服({servicer_id})失败: {e}")
            else:
                logger.warning("无在线客服可进行私信通知。")

    async def send_private_history(
        self,
        servicer_id: str,
        user_id: str,
        group_id: str,
        send_name: str,
        is_private_request: bool,
    ):

        nodes = []
        platform = self.context.get_platform(filter.PlatformAdapterType.AIOCQHTTP)
        client = platform.get_client()

        try:
            if is_private_request:

                nodes = await self.get_formatted_history(
                    target_id=int(user_id), is_group=False, count=15
                )
                location_desc = "在私聊中"
            else:

                nodes = await self.get_formatted_history(
                    target_id=int(group_id), is_group=True, count=15
                )
                location_desc = f"在群{group_id}"
        except Exception as e:
            logger.error(f"准备发送历史记录时出错: {e}", exc_info=True)

        if nodes:
            extracted_nodes_data = [
                node["data"]
                for node in nodes
                if node.get("type") == "node" and "data" in node
            ]
            self.user_pre_request_history_for_transfer[user_id] = extracted_nodes_data
            logger.debug(
                f"已为用户 {user_id} 缓存请求前历史记录，共 {len(extracted_nodes_data)} 条，用于转接。"
            )

        history_sent = False
        if nodes:
            try:
                await client.send_private_forward_msg(
                    user_id=int(servicer_id), messages=nodes
                )
                history_sent = True
                logger.info(
                    f"已向客服 {servicer_id} 发送用户 {user_id} 的{location_desc}历史记录。"
                )
            except Exception as e:
                logger.error(
                    f"发送私聊历史给客服({servicer_id})失败: {e}", exc_info=True
                )

                try:
                    await client.send_private_msg(
                        user_id=int(servicer_id),
                        message=f"[客服插件] 获取用户{location_desc}的历史记录失败: {e}",
                    )
                except Exception as e2:
                    logger.error(
                        f"发送历史记录获取失败提示给客服({servicer_id})也失败了: {e2}",
                        exc_info=True,
                    )
        else:

            logger.info(f"用户 {user_id} {location_desc}无历史记录可发送或获取失败。")

            try:
                await client.send_private_msg(
                    user_id=int(servicer_id),
                    message=f"[客服插件] 未获取到用户{location_desc}的历史记录。",
                )
            except Exception as e:
                logger.error(
                    f"发送无历史记录提示给客服({servicer_id})失败: {e}", exc_info=True
                )

        try:
            servicer_display_name_for_servicer = self._get_servicer_display_name(
                servicer_id, for_user=False
            )

            if is_private_request:
                detailed_info = f"{servicer_display_name_for_servicer}，{send_name}({user_id})在私聊申请人工前的聊天记录{'已给出' if history_sent else '未能获取'}，接下来我将转发你的消息给对方，请开始对话："
            else:
                detailed_info = f"{servicer_display_name_for_servicer}，{send_name}({user_id})在群{group_id}申请人工前的聊天记录{'已给出' if history_sent else '未能获取'}，接下来我将转发你的消息给对方，请开始对话："

            await client.send_private_msg(
                user_id=int(servicer_id), message=detailed_info
            )
            logger.info(f"已向客服 {servicer_id} 发送对话开始提示。")
        except Exception as e:
            logger.error(
                f"发送对话开始提示给客服({servicer_id})失败: {e}", exc_info=True
            )

    async def send_servicer_chat_history(
        self, target_servicer_id: str, user_id: str, user_name: str
    ):
        chat_history = self.user_servicer_chat_history.get(user_id, [])
        if not chat_history:
            logger.info(
                f"[转接] 用户 {user_id} 无与旧客服的对话记录可发送给新客服 {target_servicer_id}。"
            )
            return
        nodes = []
        old_servicer_id = self.session_map.get(user_id, {}).get(
            "servicer_id", "未知客服"
        )
        old_servicer_display_name_raw = self._get_servicer_display_name(
            old_servicer_id, for_user=False
        )
        new_servicer_display_name_raw = self._get_servicer_display_name(
            target_servicer_id, for_user=False
        )

        for record in chat_history:
            sender_type = record["sender"]
            message_content = record["message"]
            if sender_type == "user":
                sender_name = user_name
                sender_uin = user_id
            else:
                sender_name = old_servicer_display_name_raw
                sender_uin = old_servicer_id

            nodes.append(
                {
                    "type": "node",
                    "data": {
                        "name": sender_name,
                        "uin": str(sender_uin),
                        "content": message_content,
                    },
                }
            )
        if nodes:
            try:
                platform = self.context.get_platform(
                    filter.PlatformAdapterType.AIOCQHTTP
                )
                client = platform.get_client()
                await client.send_private_forward_msg(
                    user_id=int(target_servicer_id), messages=nodes
                )
                logger.info(
                    f"[转接] 已向新客服 {target_servicer_id} 发送用户 {user_id} 与旧客服({old_servicer_id})的对话历史记录 ({len(nodes)} 条)。"
                )
            except Exception as e:
                error_msg = (
                    f"[转接] 发送对话历史给新客服({target_servicer_id})失败: {e}"
                )
                logger.error(error_msg, exc_info=True)
                await self._send_simple_msg_to_servicer(
                    target_servicer_id, f"[客服插件] [转接] 获取对话历史失败: {e}"
                )
        else:
            logger.info(
                f"[转接] 用户 {user_id} 与旧客服的对话历史记录为空，尽管缓存存在。"
            )
        if nodes:
            try:
                detailed_info = f"[转接] {new_servicer_display_name_raw}，以下是用户 {user_name}({user_id}) 与旧客服 {old_servicer_display_name_raw} 的对话历史记录，请开始对话："
                await client.send_private_msg(
                    user_id=int(target_servicer_id), message=detailed_info
                )
                logger.info(
                    f"[转接] 已向新客服 {target_servicer_id} 发送对话历史提示。"
                )
            except Exception as e:
                logger.error(
                    f"[转接] 发送对话历史提示给新客服({target_servicer_id})失败: {e}",
                    exc_info=True,
                )

    async def _send_simple_msg_to_servicer(self, servicer_id: str, message: str):

        try:
            platform = self.context.get_platform(filter.PlatformAdapterType.AIOCQHTTP)
            client = platform.get_client()
            await client.send_private_msg(user_id=int(servicer_id), message=message)
        except Exception as e:
            logger.error(f"发送消息给客服({servicer_id})失败: {e}", exc_info=True)

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
                await platform.get_client().send_group_msg(
                    group_id=int(group_id), message=message
                )
            elif user_id:
                await platform.get_client().send_private_msg(
                    user_id=int(user_id), message=message
                )
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
                await platform.get_client().send_group_msg(
                    group_id=int(group_id), message=ob_message
                )
            elif user_id:
                await platform.get_client().send_private_msg(
                    user_id=int(user_id), message=ob_message
                )
        except Exception as e:
            logger.error(f"发送OB消息失败: {e}")

    @filter.command("转人工", priority=1)
    async def transfer_to_human(self, event: AiocqhttpMessageEvent):

        sender_id = event.get_sender_id()
        send_name = event.get_sender_name()
        group_id = event.get_group_id() or "0"
        is_private = event.is_private_chat()

        if sender_id in self.session_map:

            yield event.plain_result("[客服服务]\n您未申请转人工。")

            return
        if sender_id in self.pending_requests or sender_id in self.waiting_queue:
            position = self._get_user_queue_position(sender_id)
            if position != -1:

                yield event.plain_result(
                    f"@{send_name} 正在等待客服接入...\n当前排队位次：{position}"
                )

            else:

                yield event.plain_result("[客服服务]\n您未申请转人工。")

            return

        online_servicers = self._get_online_servicers()
        if not online_servicers:

            earliest_time = self._get_earliest_online_time()
            if earliest_time:
                time_str = earliest_time.strftime("%H:%M")

                yield event.plain_result(
                    f"[客服服务]\n当前无客服在线。\n最早在线时间：{time_str}"
                )

            else:

                yield event.plain_result("[客服服务]\n当前无客服在线。")

            return

        self.waiting_queue.append(sender_id)
        self.pending_requests[sender_id] = {
            "group_id": group_id,
            "timestamp": asyncio.get_event_loop().time(),
            "user_name": send_name,
            "is_private": is_private,
        }
        position = self._get_user_queue_position(sender_id)

        await self.notify_customer_group(send_name, sender_id, group_id, is_private)

        asyncio.create_task(self._handle_timeout(sender_id, group_id, is_private))

        yield event.plain_result(
            f"@{send_name} 正在等待客服接入...\n当前排队位次：{position}"
        )

    async def _handle_timeout(self, user_id: str, group_id: str, is_private: bool):

        await asyncio.sleep(self.response_timeout)
        if user_id not in self.pending_requests:
            return

        idle_servicers = self._get_idle_servicers()
        if idle_servicers:
            platform = self.context.get_platform(filter.PlatformAdapterType.AIOCQHTTP)

            msg_to_idle = (
                f"[客服插件]\n用户({user_id})的转人工请求即将超时，可回复此消息接入"
            )

            tasks = [
                platform.get_client().send_private_msg(
                    user_id=int(sid), message=msg_to_idle
                )
                for sid in idle_servicers
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    logger.error(f"私聊提醒空闲客服({idle_servicers[i]})失败: {result}")
        await asyncio.sleep(self.total_timeout - self.response_timeout)

        if user_id in self.pending_requests:

            request_info = self.pending_requests.pop(user_id, None)
            if user_id in self.waiting_queue:
                self.waiting_queue.remove(user_id)

            msg_to_user = (
                "[客服服务]\n⚠人工服务因无人应答而结束\n诶呀……看来客服都在休息呢"
            )

            await self.send(
                event=None,
                message=msg_to_user,
                group_id=group_id if not is_private else None,
                user_id=user_id if is_private else None,
            )

            user_name = request_info.get("user_name", user_id)

            location_desc = "在私聊中" if is_private else f"在群{group_id}"
            msg_to_group = (
                f"[客服插件]\n{user_name}({user_id}){location_desc}的人工请求已超时。"
            )

            platform = self.context.get_platform(filter.PlatformAdapterType.AIOCQHTTP)
            for gid in self.customer_group:
                try:
                    await platform.get_client().send_group_msg(
                        group_id=int(gid), message=msg_to_group
                    )
                except Exception as e:
                    logger.error(f"超时通知客服群({gid})失败: {e}")

    @filter.command("转人机", priority=1)
    async def transfer_to_bot(self, event: AiocqhttpMessageEvent):

        sender_id = event.get_sender_id()
        sender_name = event.get_sender_name()
        group_id = event.get_group_id() or "0"
        is_private = event.is_private_chat()
        platform = self.context.get_platform(filter.PlatformAdapterType.AIOCQHTTP)
        client = platform.get_client()

        async def _get_group_name(client, group_id: str) -> str:

            if not group_id or group_id == "0":
                return group_id
            try:

                group_info = await client.get_group_info(group_id=int(group_id))
                return group_info.get("group_name", group_id)
            except Exception as e:
                logger.warning(f"获取群名 {group_id} 失败: {e}")
                return group_id

        if sender_id in self.pending_requests or sender_id in self.waiting_queue:
            request_info = self.pending_requests.pop(sender_id, None)
            was_in_queue = sender_id in self.waiting_queue
            if was_in_queue:
                self.waiting_queue.remove(sender_id)

            location_desc = "在私聊中"
            if not ((request_info and request_info.get("is_private")) or is_private):

                target_group_id = (
                    request_info and request_info.get("group_id")
                ) or group_id
                group_name = await _get_group_name(client, target_group_id)
                location_desc = f"在群{group_name}({target_group_id})"

            msg_to_group = (
                f"[客服插件]\n{sender_name}({sender_id}){location_desc}已取消人工请求"
            )

            for gid in self.customer_group:
                try:
                    await platform.get_client().send_group_msg(
                        group_id=int(gid), message=msg_to_group
                    )
                except Exception as e:
                    logger.error(f"取消排队通知客服群({gid})失败: {e}")

            if was_in_queue:
                await self._refresh_queue_notifications()

            self.user_pre_request_history_for_transfer.pop(sender_id, None)

            yield event.plain_result("[客服服务]\n已取消您的人工服务请求。")

            return

        session = self.session_map.get(sender_id)
        if session and session["status"] == "connected":
            servicer_id = session["servicer_id"]

            user_id = sender_id
            user_name = sender_name
            session_group_id = session.get("group_id", "0")
            is_session_private = session.get("is_private", False)

            if is_session_private:
                msg_to_servicer = (
                    f"[客服插件]\n{user_name}({user_id})在私聊中结束了会话"
                )
            else:

                group_name = await _get_group_name(client, session_group_id)

                msg_to_servicer = f"[客服插件]\n{user_name}({user_id})在群{group_name}({session_group_id})结束了会话"

            try:
                await platform.get_client().send_private_msg(
                    user_id=int(servicer_id), message=msg_to_servicer
                )
            except Exception as e:
                logger.error(f"通知客服({servicer_id})用户结束会话失败: {e}")

            del self.session_map[sender_id]
            self.customer_mode_users.pop(servicer_id, None)

            if sender_id in self.user_temp_nicknames:
                self.user_temp_nicknames[sender_id].pop(servicer_id, None)
                if not self.user_temp_nicknames[sender_id]:
                    del self.user_temp_nicknames[sender_id]

            self.user_servicer_chat_history.pop(sender_id, None)

            self.user_pre_request_history_for_transfer.pop(sender_id, None)

            self._update_servicer_status(servicer_id)

            yield event.plain_result(
                "[客服服务]\n好的，您的人工服务已结束。\n已回到人机模式。"
            )

            return

        yield event.plain_result("[客服服务]\n您未申请转人工。")

    @filter.command("转接", priority=2)
    async def transfer_request(
        self, event: AiocqhttpMessageEvent, target_identifier: str = ""
    ):

        async for result in self._handle_transfer_request(
            event, target_identifier, force=False
        ):
            yield result

    @filter.command("强制转接", priority=2)
    async def force_transfer_request(
        self, event: AiocqhttpMessageEvent, target_identifier: str = ""
    ):

        async for result in self._handle_transfer_request(
            event, target_identifier, force=True
        ):
            yield result

    @filter.command("拒绝转接", priority=2)
    async def reject_transfer(self, event: AiocqhttpMessageEvent):

        target_servicer_id = event.get_sender_id()

        if target_servicer_id not in self.transfer_requests:

            yield event.plain_result("[客服插件] 您当前没有待处理的转接请求。")

            return
        request_info = self.transfer_requests.pop(target_servicer_id, None)
        if not request_info:

            yield event.plain_result("[客服插件] 转接请求已失效。")

            return
        from_servicer_id = request_info["from_servicer_id"]
        user_id = request_info["user_id"]
        user_name = request_info["user_name"]

        platform = self.context.get_platform(filter.PlatformAdapterType.AIOCQHTTP)
        try:

            await platform.get_client().send_private_msg(
                user_id=int(from_servicer_id),
                message=f"[客服插件]\n转接失败，客服{target_servicer_id}拒绝了您的转接请求。用户 {user_name}({user_id}) 已归还给您。",
            )

        except Exception as e:
            logger.error(f"通知源客服({from_servicer_id})转接被拒绝失败: {e}")

        try:
            servicer_display_name_for_user = self._get_servicer_display_name(
                target_servicer_id, for_user=True, user_id=user_id
            )
            from_servicer_display_name_for_user = self._get_servicer_display_name(
                from_servicer_id, for_user=True, user_id=user_id
            )

            msg_to_user = f"[客服服务]\n{from_servicer_display_name_for_user}未能成功转接至{servicer_display_name_for_user}，现在回到原客服为您服务。"

            if request_info["is_private"]:
                await platform.get_client().send_private_msg(
                    user_id=int(user_id), message=msg_to_user
                )
            else:
                await platform.get_client().send_group_msg(
                    group_id=int(request_info["group_id"]),
                    message=f"[CQ:at,qq={user_id}] {msg_to_user}",
                )
        except Exception as e:
            logger.error(f"通知用户({user_id})转接被拒绝失败: {e}")

        yield event.plain_result(
            f"[客服插件] 已拒绝来自客服{from_servicer_id}的转接请求。"
        )

    async def _handle_transfer_request(
        self, event: AiocqhttpMessageEvent, target_identifier: str, force: bool
    ):

        from_servicer_id = event.get_sender_id()

        if from_servicer_id not in self.servicers_id:
            return

        target_user_id = self.customer_mode_users.get(from_servicer_id)
        if not target_user_id:

            yield event.plain_result("[客服插件] 您当前未在服务任何用户。")

            return
        session = self.session_map.get(target_user_id)
        if not session or session.get("status") != "connected":

            yield event.plain_result("[客服插件] 会话状态异常。")

            return
        if not target_identifier:

            yield event.plain_result(
                f"[客服插件] 请提供目标客服的QQ号或昵称，例如：/{'强制' if force else ''}转接 123456 或 /{'强制' if force else ''}转接 小白"
            )

            return

        target_servicer_id = None
        if target_identifier.isdigit():

            if target_identifier in self.servicers_id:
                target_servicer_id = target_identifier

        else:

            for sid, config in self.servicer_config.items():
                if (
                    config.get("use_nickname", False)
                    and config.get("nickname", "") == target_identifier
                ):
                    target_servicer_id = sid
                    break
        if not target_servicer_id or target_servicer_id not in self.servicers_id:

            yield event.plain_result(
                "[客服插件] 转接失败，请检查目标客服QQ或者昵称是否正确。发送‘客服状态’指令获取所有客服信息。"
            )

            return
        if target_servicer_id == from_servicer_id:

            yield event.plain_result("[客服插件] 不能转接给自己。")

            return

        self._update_servicer_status(target_servicer_id)
        target_status = self.servicer_config[target_servicer_id]["status"]

        if not force and target_status == "offline":

            yield event.plain_result(f"[客服插件] 转接失败，目标客服不在服务时间。")

            return

        if target_status == "busy":

            yield event.plain_result(f"[客服插件] 转接失败，目标客服正在忙。")

            return

        user_id = target_user_id
        user_name = session.get("user_name", user_id)

        if user_id in self.pending_requests:
            user_name = self.pending_requests[user_id].get("user_name", user_id)
        elif user_id in self.session_map:

            pass
        from_servicer_display_name = self._get_servicer_display_name(
            from_servicer_id, for_user=True, user_id=user_id
        )
        to_servicer_display_name = self._get_servicer_display_name(
            target_servicer_id, for_user=True, user_id=user_id
        )
        platform = self.context.get_platform(filter.PlatformAdapterType.AIOCQHTTP)
        try:

            msg_to_user = f"[客服服务]\n{from_servicer_display_name}已申请转接至{to_servicer_display_name}。"

            if session.get("is_private", False):
                await platform.get_client().send_private_msg(
                    user_id=int(user_id), message=msg_to_user
                )
            else:
                await platform.get_client().send_group_msg(
                    group_id=int(session.get("group_id", "0")),
                    message=f"[CQ:at,qq={user_id}] {msg_to_user}",
                )
        except Exception as e:
            logger.error(f"通知用户({user_id})转接开始失败: {e}")

        self.transfer_requests[target_servicer_id] = {
            "from_servicer_id": from_servicer_id,
            "user_id": user_id,
            "user_name": user_name,
            "group_id": session.get("group_id", "0"),
            "is_private": session.get("is_private", False),
            "timestamp": asyncio.get_event_loop().time(),
        }

        try:
            from_servicer_display_name_for_target = self._get_servicer_display_name(
                from_servicer_id, for_user=False
            )

            await platform.get_client().send_private_msg(
                user_id=int(target_servicer_id),
                message=f"[客服插件]\n客服{from_servicer_display_name_for_target}向您转接了用户 {user_name}({user_id}) 的通话，发送‘/接入对话’来接受。",
            )

        except Exception as e:
            logger.error(f"通知目标客服({target_servicer_id})转接请求失败: {e}")

        yield event.plain_result(
            f"[客服插件] 已向客服{target_servicer_id}发送转接请求。"
        )

        asyncio.create_task(self._handle_transfer_timeout(target_servicer_id))

    async def _handle_transfer_timeout(self, target_servicer_id: str):

        await asyncio.sleep(self.transfer_response_timeout)

        if target_servicer_id in self.transfer_requests:
            request_info = self.transfer_requests.pop(target_servicer_id, None)
            if not request_info:
                return
            from_servicer_id = request_info["from_servicer_id"]
            user_id = request_info["user_id"]
            user_name = request_info["user_name"]

            platform = self.context.get_platform(filter.PlatformAdapterType.AIOCQHTTP)
            try:

                await platform.get_client().send_private_msg(
                    user_id=int(from_servicer_id),
                    message=f"[客服插件]\n转接请求超时，用户 {user_name}({user_id}) 已归还给您。",
                )

            except Exception as e:
                logger.error(f"通知源客服({from_servicer_id})转接超时失败: {e}")

            try:
                from_servicer_display_name_for_user = self._get_servicer_display_name(
                    from_servicer_id, for_user=True, user_id=user_id
                )

                msg_to_user = f"[客服服务]\n{from_servicer_display_name_for_user}的转接请求超时，现在回到原客服为您服务。"

                if request_info["is_private"]:
                    await platform.get_client().send_private_msg(
                        user_id=int(user_id), message=msg_to_user
                    )
                else:
                    await platform.get_client().send_group_msg(
                        group_id=int(request_info["group_id"]),
                        message=f"[CQ:at,qq={user_id}] {msg_to_user}",
                    )
            except Exception as e:
                logger.error(f"通知用户({user_id})转接超时失败: {e}")
            logger.info(
                f"转接请求 from {from_servicer_id} to {target_servicer_id} for user {user_id} timed out."
            )

    @filter.command("接入对话", priority=1)
    async def accept_conversation(
        self, event: AiocqhttpMessageEvent, target_id: str | int | None = None
    ):

        sender_id = event.get_sender_id()

        if sender_id not in self.servicers_id:
            return

        is_transfer = False
        request_info = None
        if sender_id in self.transfer_requests:
            is_transfer = True
            request_info = self.transfer_requests.pop(sender_id, None)
            if not request_info:

                yield event.plain_result(f"[客服插件] 转接请求已失效。")

                return
            target_id = request_info["user_id"]
            logger.info(f"客服 {sender_id} 通过转接请求接入用户 {target_id}")

        if not is_transfer:
            self._update_servicer_status(sender_id)
            if self.servicer_config[sender_id]["status"] == "busy":

                yield event.plain_result("[客服插件]\n接通失败。")

                return

        if not is_transfer:
            if reply_seg := next(
                (seg for seg in event.get_messages() if isinstance(seg, Reply)), None
            ):
                if text := reply_seg.message_str:
                    matches = re.findall(r"\((\d+)\)", text)
                    if matches:
                        target_id = matches[-1]
        if not target_id or (
            not is_transfer and str(target_id) not in self.pending_requests
        ):
            if not is_transfer:

                yield event.plain_result(f"[客服插件]\n接通失败。")

                return

        if not is_transfer:
            if str(target_id) in self.waiting_queue:
                self.waiting_queue.remove(str(target_id))
            request_info_from_pending = self.pending_requests.pop(str(target_id), None)
            if not request_info_from_pending and not is_transfer:

                yield event.plain_result(f"[客服插件]\n接通失败。")

                return

        if is_transfer:
            old_servicer_id = self.session_map.get(str(target_id), {}).get(
                "servicer_id"
            )
            if old_servicer_id:

                self.customer_mode_users.pop(old_servicer_id, None)

                self._update_servicer_status(old_servicer_id)
                logger.info(
                    f"转接成功，已断开用户 {target_id} 与原客服 {old_servicer_id} 的连接。"
                )

        session_data = {
            "servicer_id": sender_id,
            "status": "connected",
            "group_id": (
                request_info.get("group_id", "0")
                if is_transfer
                else request_info_from_pending.get("group_id", "0")
            ),
            "is_private": (
                request_info.get("is_private", False)
                if is_transfer
                else request_info_from_pending.get("is_private", False)
            ),
            "user_name": (
                request_info.get("user_name", str(target_id))
                if is_transfer
                else request_info_from_pending.get("user_name", str(target_id))
            ),
        }
        self.session_map[str(target_id)] = session_data
        self.customer_mode_users[sender_id] = str(target_id)

        self.servicer_config[sender_id]["status"] = "busy"

        user_request_group_id = None
        if (
            not session_data.get("is_private")
            and session_data.get("group_id")
            and session_data.get("group_id") != "0"
        ):
            user_request_group_id = session_data.get("group_id")
        for group_id_str in self.customer_group:
            try:
                platform = self.context.get_platform(
                    filter.PlatformAdapterType.AIOCQHTTP
                )
                target_group_id = int(group_id_str)
                servicer_display_name_for_group = self._get_servicer_display_name(
                    sender_id, for_user=False
                )
                user_source_desc = (
                    "私聊"
                    if session_data.get("is_private")
                    else f"群{session_data.get('group_id')}"
                )
                user_name = session_data.get("user_name", str(target_id))
                if is_transfer:

                    await platform.get_client().send_group_msg(
                        group_id=target_group_id,
                        message=f"[客服插件]\n{servicer_display_name_for_group}已通过转接接入{user_source_desc}中用户{user_name}({target_id})的对话",
                    )

                else:

                    if user_request_group_id and str(target_group_id) == str(
                        user_request_group_id
                    ):

                        await platform.get_client().send_group_msg(
                            group_id=target_group_id,
                            message=f"[客服插件]\n好的，现在请{servicer_display_name_for_group}前往bot的私聊进行下一步操作",
                        )

                    else:

                        await platform.get_client().send_group_msg(
                            group_id=target_group_id,
                            message=f"[客服插件]\n{servicer_display_name_for_group}已在{user_source_desc}接入{user_name}({target_id})的对话",
                        )

            except Exception as e:
                logger.error(f"客服群({group_id_str})通知失败: {e}")

        send_name = session_data.get("user_name", str(target_id))
        is_request_from_private = session_data.get("is_private", False)
        if is_transfer:

            await self.send_servicer_chat_history(
                target_servicer_id=sender_id,
                user_id=str(target_id),
                user_name=send_name,
            )

            to_servicer_display_name = self._get_servicer_display_name(
                sender_id, for_user=True, user_id=str(target_id)
            )
            from_servicer_display_name = self._get_servicer_display_name(
                self.session_map.get(str(target_id), {}).get("servicer_id", ""),
                for_user=True,
                user_id=str(target_id),
            )

            msg_to_user = f"[客服服务]\n客服{from_servicer_display_name}已成功转接至客服{to_servicer_display_name}，请继续对话。"

            await self.send(
                event=None,
                message=msg_to_user,
                group_id=(
                    session_data.get("group_id")
                    if not session_data.get("is_private")
                    else None
                ),
                user_id=target_id if session_data.get("is_private") else None,
            )
        else:

            await self.send_private_history(
                servicer_id=sender_id,
                user_id=str(target_id),
                group_id=session_data.get("group_id", "0"),
                send_name=send_name,
                is_private_request=is_request_from_private,
            )

        if not is_transfer:
            servicer_display_name = self._get_servicer_display_name(
                sender_id, for_user=True, user_id=str(target_id)
            )

            msg_to_user = f"[客服服务]\n{servicer_display_name}已接入，请开始对话，在此过程中其他功能将暂时不可用"

            await self.send(
                event=None,
                message=msg_to_user,
                group_id=(
                    session_data.get("group_id")
                    if not session_data.get("is_private")
                    else None
                ),
                user_id=target_id if session_data.get("is_private") else None,
            )

        if is_transfer:

            yield event.plain_result(
                f"[客服插件] 已成功接入用户 {target_id} 的转接对话。"
            )

        else:

            servicer_display_name_for_servicer = self._get_servicer_display_name(
                sender_id, for_user=False
            )
            yield event.plain_result(
                f"[客服插件]\n好的，现在请{servicer_display_name_for_servicer}前往bot的私聊进行下一步操作"
            )

        event.stop_event()

    @filter.command("结束对话")
    async def end_conversation(self, event: AiocqhttpMessageEvent):

        sender_id = event.get_sender_id()
        is_servicer = sender_id in self.servicers_id
        if is_servicer:

            for uid, session in self.session_map.items():
                if session["servicer_id"] == sender_id:

                    await self.send(
                        event,
                        message="[客服服务]\n客服已结束对话",
                        group_id=session["group_id"],
                        user_id=uid,
                    )

                    del self.session_map[uid]
                    self.customer_mode_users.pop(sender_id, None)

                    if uid in self.user_temp_nicknames:
                        self.user_temp_nicknames[uid].pop(sender_id, None)
                        if not self.user_temp_nicknames[uid]:
                            del self.user_temp_nicknames[uid]

                    self.user_servicer_chat_history.pop(uid, None)

                    self.user_pre_request_history_for_transfer.pop(uid, None)

                    self._update_servicer_status(sender_id)

                    yield event.plain_result(f"[客服插件]\n已结束与用户 {uid} 的对话")

                    return

            yield event.plain_result("[客服插件]\n您当前没有进行中的对话。")

        else:

            await self.transfer_to_bot(event)

    @filter.command("设置客服昵称", priority=2)
    async def set_servicer_nickname(
        self, event: AiocqhttpMessageEvent, nickname: str = ""
    ):

        sender_id = event.get_sender_id()
        if sender_id not in self.servicers_id:
            return
        if not nickname:

            yield event.plain_result("[客服配置]\n请提供昵称，例如：/设置客服昵称 小白")

            return

        if len(nickname) > 6:

            yield event.plain_result("[客服配置]\n昵称最多6个字符。")

            return
        self.servicer_config.setdefault(sender_id, {})["nickname"] = nickname

        yield event.plain_result(f"[客服配置]\n已将您的昵称设置为：{nickname}")

        self._save_persistent_config()

    @filter.command("开启客服昵称", priority=2)
    async def enable_servicer_nickname(self, event: AiocqhttpMessageEvent):

        sender_id = event.get_sender_id()
        if sender_id not in self.servicers_id:
            return
        self.servicer_config.setdefault(sender_id, {})["use_nickname"] = True
        nickname = self.servicer_config[sender_id].get("nickname", "")
        display_name = f"客服{nickname}" if nickname else f"客服{sender_id}"

        yield event.plain_result(
            f"[客服配置]\n已开启客服昵称。\n您当前的显示名称为：{display_name}"
        )

        self._save_persistent_config()

    @filter.command("关闭客服昵称", priority=2)
    async def disable_servicer_nickname(self, event: AiocqhttpMessageEvent):

        sender_id = event.get_sender_id()
        if sender_id not in self.servicers_id:
            return
        self.servicer_config.setdefault(sender_id, {})["use_nickname"] = False

        yield event.plain_result(
            f"[客服配置]\n已关闭客服昵称。\n您当前的显示名称为：客服{sender_id}"
        )

        self._save_persistent_config()

    @filter.command("设置客服时间", priority=2)
    async def set_servicer_time(self, event: AiocqhttpMessageEvent, times: str = ""):

        sender_id = event.get_sender_id()
        if sender_id not in self.servicers_id:
            return

        if not times:

            full_message = event.message_str
            cmd_prefix = "/设置客服时间"
            if full_message.startswith(cmd_prefix):
                times_str = full_message[len(cmd_prefix) :].strip()
                times_list = times_str.split() if times_str else []
            else:

                times_list = []
        else:

            times_list = times.split()
        if not times_list:

            yield event.plain_result(
                "[客服配置]\n请提供在线时间，例如：/设置客服时间 12:00~20:00 或 /设置客服时间 24h"
            )

            return
        schedule = []
        try:
            if len(times_list) == 1 and times_list[0].lower() == "24h":

                self.servicer_config.setdefault(sender_id, {})["online_schedule"] = []

                yield event.plain_result("[客服配置]\n已设置为24小时在线。")

                self._update_servicer_status(sender_id)

                self._save_persistent_config()
                return

            for time_str in times_list:
                if "~" not in time_str:
                    raise ValueError("时间格式错误")
                start_str, end_str = time_str.split("~")
                start_time = datetime.strptime(start_str.strip(), "%H:%M").time()
                end_time = datetime.strptime(end_str.strip(), "%H:%M").time()
                schedule.append((start_time, end_time))
            self.servicer_config.setdefault(sender_id, {})["online_schedule"] = schedule
            time_list_str = ", ".join(
                [f'{s.strftime("%H:%M")}~{e.strftime("%H:%M")}' for s, e in schedule]
            )

            yield event.plain_result(f"[客服配置]\n已设置在线时间：{time_list_str}")

            self._update_servicer_status(sender_id)

            self._save_persistent_config()
        except Exception as e:
            logger.error(f"解析客服时间失败: {e}")

            yield event.plain_result(
                "[客服配置]\n诶……设置失败，请检查格式\n例如：/设置客服时间 12:00~20:00 或 /设置客服时间 24h"
            )

    @filter.command("上班", priority=2)
    async def go_to_work(self, event: AiocqhttpMessageEvent):
        sender_id = event.get_sender_id()
        if sender_id not in self.servicers_id:
            return
        self._update_servicer_status(sender_id)
        current_status = self.servicer_config[sender_id].get("status", "unknown")
        manual_override = self.servicer_config[sender_id].get("manual_status_override")
        if current_status == "busy":
            yield event.plain_result("[客服配置] 您正在服务用户，请先结束对话。")
            return
        if manual_override == "idle":
            yield event.plain_result("[客服配置] 您已经在上班状态了。")
            return
        if manual_override == "offline":
            self.servicer_config[sender_id].pop("manual_status_override", None)
            self._update_servicer_status(sender_id)
            new_status = self.servicer_config[sender_id]["status"]
            if new_status == "busy":
                yield event.plain_result(
                    "[客服配置] 已退出手动下班模式，但您已开始服务用户。"
                )
            elif new_status == "idle":
                yield event.plain_result(
                    "[客服配置] 已退出手动下班模式，当前为自动空闲状态。"
                )
            else:
                yield event.plain_result(
                    "[客服配置] 已退出手动下班模式，根据时间计划当前为离线状态。"
                )
            self._save_persistent_config()
            return
        self.servicer_config.setdefault(sender_id, {})[
            "manual_status_override"
        ] = "idle"
        self._update_servicer_status(sender_id)
        yield event.plain_result("[客服配置] 好的，您已进入手动上班状态。")
        self._save_persistent_config()

    @filter.command("下班", priority=2)
    async def off_work(self, event: AiocqhttpMessageEvent):
        sender_id = event.get_sender_id()
        if sender_id not in self.servicers_id:
            return
        self._update_servicer_status(sender_id)
        current_status = self.servicer_config[sender_id].get("status", "unknown")
        manual_override = self.servicer_config[sender_id].get("manual_status_override")
        if current_status == "busy":
            yield event.plain_result("[客服配置] 您正在服务用户，请先结束对话。")
            return
        if manual_override == "offline":
            yield event.plain_result("[客服配置] 您已经处于下班状态了。")
            return
        if manual_override == "idle":
            self.servicer_config[sender_id].pop("manual_status_override", None)
            self._update_servicer_status(sender_id)
            new_status = self.servicer_config[sender_id]["status"]
            if new_status == "busy":
                yield event.plain_result(
                    "[客服配置] 已退出手动上班模式，但您已开始服务用户。"
                )
            elif new_status == "idle":
                yield event.plain_result(
                    "[客服配置] 已退出手动上班模式，根据时间计划当前为自动空闲状态。"
                )
            else:
                yield event.plain_result(
                    "[客服配置] 已退出手动上班模式，根据时间计划当前为离线状态。"
                )
            self._save_persistent_config()
            return
        self.servicer_config.setdefault(sender_id, {})[
            "manual_status_override"
        ] = "offline"
        self._update_servicer_status(sender_id)
        yield event.plain_result("[客服配置] 好的，您已进入手动下班状态。")
        self._save_persistent_config()

    @filter.command("客服状态", priority=2)
    async def servicer_status(self, event: AiocqhttpMessageEvent):

        sender_id = event.get_sender_id()
        if sender_id not in self.servicers_id:
            return

        status_lines = ["[客服状态]\n格式为：客服[昵称]([QQ])-[状态]"]

        for sid in self.servicers_id:
            self._update_servicer_status(sid)

        for sid in self.servicers_id:
            config = self.servicer_config.get(sid, {})
            nickname = config.get("nickname", "")
            use_nickname = config.get("use_nickname", False)

            status = config.get("status", "unknown")

            display_status = {"offline": "离线", "idle": "空闲", "busy": "繁忙"}.get(
                status, status
            )

            if use_nickname and nickname:
                line = f"客服{nickname}({sid})--{display_status}"
            else:
                line = f"客服{sid}--{display_status}"

            status_lines.append(line)

        queue_info = f"\n当前排队人数: {len(self.waiting_queue)}"
        if self.waiting_queue:
            queue_info += f"\n队首用户: {self.waiting_queue[0]}"

        yield event.plain_result("\n".join(status_lines) + queue_info)

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

                if target_user in self.user_servicer_chat_history:
                    self.user_servicer_chat_history[target_user].append(
                        {"sender": "servicer", "message": event.message_str}
                    )
                else:
                    self.user_servicer_chat_history[target_user] = [
                        {"sender": "servicer", "message": event.message_str}
                    ]

                group_id = session["group_id"]
                is_private = session["is_private"]
                if not is_private:

                    message = f"[CQ:at,qq={target_user}] " + event.message_str
                    await self.send(event=None, message=message, group_id=group_id)
                else:

                    await self.send(
                        event=None, message=event.message_str, user_id=target_user
                    )
                event.stop_event()
            return

        session = self.session_map.get(sender_id)
        if session and session.get("status") == "connected":
            servicer_id = session["servicer_id"]

            is_session_private = session.get("is_private", False)
            session_group_id = session.get("group_id", "0")

            if is_session_private and not event.is_private_chat():
                return

            if not is_session_private:

                if not event.is_private_chat() and str(
                    event.get_group_id() or "0"
                ) != str(session_group_id):
                    return

            if sender_id in self.user_servicer_chat_history:
                self.user_servicer_chat_history[sender_id].append(
                    {"sender": "user", "message": event.message_str}
                )
            else:
                self.user_servicer_chat_history[sender_id] = [
                    {"sender": "user", "message": event.message_str}
                ]

            await self.send(event=None, message=event.message_str, user_id=servicer_id)
            event.stop_event()
