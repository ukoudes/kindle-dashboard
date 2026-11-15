# GitHub 发布指引

## 交付物

`dist/` 中包含：

- `Kindle-Dashboard-v1.1.0-macOS-arm64.zip`：macOS App 分发包。
- `kindle-dashboard-v1.1.0-source.zip`：准备上传仓库的源码包，无本地 Git 历史。
- `SHA256SUMS.txt`：这两个包的校验值。
- `PRIVACY-REPORT.json`：本次产物内容审计摘要，不列出真实个人信息。

不要上传整个开发文件夹。只使用经过审计的源码包，以及列出的分发产物。不要附带真实截图、配对配置、数据库、日志、备份或个人测试结果。

## 先决定公开身份与许可

GitHub 账号、提交作者、邮箱、签名证书、Issue 与发布活动都可能关联身份。清理文件无法让现有 GitHub 账号匿名。发布前选择自己愿意公开的身份；不要在提交、标签或 Release 中加入真实姓名、邮箱或网络信息。

本项目暂未指定开源许可证。先保留 `LICENSE-NOTICE.md`，不要勾选自动添加 MIT 等许可。公开仓库仍受 GitHub 关于查看和 fork 的平台条款约束，不能把“没有许可证”解释为任何人都无法获取副本。[GitHub 许可说明](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/licensing-a-repository)

## 创建仓库

1. 新建仓库，建议先设为 Private，仓库名可用 `kindle-dashboard`。
2. 解压源码包，将顶层目录内的源文件上传到仓库根目录。不要把源码 ZIP 当成整个仓库内容，也不要带入已有 `.git`。
3. 检查文件列表：不应出现 `.venv`、`data`、`clients`、数据库、日志、截图、安装包或密钥。
4. 在网页中核对 README、Windows 指引和安全说明，确认不含个人使用经历、地址或设备信息。
5. 单独决定何时将仓库改为 Public。公开后，删除或改回 Private 不能收回别人已复制的内容。

`.gitignore` 只是防误提交措施，不能清理已经提交的历史。本源码包不包含 Git 历史，不需要从旧的开发仓库继续推送。[GitHub 敏感数据说明](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/removing-sensitive-data-from-a-repository)

## 创建 Release

1. 打开仓库的 Releases，选择 Draft a new release。
2. 新建版本标签 `v1.1.0`，选择已审查的源码提交。
3. 标题填写 `Kindle 看板 v1.1.0`，说明可采用仓库中的 `RELEASE-NOTES.md`。
4. 上传 macOS App ZIP、`SHA256SUMS.txt` 与 `PRIVACY-REPORT.json`。源码通常由 GitHub 根据提交自动生成，已有源码 ZIP 也可作为额外附件。
5. 当前版本勾选 Pre-release，先保存 Draft，核对下载包与说明，再由发布者手动决定是否 Publish。

官方操作见 [GitHub Release 文档](https://docs.github.com/en/repositories/releasing-projects-on-github/managing-releases-in-a-repository)。

## Apple 签名与公证

当前 App 使用本地临时签名，未经过 Apple 公证。跨设备下载后可能被 Gatekeeper 阻止，因此本包只能如实标注为未公证预发布版本，不能声称具备正式开发者签名。

正式面向普通用户分发前，需要由发布者准备 Developer ID 身份，对所有原生组件与 App 正确签名、启用适当的 Hardened Runtime、提交 `notarytool` 公证并 stapling，再重新打包、生成校验值并重做隐私审计。签名身份与证书本身可能公开个人或组织信息，应在使用前核对。不要将证书、私钥、公证凭据或 Apple 账号信息提交到仓库。

不要使用关闭系统安全检查或删除 quarantine 属性的脚本作为安装流程。[Apple 公证说明](https://developer.apple.com/documentation/security/notarizing-macos-software-before-distribution)

## 重新制作发行包

在 Apple Silicon Mac 上创建开发环境并安装依赖，再执行：

```bash
mac-app/build.command
.venv/bin/python scripts/prepare_release.py
```

发布脚本使用文件白名单、固定 ZIP 时间戳和规范权限，不写入 macOS 扩展属性、资源分叉、本机 UID/GID 或提交历史。生成产物后仍需在本地解压核验，并使用隐私扫描器检查文件内容。

```bash
.venv/bin/python scripts/privacy_audit.py dist
```

扫描通过不等于数学意义上的匿名保证；审查范围为交付文件及其内容，不涵盖 GitHub 账号、其他设备缓存、系统快照或已经公开的副本。准备工作不会自动上传文件。
