# 个人AI助手记忆与知识管理

面向长期AI协作的本地资料管理工具，支持对话增量导入、来源版本检查和主题状态维护。

![运行示例](docs/preview.png)

## 运行

Python 3.10+，仅使用标准库。

```sh
python demo.py
python -m unittest discover -s tests -v
python -m http.server 8000 --directory docs
```

打开 http://localhost:8000 或直接打开 `docs/index.html`。演示会在`runtime/`创建虚构工作区，运行首次扫描、人工核读示例、资料更新及旧签收拒绝。真实笔记、账号配置和历史对话均不在仓库内。

## 运行流程

1. `prompt_intake.py`逐行增量读取显式指定的JSONL导出目录；内容散列、文件偏移和SQLite索引支持重复导入与追加。
2. `continuity.py scan`比较来源版本和已读记录，列出新增或修改过、需要重新阅读的资料。
3. 维护者阅读来源、更新主题状态，提交包含实际版本SHA和输出文件的核读计划。
4. `continuity.py seal`校验版本与输出路径，签收已读版本；来源更新后重新进入队列。
5. `knowledge_gate.py`检查受控标签、Markdown本地链接和知识图谱引用。

```sh
python tools/prompt_intake.py --roots examples/sessions --output runtime/memory/prompt-intake
python tools/continuity.py scan --root runtime --brief
python tools/knowledge_gate.py runtime
```

## 开发过程

我负责需求、资料分层、状态更新规则和验收，使用AI辅助实现Python代码。公开仓库提供导入、版本检查和记录校验工具，示例使用虚构资料。

## 文件分层

`notes/`是原文层；`memory/prompt-intake/`是派生消息索引；`memory/continuity/`维护版本队列与核读记录；`state/`保存可恢复主题状态；`memory/classification/`维护标签；可选的`knowledge-graph.json`连接实体与来源。

## 使用范围

输入是本地导出的JSONL文件，仓库提供导入、版本检查和记录校验。阅读资料、更新摘要由维护者完成；模型调用、向量检索和聊天账号连接需另行接入。导入器会过滤部分自动提示和常见凭据格式，使用自己的材料时需核对导入结果。

详见[架构与数据流](ARCHITECTURE.md)。公开演示由真实Python运行结果生成；GitHub Pages选择GitHub Actions后可运行Demo工作流。
