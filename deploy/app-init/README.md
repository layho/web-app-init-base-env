# macOS 应用初始化

将独立入口 `bootstrap.sh` 保存为 `~/bin/app-init`，之后只需运行：

```bash
bash ~/bin/app-init
```

入口会检查 GitHub 网站和 API 的连通性。直连失败时输入局域网或本机的 HTTP 代理地址，
例如 `http://127.0.0.1:7890`，并复测两个地址。入口匿名拉取公开基座。需要目标仓库写权限时，通过浏览器登录 GitHub。随后输入已创建的空 GitHub 仓库
（格式 `owner/repo`）、正式域名、管理员账号、密码和只限该目标仓库的细粒度令牌。
令牌需要 **Issues 读写**和 **Contents 读取**，分别用于反馈与后台 Git 拉取。
浏览器登录用于首次推送。后台读仓库令牌保存在本地 Git 目录中，文件权限为 `600`。
需要代理时，若希望开机后自动拉取和同步反馈，代理也必须在用户登录前可用。

开发者可以从基座的本地副本运行内部入口 `bash deploy/app-init/app-init`（默认使用 SSH）。

使用有管理权限的普通 macOS 用户运行。Apple Silicon 与 Intel Mac 的标准 Homebrew 路径均可用。
需要 Python 3.10+、可构建前端的 Node.js/npm 和 PostgreSQL 服务端工具。若已安装 Homebrew，可以用
`bash deploy/app-init/app-init --install-deps` 安装缺失依赖。安装应用时由 macOS 的 `sudo`
提示授权注册系统服务。基座仓库的副本可以放在任意本地目录。

目标 Mac 需要一个在该机运行的远程管理 Cloudflare Tunnel。如果使用内部入口的 SSH 模式，
还需要无人登录时可用的 SSH 凭据。新机器应配置其对应的隧道。使用已有隧道前应确认它的连接器都能访问同一个回源，
因为此脚本添加的回源地址是该机的 `127.0.0.1`。

首次初始化时脚本提示输入 Cloudflare 配置，隐藏令牌输入，并将配置以 `600` 权限保存到
`~/.config/app-init/cloudflare.json`。后续自动读取已有文件，字段如下：

```json
{
  "account_id": "...",
  "zone_id": "...",
  "tunnel_id": "...",
  "api_token": "..."
}
```

Cloudflare API Token 需能读取和更新指定的 Tunnel 配置及该 Zone 的 DNS。目标仓库 Token
单独配置。自动合并使用具有合并权限的 GitHub CLI 登录。
初始化会校验域名冲突和本机 SHA 后添加指定域名的 DNS 与回源规则。

应用目录为 `~/Applications/app-init/owner--repo`。每个应用包含独立 PostgreSQL 数据目录、
本机端口和开机启动任务。更新器每五分钟检查 main，同时响应应用内的手动更新按钮。
脚本以公网 HTTPS 登录、会话恢复、运行 SHA 和真实反馈同步通过作为完成条件。

仅生成基座可运行 `bash deploy/app-init/app-init --seed-only`。再次运行会复用已有仓库和
本地配置。生成后的仓库可用 `bash deploy/macmini/acceptance.sh --release PR号 完整候选SHA`
执行验收、合并和部署。
