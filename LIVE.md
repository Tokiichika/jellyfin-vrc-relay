# OBS → VRChat 直播（2.9.0）

本版提供实时直播：电脑用 OBS 推流到云端，观众从云端观看。**不录制、不提供回放、不支持拖动到过去的画面**，也不把直播写入点播缓存。点播功能保持原来的缓存和进度控制。

## 传输方式

```text
OBS（H.264 + AAC）
  └─ RTMP / TCP 1935 → MediaMTX（内部媒体引擎）
                         └─ RTSP / TCP → Relay 直播限速网关
                                           └─ TCP 8554 → VRChat PC 播放器
```

管理页面仍使用原来的 HTTPS 域名。直播走独立 TCP 端口，不经过网站的 Nginx `location /`。本版不提供 HLS、WebRTC、RTMP 拉流或 RTSP UDP 播放入口，避免产生绕过网关限速和清退的观看通道。MediaMTX 的 RTSP 端口与管理 API 仅在 Docker 内部开放。

VRChat 使用 `rtspt://主机:8554/live/随机标识`，表示 RTSP over TCP，**不是 TLS 加密**。不同世界、视频播放器组件的支持情况不同；需要支持这种协议的 PC 播放器，可能还需在 VRChat 中允许不受信任的视频 URL。普通浏览器无法直接播放这个地址。VLC 中可使用 `rtsp://` 地址并强制 RTP over RTSP (TCP) 测试。

## 安装 / 更新

新部署运行 `sudo bash install.sh`；已有部署按 [UPDATE.md](UPDATE.md) 从独立更新包升级。脚本会：

1. 保留原管理密钥、配置、Compose 项目和视频数据。
2. 为直播生成独立内部鉴权密钥，写入 `config/.env`；生成 `config/mediamtx.yml`。
3. 在现有 `compose.yaml` 上叠加 `compose.live.yaml`，启动固定版本 `bluenviron/mediamtx:1.21.1`。
4. 检查点播管理服务健康状态，以及直播引擎连接状态。

升级新增直播容器和端口映射，会重建 relay；请选无人观看时操作。现有 `compose.override.yaml` / `.yml` 也会加载，但 `compose.live.yaml` 最后应用。自定义网络必须让 relay 和 mediamtx 能以服务名互相访问；已有同名 mediamtx 服务或端口占用应先解决。

**在云安全组和服务器防火墙中放行 TCP 1935、8554。** 1935 可进一步只允许自己的推流 IP，8554 面向观看端。不要把 Docker 内部 9997 管理 API 或 MediaMTX 内部 RTSP 另行映射到公网。

域名必须直连服务器。网站可以使用独立的代理/CDN，但本版直播地址不能经过仅支持 HTTP 的 CDN/ESA。需要时设置 `LIVE_PUBLIC_HOST` 为另一个直连域名或公网 IP；建议使用服务器公网 IPv4 的 A 记录。网关按 TCP 连接的来源 IP 分配额度，在前面添加 TCP 代理会导致多个观众合并为代理 IP；本版不支持 PROXY protocol。

部署后统一用包装脚本管理两个服务：

```bash
sudo bash compose.sh ps
sudo bash compose.sh logs --tail=100 relay mediamtx
sudo bash compose.sh up -d --build
```

不要只执行原来的 `docker compose up`，它不会自动加载命名为 `compose.live.yaml` 的叠加文件。使用自定义项目名时，所有命令继续使用原 `COMPOSE_PROJECT_NAME`。

## OBS 与管理页

1. 登录后点击导航的“直播管理”，创建直播间。点播、直播、设置共用一次登录，无需重复输入密钥。
2. 展开直播间的“OBS 连接信息与推流密钥”。OBS → 设置 → 直播 → 服务选择“自定义”。将 **OBS 服务器**填入服务器栏，将 **OBS 串流密钥**完整填入串流密钥栏，包含 `?user=...&pass=...` 部分。
3. OBS 输出 **H.264 视频 + AAC 音频**。建议起步使用 1080p、30 fps、CBR 6000 kbps、AAC 192 kbps、关键帧间隔 2 秒；可根据线路降低到 720p。不要使用 HEVC、AV1 或高码率无损输出。
4. 开始推流，管理页显示“正在直播”后，复制播放地址到房间播放器。直播间的地址在服务重启后仍有效，不需要每场重新创建。

服务器只负责协议转换与转发，**不执行转码**，直播也不调用 NAS。编码能力取决于 OBS 所在电脑。收到媒体轨道不代表格式适合 VRChat；若有画面无声音，先确认 OBS 音轨为 AAC，若卡顿则检查 H.264、输出码率、网络和房间缓冲设置。

## 独立限速与观看控制

在“设置 → 直播带宽”配置直播预算，写入 `config/.env` 后即时生效：

