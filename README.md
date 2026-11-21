# Web App Init Base Env

在 macOS 上把空的 GitHub 仓库初始化为可部署的 Web 应用。基座使用 React、TypeScript、Ant Design、FastAPI 和 PostgreSQL。

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
