#!/usr/bin/env bash
# 在跑 florr 的 Ubuntu 服务器(现在是 140)上装官网 florrfarm.cc.cd 的安装包镜像.
# 前提: 已经跑过 deploy/ubuntu/setup.sh(装好了 Caddy). 可以重复执行.
#   sudo bash deploy/mirror/install.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ "$(id -u)" -ne 0 ]; then
  echo "需要 root: sudo bash $0" >&2
  exit 1
fi
if ! command -v caddy >/dev/null 2>&1; then
  echo "没装 Caddy, 先跑 deploy/ubuntu/setup.sh" >&2
  exit 1
fi

echo "==> 装 git / python3(同步脚本只用标准库)"
apt-get update -qq
apt-get install -y git python3 >/dev/null

echo "==> 建专用用户 florrfarm 和数据目录"
if ! id florrfarm >/dev/null 2>&1; then
  useradd --system --home-dir /var/lib/florrfarm --shell /usr/sbin/nologin florrfarm
fi
install -d -o florrfarm -g florrfarm -m 755 /var/lib/florrfarm /var/lib/florrfarm/download

echo "==> 装同步脚本和 systemd 定时器"
install -d -m 755 /opt/florrfarm-mirror
install -m 644 "$HERE/mirror_sync.py" /opt/florrfarm-mirror/mirror_sync.py
install -m 644 "$HERE/florr-mirror.service" "$HERE/florr-mirror.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now florr-mirror.timer

echo "==> 配 Caddy 站点 florrfarm.cc.cd"
install -d -m 755 /etc/caddy/sites.d
# 先备份 Caddyfile: 下面校验不过就撤掉站点文件、还原 Caddyfile, 不留一份让 Caddy
# 下次 reload / 重启直接起不来的配置
cp -p /etc/caddy/Caddyfile /etc/caddy/Caddyfile.bak-florrfarm
install -m 644 "$HERE/florrfarm.caddy" /etc/caddy/sites.d/florrfarm.caddy
# 老版本 setup.sh 生成的 Caddyfile 没有这行; 新版本已经带了, 不会重复加
if ! grep -qF 'import /etc/caddy/sites.d/*.caddy' /etc/caddy/Caddyfile; then
  printf '\nimport /etc/caddy/sites.d/*.caddy\n' >> /etc/caddy/Caddyfile
fi
# florrfarm.caddy 的 /admin 要 import 后台密码文件(deploy/stats/install.sh 生成). 还没装统计时
# 先放一个一律 403 的占位, 不然配置校验不过
if [ ! -f /etc/caddy/florrfarm-admin.auth ]; then
  printf 'respond "后台还没配密码" 403\n' > /etc/caddy/florrfarm-admin.auth
fi
if ! caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile; then
  rm -f /etc/caddy/sites.d/florrfarm.caddy
  cp -p /etc/caddy/Caddyfile.bak-florrfarm /etc/caddy/Caddyfile
  echo "Caddy 配置校验没过(报错见上面): 已撤掉 florrfarm.caddy 并还原 /etc/caddy/Caddyfile, 正在跑的 Caddy 没动" >&2
  exit 1
fi
systemctl reload caddy

echo "==> 先同步一次"
if systemctl start florr-mirror.service; then
  echo "    同步完成"
else
  echo "    首次同步失败, 看日志: journalctl -u florr-mirror -n 50" >&2
fi
echo "    网站文件: /var/lib/florrfarm/site-repo/site"
echo "    安装包:   /var/lib/florrfarm/download"
