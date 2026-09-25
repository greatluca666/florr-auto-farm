# 无头 Ubuntu 服务器部署

这机器没有显示器, 但整个工具靠 `pyautogui` 读屏 + 注入鼠标键盘 —— 所以要给它一块
**虚拟屏**(Xvfb), 而不是改成"不用屏幕"。

```
systemd
 ├─ florr-xvfb.service   Xvfb :99  (1920x1080x24 虚拟屏)
 ├─ florr-wm.service     openbox   (没 WM 时 Chrome 全屏不生效)
 ├─ florr-bot.service    ExecStartPre: main.py --launch-chrome
 │                       ExecStart:    main.py --worker
 └─ florr-vnc.service    x11vnc (可选, 只监听 127.0.0.1)
```

florr.io 是 **Canvas 2D**(见 `canvas_hook.js` 打的是 `CanvasRenderingContext2D.prototype`),
不是 WebGL —— CPU 软件光栅就能画, 服务器不需要显卡。

## 安装

```bash
sudo bash deploy/ubuntu/setup.sh
```

装完还差两件**必须人工做**的事, 因为无头机上没有 GUI:

**1. `config.json`** — 在有显示器的机器上跑 GUI 配好时块/刷怪区, 拷过来:
```bash
scp config.json 服务器:/opt/florr-auto-pathing/config.json
```

**2. Chrome 账号 profile** — 登录 florr 要人点, 无头机点不了。把已登录的 profile
目录整个拷过来:
```bash
scp -r chrome-profiles/默认 服务器:/opt/florr-auto-pathing/chrome-profiles/默认
sudo chown -R florr:florr /opt/florr-auto-pathing
```

然后:
```bash
sudo systemctl enable --now florr-bot.service
journalctl -u florr-bot -f
```

## Web 控制面板

`setup.sh` 跑完会自动装好一个网页控制面板（HTTPS，密码保护），不用再手动
SSH 查状态/改配置/启停 bot。地址和密码在 `setup.sh` 跑完的输出里，密码也存在
`/etc/florr-webctl/password`（0600，只有 `florr` 用户能读）。

两台机器各自跑一份，互不依赖对方。想要打开任意一台就能同时看到两边状态，
按 `setup.sh` 输出里给的三步操作把 `FLORR_WEBCTL_PEER_URL` 互相指向对方即可。

面板能做的事：
- 看两台机器的 systemd unit 状态、内存/swap、当前生效的时间块
- 编辑 config.json（保存时服务端校验，不合法直接拒绝、列出具体原因——校验
  跑在 `webctl.py` 里，不是浏览器 JS，这样才拒得住，不会被绕过）
- 启停/重启 `florr-bot.service`
- 看 `florr-bot` 的实时日志

面板做不到的事（还是走本页面前面几节的手动流程）：florr 账号登录、生成
config.json 的初始内容——这些仍然要在有显示器的机器上用 GUI 做，面板只负责
之后的日常运维。

架构/安全设计细节见 `webctl.py` 文件开头的说明。

## 先验一遍再上生产

坐标常量是在用户 1920x1080 的 Windows 客户端上一像素一像素标出来的。虚拟屏跟真
客户端未必长得一模一样(字体、缩放、Chrome 版本都可能差)。跑一次校准诊断:

```bash
sudo -u florr DISPLAY=:99 /opt/florr-auto-pathing/venv/bin/python \
  /opt/florr-auto-pathing/debug_screen_pos.py
```

它会截虚拟屏并标出识别到的位置。对不上就说明要重新标坐标, **不要**直接开着跑。

想亲眼看:
```bash
sudo systemctl start florr-vnc.service
ssh -L 5900:127.0.0.1:5900 你@服务器     # 本地 VNC 连 127.0.0.1:5900
```
VNC 口无认证, 靠 `-localhost` + SSH 隧道兜底, 别改成监听 0.0.0.0。

## 这台机器上没有的功能

