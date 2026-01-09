from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)
from astrbot.core.agent.message import AssistantMessageSegment, TextPart, UserMessageSegment
import random
import asyncio
import time
import json

@register(
    "astrbot_plugin_llm_poke",
    "和泉智宏",
    "调用LLM的戳一戳回复插件",
    "1.4", 
    "https://github.com/0d00-Ciallo-0721/astrbot_plugin_llm_poke",
)
class LLMPokePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        
        # 用户戳一戳时间戳记录
        self.user_poke_timestamps = {}
        
        # 从配置文件加载配置
        self.trigger_probability = config.get("trigger_probability", 1.0)  
        self.enabled_groups = config.get("enabled_groups", [])
        self.poke_interval = config.get("poke_interval", 1.0)

        # --- v1.4 Update: 加载新配置 ---
        self.enable_in_groups = config.get("enable_in_groups", True)
        self.enable_in_private = config.get("enable_in_private", True)
        # 确保黑名单id转为字符串，方便比对
        self.blacklisted_users = [str(uid) for uid in config.get("blacklisted_users", [])]
        # -----------------------------
        
        # 概率配置
        self.normal_reply_probability = config.get("normal_reply_probability", 0.3)
        self.llm_reply_probability = 1 - self.normal_reply_probability  # 确保两者和为1
        
        self.poke_back_probability = config.get("poke_back_probability", 0.1)
        self.super_poke_probability = config.get("super_poke_probability", 0.01)
        self.no_action_probability = 1 - self.poke_back_probability - self.super_poke_probability
        
        # 反戳次数配置
        self.poke_back_times = config.get("poke_back_times", 1)
        self.super_poke_times = config.get("super_poke_times", 5)
        self.poke_history = config.get("poke_history", "[戳了戳你]")
        # 预设回复
        self.normal_replies = config.get("normal_replies", [
            "没有察觉到你的戳戳呢~",
            "哎呀，我刚刚走神了，没感觉到~",
            "嗯？有人戳我吗？可能是错觉...",
            "刚才没注意到呢，下次戳重一点~"
        ])
        
        # 提示词配置
        self.poke_prompts = {
            "1": config.get("poke_prompt_1", "有人戳了戳你，请你回复一句俏皮的话。"),
        }
        
        self.poke_back_prompts = {
            "A": config.get("poke_back_prompt_A", "你决定戳回对方，请说一句调皮的话。"),
        }
        
        logger.info("LLM戳一戳插件(v1.4)已初始化完成！")

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_poke(self, event: AstrMessageEvent):
        """监听并响应戳一戳事件"""
        # 仅处理aiocqhttp平台的事件
        if event.get_platform_name() != "aiocqhttp":
            return
            
        raw_message = getattr(event.message_obj, "raw_message", None)
        
        # 检查是否为戳一戳事件
        if (
            not raw_message or
            raw_message.get('post_type') != 'notice' or
            raw_message.get('notice_type') != 'notify' or
            raw_message.get('sub_type') != 'poke'
        ):
            return
            
        # 获取事件相关信息
        bot_id = raw_message.get('self_id')
        sender_id = raw_message.get('user_id')
        target_id = raw_message.get('target_id')
        group_id = raw_message.get('group_id')

        # --- v1.4 Update: 黑名单检查 ---
        if str(sender_id) in self.blacklisted_users:
            logger.info(f"用户 {sender_id} 在黑名单中，忽略戳一戳。")
            return

        # --- v1.4 Update: 作用域开关检查 ---
        if group_id:
            # 是群聊消息
            if not self.enable_in_groups:
                return
            
            # 原有的群白名单逻辑
            if self.enabled_groups and str(group_id) not in [str(g) for g in self.enabled_groups]:
                return
        else:
            # 是私聊消息
            if not self.enable_in_private:
                return
            
        # 检查是否是用户戳机器人
        if not bot_id or not sender_id or not target_id or str(target_id) != str(bot_id):
            return

        # 根据总概率决定是否响应
        if random.random() > self.trigger_probability:
            logger.info(f"戳一戳事件未达到触发概率({self.trigger_probability})，本次不响应。")
            return
            
        # 记录戳一戳时间戳并清理过期记录
        now = time.time()
        if sender_id not in self.user_poke_timestamps:
            self.user_poke_timestamps[sender_id] = []
        self.user_poke_timestamps[sender_id].append(now)
        three_minutes_ago = now - 3 * 60
        self.user_poke_timestamps[sender_id] = [
            t for t in self.user_poke_timestamps[sender_id] if t > three_minutes_ago
        ]

        # --- 核心逻辑修复：先判定动作类型，再生成回复 ---
        action_rand = random.random()
        is_super_poke = action_rand < self.super_poke_probability
        is_normal_poke_back = not is_super_poke and action_rand < (self.super_poke_probability + self.poke_back_probability)
        
        # 确定本次使用的提示词模板和物理反戳次数
        if is_super_poke or is_normal_poke_back:
            # 准备反戳：使用反戳提示词
            poke_prompt_key = random.choice(list(self.poke_back_prompts.keys()))
            current_prompt = self.poke_back_prompts[poke_prompt_key]
            do_physical_poke = True
            poke_times = self.super_poke_times if is_super_poke else self.poke_back_times
        else:
            # 普通被戳回复：使用被戳提示词
            poke_prompt_key = random.choice(list(self.poke_prompts.keys()))
            current_prompt = self.poke_prompts[poke_prompt_key]
            do_physical_poke = False

        # 生成回复内容（区分预设文本和LLM）
        response = None
        is_llm_reply = False
        if random.random() < self.normal_reply_probability:
            response = random.choice(self.normal_replies)
        else:
            response = await self.get_llm_respond(event, current_prompt)
            is_llm_reply = True
            
        if not response:
            return

        # 发送回复
        yield event.plain_result(response)

        # 如果是 LLM 生成的回复，统一存入上下文
        if is_llm_reply:
            try:
                user_msg = UserMessageSegment(content=[TextPart(text=self.poke_history)])
                assistant_msg = AssistantMessageSegment(content=[TextPart(text=response)])
                umo = event.unified_msg_origin
                curr_cid = await self.context.conversation_manager.get_curr_conversation_id(umo)
                if curr_cid:
                    await self.context.conversation_manager.add_message_pair(
                        cid=curr_cid,
                        user_message=user_msg,
                        assistant_message=assistant_msg
                    )
            except Exception as e:
                logger.error(f"保存戳一戳上下文失败: {e}")

        # 如果命中反戳概率，执行物理反戳动作
        if do_physical_poke:
            await self.do_poke_back(event, sender_id, group_id, poke_times)
            
        # 阻止默认的LLM请求
        event.should_call_llm(False)
        
    async def get_llm_respond(self, event: AstrMessageEvent, prompt_template: str) -> str:
        """调用LLM生成回复"""
        try:
            # 获取当前会话ID
            umo = event.unified_msg_origin
            curr_cid = await self.context.conversation_manager.get_curr_conversation_id(umo)
            conversation = None
            contexts = []
            
            # 获取当前会话对象和上下文
            if curr_cid:
                conversation = await self.context.conversation_manager.get_conversation(umo, curr_cid)
                if conversation:
                    contexts = json.loads(conversation.history)
            
            # 获取当前人格设置
            provider = self.context.get_using_provider()
            if not provider:
                return random.choice(self.normal_replies)
            
            # 动态获取人格提示词
            personality_prompt = ""
            
            # 从会话中获取人格ID
            if conversation and hasattr(conversation, 'persona_id'):
                persona_id = conversation.persona_id
                
                # 获取所有已加载的人格
                all_personas = self.context.provider_manager.personas
                
                # 如果用户明确取消了人格
                if persona_id == "[%None]":
                    personality_prompt = ""  # 用户明确取消了人格，使用空提示
                # 如果用户设置了特定人格
                elif persona_id:
                    # 在所有人格中查找匹配的人格
                    for persona in all_personas:
                        if persona.get("name") == persona_id:
                            personality_prompt = persona.get("prompt", "")
                            break
                # 如果没有设置人格（新会话），使用默认人格
                else:
                    # 获取默认人格名称
                    default_persona_name = self.context.provider_manager.selected_default_persona.get("name")
                    if default_persona_name:
                        # 在所有人格中查找默认人格
                        for persona in all_personas:
                            if persona.get("name") == default_persona_name:
                                personality_prompt = persona.get("prompt", "")
                                break
            
            # 如果上面的逻辑没有找到人格提示词，使用提供商的当前人格作为备选
            if not personality_prompt and hasattr(provider, 'curr_personality') and provider.curr_personality:
                personality_prompt = provider.curr_personality.get("prompt", "")

            combined_prompt = f"{self.poke_history} {prompt_template}"
            # 格式化提示词，加入用户名
            format_prompt = combined_prompt.format(username=event.get_sender_name())

            # 调用LLM
            llm_response = await provider.text_chat(
                prompt=format_prompt,
                system_prompt=personality_prompt,
                contexts=contexts,
            )
            
            return llm_response.completion_text
            
        except Exception as e:
            logger.error(f"LLM调用失败: {e}")
            return None

            
    async def do_poke_back(self, event: AiocqhttpMessageEvent, user_id: int, group_id: int, times: int):
        """执行反戳操作"""
        try:
            client = event.bot
            payloads = {"user_id": user_id}
            if group_id:
                payloads["group_id"] = group_id
                
            for _ in range(times):
                try:
                    await client.api.call_action('send_poke', **payloads)
                    await asyncio.sleep(self.poke_interval)
                except Exception as e:
                    logger.error(f"反戳失败: {e}")
                    break
                    
        except Exception as e:
            logger.error(f"反戳操作失败: {e}")
