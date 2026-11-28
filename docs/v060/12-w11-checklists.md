# V0.6.0 迁移、回滚与验收

这些清单属于联合候选。它们不授权 preview 合并、main 发布、安装、卸载、提权、凭据变更或生产数据库修改。

## 迁移

1. 从 `preview` 看候选 diff。不要以隔离祖先 `b201196` 为 PR base。
2. 确认 W01 仍通过 `app.control_authz.AuthzService` 认证，没有第二套认证实现。
3. 确认生产开关仍为关闭：发布、真实升级、真实清理、真实服务切换。
4. 新代码先在临时目录跑回归。不要把临时目录指到生产数据目录。
5. 74 项追踪里 BLOCKED 的编号保持 BLOCKED，直到补上命名的环境证据。

## 回滚

1. 不合并就没有运行面回滚。候选分支可以保留，不需要改 `preview` 或 `main`。
2. 若候选曾经在临时目录写下 journal，删除该临时目录即可。不要删除生产锁或生产 journal。
3. 端口代次一旦在某个 journal 里释放，同一代次不能再预约。回滚测试要使用新的临时 journal，而不是把旧代次改回去。
4. 部署适配脚本默认退出码 2。它没有被 `manage.sh` 的 update 路径调用。
5. app-init 适配入口只打印 BLOCKED，不调用安装器。

## 验收

- [ ] 精确 HEAD、tree 和每个并入步骤的父提交已记录。
- [ ] 现有包测试和 `tests/test_v060_w11_joint.py` 在临时目录通过，并保留命令输出。
- [ ] 拒绝的验收记录不会被 `_block_publish` 当成已接受。
- [ ] 只读 `/api/v1/control/board` 的 `side_effect` 为 false，且 `items` 为空。
- [ ] F11、U09、S10、D10、A12、I12、R10 仍是 BLOCKED，并写明缺少的环境。
- [ ] 没有新增 GitHub Actions workflow。
- [ ] `preview` 和 `main` 的 SHA 没有被这次推送改变。
- [ ] 没有伪造 Owner 验收、MacCI 回执或公网运行 SHA。
