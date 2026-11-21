#!/bin/bash
# Standalone entry point. Install this file as ~/bin/app-init.
set -euo pipefail
umask 077
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

foundation=layho/web-app-init-base-env
branch=main
fail() { printf '%s\n' "$*" >&2; exit 1; }

if [[ "${1:-}" == --help ]]; then
  printf '%s\n' '在 Mac 终端执行此文件。它会检查网络与权限、拉取应用基座并启动初始化。'
  exit 0
fi
[[ "$(uname -s)" == Darwin && "$EUID" != 0 ]] || fail '请在 macOS 上用普通管理员账户运行。'
[[ -t 0 ]] || fail '请在可交互的 Mac 终端运行。'

github_reachable() {
  local url
  for url in https://github.com/ https://api.github.com/; do
    curl --silent --fail --output /dev/null --connect-timeout 5 --max-time 10 "$@" "$url" || return 1
  done
}

if ! github_reachable --noproxy '*'; then
  printf '%s\n' '无法访问 GitHub 网站或 API。若有 HTTP 代理，请填写地址，例如 http://127.0.0.1:7890。'
  while true; do
    read -r -p 'HTTP 代理地址（直接回车表示没有代理）：' proxy
    [[ -n "$proxy" ]] || fail 'GitHub 网络不可用。网络恢复后重新执行 app-init。'
    if [[ ! "$proxy" =~ ^http://([A-Za-z0-9.-]+|\[[0-9a-fA-F:]+\]):[0-9]{1,5}/?$ ]]; then
      printf '%s\n' '请输入不含账号密码的 HTTP 代理地址，格式为 http://主机:端口。'
    elif github_reachable --noproxy '' --proxy "$proxy"; then
      export APP_INIT_PROXY="$proxy"
      export https_proxy="$proxy" HTTPS_PROXY="$proxy" http_proxy="$proxy" HTTP_PROXY="$proxy"
      export no_proxy="localhost,127.0.0.1,::1" NO_PROXY="localhost,127.0.0.1,::1"
      break
    else
      printf '%s\n' '这个代理未通过 GitHub 网站及 API 连通性检查，请检查代理是否已启动或换一个地址。'
    fi
  done
fi
printf '%s\n' 'GitHub 网络检查通过。'

if ! xcode-select -p >/dev/null 2>&1; then
  xcode-select --install || true
  fail '请完成 macOS 命令行工具安装，再重新执行 app-init。'
fi
work="$(mktemp -d "${TMPDIR:-/tmp}/app-init.XXXXXXXX")"
trap 'rm -rf "$work"' EXIT
git init -q "$work"
export GIT_TERMINAL_PROMPT=0

git -C "$work" remote add origin "https://github.com/$foundation.git"
git -C "$work" -c credential.helper= fetch --quiet --depth=1 --no-tags origin "refs/heads/$branch" ||
  fail "无法下载公开基座 $foundation，请检查网络后重试。"
git -C "$work" checkout --quiet --detach FETCH_HEAD
printf '已拉取基座 %s\n' "$(git -C "$work" rev-parse HEAD)"
bash "$work/deploy/app-init/app-init" --install-deps --github-https
