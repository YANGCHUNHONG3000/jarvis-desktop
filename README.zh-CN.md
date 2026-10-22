# Jarvis 桌面 Agent

[English](README.md) · [快速开始](docs/QUICKSTART.md) · [贡献指南](CONTRIBUTING.md)

Jarvis 桌面 Agent 是面向 Windows 的桌面自动化与研究服务。本公开版本包含独立的 `agent_service` 和十个 `run_jarvis_*.py` 启动器，不依赖语音运行时或 OpenClaw。

## 能力范围

- 在受支持的网站上搜索、查看和总结网页，并执行有界的浏览器输入、点击和读取。登录与 CAPTCHA 由用户手动完成。
- 通过 Windows COM 自动化创建、编辑、核验并交接 WPS Writer 和 WPS Spreadsheets 原生文档。
- 为受支持的工作流保留持久回执。如果外部操作可能已经发生但无法核实，结果会标为 `unknown`，不会自动重放该操作。
- 用引文和发布方证据比对说法。这是证据核对，不保证说法为真。

v4 后续任务后端已经存在，但入口尚未接入主 GUI。本项目不是通用自主电脑 Agent；能力限于已实现的工作流和受支持的网站/应用。

## 要求与安装

Windows 10/11、Python 3.12、Microsoft Edge，以及安装了 Writer 和 Spreadsheets 的 WPS Office。安装和启动示例见[快速开始](docs/QUICKSTART.md)。

尚未验证全新电脑上的安装。不同 Office 安装之间的 Word/Excel COM 兼容性尚未验收；本版本说明的是 WPS。桌面发行范围不包含语音、本地或云端模型权重、用户数据、凭证或私人历史。

## 云端模型与费用控制

除非所有者创建本地授权文件，否则云端模型调用默认禁用。启用时，授权文件必须包含 `authorized: true`、`daily_limit_cny: "5.00"` 和 `timezone: "America/New_York"`。这表示允许将所选请求内容发送给配置的模型提供方；启用前请检查任务可能包含的内容。API 密钥应只放在被忽略的本地文件 `DEEPSEEK_API_KEY.txt`，绝不可提交。

预算账本同时执行纽约自然日和滚动24小时两个人民币5元上限。未结算的预留金额继续占用两个窗口。上限是消费封顶，不代表免费，也不表示当前可用余额。

## 许可与状态

原项目采用 MIT License。Copyright (c) 2026 YANGCHUNHONG3000 and contributors。依赖信息见[第三方声明](THIRD_PARTY.md)。这是早期公开版本，不宣称已有外部用户采用，也不宣称已验证全新环境安装。
