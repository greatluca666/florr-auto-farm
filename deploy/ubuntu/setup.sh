#!/usr/bin/env bash
# florr-auto-pathing: 无头 Ubuntu 服务器一次性安装脚本.
#
# 装什么 / 为什么:
#   Xvfb              虚拟 X 显示. 这机器没有显示器, 但 pyautogui 的鼠标键盘注入
#                     和截图都要一个 X server, Chrome 也要有地方渲染.
#   openbox           窗口管理器. Xvfb 自己不带 WM, 没有 WM 时 Chrome 的
#                     --start-fullscreen 不生效, 窗口尺寸/位置也不确定 —— 而
#                     utils.py 里所有坐标常量都是照 1920x1080 全屏量的.
#   scrot             pyautogui 在 Linux 上的截图后端(pyscreeze 调它).
#   python3-tk        pyautogui 的可选依赖; 装上避免 import 期的告警.
#   x11vnc            可选: 想亲眼看看虚拟屏上在发生什么时用.
#   google-chrome     游戏本体跑的地方. florr.io 是 Canvas 2D(不是 WebGL),
#                     软件光栅就够, 不需要显卡.
#
# 用法: sudo bash deploy/ubuntu/setup.sh [安装目录]
#       安装目录默认 /opt/florr-auto-pathing
set -euo pipefail

INSTALL_DIR="${1:-/opt/florr-auto-pathing}"
SERVICE_USER="${FLORR_USER:-florr}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

if [[ $EUID -ne 0 ]]; then
  echo "要 root 跑: sudo bash $0 $*" >&2
  exit 1
fi

echo "==> 确保有 swap"
# 实机踩过的坑: 这类小规格机器常见 956MB 内存, 关掉 Chrome+Xvfb+opencv 一起跑
# 基本不够. 更险的是"关掉旧 swap 腾地方装系统"这个操作本身 —— 曾经在一台
# 956MB/满swap的机器上直接 swapoff 已有的 swap 文件, 内核要把将近1G的换页数据
# 一次性搬回本就不够的物理内存, 陷入40分钟的换页抖动, sshd 都握不上手. 教训是:
# 任何时候都不能让机器完全没有 swap 兜底, 装的时候就先建好, 不要等出问题再救.
#
# 只在没有任何生效中的 swap 时才建 —— 已有 swap(不管叫什么名字、在哪个路径)
# 就跳过, 不重复建、不打扰已有配置.
if [[ "$(swapon --show | wc -l)" -eq 0 ]]; then
  SWAP_SIZE_MB=2048
  echo "    没有生效中的 swap, 建 /swapfile (${SWAP_SIZE_MB}MB)"
  fallocate -l "${SWAP_SIZE_MB}M" /swapfile 2>/dev/null \
    || dd if=/dev/zero of=/swapfile bs=1M count="$SWAP_SIZE_MB"
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  grep -q "^/swapfile " /etc/fstab || echo "/swapfile none swap sw 0 0" >> /etc/fstab
  echo "    swap 就绪: $(swapon --show --noheadings | awk '{print $1, $3}')"
else
  echo "    已有生效中的 swap, 跳过:"
  swapon --show
fi

echo "==> 装系统包"
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
  xvfb openbox scrot x11vnc \
  python3 python3-venv python3-dev python3-tk \
  wget ca-certificates procps \
  curl gnupg openssl

if ! command -v google-chrome >/dev/null 2>&1 \
   && ! command -v google-chrome-stable >/dev/null 2>&1; then
  echo "==> 装 Google Chrome"
  tmp_deb="$(mktemp --suffix=.deb)"
  wget -qO "$tmp_deb" \
    https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
  apt-get install -y "$tmp_deb"
  rm -f "$tmp_deb"
else
  echo "==> Chrome 已装, 跳过"
fi

echo "==> 建服务账号 $SERVICE_USER"
# 非 root 跑 Chrome: 保住渲染器沙箱. cdp_bridge._linux_extra_flags() 只在 euid==0
# 时才补 --no-sandbox, 所以这里建了普通用户就不会走到那条降级路上.
id -u "$SERVICE_USER" >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin "$SERVICE_USER"

echo "==> 部署代码到 $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
# --exclude: venv 要在目标机重建(轮子是平台相关的); chrome-profiles 是运行期数据,
# 重装时不该被仓库里的空目录盖掉.
tar -C "$REPO_ROOT" --exclude=.git --exclude='venv*' --exclude='.venv' \
    --exclude=chrome-profiles --exclude='__pycache__' -cf - . \
  | tar -C "$INSTALL_DIR" -xf -

