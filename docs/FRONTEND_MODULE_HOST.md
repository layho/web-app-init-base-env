# 前端模块打开与菜单投影

这是 #156 C2 的下一段，依赖并增量于注册 SDK [PR #157](https://github.com/layho/web-app-init-base-env/pull/157) 的精确头 `4b89adc39c6e79f63283c0a9a949dbfa375d3fe1`。本段不改该头里的注册实现，也不把菜单接到 `main.tsx` 或 `WorkspaceShell.tsx`。

## 新增

- `frontend/src/module-sdk/host/open.ts`
- `frontend/src/module-sdk/host/open.test.mjs`

公开函数是 `moduleMenu` 和 `openModule`。它们只接受 `registerModules` 已经返回的结果。

## 行为

- 菜单按注册结果的现有顺序投影 `id`、`routeKey`、`title`、`order`、图标名和 `permission`。被拒绝的模块、保留名和基座页面键不会出现。
- 不传 `granted` 时，已接受模块都可见。`permission` 仍只是声明。
- 传入 `granted` 时，`permission === null` 的模块保持可见；声明不在列表中的模块从菜单消失。再用它的 `routeKey` 调用 `openModule` 得到 `permission-hidden`，且不会调用该模块的 `load`。这不是后端鉴权，也不是跨应用隔离。
- 未知 `routeKey`、被拒绝模块的 `routeKey`、基座页面键和非字符串返回 `unknown-route`。
- 加载或渲染失败分别返回 `load-failed`、`render-failed`。失败留在该模块，相邻模块仍可打开。
- `OPEN_FAILURE_TEXT` 是给以后壳使用的固定短句。当前没有按钮，也没有页面入口。

## 版本

宿主 SDK 常量仍是 `0.1.0`。本段不修改 `package.json`、lockfile、`app/release_notes.json` 或任何全局版本号，不写入 0.1.41。

## 尚未接线

壳文件仍由 #148 占用。在那些文件释放并且接线契约一致之前，本投影不能当作已经上线的导航。
