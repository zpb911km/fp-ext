---
name: webui_filehub_protocol
description: WebUI 文件集散通道约定：fp_inbox/fp_outbox 收发协议与相对引用格式
type: reference
created: 2026-11-03 19:22
updated: 2026-11-03 19:22
context: 用户经 fp-webui 远程与我交互时收发文件的唯一正规通道；跨平台（/tmp、Windows %TEMP%、Termux）
hint: 引用格式 [file:inbox/name]
---

fp-webui 文件集散通道（file hub，2025 定稿实现）：
- 位置：<tempdir>/fp_inbox 与 <tempdir>/fp_outbox（tempfile.gettempdir() 解析；POSIX=/tmp，Windows=%TEMP%，Termux=.../usr/tmp）；可被环境变量 FP_FILEHUB_DIR 覆盖。
- 语义：多对多文件副本集散点。哑管道：撞名即覆盖、删了就删、无清理策略；智能（挪文件/决策）在端点侧，我负责把 inbox 文件复制/移动到实际工作位置，产物写入 outbox。
- 协议：消息中引用一律用相对格式 [file:inbox/<name>] / [file:outbox/<name>]，不写绝对路径（物理位置随平台漂移）。用户消息中的该引用=已上传到 inbox；我回复中写 [file:outbox/<name>]，前端自动渲染为下载链接。
- 给用户交产物：写文件到 outbox 后，回复中放 [file:outbox/文件名]。
- API：POST /api/files（multipart→inbox）、GET /api/files/download?box=&name=（realpath 防穿越）。实现见 packages/fp-webui（main.py 端点、static/ 前端），文档见 packages/fp-webui/README.md「文件集散通道」。
