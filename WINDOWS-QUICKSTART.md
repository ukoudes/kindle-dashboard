# Windows 采集与可选转发

Windows 可采集本机用量并上报到汇总端，也可为 Kindle 转发看板。使用方式由用户自行选择网络环境，不要求特定网络拓扑。

## 生成独立配置

在汇总端的源码环境中运行以下命令，将 `DASHBOARD_HOST` 替换为汇总端实际地址：

```bash
.venv/bin/python kindle_dashboard.py pair --name "Windows" --platform Windows --server-url http://DASHBOARD_HOST:8000
```

它会生成私有 `clients/agent.json`。只把这份配置安全地交给对应的 Windows 设备，不要上传到 GitHub、共享目录或公共 Release。

发行包不附带配置、配对凭据或其他设备的信息。

## 启动采集器

1. Windows 安装 Python 3.10 或更新版本，并确保可运行 `py`。
2. 将项目源码与该设备的私有 `clients/agent.json` 放在同一项目目录结构中。
3. 运行 `start-agent-windows.cmd`。采集器读取本机记录，并向配置的汇总端上报。

默认读取当前用户的 `~/.codex/sessions`，也支持配置 `codex_dir`。WSL 或自定义目录需要用户明确指定可访问的路径。不要复制其他设备的 `data`、虚拟环境或源记录。

## 可选看板转发

启动器同时开启端口 8001 的转发。Kindle 可打开启动窗口显示的地址，路径为 `/kindle`。转发地址必须使用用户自己的实际地址；项目不预设任何网络地址。

转发要求 Windows 能访问汇总端。它不会绕过网络隔离或防火墙，也不会向 Kindle 转发上报凭据；查看页面仍需单独配对。需要允许入站连接时，应只对用户信任的网络与接口开放。

HTTP 不加密传输。共享或不受信任的网络需要另行配置安全网络或加密通道。

## 排查顺序

1. 检查 Windows 能否访问配置中的汇总端地址。
2. 检查 Windows 本机能否访问 `http://localhost:8001/kindle`。
3. 检查 Kindle 能否访问启动窗口显示的转发地址。

两台电脑均需保持唤醒。设备在线但日志为零可能表示未找到记录，不应直接解释为没有用量。断网后未上报记录保留在 Windows 自己的 `data/` 中，恢复后重试。

`start-windows.cmd` 启动独立汇总端；仅需采集与转发时使用 `start-agent-windows.cmd`。
