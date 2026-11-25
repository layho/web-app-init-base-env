# 前端模块 SDK（C2 第一段）

本文件描述已实现、可独立测试的注册库。它还没有接入 `frontend/src/main.tsx` 或 `WorkspaceShell.tsx`。未接线的代码不能当作已上线的菜单或页面。

## 范围

- 代码在 `frontend/src/module-sdk/`。
- 宿主版本常量是 `FOUNDATION_UI_SDK = 0.1.0`。
- 公开入口是 `registerModules`、`loadModule`、`renderModuleIsolated` 和 `exampleNotesModule`。
- 不修改现有页面、shell、样式、`package.json`、lockfile、后端或初始化器。

## 注册类型

模块描述只包含这些字段：

| 字段 | 规则 |
|---|---|
| `id` | `^[a-z][a-z0-9-]{1,63}$`，全局唯一 |
| `routeKey` | 同一规则，全局唯一。当前是壳内页面键，不是 URL |
| `title` | 去空白后 1 到 40 个字符 |
| `order` | 安全整数。先按 `order`，再按 `id` |
| `icon` | 只能是 `REGISTERED_ICONS` 中的名字，不能是组件或 URL |
| `sdk` | `{ min, max }`，均为 `x.y.z`，且 `min <= 宿主版本 <= max` |
| `permission` | 可省略。若提供，只能是短声明标识。前端隐藏不是权限执行 |
| `load` | 函数，返回 `{ default: 视图函数 }`。注册时不会调用它 |

未知字段会拒绝该模块。注册函数不根据字符串路径做动态导入。

## 保留名

模块不能使用这些 `id` 或 `routeKey`：`workspace`、`project`、`tasks`、`releases`、`logs`、`feedback`、`login`、`diagnostics`、`settings`。前六项是当前基座页面，仍由现有组件渲染。

## 失败隔离

- 重复 id、重复 routeKey、保留名、非法图标、非法版本、不兼容版本、未知字段：只拒绝该条，其余模块保留。
- `load` 抛错或没有 `default` 函数：`loadModule` 返回 `load-failed`，不向外抛。
- 视图函数抛错：`renderModuleIsolated` 返回 `render-failed`，不向外抛。
- 这些隔离只保护调用方的控制流。它不是进程沙箱，也不是跨应用凭据隔离。

## 示例

`exampleNotesModule()` 注册 `example-notes`，图标名 `experiment`，权限声明 `modules.example-notes.read`。示例放在 `frontend/src/module-sdk/examples/`，因为当前批准的新目录是 `frontend/src/module-sdk/**`。Codex 另提到的 `frontend/src/example-modules/` 本轮没有创建，避免在后一次预约之外再加一个会被整树复制的目录。

## 尚未接线

`deploy/app-init/app_init.py` 会复制整个 `frontend/src`，升级规划把该目录下的 TypeScript 视为基座托管文件。因此本目录现在是基座 SDK，不是可独立于升级存活的业务目录。业务模块的最终位置仍要等所有权 manifest。在 shell 文件从 #148 释放并且接线契约一致之前，不改 `main.tsx` 和 `WorkspaceShell.tsx`。