| 配置 | 默认值 | 含义 |
| --- | --- | --- |
| `LIVE_TOTAL_MBPS` | 100 | 直播独立总带宽预算，Mbps |
| `LIVE_UTILIZATION` | 80 | 可使用的预算比例，% |
| `LIVE_CLIENT_MBPS` | 16 | 每出口 IP 上限，Mbps；0 表示只动态均分 |

默认直播可用出口为 80 Mbps，最多准入 8 个不同出口 IP。新 IP 加入后，如果均分额度将低于 10 Mbps，则拒绝新连接，保留原有观众。同一 IP 下所有直播间的连接共享该 IP 的限额；人数按出口 IP 统计，包含正在加载的连接，不等于 VRChat 玩家数。连接断开后释放名额。网关另有 64 条并发 TCP 连接的保护上限。

10 Mbps 是**本服务的准入预算下限**，不能保证客户端线路真的达到这个速度。同 IP 下多人、多直播间同时观看可能超过共享额度。OBS 视频加音频的实际总码率应明显低于最小观看额度；如果输入速度长期超过限额，TCP 背压会增大延迟，最终可能断流，不会自动降低清晰度。短时统计受采样、协议开销和缓冲影响，显示值不一定精确等于 OBS 目标码率。

点播和直播预算**相互独立，不自动借用或合并**。默认点播为 200 × 80% = 160 Mbps，直播为 100 × 80% = 80 Mbps；同时满载会超过 200 Mbps 服务器。请按实际容量分配，例如 200 Mbps 出口同时使用时，可把点播预算设为 100 × 80% = 80 Mbps，直播也设为 100 × 80% = 80 Mbps，给其他流量留余量。若只用其中一种，可为它单独分配更多。

- **清退并暂停全部直播观看**：断开直播网关观众，拒绝新观看，OBS 继续推流。点播不受影响。手动恢复后才允许重连。
- **直播间清退并暂停观看**：仅影响该房间；全局暂停时，单房间恢复仍不能观看。
- **停用直播间**：禁止新推流和观看，同时断开现有推流及观众。重新启用后需让 OBS 重新推流。
- **重置推流密钥**：断开当前 OBS，旧推流密钥失效，需在 OBS 更新。播放地址保持不变。
- **删除直播间**：推流与播放入口失效。若播放地址泄露，可删除并重新创建直播间。

暂停及房间信息保存在数据卷的 `live.json`，重启保留。清空点播列表/缓存不会删除直播间。清退无法撤回客户端已经缓冲的画面，也不会把玩家踢出 VRChat 房间。

## 端口与部署配置

手动编辑 `config/.env` 后重跑 `sudo bash install.sh --non-interactive`：

```dotenv
# 留空时使用 PUBLIC_BASE_URL 中的主机名；不填协议、路径或端口。
LIVE_PUBLIC_HOST=''
LIVE_RTMP_PORT=1935
LIVE_RTSP_PORT=8554
```

这两个端口是宿主机公开端口，容器内仍使用 1935、8554。不能相同，范围 1024–65535；修改后同步放行防火墙、更新 OBS 和播放器地址。`LIVE_INTERNAL_TOKEN` 自动生成，请勿分享；修改它同样需重跑脚本，让引擎配置与服务环境一致。

RTMP / RTSP TCP 是明文传输，本版未配置 RTMPS / RTSPS。播放地址是观看凭证，OBS 串流密钥是发布凭证；不要把它们与管理密钥、配置文件或调试抓包公开发布。直播页面显示的凭据仅在管理鉴权后返回。第三方 MediaMTX 镜像的许可及相关说明由其上游项目提供。

## 排查与验收

先确认引擎在线，再确认 OBS 推流有轨道和输入速率，最后测试观看：

- 引擎未连接：执行 `sudo bash compose.sh ps` 和日志命令，检查配置文件、服务名网络与内部鉴权。
- OBS 连不上：检查直连域名、1935 TCP、串流密钥完整性以及直播间是否停用。
- VRC 连不上：检查 8554 TCP、`rtspt://`、房间播放器支持、全局/房间暂停与准入容量。
- 推流正常但延迟高：降低 OBS 码率、确认输出 H.264/AAC，检查观看侧共享 IP、出口预算和播放器缓冲；本版不承诺固定延迟。
- 调试日志在点播页查看；设置中开启调试后会增加连接诊断，默认不记录完整地址和密钥。

本版已在 Windows 本机使用官方 MediaMTX 1.21.1 与 FFmpeg 完成 H.264/AAC RTMP 推流、RTSP TCP 解码、清退暂停、推流密钥轮换测试。**尚未在真实 Ubuntu Docker、OBS GUI 或 VRChat 世界中完成验收**；发布前请用自己的服务器和播放器验证，尤其是 Docker 端口、升级保留、吞吐量和断流重连。

上游参考：[MediaMTX OBS 设置](https://mediamtx.org/docs/publish/obs-studio)、[鉴权](https://mediamtx.org/docs/features/authentication)、[配置](https://mediamtx.org/docs/references/configuration-file)。
