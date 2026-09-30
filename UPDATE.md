# 2.9.0 更新说明

新增 OBS 实时直播、独立直播管理视图、带宽控制与清退功能。点播、直播、设置共用登录和导航。无录制、回放或直播进度拖动；完整说明见 [LIVE.md](LIVE.md)。

## 2.9.0 r1 点播修订

“是否重新编码”后可选择 H.264 / HEVC（H.265）。烧录字幕不再自动勾选或改成 H.264；需手动开启重新编码。设置新增默认视频编码，未配置时仍使用 H.264。已有播放链接与缓存保持原编码，可从已安装的 2.9.0 升级本修订。

## 新部署

解压 `jellyfin-vrc-relay-v2.9.0-r1-full.zip`，进入 jellyfin-vrc-relay 目录：

```bash
sudo bash install.sh
```

脚本自动生成管理配置和直播内部鉴权，启动 relay 与 MediaMTX，检查服务。配置网站 HTTPS 反向代理后，还需放行 TCP 1935（OBS）和 8554（直播观看）。无需手动改配置模板名称或运行迁移脚本。

## 从 2.5.1 / 2.6.x / 2.7.x / 2.8.x / 2.9.0 更新

解压 `jellyfin-vrc-relay-v2.9.0-r1-upgrade.zip` 到独立临时目录，**不要先覆盖原目录**。在更新包目录执行：

```bash
sudo bash update.sh /opt/jellyfin-vrc-relay
```

请换成自己的原部署目录。脚本先备份被替换的程序文件到原目录 `.upgrade-backups/时间戳/`，再重建原服务。保留 `compose.yaml`、反向代理配置、管理密钥、视频列表和缓存卷；会向 `config/.env` 添加直播参数与随机内部密钥，生成 `config/mediamtx.yml`。如同名文件是手工配置，脚本拒绝覆盖并给出提示。

新增 `compose.live.yaml` 在原 compose.yaml 和可选 compose.override.yaml/yml 后加载，新增 mediamtx 服务、1935/8554 公开端口，并为 relay 增加直播网关配置。自定义 Compose 网络需确保两个服务互通，端口不能与已有服务冲突。升级会重启服务，请选无人观看时操作。

若原部署通过 1Panel 或 `-p` 使用了自定义项目名，继续传入原名：

```bash
sudo env COMPOSE_PROJECT_NAME=原项目名 bash update.sh /原部署目录
```

版本从原 app.py 的字面量版本字段读取，不依据网页缓存、压缩包名称或 Docker 镜像标签；支持符合 config/.env 布局的 2.5.1、2.6.x–2.9.x。缺少关键程序文件、版本冲突或配置结构不符时，在覆盖前拒绝升级。

## 更新后检查

1. 登录确认 v2.9.0，原视频、缓存和复制记录仍存在；点播播放、预载及排序正常。
2. 切换“直播管理”和“设置”，确认无需再次登录；刷新仍可恢复登录。
3. 确认直播引擎在线，创建直播间，OBS 填写服务器与串流密钥，输出 H.264/AAC。
4. 放行 TCP 1935/8554，用支持 RTSP TCP 的 VRC PC 播放器测试 rtspt 地址。
5. 测试直播暂停/恢复与点播独立，重置推流密钥后更新 OBS；不要用正在正式观看的直播间测试删除。
6. 在设置中重新分配直播/点播预算。默认点播有效 160 Mbps、直播有效 80 Mbps，同时满载超过 200 Mbps 出口；例如分别设为 80 Mbps 有效预算并留余量。

后续运维使用：

```bash
sudo bash compose.sh ps
sudo bash compose.sh logs --tail=100 relay mediamtx
sudo bash compose.sh up -d --build
```

不要执行 `down -v`，不要改变原项目名或缓存卷名。手动修改直播端口、主机或内部密钥后，执行 `sudo bash install.sh --non-interactive`。

## 失败与回退

失败时不会删除配置或缓存，也不会报告更新成功。先查看上述两个服务日志，检查端口冲突、镜像下载、内部网络和配置。

回退到 2.8.x 或更早版本：先在原目录执行 `sudo bash compose.sh stop mediamtx` 停止直播引擎；把脚本打印的备份目录中的程序文件覆盖回原目录，然后执行旧版 `sudo bash install.sh --non-interactive`，使 relay 按原 compose.yaml 重建并去掉直播端口。新增 compose.live.yaml 不会被旧版安装脚本自动加载。此后按旧版文档运维，不要运行新版 compose.sh 启动直播叠加配置。若使用自定义项目名，各步均保留它。

新增直播配置字段与 live.json 可以保留，不影响旧版读取；备份只包含程序，不包含配置与数据，不能代替部署备份。新版本清退的直播在旧版中不可用；原点播暂停状态仍由对应版本行为决定。

## 已验证与待验收

本地单元/回归测试、浏览器切换与登录测试，以及官方 MediaMTX 1.21.1 + FFmpeg 的 RTMP 推流 / RTSP TCP 解码、暂停清退、推流密钥轮换验证。Docker 构建、Ubuntu 一键部署、真实 OBS GUI 与 VRChat 世界尚需部署后验收。
