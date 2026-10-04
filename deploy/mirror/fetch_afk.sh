#!/usr/bin/env bash
# florr-auto-afk 只有 v1.1.1 这一个版本、不再更新: 把它的安装包从 GitHub Release 拉到镜像目录,
# 校验 SHA-256 通过才放进去. 官网域名下 /afk/<包名> 由 florrfarm.caddy 提供.
# 放在 /var/lib/florrfarm/afk 而不是 download/ —— mirror_sync.py 的 prune 会删掉 download/
# 里所有不属于 florr-auto-farm 的文件. 可以重复执行: 已经有且校验过就什么都不做.
#   sudo bash deploy/mirror/fetch_afk.sh
set -euo pipefail

FILE="florr-auto-afk-v1.1.1-auto.zip"
SHA256="74488ef58966d123ace6d19ebb11c05d7ac8ee992abd949289714a8a866e7d74"
URL="https://github.com/greatluca666/florr-auto-afk/releases/download/v1.1.1/${FILE}"
DEST_DIR="${FLORR_AFK_DIR:-/var/lib/florrfarm/afk}"

# 目录归 florrfarm(root 跑的时候才改属主), Caddy 只读
install -d -m 755 "$DEST_DIR"
if [ "$(id -u)" -eq 0 ] && id florrfarm >/dev/null 2>&1; then
  chown florrfarm:florrfarm "$DEST_DIR"
fi

check() { [ "$(sha256sum "$1" | cut -d' ' -f1)" = "$SHA256" ]; }

if [ -f "$DEST_DIR/$FILE" ] && check "$DEST_DIR/$FILE"; then
  echo "已经有 $FILE 且校验通过, 不用再拉"
  exit 0
fi

PART="$DEST_DIR/$FILE.part"
rm -f "$PART"
echo "==> 从 GitHub 拉 $FILE"
curl --fail --location --silent --show-error --retry 5 --retry-delay 5 -o "$PART" "$URL"
if ! check "$PART"; then
  rm -f "$PART"
  echo "SHA-256 对不上, 已删掉下载的文件, 没有放进镜像" >&2
  exit 1
fi
chmod 644 "$PART"
if [ "$(id -u)" -eq 0 ] && id florrfarm >/dev/null 2>&1; then
  chown florrfarm:florrfarm "$PART"
fi
mv -f "$PART" "$DEST_DIR/$FILE"
echo "==> 好了: $DEST_DIR/$FILE"
