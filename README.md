# 我会牢牢记住你

一个面向拟人陪伴场景、并可作为“我会永远陪着你”体系记忆中枢的结构化长期记忆插件。

它的目标不是把所有聊天历史塞回模型，也不是把流水账当作长期记忆，而是让 Bot 清楚地区分：

- 自己做过什么
- 当前正在和谁说话
- 哪些内容只是短期对话事件，哪些内容已经被阶段性总结为长期记忆
- 哪些内容属于私聊、群聊、公开片段或自我时间线
- 哪些记忆是确定事实、人格生活、创作内容、导入摘要或待审核猜测

## 第一版能力

- SQLite 结构化记忆库
- 短期时间线按条数/时间窗口调用模型总结为长期记忆
- 阶段性总结模型可独立选择主/备用 Provider，也可按需填写 model 覆盖
- 短期上下文压缩模型可独立选择主/备用 Provider，失败时退回轻量压缩
- LivingMemory 风格阶段性总结：`summary/topics/key_facts/participants/sentiment/importance`
- 双通道摘要：`canonical_summary` 用于检索，`persona_summary` 保留人格口吻
- 总结失败重试：失败窗口会记录 retry，连续失败到上限后跳过，避免阻塞后续整理
- 私聊/群聊/自我时间线隔离
- 关系身份基础表
- 主链 LLM 前的临时记忆注入
- 用户消息和 Bot 回复默认只进入短期时间线，不直接当作长期记忆
- 稳定事实抽取：偏好、称呼、生日、明确记住事项
- 关系边记录：群成员归属、身份声明待审
- 注入日志：记录选中与过滤原因，便于排障
- 去重合并：重复记忆强化已有条目而不是无限堆叠
- 管理命令：状态、搜索、最近、手动添加、审核、删除
- LivingMemory 数据库预览与保守导入
- 供陪伴体系其他插件使用的 `remember_you` 桥接对象
- LLM 主动工具：主动回忆、主动记忆、创建/读取 Bot 自己可见的陪伴笔记
- 分槽上下文调度：当前消息、短期上下文和长期记忆统一编排，默认只用当前用户消息检索
- 特色检索架构：`current_message` 稳定模式、`guarded_companion` 受保护陪伴线索、`companion_augmented` 强联动增强检索
- 陪伴插件线索识别：可读取 `topic/entities/facts/keywords/intent` 类结构化提示，但默认不让旧线索污染新请求
- 睡眠维护：手动触发维护、去重修复和最近维护状态记录

## 模块边界

```text
main.py                 AstrBot 入口，只保留注册、钩子和命令转发
core/service.py         记忆主链路：捕获、注入、桥接写入
core/commands.py        /rmem 管理命令的文本和流程
core/store.py           SQLite 表结构和读写
core/models.py          结构化记忆、实体、会话上下文
core/identity.py        从 AstrBot 事件中解析用户、群、会话边界
core/visibility.py      私聊/群聊/自我时间线的可见性策略
core/retrieval.py       当前会话可见记忆的检索和排序
core/context_orchestrator.py  检索意图提取与上下文调度输入
core/injection.py       注入文本格式，不参与存储
core/bridge.py          对其他插件开放的低耦合 API
core/astrbot_compat.py  AstrBot TextPart/logger 的薄兼容层
core/migration_livingmemory.py  LivingMemory 预览与保守导入
```

## 主链上下文策略

每轮 LLM 请求前，插件会先整理一个临时上下文包：

```text
当前用户消息
→ 检索架构：默认 current_message；可选 guarded_companion / companion_augmented
→ 检索意图：当前消息，或在受保护模式下有限使用 topic、entities、facts、keywords、intent
→ 分槽召回：Bot 自我时间线、用户画像、当前窗口、阶段总结、稳定记忆
→ 权限、审核、ACL 和可见性过滤
→ 短期上下文 + 分槽长期记忆
→ TextPart.mark_as_temp 临时注入
```

上下文包不会写回 AstrBot 历史；默认保持 AstrBot 原生短期上下文，RememberYou 只追加少量可见长期记忆。需要替代原生历史或强联动陪伴线索时，再显式开启对应配置。

## 常用命令

```text
/rmem status
/rmem search 关键词
/rmem explain 关键词
/rmem recent 10
/rmem add 这是一条手动记忆
/rmem summarize
/rmem review list
/rmem review approve <memory_id>
/rmem review reject <memory_id>
/rmem visibility <memory_id> private_pair|group_public|bot_self|shareable|internal
/rmem promote <memory_id>
/rmem archive <memory_id>
/rmem timeline 10
/rmem relations 20
/rmem threads list
/rmem logs 5
/rmem maintenance
/rmem sleep status
/rmem sleep run
/rmem delete <memory_id>
/rmem import_livingmemory preview
/rmem import_livingmemory run
```

## 迁移原则

LivingMemory 只作为可选迁移来源，不是本插件的核心依赖。导入内容默认写入为：

- `reality_level = imported_summary`
- `review_status = pending`
- `confidence <= 0.5`

这样可以保留旧数据，又不会把旧摘要直接当成确定事实注入。
