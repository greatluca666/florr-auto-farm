# 官网 florrfarm.cc.cd 的安装包镜像

国内直接从 GitHub 下载安装包慢且时常打不开, 所以由这台服务器从 GitHub Releases
把包镜像下来, 前面套 Cloudflare 代理缓存。设计见
`docs/superpowers/specs/2026-09-26-official-site-and-auto-update-design.md`(只在私有仓库里)。

```
GitHub Releases ──(每 10 分钟, mirror_sync.py)──> /var/lib/florrfarm/{latest.json, download/}
公开仓库 site/  ──(同一次同步, git 稀疏检出)────> /var/lib/florrfarm/site-repo/site
浏览器 / 程序 ──> Cloudflare ──> Caddy(sites.d/florrfarm.caddy)
```

服务器不持有任何密钥; 安装包的真实性靠程序验证 Ed25519 签名(私钥只在 GitHub secret 里)。

## 安装

```bash
sudo bash deploy/mirror/install.sh
```

可以重复执行。需要先跑过 `deploy/ubuntu/setup.sh`(装 Caddy)。

## Cloudflare(一次性, 在网页上操作)

1. 注册 Cloudflare, 添加站点 `florrfarm.cc.cd`, 选 Free 计划。
2. 在 DNSHE 把 `florrfarm.cc.cd` 的 NS 改成 Cloudflare 给的两个。
3. Cloudflare → DNS: 加 A 记录, 名称 `florrfarm.cc.cd`, 内容是这台服务器的公网 IP, 代理状态打开(橙色云朵)。
4. Cloudflare → SSL/TLS: 模式选 **Full (strict)**; **「Always Use HTTPS」保持关闭** ——
   它会在边缘把 Let's Encrypt 的 HTTP 验证请求也重定向掉, Caddy 的证书就续不了期。HTTP→HTTPS 由 Caddy 自己跳。

## 日常

- 看同步日志: `journalctl -u florr-mirror -n 50`
- 立刻同步一次(不等 10 分钟): `sudo systemctl start florr-mirror`
- 定时器状态: `systemctl list-timers florr-mirror.timer`

镜像只挑「正式版」: 已发布(不是草稿/预发布)、tag 是 `vX.Y.Z`、同时带
`florr-auto-farm-vX.Y.Z-win64.zip` 和 `.zip.sig`。不符合的 Release 直接跳过。
