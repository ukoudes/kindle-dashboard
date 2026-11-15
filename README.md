# Kindle 看板

将本地 AI 用量显示在电脑和 Kindle 上。macOS 应用管理服务与查看授权，Windows 可选运行采集器；Kindle 使用浏览器查看灰度看板。

## 功能

- Coding Plan：今日与近 30 天 Tokens、估算金额、Codex 剩余额度，以及最近三条主会话。
- API：仅统计明确标记为 API 的记录；未知费用和未接入余额不显示为零。
- 多设备：设备用量分别展示，账号额度不按设备重复相加。
- 访问管理：一次性配对码、授权浏览器列表、单独撤销和全部撤销。
- 自动恢复：网络中断时保留最近一次图片，恢复后继续刷新。

## macOS 安装

下载 Release 中的 `Kindle-Dashboard-v1.1.0-macOS-arm64.zip`，解压后将 `Kindle 看板.app` 拖入“应用程序”。此包面向 Apple Silicon，最低 macOS 13，不支持 Intel Mac。

安装版内置 Python 和 Pillow；账号额度及本机汇总依赖另外安装的 CodexBar。未安装或未登录时，相关数据保持未知。

当前分发包使用本地临时签名，**未经过 Apple Developer ID 签名与公证**。它适合预发布验证，不应描述为已公证的正式发行版。正式分发要求见 [GitHub 发布指引](GITHUB-RELEASE-GUIDE.md)。

1. 打开应用，点击“启动看板”。
2. 在“访问管理”中生成配对码。
3. 在电脑或 Kindle 浏览器打开应用显示的地址，填写设备名称与配对码。

配对码 5 分钟、一次有效，错误尝试 10 次后失效；最多授权 8 个浏览器，授权最长 90 天。关闭窗口或退出控制应用不会停止服务；停止请使用“停止服务”。Mac 休眠时无法持续提供看板。目前不设置开机自启。

## 安全边界

**当前使用 HTTP 明文传输，只适用于可信网络。配对限制访问权限，不提供传输加密。** 不要将服务直接暴露到公网。共享网络中的监听或中间人风险仍然存在。

页面、图片和统计接口均要求查看授权。采集上报使用独立凭据；采集器拒绝上游重定向。远程访问不能管理配对授权。访问管理中的“最近访问”不代表设备仍在线，来源 IP 也可能属于转发设备。

应用运行时才会生成本机配置、随机设备标识和授权。发行包不预置设备、账号、访问地址、数据库、日志、截图或凭据。应用不向项目开发者发送统计遥测；可选采集器仅向用户配置的汇总端上报，CodexBar 独立查询其所选账号。

更多说明见 [SECURITY.md](SECURITY.md)。

## 数据与统计口径

- macOS 安装版数据保存在 `~/Library/Application Support/Kindle 看板/data`，包含统计、配置、授权、采集队列和日志。源码运行默认使用项目内 `data/`。
- 金额为 USD 估算，不是订阅实付账单。订阅和 API 记录分别归类；费用覆盖不完整时显示未知。
- Mac 顶部金额和 Tokens 来自同一份 CodexBar 本机 Codex 汇总。Windows 当前展示已上报的部分记录，不并入 Mac 金额。
- Tokens 为输入加输出；缓存是输入的子集，不再次相加。重复累计快照与重复上报去重，压缩记录按响应标识补计。
- 默认按 UTC+8 统计。过期、跨日或时区不匹配的汇总不会伪装为当前读数。
- 最近对话只显示三条主会话，数值为已采集累计 Tokens。后台任务计入整体用量，但不占最近列表。
- 会话明细只读取普通 JSONL，覆盖可能小于 CodexBar 汇总。不保存或上传对话正文、提示词与登录凭据；会话显示标题属于统计元数据，也可能包含敏感内容。

## 从源码运行

需要 Python 3.10+；构建 macOS App 需要 Apple Silicon Mac、Xcode 命令行工具及 Python 3.12。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python kindle_dashboard.py serve
```

电脑入口为 `http://localhost:8000`。Kindle 使用启动时显示的地址，路径为 `/kindle`；网络变化后地址可能变化。其他设备必须能通过用户配置的网络到达汇总端。

构建独立 macOS App：

```bash
mac-app/build.command
```

构建不会复制个人运行数据。发布请使用 `scripts/prepare_release.py`，其源文件采用显式白名单，另见发布指引。

## Windows 可选采集

Windows 需要 Python 3.10+，采集模式不需要 Pillow。先在汇总端为其生成独立配置，再启动 Windows 采集器。**发行包不包含已经配对的配置。** 具体步骤见 [WINDOWS-QUICKSTART.md](WINDOWS-QUICKSTART.md)。

## 自定义 API 记录

将 JSONL 路径加入设备配置中的 `jsonl_paths`。每行一个 JSON 对象，必须明确声明 `billing_type: "api"` 才进入 API 统计。以下均为虚构示例：

```json
{"id":"example-request","timestamp":"2020-01-01T12:00:00+08:00","source":"Example API","model":"example-model","billing_type":"api","session_id":"example-session","title":"示例会话","input_tokens":100,"cached_input_tokens":20,"output_tokens":10,"cost_usd":0.01,"cost_kind":"reported"}
```

ID 在单个设备内必须唯一且稳定。未知费用使用 `null`；估算费用使用 `cost_kind: "estimated"`。请勿将真实记录、标题或配对配置提交到仓库。

## 测试

```bash
.venv/bin/python -m unittest discover -s tests -v
```

测试使用临时目录、虚构数据和本机临时端口。可选浏览器恢复检查需要 Node 22+ 与 Chrome：

```bash
node tests/check_kindle_recovery.mjs
```

## 许可与发布

本项目暂未指定开源许可证，参见 [LICENSE-NOTICE.md](LICENSE-NOTICE.md)。第三方组件按其各自许可证分发，声明位于安装包的 `THIRD-PARTY-NOTICES.md` 及 App 中保留的许可证文件。源码包与安装包均不包含本地 Git 历史。
