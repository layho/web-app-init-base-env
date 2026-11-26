# Web App Init Base Env

在 macOS 上把空的 GitHub 仓库初始化为可部署的 Web 应用。基座使用 React、TypeScript、Ant Design、FastAPI 和 PostgreSQL。

## 使用前准备

| 何时使用 | 准备什么 | 去哪里获取或输入 |
|---|---|---|
| 每个新项目 | 空 GitHub 仓库，启用 Issues；一个有推送权限的 GitHub 账号 | 创建仓库时跳过 README、许可证和 .gitignore。输入 `owner/repo` 后，按提示在浏览器授权首次推送。 |
| 每个新项目 | 一个限该仓库的细粒度 GitHub Token | GitHub → Settings → Developer settings → Personal access tokens → Fine-grained tokens。选择正确的 Resource owner，在 Only select repositories 选目标仓库。Repository permissions 中选 Issues 读写、Contents 只读。生成后在终端隐藏输入。 |
| 每个新项目 | 应用子域名、管理员用户名和新密码 | 选该 DNS 区域下可用的子域名。自行设置应用密码（12–256 个字符），用于网站登录。 |
| 每台 Mac 首次配置 | Cloudflare Account ID、Zone ID、Tunnel ID 和 API Token | 域名概览的 API 区域可复制前两个 ID。Tunnel ID 取自这台 Mac 正在运行的远程管理隧道。令牌授予指定域名 DNS Edit，以及隧道所在账户的 Cloudflare One Connector: cloudflared Edit。已有本机配置会自动复用。 |
| 安装系统服务时 | 当前 Mac 管理员的登录密码 | 在终端看到 sudo 的 `Password:` 时输入，输入过程中屏幕无回显。 |

Cloudflare 的连接器运行令牌供 `cloudflared` 服务连接隧道。初始化程序提示的 Cloudflare API Token 用于编辑域名记录和回源规则。密码和令牌仅在本机交互输入。 [GitHub 令牌说明](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens) · [Cloudflare 隧道说明](https://developers.cloudflare.com/tunnel/get-started/)

## 使用

下载 [独立 app-init 脚本](https://raw.githubusercontent.com/layho/web-app-init-base-env/main/deploy/app-init/bootstrap.sh)，一次性保存为 `~/bin/app-init`。在 Mac 的终端运行：

```bash
bash ~/bin/app-init
```

脚本检查 GitHub 网络，直连失败时询问 HTTP 代理。基座从此公开仓库匿名下载。初始化目标仓库时，脚本检查 GitHub 写权限并在需要时引导浏览器登录。

准备一个空的目标仓库、域名和目标 Mac 上运行的远程管理 Cloudflare Tunnel。目标仓库令牌需要 Issues 读写和 Contents 读取。管理员密码和令牌在终端隐藏输入。依赖、Cloudflare 配置和发布命令见 [macOS 安装说明](deploy/app-init/README.md)。

## 基座功能

- 登录、退出、会话恢复和 CSRF 校验
- 左侧导航与空工作台、项目信息及开发任务视图
- 意见反馈入库与 GitHub Issues 同步
- 运行版本和 Git SHA、更新记录、系统日志和手动更新
- 独立 PostgreSQL、开机自检、五分钟更新、测试及发布失败回退


## 基座自身独立验收站（Mac mini）

**仅供基座候选版本使用。** 不要用 app-init 初始化本仓库，不要在正式应用目录执行覆盖安装。基座源代码与验收部署分别使用 main 和 preview 分支；preview 是候选通道，不是正式发布版本。

首次部署（普通 Mac 用户，非 sudo；先审核、测试候选提交）：

```bash
cd ~
git clone --branch preview --single-branch git@github.com:layho/web-app-init-base-env.git web-app-init-base-env-preview
cd ~/web-app-init-base-env-preview
bash deploy/base-preview.sh --install-deps
```

脚本复用 `~/.config/app-init/cloudflare.json`，在隐藏输入中读取管理员密码及基座仓库 Issues 读写 Token，自动选择并保存独立端口，使用独立 PostgreSQL 数据目录、Unix socket 和 com.layho.basepreview 服务标签。公网地址为 https://base.layho.com。遇到端口或路由冲突立即停止；不会自动终止其他进程或替换现有域名映射。

部署完成后避免手动 git pull，五分钟更新器仅跟踪 origin/preview。已有验收站补验：

```bash
cd ~/web-app-init-base-env-preview
bash deploy/base-preview.sh --verify
```

合并 main 前必须核对候选提交 SHA、完整自动化测试及真实 Mac/HTTPS 验收结果。preview 不推送至下游项目。基座仓库公开，站内反馈会同步到公开 Issues，请勿提交秘密或内部敏感数据。若部署中断，保留数据库及本机配置，先核对日志及端口再重试；禁止为迁就端口冲突而自动更改已配置端口。

## 开发与发布规范

遵守 [版本驱动开发规范](docs/VERSION_DEVELOPMENT.md) 与 [当前版本计划](docs/RELEASE_PLAN.md)。