echo "==> 挑 Python 解释器"
# requirements.txt 把 numpy<2 和 opencv-python<5 锁在一起(原因见 requirements.txt
# 的注释). numpy<2 解析出来是 1.26.4, 2024-02 发的 —— 比 Python 3.13
# 早, 所以没有 cp313 轮子. 而 Ubuntu 24.10 起默认 python3 就是 3.13:
# 拿它建 venv 的话 pip 会退回去从源码编译 numpy, 在没装编译器的机器上直接炸
# ("Unknown compiler(s): cc/gcc/clang"), 装了编译器也要等 opencv 编几十分钟,
# 小内存机器上还会 OOM. 所以这里挑一个有轮子的解释器, 而不是补编译器.
pick_python() {
  local cand ver
  for cand in python3.12 python3.11 python3.10 python3; do
    command -v "$cand" >/dev/null 2>&1 || continue
    ver=$("$cand" -c 'import sys; print("%d%02d" % sys.version_info[:2])' 2>/dev/null) || continue
    # 下限 3.10: 代码里用了 3.10+ 的语法. 上限 3.12: 见上面的轮子问题.
    if [[ "$ver" -ge 310 && "$ver" -le 312 ]]; then echo "$cand"; return 0; fi
  done
  return 1
}

PY="$(pick_python || true)"
if [[ -z "$PY" ]]; then
  echo "==> 系统 python3 是 $(python3 -V 2>&1), 没有轮子; 装 python3.12"
  apt-get install -y --no-install-recommends python3.12 python3.12-venv python3.12-dev \
    || {
      cat >&2 <<'ERR'
❌ 这个发行版的源里没有 python3.12.
   两条路(推荐第一条):
   1. 加 deadsnakes 源再重跑本脚本:
        sudo add-apt-repository -y ppa:deadsnakes/ppa && sudo apt-get update
   2. 改用 numpy>=2 + opencv-python>=5 那套组合 —— 但 utils.py 的迷宫识别是照
      numpy<2 写的, 换了要重新验证, 不要在没跑过 debug_screen_pos.py 前上生产.
ERR
      exit 1
    }
  PY="$(pick_python)" || { echo "❌ 装完 python3.12 还是挑不出解释器" >&2; exit 1; }
fi
echo "    用 $PY ($("$PY" -V 2>&1))"

echo "==> 建 venv + 装依赖"
# --clear: 本脚本第一次可能是拿错版本的解释器建过一次 venv 了(比如 3.13 装 numpy
# 失败中断). 不清干净的话 `python3.12 -m venv 已存在目录` 会留下混着两个版本的
# pyvenv.cfg / lib 目录, 装出来的东西跑起来才炸, 比当场失败更难查.
"$PY" -m venv --clear "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/pip" install --upgrade pip
# --only-binary: numpy/opencv 只许装预编译轮子. 没有匹配轮子时立刻报错退出,
# 而不是安静地掉进源码编译 —— 那条路要么缺编译器当场炸, 要么编几十分钟再炸,
# 错误信息还指向 meson, 看不出真正原因是解释器版本挑错了.
"$INSTALL_DIR/venv/bin/pip" install --only-binary=numpy,opencv-python \
  -r "$INSTALL_DIR/requirements.txt"

mkdir -p "$INSTALL_DIR/chrome-profiles"
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"

echo "==> 装 systemd unit"
install -m 0644 "$REPO_ROOT/deploy/ubuntu/florr-xvfb.service"   /etc/systemd/system/
install -m 0644 "$REPO_ROOT/deploy/ubuntu/florr-wm.service"     /etc/systemd/system/
install -m 0644 "$REPO_ROOT/deploy/ubuntu/florr-bot.service"    /etc/systemd/system/
install -m 0644 "$REPO_ROOT/deploy/ubuntu/florr-vnc.service"    /etc/systemd/system/
install -m 0644 "$REPO_ROOT/deploy/ubuntu/florr-webctl.service" /etc/systemd/system/
systemctl daemon-reload

echo "==> 装 Caddy(Web 控制面板的反向代理/自动 HTTPS)"
if ! command -v caddy >/dev/null 2>&1; then
  apt-get install -y --no-install-recommends debian-keyring debian-archive-keyring \
    apt-transport-https
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    | tee /etc/apt/sources.list.d/caddy-stable.list
  apt-get update
  apt-get install -y caddy
else
  echo "    Caddy 已装, 跳过"
fi

echo "==> 生成 Web 控制面板共享密码"
# 两台机器要用同一个密码才能在统一面板里互相看到对方 —— 见下面的提示文案.
PASSWORD_DIR=/etc/florr-webctl
PASSWORD_FILE="$PASSWORD_DIR/password"
mkdir -p "$PASSWORD_DIR"
if [[ -f "$PASSWORD_FILE" ]]; then
  echo "    已有密码文件, 跳过生成: $PASSWORD_FILE"