| 功能 | 状态 | 原因 |
|---|---|---|
| GUI 控制面板 | 用不了 | customtkinter 要 X; 无头机上请在别处配好 config.json 拷过来 |
| 时块调度 / 自动换账号 | 用不了 | 那是 GUI 里的调度器(`gui_app._enter_block`)。worker 只读 `config["active"]` 那一片 |
| 悬浮状态窗 | 自动降级 | `overlay.create_overlay()` 在非 mac/win 上返回 `_NullOverlay`, 状态只进 journalctl |
| AFK 弹窗自动处理 | 没有 | `afk_watch` 靠 `segment.exe`, Windows 二进制。`sys.platform != "win32"` 时整段跳过 |

## 排障

**`--launch-chrome` 报"等了 60s 没等到 florr.io 标签页"**
```bash
systemctl status florr-xvfb florr-wm            # 两个都得是 active
sudo -u florr DISPLAY=:99 google-chrome --version
```
Chrome 崩在启动阶段最常见的两个原因: 以 root 跑(应该用 `florr` 用户)、`/dev/shm`
太小(容器里才会遇到, 裸机默认是内存的一半)。

**worker 起来就退, 退出码 0**
以前的坑: worker 把"stdin 管道被关"当停止信号, 而 systemd 默认把 stdin 接
`/dev/null`(一读就 EOF)。`main._install_worker_stdin_watcher()` 现在只在 stdin
确实是管道时才装监视线程。如果你改过 unit 里的 `StandardInput=`, 改回 `null`。

**鼠标动了但游戏没反应**
确认 worker 跟 Chrome 在同一个 `DISPLAY` 上。两个 unit 都写死 `DISPLAY=:99`,
手动跑命令时别忘了带。

**装依赖时 numpy 报 `Unknown compiler(s): cc/gcc/clang`**

这不是缺编译器, 是**解释器版本挑错了**。`requirements.txt` 锁了 `numpy<2`(和
`opencv-python<5` 绑在一起), 解析出来是 numpy 1.26.4 —— 2024-02 发的, 比 Python
3.13 早, 所以没有 cp313 轮子。而 Ubuntu 24.10 起默认 `python3` 就是 3.13, pip
找不到轮子就退回去从源码编译。

补 `build-essential` 能让它编过, 但那是错的方向: opencv 从源码编要几十分钟, 小
内存机器上还会 OOM。正确做法是用 Python 3.12 建 venv —— `setup.sh` 的
`pick_python()` 会自动挑, 挑不到就装 `python3.12`。直接重跑脚本即可:

```bash
sudo bash deploy/ubuntu/setup.sh
```

`pip install` 带了 `--only-binary=numpy,opencv-python`, 所以以后再遇到轮子缺失会
当场报错退出, 不会再安静地掉进几十分钟的源码编译。

如果发行版源里没有 python3.12(25.x 之类):
```bash
sudo add-apt-repository -y ppa:deadsnakes/ppa && sudo apt-get update
sudo bash deploy/ubuntu/setup.sh
```

**Chrome 进程杀不干净**
`cdp_bridge._quit_all_chrome()` 在 Linux 上走 `pkill "^(chrome|chromium)$"` ——
只匹配**进程名**, 不加 `-f`。加 `-f` 会拿整条命令行去匹配, 而
`python main.py --launch-chrome` 里就带着 "chrome" 这个词, 第一个被杀的是我们自己。

**Web 面板打不开 / 显示证书错误**

```bash
systemctl status caddy florr-webctl   # 两个都得是 active
journalctl -u caddy -n 50             # 证书申请失败常见原因: 80/443 端口被占,
                                       # 或者 nip.io 域名解析还没生效(等几分钟)
```

**面板上"对端"卡片一直显示"无法连接"**

先确认两台的密码是不是同一个(`/etc/florr-webctl/password` 内容要一致)，再确认
`/etc/systemd/system/florr-webctl.service` 里的 `FLORR_WEBCTL_PEER_URL` 填对了
对方的地址，改完记得 `daemon-reload` + `restart florr-webctl`。
