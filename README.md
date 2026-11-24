# MonitorPanel — zzj3 服务器监控面板（安卓壳 + 服务端源码）

DIY 轻量主机监控面板：服务端 Python 标准库零依赖采集 `/proc` 指标，前端单文件 HTML（宝塔绿主题）；安卓端是一个 WebView 壳工程，打开即加载面板。

## 仓库结构

```
629/
├── app/                              # 安卓 WebView 壳工程模块
│   ├── build.gradle
│   ├── proguard-rules.pro
│   └── src/main/
│       ├── AndroidManifest.xml
│       ├── java/com/lyh/monitorpanel/MainActivity.java
│       └── res/                      # 布局、主题、颜色、图标
├── panel-src/                        # 服务端面板源码（部署在 zzj3）
│   ├── app.py                        # 采集 + HTTP 服务（标准库，无 pip 依赖）
│   └── index.html                    # 前端页面，3 秒自动刷新
├── settings.gradle
├── build.gradle
├── gradle.properties
└── gradle/wrapper/gradle-wrapper.properties
```

## 一、编译安卓 APK

### 环境要求
- Android Studio（Hedgehog 2023.1 或更新版本均可）
- JDK 11 及以上（Android Studio 自带）
- 已安装 Android SDK Platform 33、Build-Tools 33.0.x

### 技术版本
| 项 | 版本 |
|----|------|
| AGP | 7.4.2 |
| Gradle | 7.6.4 |
| compileSdk / targetSdk | 33 |
| minSdk | 21（Android 5.0） |
| 外部依赖 | 无（仅系统框架 API） |

### 步骤
1. `git clone` 本仓库，Android Studio 选择 **Open** 打开 `629` 目录。
2. 等待 Gradle Sync 完成（首次会自动下载 Gradle 7.6.4 与 SDK 依赖）。
3. 菜单 **Build → Build Bundle(s) / APK(s) → Build APK(s)**。
4. 生成的 APK 在 `app/build/outputs/apk/debug/app-debug.apk`，传到手机安装即可。

### 云端编译（GitHub Actions，无需本地 Android Studio）
- 工作流：`.github/workflows/build-apk.yml`
- 推送 `main` 或在 Actions 页手动触发（`workflow_dispatch`）后，runner 自动装 SDK、用 Gradle 7.6.4 编译 debug APK。
- 构建完成后，在该次运行页面底部 **Artifacts** 下载 `monitor-panel-debug`，解压即得 `app-debug.apk`（artifact 保留 30 天）。
- 也可在仓库页 **Actions → Build Android APK → Run workflow** 手动重跑。

### 修改面板地址 / 密钥
打开 `app/src/main/java/com/lyh/monitorpanel/MainActivity.java`，改顶部常量：

```java
private static final String PANEL_URL =
        "http://zzj3.wch1.top:32304/?token=LaVORht8oo3xLUOD";
```

> 面板为 HTTP 明文，已在 `AndroidManifest.xml` 开启 `usesCleartextTraffic`；如改成 HTTPS 可去掉该项。

## 二、服务端面板（panel-src）

### 当前部署（zzj3）
- 目录：`/opt/monitor-panel/`
- 服务：`monitor-panel.service`（systemd，开机自启，崩溃自动重启）
- 内网监听：`8899`；公网映射：`32304 → 8899`
- 面板地址：`http://zzj3.wch1.top:32304/?token=LaVORht8oo3xLUOD`

### 运维命令（SSH 到 zzj3 后）
```bash
systemctl status monitor-panel      # 状态
systemctl restart monitor-panel     # 重启
journalctl -u monitor-panel -f      # 日志
```

### 重新部署服务端
把 `panel-src/app.py`、`panel-src/index.html` 上传到 `/opt/monitor-panel/`，然后：

```bash
systemctl restart monitor-panel
```

### 采集指标
CPU 使用率/核数/负载、内存/缓存/Swap、磁盘分区用量、网卡上下行速率与累计、进程 Top8（按 CPU）、Docker 运行容器列表、主机名/系统/内核/运行时间。

## 三、监控对象
- 主机：`zzj3.wch1.top`（公网 IP `222.186.57.226`，SSH 端口 `32614`）
- 系统：Ubuntu 24.04.3 LTS，2 核 / 1.9G 内存 / 26G 磁盘
