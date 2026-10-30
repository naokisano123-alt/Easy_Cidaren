# 大模型兜底答题（第一版）

默认关闭。原有词库和规则未找到答案时，才请求兼容 Chat Completions 协议的模型服务。此适配器不依赖某个厂商 SDK；服务需接受 `POST {base_url}/chat/completions` 的 `model/messages/max_tokens/stream`，并返回 `choices[0].message.content` 与 `finish_reason: stop`。具体服务是否兼容需实测。

## 配置

1. 安装项目原有依赖；此功能使用已有的 Requests，无额外依赖。
2. 复制 `config/llm.example.json` 为 `config/llm.local.json`。
3. 设置 `enabled` 为 `true`，填写服务商的 `base_url` 和 `model`。base_url 填 API 根地址（通常包含 `/v1`），不要包含 `/chat/completions`；必须使用 HTTPS。
4. 在启动程序的同一终端中设置环境变量。Windows PowerShell 示例：

```powershell
$env:CIDAREN_LLM_API_KEY = "你的密钥"
python main.py
```

macOS/Linux：

```bash
export CIDAREN_LLM_API_KEY='你的密钥'
python main.py
```

配置文件只记录密钥环境变量的名称 `api_key_env`，不记录密钥。`llm.local.json` 和 `.env` 文件已被 Git 忽略。不要将真实密钥写入示例、Prompt 或提交到仓库。请在本机设置密钥，无需在聊天中发送。

## 行为和范围

- 顺序为：现有答案库/规则 → 大模型 → 跳过。原先几处随机猜选项、猜第三项、提交最后遍历词的行为改为未匹配，关闭模型时也会跳过这些题。
- 文本选择题支持模式 15、16、17、18、41–44；填空支持 32、51–54、73。只有明确空位约束的题才发给模型。听力模式 11、13、21、22 不发给文本模型；原有规则仍可处理有对应规则的题。模式 31 仍用原逻辑。
- Prompt 位于 `config/llm_prompt.txt`，可编辑，无需训练。每题携带固定 Prompt；不携带历史对话或完整词库。只附最多 20 条与中文题干/释义直接匹配的课程词条。
- 程序仅发送白名单题目字段，不发送词达人 Token、topic_code、任务 ID、账号信息或音频地址。题目文本会发送到配置的模型服务，请选用你信任的服务。
- JSON 输出必须通过选项 ID、空位数量、前缀、词长与词库约束检查。选择题会映射回原提交标记，拼写和短语会转换成原代码要求的格式。格式有效不代表语义一定正确。
- 每次最多一次模型请求，不自动重试；连接超时 5 秒，读取超时默认 15 秒（配置范围 1–60 秒）。读取超时是网络读取等待限制，不是整题总耗时硬上限。拒绝重定向；HTTP 错误、无效/截断输出或模型返回 unknown 时跳过。
- 内存缓存最多保存 256 个通过格式校验的结果，键包含模型、地址、Prompt 和题目。退出后清空；模型答案不直接写入标准答案库。
- 本版本通过 JSON 文件配置，尚未增加 GUI 设置或真实 API 连通性测试。

## 本地验证

```bash
python -m unittest discover -s tests -v
```

测试用模拟 API，不消耗额度，不连接词达人。正式验证需在本机配置模型服务，并确认其返回格式符合上述协议。