elif [[ -n "${FLORR_WEBCTL_PASSWORD:-}" ]]; then
  printf '%s' "$FLORR_WEBCTL_PASSWORD" > "$PASSWORD_FILE"
  echo "    用环境变量 FLORR_WEBCTL_PASSWORD 里给的密码"
else
  openssl rand -hex 16 > "$PASSWORD_FILE"
  echo "    生成了随机密码: $(cat "$PASSWORD_FILE")"
fi
chmod 600 "$PASSWORD_FILE"
chown "$SERVICE_USER:$SERVICE_USER" "$PASSWORD_FILE"

echo "==> 配 Caddy(nip.io 自动 HTTPS, 反代到 127.0.0.1:8765)"
PUBLIC_IP="$(curl -s -4 ifconfig.me || curl -s -4 icanhazip.com)"
NIP_HOST="${PUBLIC_IP//./-}.nip.io"
cat > /etc/caddy/Caddyfile <<CADDYEOF
${NIP_HOST} {
    reverse_proxy 127.0.0.1:8765
}
CADDYEOF
systemctl enable --now caddy
systemctl restart caddy
echo "    面板地址: https://${NIP_HOST}/"

echo "==> 写 sudoers 白名单(webctl 只能启停 florr-bot, 不能干别的)"
SUDOERS_FILE=/etc/sudoers.d/florr-webctl
cat > "$SUDOERS_FILE" <<SUDOEOF
$SERVICE_USER ALL=(root) NOPASSWD: /usr/bin/systemctl start florr-bot.service, /usr/bin/systemctl stop florr-bot.service, /usr/bin/systemctl restart florr-bot.service
SUDOEOF
chmod 0440 "$SUDOERS_FILE"
if ! visudo -cf "$SUDOERS_FILE"; then
  echo "❌ sudoers 语法检查失败, 删掉刚写的文件, 不让一条坏规则留在系统里" >&2
  rm -f "$SUDOERS_FILE"
  exit 1
fi

echo "==> 把 $SERVICE_USER 加进 systemd-journal 组(不然读不了 journalctl 日志)"
usermod -aG systemd-journal "$SERVICE_USER"

echo "==> 起 Web 控制面板"
systemctl enable --now florr-webctl.service
# enable --now 在单元已经在跑时是空操作 —— 本脚本其它每一步都写成可重跑的
# 升级路径(Chrome/Caddy/密码文件都是"已存在就跳过", Caddyfile/sudoers 是无条件
# 覆盖), 但少了这一行的话, 重跑脚本部署了新代码, 跑着的还是旧进程, 而且不会
# 报错 —— 跟下面 Caddy 那对 enable --now + restart 是同一个模式(那对已经是对的,
# 不要动), 这里补上本来缺的.
systemctl restart florr-webctl.service

cat <<EOF

==> 装完了. 还差两件事(都得人来做, 脚本代劳不了):

1. config.json —— 无头机上没有 GUI 生成不了.
   在有显示器的机器上跑 GUI 配好时块/刷怪区, 然后拷过来:
       scp config.json  这台机:$INSTALL_DIR/config.json

2. Chrome 账号登录 —— 登录要人点, 无头机上点不了.
   把有显示器那台机器上登录好的 profile 目录整个拷过来:
       scp -r chrome-profiles/默认  这台机:$INSTALL_DIR/chrome-profiles/默认

   拷完修一下属主:
       sudo chown -R $SERVICE_USER:$SERVICE_USER $INSTALL_DIR

然后起服务:
       sudo systemctl enable --now florr-bot.service

看日志:
       journalctl -u florr-bot -f

想亲眼看虚拟屏(可选, 默认只监听 127.0.0.1, 要从外面看请走 SSH 隧道):
       sudo systemctl start florr-vnc.service
       ssh -L 5900:127.0.0.1:5900 你@这台机     # 然后本地 VNC 连 127.0.0.1:5900

Web 控制面板已经起来了:
       https://${NIP_HOST}/
       密码在: $PASSWORD_FILE

想让两台机器的面板互相看到对方(统一视图), 两台都跑完这个脚本后:
  1. 确认两台用的是同一个密码 —— 如果不是, 把其中一台的密码文件内容拷到
     另一台同路径下, 重启 florr-webctl: sudo systemctl restart florr-webctl
  2. 编辑 /etc/systemd/system/florr-webctl.service, 把 FLORR_WEBCTL_PEER_URL
     改成对方的面板地址(比如这台是 desert 号, 就填另一台的 https://...nip.io,
     不要带结尾的斜杠)
  3. sudo systemctl daemon-reload && sudo systemctl restart florr-webctl
     两台都做完这三步, 打开任意一台的面板就能同时看到两边状态.
EOF
