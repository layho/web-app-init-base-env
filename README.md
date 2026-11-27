# Web App Init Base Env

面向 macOS 的 Web 应用初始化与持续交付基座。将空 GitHub 仓库初始化为独立运行、可登录、可反馈、可升级的 Web 应用。技术栈为 React、TypeScript、Ant Design、FastAPI、PostgreSQL。

## 版本与功能

正式基线 V0.4.0；本分支版本标识 V0.5.0，集成候选见 [PR #196](https://github.com/layho/web-app-init-base-env/pull/196)。本候选文档不代表已发布或已安装宿主 runtime。实际运行版本以网页 SHA、GitHub 分支及独立回执为准。

| 能力 | 使用方式 |
|---|---|
| 应用初始化 | app-init 创建独立应用、数据库、端口、域名、登录及后台服务 |
| 开发任务 | 站内反馈同步 GitHub Issues；任务看板追踪 PR、测试及交付 |
| 手动干预 | 任务看板立即测试、请求安全合并、冲突修复及 Draft 操作；实际结果以回执为准 |
| 版本发布 | 基座 preview/main 双通道、精确候选测试、审查、冻结及发布 |
| 运维 | 运行诊断、版本记录、更新检查、手动部署、失败回退 |
| HostOps | 认证宿主心跳、只读诊断、受控任务与审计；非任意远程 Shell |
| V0.5.0 候选新增 | 模块 SDK 和升级所有权保护、持久化集成队列、执行恢复及宿主回执诊断；须完成候选及实机验证 |

## 架构与支持范围

- Core：登录、会话、反馈、开发交付、运维和公共 UI。
- APP Modules：应用自有代码与路由，通过显式 SDK 注册；升级时保护自有目录。
- MacCI：独立宿主测试、preview 合并协调器、main 发布器及回执交付；与 Web 进程分别安装。
- PostgreSQL：每个应用独立实例；模块可显式使用独立数据库角色。模块仍是同进程可信代码，不是沙箱。
- Cloudflare Tunnel：为本机回源提供 HTTPS 入口。
- 当前安装与服务管理支持 macOS；Linux app-init 属于后续版本，未交付。

## 使用前准备

| 何时使用 | 准备什么 | 去哪里获取或输入 |
|---|---|---|
| 每个新项目 | 空 GitHub 仓库，启用 Issues；一个有推送权限的 GitHub 账号 | 创建仓库时跳过 README、许可证和 .gitignore。输入 `owner/repo` 后，按提示在浏览器授权首次推送。 |
| 每个新项目 | 一个限该仓库的细粒度 GitHub Token | GitHub → Settings → Developer settings → Personal access tokens → Fine-grained tokens。选择正确的 Resource owner，在 Only select repositories 选目标仓库。Repository permissions 中选 Issues 读写、Contents 只读。生成后在终端隐藏输入。 |
| 每个新项目 | 应用子域名、管理员用户名和新密码 | 选该 DNS 区域下可用的子域名。自行设置应用密码（12–256 个字符），用于网站登录。 |
| 每台 Mac 首次配置 | Cloudflare Account ID、Zone ID、Tunnel ID 和 API Token | 域名概览的 API 区域可复制前两个 ID。Tunnel ID 取自这台 Mac 正在运行的远程管理隧道。令牌授予指定域名 DNS Edit，以及隧道所在账户的 Cloudflare One Connector: cloudflared Edit。已有本机配置会自动复用。 |
| 安装系统服务时 | 当前 Mac 管理员的登录密码 | 在终端看到 sudo 的 `Password:` 时输入，输入过程中屏幕无回显。 |

Cloudflare 的连接器运行令牌供 `cloudflared` 服务连接隧道。初始化程序提示的 Cloudflare API Token 用于编辑域名记录和回源规则。密码和令牌仅在本机交互输入。 [GitHub 令牌说明](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens) · [Cloudflare 隧道说明](https://developers.cloudflare.com/tunnel/get-started/)

## 快速开始：创建新应用

下载 [独立 app-init 脚本](https://raw.githubusercontent.com/layho/web-app-init-base-env/main/deploy/app-init/bootstrap.sh)，保存为 `~/bin/app-init`，检查来源后运行：

```bash
bash ~/bin/app-init
```

准备空仓库、域名及目标 Mac 上的 Cloudflare Tunnel。脚本检查 GitHub 网络和权限，需要时提示代理及浏览器授权；输入管理员及仓库凭据。安装路径默认 `~/Projects/app-init/owner--repo`，旧安装路径继续保留。支持 Apple Silicon 与 Intel Mac，Python 3.10+、Node.js/npm、PostgreSQL 工具及所需系统服务。详细参数、权限和依赖见 [macOS 安装说明](deploy/app-init/README.md)。

新应用跟踪自己的 main；五分钟更新器及网页手动更新使用同一交付机制。检查更新只读，不等于部署。

## 基座自身验收站

仅用于基座候选，不用 app-init 覆盖安装本仓库或正式业务应用。在已审核的候选上首次部署（普通 Mac 用户）：

```bash
cd ~
git clone --branch preview --single-branch git@github.com:layho/web-app-init-base-env.git web-app-init-base-env-preview
cd ~/web-app-init-base-env-preview
bash deploy/base-preview.sh --install-deps
```

复用本机 Cloudflare 配置；独立端口、PostgreSQL 数据目录、Unix socket 和 com.layho.basepreview 服务标签。公网入口 https://base.layho.com。端口或域名冲突须停止核查，不自动替换其他应用。已有站点补验：

```bash
cd ~/web-app-init-base-env-preview
bash deploy/base-preview.sh --verify
```

部署后由更新器跟踪 origin/preview，避免人工 git pull。preview 不推送给下游项目。基座为公开仓库，反馈勿包含秘密。

## 开发、测试及正式发布

1. 固定一个活动版本、任务范围和唯一候选；开发集中提交，Draft 不代表发布授权。
2. MacCI 对精确 Head/Base/Merge 执行隔离测试；变更候选必须重新核对回执。
3. Ready 后仍需可信审查、现有冻结门禁和精确测试证据。preview 集成与部署分别核实。
4. 按发布规范验收、推进 main、核对运行 SHA；业务应用随后通过正式基座升级机制接收更新。

V0.5.0 的 Draft 暂停自动送测规则须在新版可信 Runner 安装后才生效；可按既有权限定向立即测试。禁止修改生产数据库或使用 GitHub Actions 替代真实 MacCI 回执。

参见 [版本开发规范](docs/VERSION_DEVELOPMENT.md)、[版本计划](docs/RELEASE_PLAN.md)、[preview 协调器](deploy/macci/PREVIEW-MERGE.md)、[main 发布器](deploy/macci/MAIN-PUBLISH.md)。

## V0.5.0 集成队列与异常恢复

新版候选提供持久化预约、精确候选证据、合并 journal、过期 lease 识别及待对账保护。未知 push 结果不得自动重推；只有可信宿主证据可确认集成。已确认集成不等于部署或发布。

宿主运行模块与 Web 版本分别更新。安装固定可信安装器前独立审查其摘要，安装时验证 runner、merge_preview 和 preview_journal 的各自摘要。管理模式由可信本机配置显式启用；首个活动版本自动初始化，版本切换不得仅凭 PR 标题或网页请求。具体配置、回滚及限制见 [集成队列](docs/v050-integration-queue.md)、[宿主升级](deploy/macci/UPGRADE.md)。

## 应用模块开发及基座升级

业务模块使用 app_modules/、frontend/src/app-modules/、migrations/modules/ 等应用自有目录；基座升级不覆盖既有模块文件。后端显式注册路由，使用独立命名空间、精确依赖声明和独立 PostgreSQL 角色。角色名称检查并非真实 ACL 验证；数据库权限由部署方独立配置。共享 Python 进程不支持互不兼容的依赖版本。

- [后端模块 SDK](docs/BACKEND_MODULE_SDK.md)
- [前端模块 SDK](docs/FRONTEND_MODULE_SDK.md)
- [前端 React 模块宿主](docs/FRONTEND_MODULE_REACT_HOST.md)
- [升级所有权预检](docs/APP_UPGRADE_PREFLIGHT.md)

## 日常运维、安全与恢复

系统日志、宿主回执、任务看板、发布记录分别展示实际证据。回执过期或不可用不视为健康。HostOps 心跳可选传输有限的合并历史；先升级网关，再安装 reviewed Agent 并启用。跨 OS 用户访问不会自动授权。

更新失败时保留数据库与本机配置，核对日志、端口、运行 SHA 和回退证据。不要删除锁、清空 journal、复用旧测试或强推绕过保护。部署 .env 及 .base-preview.env 必须是运行用户拥有的普通文件，权限 600 或 400。不要将凭据提交至仓库。

参见 [HostOps 安装](deploy/hostops/README.md)、[反馈交付](docs/FEEDBACK-DELIVERY.md)、[回执交付](deploy/macci/RECEIPT-DELIVERY.md)。

## 依赖及回归

运行和测试依赖分别由 requirements-runtime.in 和 requirements-dev.in 声明，锁文件含发行包哈希。修改依赖后重新生成：

```bash
uv pip compile --universal --python-version 3.10 --generate-hashes requirements-runtime.in -o requirements-runtime.txt
uv pip compile --universal --python-version 3.10 --generate-hashes requirements-dev.in -c requirements-runtime.txt -o requirements.txt
```

开发测试使用隔离环境；完整候选以 MacCI 实际回执为准。历史版本详见 [版本记录](app/release_notes.json)，不以本文替代实际运行证据。
