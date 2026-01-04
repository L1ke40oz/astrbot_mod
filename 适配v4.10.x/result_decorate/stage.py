import random
import re
import time
import traceback
from collections.abc import AsyncGenerator

from astrbot.core import file_token_service, html_renderer, logger
from astrbot.core.message.components import At, File, Image, Node, Plain, Record, Reply
from astrbot.core.message.message_event_result import ResultContentType
from astrbot.core.pipeline.content_safety_check.stage import ContentSafetyCheckStage
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.message_type import MessageType
from astrbot.core.star.session_llm_manager import SessionServiceManager
from astrbot.core.star.star import star_map
from astrbot.core.star.star_handler import EventType, star_handlers_registry

from ..context import PipelineContext
from ..stage import Stage, register_stage, registered_stages


@register_stage
class ResultDecorateStage(Stage):
    async def initialize(self, ctx: PipelineContext):
        self.ctx = ctx
        self.reply_prefix = ctx.astrbot_config["platform_settings"]["reply_prefix"]
        self.reply_with_mention = ctx.astrbot_config["platform_settings"][
            "reply_with_mention"
        ]
        self.reply_with_quote = ctx.astrbot_config["platform_settings"][
            "reply_with_quote"
        ]
        self.t2i_word_threshold = ctx.astrbot_config["t2i_word_threshold"]
        try:
            self.t2i_word_threshold = int(self.t2i_word_threshold)
            self.t2i_word_threshold = max(self.t2i_word_threshold, 50)
        except BaseException:
            self.t2i_word_threshold = 150
        self.t2i_strategy = ctx.astrbot_config["t2i_strategy"]
        self.t2i_use_network = self.t2i_strategy == "remote"
        self.t2i_active_template = ctx.astrbot_config["t2i_active_template"]

        self.forward_threshold = ctx.astrbot_config["platform_settings"][
            "forward_threshold"
        ]

        # 分段回复配置
        self.words_count_threshold = int(
            ctx.astrbot_config["platform_settings"]["segmented_reply"][
                "words_count_threshold"
            ],
        )
        self.enable_segmented_reply = ctx.astrbot_config["platform_settings"][
            "segmented_reply"
        ]["enable"]
        self.only_llm_result = ctx.astrbot_config["platform_settings"][
            "segmented_reply"
        ]["only_llm_result"]
        self.split_mode = ctx.astrbot_config["platform_settings"][
            "segmented_reply"
        ].get("split_mode", "regex")
        self.regex = ctx.astrbot_config["platform_settings"]["segmented_reply"]["regex"]
        self.split_words = ctx.astrbot_config["platform_settings"][
            "segmented_reply"
        ].get("split_words", ["。", "？", "！", "~", "…"])
        if self.split_words:
            escaped_words = sorted(
                [re.escape(word) for word in self.split_words], key=len, reverse=True
            )
            self.split_words_pattern = re.compile(
                f"(.*?({'|'.join(escaped_words)})|.+$)", re.DOTALL
            )
        else:
            self.split_words_pattern = None
        self.content_cleanup_rule = ctx.astrbot_config["platform_settings"][
            "segmented_reply"
        ]["content_cleanup_rule"]

        # 内容安全检查
        self.content_safe_check_reply = ctx.astrbot_config["content_safety"][
            "also_use_in_response"
        ]
        self.content_safe_check_stage = None
        if self.content_safe_check_reply:
            for stage_cls in registered_stages:
                if stage_cls.__name__ == "ContentSafetyCheckStage":
                    self.content_safe_check_stage = stage_cls()
                    await self.content_safe_check_stage.initialize(ctx)

        provider_cfg = ctx.astrbot_config.get("provider_settings", {})
        self.show_reasoning = provider_cfg.get("display_reasoning_text", False)

    def _split_text_by_words(self, text: str) -> list[str]:
        """使用分段词列表分段文本"""
        if not self.split_words_pattern:
            return [text]
        segments = self.split_words_pattern.findall(text)
        result = []
        for seg in segments:
            if isinstance(seg, tuple):
                content = seg[0]
                if not isinstance(content, str): continue
                for word in self.split_words:
                    if content.endswith(word):
                        content = content[: -len(word)]
                        break
                if content.strip(): result.append(content)
            elif seg and seg.strip():
                result.append(seg)
        return result if result else [text]

    async def process(
        self,
        event: AstrMessageEvent,
    ) -> None | AsyncGenerator[None, None]:
        result = event.get_result()
        if result is None or not result.chain:
            return

        if result.result_content_type == ResultContentType.STREAMING_RESULT:
            return

        is_stream = result.result_content_type == ResultContentType.STREAMING_FINISH

        # 回复时检查内容安全
        if (
            self.content_safe_check_reply
            and self.content_safe_check_stage
            and result.is_llm_result()
            and not is_stream
        ):
            text = "".join([comp.text for comp in result.chain if isinstance(comp, Plain)])
            if isinstance(self.content_safe_check_stage, ContentSafetyCheckStage):
                async for _ in self.content_safe_check_stage.process(event, check_text=text):
                    yield

        # 发送消息前事件钩子 (Hook)
        handlers = star_handlers_registry.get_handlers_by_event_type(
            EventType.OnDecoratingResultEvent,
            plugins_name=event.plugins_name,
        )
        for handler in handlers:
            try:
                await handler.handler(event)
            except Exception:
                logger.error(traceback.format_exc())
            if event.is_stopped(): return

        if is_stream: return

        result = event.get_result()
        if result is None: return

        if len(result.chain) > 0:
            # 1. 回复前缀
            if self.reply_prefix:
                for comp in result.chain:
                    if isinstance(comp, Plain):
                        comp.text = self.reply_prefix + comp.text
                        break

            # 2. 分段回复
            if self.enable_segmented_reply and event.get_platform_name() not in ["qq_official", "weixin_official_account", "dingtalk"]:
                if (self.only_llm_result and result.is_llm_result()) or not self.only_llm_result:
                    new_chain = []
                    for comp in result.chain:
                        if isinstance(comp, Plain):
                            if len(comp.text) > self.words_count_threshold:
                                new_chain.append(comp)
                                continue
                            if self.split_mode == "words":
                                split_response = self._split_text_by_words(comp.text)
                            else:
                                split_response = re.findall(self.regex or r".*?[。？！~…]+|.+$", comp.text, re.DOTALL | re.MULTILINE)
                            
                            for seg in split_response:
                                if self.content_cleanup_rule:
                                    seg = re.sub(self.content_cleanup_rule, "", seg)
                                if seg.strip(): new_chain.append(Plain(seg))
                        else:
                            new_chain.append(comp)
                    result.chain = new_chain

            # 3. TTS 逻辑 (修改为标签触发)
            tts_provider = self.ctx.plugin_manager.context.get_using_tts_provider(event.unified_msg_origin)
            
            # 优先处理推理内容的显示 (如果没开启 TTS 且有推理内容)
            if self.show_reasoning and event.get_extra("_llm_reasoning_content"):
                reasoning_content = event.get_extra("_llm_reasoning_content")
                result.chain.insert(0, Plain(f"🤔 思考: {reasoning_content}\n"))

            if (
                bool(self.ctx.astrbot_config["provider_tts_settings"]["enable"])
                and result.is_llm_result()
                and SessionServiceManager.should_process_tts_request(event)
                and tts_provider
            ):
                new_chain = []
                for comp in result.chain:
                    if isinstance(comp, Plain):
                        # 使用正则查找 <tts> 标签内容，支持一段文本中包含多个标签
                        # 逻辑：如果这段文本包含标签，则解析；如果不包含，原样保留
                        if "<tts>" in comp.text and "</tts>" in comp.text:
                            # 简单的提取逻辑，你也可以改为支持多个标签的循环解析
                            # 这里沿用你提供的逻辑：假设 Plain 段落是被标签包裹的
                            raw_text = comp.text.strip()
                            if raw_text.startswith("<tts>") and raw_text.endswith("</tts>"):
                                text_to_convert = raw_text[5:-6].strip()
                                if not text_to_convert: continue
                                try:
                                    audio_path = await tts_provider.get_audio(text_to_convert)
                                    if audio_path:
                                        use_file_service = self.ctx.astrbot_config["provider_tts_settings"]["use_file_service"]
                                        callback_api_base = self.ctx.astrbot_config["callback_api_base"]
                                        url = None
                                        if use_file_service and callback_api_base:
                                            token = await file_token_service.register_file(audio_path)
                                            url = f"{callback_api_base}/api/file/{token}"
                                        
                                        new_chain.append(Record(file=url or audio_path, url=url or audio_path))
                                        if self.ctx.astrbot_config["provider_tts_settings"]["dual_output"]:
                                            new_chain.append(Plain(text_to_convert))
                                    else:
                                        new_chain.append(Plain(text_to_convert))
                                except Exception:
                                    logger.error(traceback.format_exc())
                                    new_chain.append(Plain(text_to_convert))
                            else:
                                # 包含标签但不是完全包裹，可能需要更复杂的正则拆分，这里暂时保留
                                new_chain.append(comp)
                        else:
                            new_chain.append(comp)
                    else:
                        new_chain.append(comp)
                result.chain = new_chain

            # 4. 文本转图片 (T2I)
            elif (result.use_t2i_ is None and self.ctx.astrbot_config["t2i"]) or result.use_t2i_:
                parts = ["\n\n" + comp.text for comp in result.chain if isinstance(comp, Plain)]
                plain_str = "".join(parts)
                if plain_str and len(plain_str) > self.t2i_word_threshold:
                    try:
                        url = await html_renderer.render_t2i(plain_str, return_url=True, use_network=self.t2i_use_network, template_name=self.t2i_active_template)
                        if url:
                            if url.startswith("http"): result.chain = [Image.fromURL(url)]
                            elif self.ctx.astrbot_config["t2i_use_file_service"] and self.ctx.astrbot_config["callback_api_base"]:
                                token = await file_token_service.register_file(url)
                                url = f"{self.ctx.astrbot_config['callback_api_base']}/api/file/{token}"
                                result.chain = [Image.fromURL(url)]
                            else: result.chain = [Image.fromFileSystem(url)]
                    except Exception:
                        logger.error("文本转图片失败。")

            # 5. 转发消息 (合并转发)
            if event.get_platform_name() == "aiocqhttp":
                word_cnt = sum([len(comp.text) for comp in result.chain if isinstance(comp, Plain)])
                if word_cnt > self.forward_threshold:
                    result.chain = [Node(uin=event.get_self_id(), name="AstrBot", content=[*result.chain])]

            # 6. At & 引用回复
            has_plain = any(isinstance(item, Plain) for item in result.chain)
            if has_plain:
                if self.reply_with_mention and event.get_message_type() != MessageType.FRIEND_MESSAGE:
                    result.chain.insert(0, At(qq=event.get_sender_id(), name=event.get_sender_name()))
                    if len(result.chain) > 1 and isinstance(result.chain[1], Plain):
                        result.chain[1].text = "\n" + result.chain[1].text
                if self.reply_with_quote and not any(isinstance(item, File) for item in result.chain):
                    result.chain.insert(0, Reply(id=event.message_obj.message_id))