import asyncio
import itertools
import logging
import time
import uuid
from collections.abc import Awaitable
from typing import Any, cast, Dict

from aiocqhttp import CQHttp, Event
from aiocqhttp.exceptions import ActionFailed

from astrbot.api import logger
from astrbot.api.event import MessageChain
from astrbot.api.message_components import *
from astrbot.api.platform import (
    AstrBotMessage,
    MessageMember,
    MessageType,
    Platform,
    PlatformMetadata,
)
from astrbot.core.platform.astr_message_event import MessageSesion

from ...register import register_platform_adapter
from .aiocqhttp_message_event import *
from .aiocqhttp_message_event import AiocqhttpMessageEvent


@register_platform_adapter(
    "aiocqhttp",
    "适用于 OneBot V11 标准的消息平台适配器，支持反向 WebSockets。",
    support_streaming_message=False,
)
class AiocqhttpAdapter(Platform):
    def __init__(
        self,
        platform_config: dict,
        platform_settings: dict,
        event_queue: asyncio.Queue,
    ) -> None:
        super().__init__(platform_config, event_queue)

        self.settings = platform_settings
        self.host = platform_config["ws_reverse_host"]
        self.port = platform_config["ws_reverse_port"]

        self.metadata = PlatformMetadata(
            name="aiocqhttp",
            description="适用于 OneBot 标准的消息平台适配器，支持反向 WebSockets。",
            id=cast(str, self.config.get("id")),
            support_streaming_message=False,
        )

        self.bot = CQHttp(
            use_ws_reverse=True,
            import_name="aiocqhttp",
            api_timeout_sec=180,
            access_token=platform_config.get(
                "ws_reverse_token",
            ),  # 以防旧版本配置不存在
        )

        # --- 消息分段聚合相关配置 ---
        self.user_message_buffers: Dict[str, Dict[str, Any]] = {}
        # 用户发送分段消息的等待时间（秒）
        self.segment_wait_time: float = self.config.get("segment_input_wait_sec", 10)

        @self.bot.on_request()
        async def request(event: Event):
            abm = await self.convert_message(event)
            if abm:
                await self.handle_msg(abm)

        @self.bot.on_notice()
        async def notice(event: Event):
            abm = await self.convert_message(event)
            if abm:
                await self.handle_msg(abm)

        @self.bot.on_message("group")
        async def group(event: Event):
            abm = await self.convert_message(event)
            if abm:
                await self.handle_msg(abm)

        @self.bot.on_message("private")
        async def private(event: Event):
            abm = await self.convert_message(event)
            if abm:
                await self.handle_msg(abm)

        @self.bot.on_websocket_connection
        def on_websocket_connection(_):
            logger.info("aiocqhttp(OneBot v11) 适配器已连接。")

    async def send_by_session(
        self,
        session: MessageSesion,
        message_chain: MessageChain,
    ):
        is_group = session.message_type == MessageType.GROUP_MESSAGE
        if is_group:
            session_id = session.session_id.split("_")[-1]
        else:
            session_id = session.session_id
        await AiocqhttpMessageEvent.send_message(
            bot=self.bot,
            message_chain=message_chain,
            event=None,  # 这里不需要 event，因为是通过 session 发送的
            is_group=is_group,
            session_id=session_id,
        )
        await super().send_by_session(session, message_chain)

    async def convert_message(self, event: Event) -> AstrBotMessage | None:
        logger.debug(f"[aiocqhttp] RawMessage {event}")

        if event["post_type"] == "message":
            abm = await self._convert_handle_message_event(event)
            if abm and abm.sender.user_id == "2854196310":
                # 屏蔽 QQ 管家的消息
                return None
        elif event["post_type"] == "notice":
            abm = await self._convert_handle_notice_event(event)
        elif event["post_type"] == "request":
            abm = await self._convert_handle_request_event(event)
        else:
            return None

        return abm

    async def _convert_handle_request_event(self, event: Event) -> AstrBotMessage:
        """OneBot V11 请求类事件"""
        abm = AstrBotMessage()
        abm.self_id = str(event.self_id)
        abm.sender = MessageMember(
            user_id=str(event.user_id), nickname=str(event.user_id)
        )
        abm.type = MessageType.OTHER_MESSAGE
        if event.get("group_id"):
            abm.type = MessageType.GROUP_MESSAGE
            abm.group_id = str(event.group_id)
        else:
            abm.type = MessageType.FRIEND_MESSAGE
        abm.session_id = (
            str(event.group_id)
            if abm.type == MessageType.GROUP_MESSAGE
            else abm.sender.user_id
        )
        abm.message_str = ""
        abm.message = []
        abm.timestamp = int(time.time())
        abm.message_id = uuid.uuid4().hex
        abm.raw_message = event
        return abm

    async def _convert_handle_notice_event(self, event: Event) -> AstrBotMessage:
        """OneBot V11 通知类事件"""
        abm = AstrBotMessage()
        abm.self_id = str(event.self_id)
        abm.sender = MessageMember(
            user_id=str(event.user_id), nickname=str(event.user_id)
        )
        abm.type = MessageType.OTHER_MESSAGE
        if event.get("group_id"):
            abm.group_id = str(event.group_id)
            abm.type = MessageType.GROUP_MESSAGE
        else:
            abm.type = MessageType.FRIEND_MESSAGE
        abm.session_id = (
            str(event.group_id)
            if abm.type == MessageType.GROUP_MESSAGE
            else abm.sender.user_id
        )
        abm.message_str = ""
        abm.message = []
        abm.raw_message = event
        abm.timestamp = int(time.time())
        abm.message_id = uuid.uuid4().hex

        if "sub_type" in event:
            if event["sub_type"] == "poke" and "target_id" in event:
                abm.message.append(Poke(qq=str(event["target_id"]), type="poke"))

        return abm

    async def _convert_handle_message_event(
        self,
        event: Event,
        get_reply=True,
    ) -> AstrBotMessage:
        """OneBot V11 消息类事件"""
        assert event.sender is not None
        abm = AstrBotMessage()
        abm.self_id = str(event.self_id)
        abm.sender = MessageMember(
            str(event.sender["user_id"]),
            event.sender.get("card") or event.sender.get("nickname", "N/A"),
        )
        if event["message_type"] == "group":
            abm.type = MessageType.GROUP_MESSAGE
            abm.group_id = str(event.group_id)
            abm.group = Group(str(event.group_id))
            abm.group.group_name = event.get("group_name", "N/A")
        elif event["message_type"] == "private":
            abm.type = MessageType.FRIEND_MESSAGE
        
        abm.session_id = (
            str(event.group_id)
            if abm.type == MessageType.GROUP_MESSAGE
            else abm.sender.user_id
        )

        abm.message_id = str(event.message_id)
        abm.message = []

        message_str = ""
        if not isinstance(event.message, list):
            err = f"aiocqhttp: 无法识别的消息类型: {event.message!s}，此条消息将被忽略。如果您在使用 go-cqhttp，请将其配置文件中的 message.post-format 更改为 array。"
            logger.critical(err)
            try:
                await self.bot.send(event, err)
            except BaseException as e:
                logger.error(f"回复消息失败: {e}")
            raise ValueError(err)

        for t, m_group in itertools.groupby(event.message, key=lambda x: x["type"]):
            if t == "text":
                current_text = "".join(m["data"]["text"] for m in m_group).strip()
                if not current_text:
                    continue
                message_str += current_text
                abm.message.append(ComponentTypes[t](text=current_text))

            elif t == "file":
                for m in m_group:
                    if m["data"].get("url") and m["data"].get("url").startswith("http"):
                        file_name = m["data"].get("file_name", "") or m["data"].get("name", "") or m["data"].get("file", "") or "file"
                        abm.message.append(File(name=file_name, url=m["data"]["url"]))
                    else:
                        try:
                            ret = None
                            if abm.type == MessageType.GROUP_MESSAGE:
                                ret = await self.bot.call_action(
                                    action="get_group_file_url",
                                    file_id=event.message[0]["data"]["file_id"],
                                    group_id=event.group_id,
                                )
                            elif abm.type == MessageType.FRIEND_MESSAGE:
                                ret = await self.bot.call_action(
                                    action="get_private_file_url",
                                    file_id=event.message[0]["data"]["file_id"],
                                )
                            if ret and "url" in ret:
                                file_url = ret["url"]
                                file_name = ret.get("file_name", "") or ret.get("name", "") or m["data"].get("file", "") or m["data"].get("file_name", "")
                                abm.message.append(File(name=file_name, url=file_url))
                            else:
                                logger.error(f"获取文件失败: {ret}")
                        except Exception as e:
                            logger.error(f"获取文件失败: {e}")

            elif t == "reply":
                for m in m_group:
                    if not get_reply:
                        abm.message.append(ComponentTypes[t](**m["data"]))
                    else:
                        try:
                            reply_event_data = await self.bot.call_action(action="get_msg", message_id=int(m["data"]["id"]))
                            reply_event_data["post_type"] = "message"
                            new_event = Event.from_payload(reply_event_data)
                            if not new_event: continue
                            abm_reply = await self._convert_handle_message_event(new_event, get_reply=False)
                            abm.message.append(Reply(
                                id=abm_reply.message_id, chain=abm_reply.message,
                                sender_id=abm_reply.sender.user_id, sender_nickname=abm_reply.sender.nickname,
                                time=abm_reply.timestamp, message_str=abm_reply.message_str,
                                text=abm_reply.message_str, qq=abm_reply.sender.user_id
                            ))
                        except Exception as e:
                            logger.error(f"获取引用消息失败: {e}")
                            abm.message.append(ComponentTypes[t](**m["data"]))
            elif t == "at":
                first_at_self_processed = False
                at_parts = []
                for m in m_group:
                    try:
                        if m["data"]["qq"] == "all":
                            abm.message.append(At(qq="all", name="全体成员"))
                            continue
                        at_info = await self.bot.call_action(action="get_group_member_info", group_id=event.group_id, user_id=int(m["data"]["qq"]), no_cache=False)
                        if at_info:
                            nickname = at_info.get("card", "") or at_info.get("nick", "") or at_info.get("nickname", "")
                            is_at_self = str(m["data"]["qq"]) in {abm.self_id, "all"}
                            abm.message.append(At(qq=m["data"]["qq"], name=nickname))
                            if is_at_self and not first_at_self_processed:
                                first_at_self_processed = True
                            else:
                                at_parts.append(f" @{nickname}({m['data']['qq']}) ")
                        else:
                            abm.message.append(At(qq=str(m["data"]["qq"]), name=""))
                    except Exception as e:
                        logger.error(f"获取 @ 用户信息失败: {e}")
                message_str += "".join(at_parts)
            elif t == "markdown":
                text = m["data"].get("markdown") or m["data"].get("content", "")
                abm.message.append(Plain(text=text))
                message_str += text
            else:
                for m in m_group:
                    try:
                        if t not in ComponentTypes: continue
                        abm.message.append(ComponentTypes[t](**m["data"]))
                    except Exception as e:
                        logger.exception(f"消息段解析失败: {e}")

        abm.timestamp = int(time.time())
        abm.message_str = message_str
        abm.raw_message = event
        return abm

    # --- 聚合逻辑核心方法 ---

    async def handle_msg(self, message: AstrBotMessage):
        """处理传入的消息，支持分段输入聚合。"""
        # 非消息类型或空内容，直接提交
        if message.raw_message.get("post_type") != "message" or (
            not message.message_str and not message.message
        ):
            await self._commit_message_event(message)
            return

        # 指令（如 / 开头）不进入缓冲，立即处理
        if message.message_str.strip().startswith("/"):
            await self._commit_message_event(message)
            return

        session_id = message.session_id

        # 重置现有计时器
        if session_id in self.user_message_buffers:
            timer = self.user_message_buffers[session_id].get("timer")
            if timer:
                timer.cancel()
        else:
            self.user_message_buffers[session_id] = {"messages": [], "timer": None}

        # 存入缓冲区
        self.user_message_buffers[session_id]["messages"].append(message)

        # 开启新计时器
        self.user_message_buffers[session_id]["timer"] = asyncio.create_task(
            self._schedule_processing(session_id)
        )

    async def _schedule_processing(self, session_id: str):
        try:
            await asyncio.sleep(self.segment_wait_time)
            await self._process_buffered_messages(session_id)
        except asyncio.CancelledError:
            pass 
        except Exception as e:
            logger.error(f"调度聚合任务出错: {e}")
            if session_id in self.user_message_buffers:
                del self.user_message_buffers[session_id]

    async def _process_buffered_messages(self, session_id: str):
        if session_id not in self.user_message_buffers:
            return

        buffered_data = self.user_message_buffers.pop(session_id)
        message_list = buffered_data.get("messages", [])
        if not message_list:
            return

        final_message = message_list[0]
        if len(message_list) > 1:
            combined_chain = list(final_message.message)
            combined_str = final_message.message_str

            for i in range(1, len(message_list)):
                next_msg = message_list[i]
                if combined_str and next_msg.message_str:
                    combined_str += "\n"
                combined_str += next_msg.message_str
                combined_chain.extend(next_msg.message)
            
            final_message.message = combined_chain
            final_message.message_str = combined_str.strip()
            # 采用最后一段消息的上下文
            final_message.message_id = message_list[-1].message_id
            final_message.raw_message = message_list[-1].raw_message

        logger.info(f"聚合消息完毕 ({session_id}): {final_message.message_str}")
        await self._commit_message_event(final_message)

    async def _commit_message_event(self, message: AstrBotMessage):
        """统一提交事件的方法"""
        message_event = AiocqhttpMessageEvent(
            message_str=message.message_str,
            message_obj=message,
            platform_meta=self.meta(),
            session_id=message.session_id,
            bot=self.bot,
        )
        self.commit_event(message_event)

    # --- 基础方法 ---

    def run(self) -> Awaitable[Any]:
        if not self.host or not self.port:
            logger.warning("aiocqhttp: 未配置反向WS地址，使用默认 0.0.0.0:6199")
            self.host = "0.0.0.0"
            self.port = 6199

        coro = self.bot.run_task(
            host=self.host,
            port=int(self.port),
            shutdown_trigger=self.shutdown_trigger_placeholder,
        )

        for handler in logging.root.handlers[:]:
            logging.root.removeHandler(handler)
        logging.getLogger("aiocqhttp").setLevel(logging.ERROR)
        self.shutdown_event = asyncio.Event()
        return coro

    async def terminate(self):
        self.shutdown_event.set()

    async def shutdown_trigger_placeholder(self):
        await self.shutdown_event.wait()
        logger.info("aiocqhttp 适配器已被关闭")

    def meta(self) -> PlatformMetadata:
        return self.metadata

    def get_client(self) -> CQHttp:
        return self.bot