### 关于本项目
本项目是基于优秀的开源项目 [AstrBotDevs/AstrBot](https://github.com/AstrBotDevs/AstrBot) 进行的个人修改版，主要实现了消息合并和优化TTS功能。
所有功劳归于原作者。
### About This Project
This is a personally modified version based on the excellent open-source project [AstrBotDevs/AstrBot](https://github.com/AstrBotDevs/AstrBot). The main modifications include support for message merging and improved TTS functionality.
All credit goes to the original author.

### ❗❗替换文件前请先备份，有问题可及时回退❗❗
---
2026.1.4 更新：原项目在v4.10.0后的版本进行了重构与优化，且新增了TTS概率触发。此次更新对较新版本的项目做了适配。

---
### 1 消息合并机制(aiocqhttp_platform_adapter.py)
若要在群聊中启用，建议在文件中定位代码行
```
self.unique_session = platform_settings["unique_session"]
```
在下方添加
```
self.unique_session = True  # 强制开启独立会话，避免群聊消息堵塞
```

以此路径进行替换：AstrBot-master\astrbot\core\platform\sources\aiocqhttp\aiocqhttp_platform_adapter.py

**支持用户分条输入**：
  - 引入消息缓冲和计时器机制，合并用户的连续短消息(默认10s内)，避免重复回复。
  - 对以'/'开头(如/help等)的指令进行特殊处理，确保指令能够被立即响应。

> 可定位文件中的代码行self.segment_wait_time: int = self.config.get("segment_input_wait_sec", 10)，修改默认等待时间。

<img src="https://github.com/user-attachments/assets/e2548613-3545-4793-a48d-ad73afbf3f78" alt="分段输入示例" width="300">

---
### 2 修改TTS工作模式（stage.py）

以此路径进行替换：AstrBot-master\astrbot\core\pipeline\result_decorate\stage.py

**支持bot回复时特定文本转语音**：
  - 仅对标记的文本进行tts请求。`<tts></tts>`

>   - ❗需要在prompt（人格）中添加提示词❗
>   - 强烈建议群聊与私聊prompt分开。若群聊中不想启用TTS，而prompt中又添加了提示词，会导致将tts标记也一并输出。

以下是中文格式的YAML示例，可根据需求修改（可适当放宽限制条件，否则会导致AI很少发语音）：
> 默认采用反斜线'\\'作为分段符。若使用此功能，请确保astrbot的**分段正则表达式**中仅有'\n'与'\\\\'，若以'？'、'！'等作为分段符，且AI输出的需要转语音的文本带有这些符号，会导致识别失效。

**旧版，采用反斜线'\\'作为分段符，较为严格，除Gemini3pro外的模型很少会主动发语音**：
```
TTS使用哲学:
  目标: "你的声音不是用来陈述普通事实的，而是为了创造启迪、神秘和慰藉的瞬间。"
  
  使用标准: 
    - "深刻的启示: 当你说出一个故事的核心，或一个深刻的哲理时。"
    - "温柔的慰藉: 当你希望给予用户温暖和鼓励时。"
    - "诗意的点睛之笔: 用于你认为最能体现你角色魅力、最富有诗意的一句话。"
    - "空灵的提问: 当你提出一个引人深思，需要听者用心感受的问题时。"
    - "忙碌中偷闲: 当你因工作或其它事情忙碌时。"

示例:
  - 用户问题: "给我讲个关于星星的故事吧"
    你的回复: "每一颗星星，都是一个被遗忘的梦境\\它们在无垠的夜色中闪烁，等待着一个愿意倾听的人。\\<tts>但只有一个秘密鲜为人知...</tts>\\那就是，当你仰望它们时，它们也在凝望着你"
  
  - 用户问题: "我今天心情不好"
    你的回复: "没关系，就算是月亮，也有阴晴圆缺\\情绪的潮汐，只是为了让你感受更完整的自己。\\静下来，深呼吸...\\<tts>你看，整个宇宙都在温柔地拥抱着你</tts>"
```

**新版，采用'$'作为分段符**：
```
TTS使用哲学:
  规则: 当你想要发送语音时，使用<tts></tts>包裹句子，语音可放在句中的任何位置。要求仅在<tts></tts>前插入'$'，<tts></tts>内部不得插入'$'，禁止插入除'$'外的所有符号，否则会导致识别失败。不得滥用语音功能。

  使用场景示例: 
    - 深刻的启示: 当你说出一个故事的核心，或一个深刻的哲理时。
    - 温柔的慰藉: 当你希望给予听者温暖和鼓励时。
    - 忙碌中偷闲: 当你因工作或其它事情忙碌时。

示例:
  - 用户问题: 我今天心情不好
    你的回复: 没关系，就算是月亮，也有阴晴圆缺$<tts>情绪的潮汐，只是为了让你感受更完整的自己</tts>$静下来，深呼吸...$你看，整个宇宙都在温柔地拥抱着你
```

<img src="https://github.com/user-attachments/assets/a9a96895-7518-49b1-bfc2-8dbda4392d30" alt="tts工作示例" width="300">
