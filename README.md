# MoviePilot 第三方插件仓：首字母归档（PinyinSorter）

> GitHub 仓库：<https://github.com/qydxd1/MoviePilot-Plugins>（第三方插件市场，可直接在 MoviePilot 里添加）

MoviePilot v3 插件：**整理完成后把媒体目录按标题首字母归档到库根下的字母目录，并刷新 Emby / Jellyfin / Plex**。

配合 qBittorrent 使用时整条链路是：

```
磁力链接 → qBittorrent 下载 → MoviePilot 识别/分类/重命名/搬移 → transfer.complete 事件
        → 本插件把 <库根>/<标题 (年份)> 搬到 <库根>/<首字母>/<标题 (年份)> → 刷新媒体库
```

如果打开「下载直达字母目录」，下载器那一跳就会把文件放进 `<基准目录>/<首字母>/`：
搜索/订阅下载本来就带保存目录（如 `/Media/动画电影`），插件直接在其后追加首字母；
只有调用方没指定保存目录时，才用「直达父目录」模板（`/Media/{category}` = 按类别分流）：

```
磁力链接 → resource.download 事件（本插件改写 save_path）→ qBittorrent 直接下到 /Media/动画电影/J/
        → MoviePilot 整理（同盘 rename）→ 本插件归档到 /Media/动画电影/J/<标题 (年份)>/ → 刷新媒体库
```

如果打开「下载时规范命名」，任务一提交就会改名成规范名（边下边是规范名，不用等下完再整理）：

```
磁力链接 → qBittorrent 拿到元数据 → 本插件把任务名改成 进击的巨人 (2013)、
           电影文件放进 <标题 (年份)>/、剧集放进 Season 01/（字幕跟随）
           → 下载完成 → MoviePilot 整理 → 归档 → 刷新
```

MoviePilot v3 自身在整理完成后不再主动刷新媒体库（依赖 Emby 实时监控兜底），
本插件补上「搬移后立刻刷新」这一步。

---

## 目录结构

```
moviepilot-local-plugins/
├── package.v3.json                       # 本地插件仓索引（v3 宿主读这个）
├── plugins.v3/
│   └── pinyinsorter/                     # 目录名 = 插件类名小写
│       └── __init__.py                   # 插件本体（类名 PinyinSorter = 插件 ID）
└── tests/
    └── v3/pinyinsorter/
        └── standalone_selftest.py        # 离线自测（桩模块替代宿主，零依赖）
```

插件按官方 [V3 插件开发指南](https://github.com/jxxghp/MoviePilot-Plugins/blob/main/docs/Plugin_Development.md) 编写：
`package.v3.json` 的 `version` 与历史版本、插件类 `plugin_version` 三处一致（1.8.1），
`history` 当前版本在最上面；只从 `app.sdk.*` 导入；外部 API 声明 `auth: "apikey"`，
页面按钮用 `auth: "bear"`；**零第三方依赖**（不需要 `pip install` 任何东西）。

---

## 一、前置条件

| 项 | 要求 |
| --- | --- |
| MoviePilot | **v3**（宿主版本 ≥ 3.0.0） |
| 下载器 | qBittorrent 或任意 MoviePilot 支持的下载器，已配置可用 |
| 媒体服务器 | 已在 MoviePilot「设置 → 媒体服务器」里配置并**启用** Emby / Jellyfin / Plex |
| 媒体库 | Emby 媒体库根路径 = 插件里填的「库根目录」，例如 `/media/movies`，**不要**指向单个影片目录 |

> 插件里填的路径必须是 **MoviePilot 容器内路径**（和「设置 → 目录」里的一致），不是宿主机路径。

---

## 二、安装

### 方式 A：GitHub 插件市场（推荐，可在线更新）

1. MoviePilot「设置 → 系统设置 → 插件 → 插件市场地址」（`PLUGIN_MARKET`）追加：

   ```
   https://github.com/qydxd1/MoviePilot-Plugins
   ```

   多个地址用英文逗号分隔。

2. 保存后刷新插件市场，搜「**首字母归档**」→ 安装。
3. 建议同时配置 `GITHUB_TOKEN`（MoviePilot 设置里的同名变量），避免访问 GitHub 被限流。
   插件本身**零第三方依赖**，无需 `PIP_PROXY`。

### 方式 B：本地插件仓（离线/调试用）

1. 把整个 `moviepilot-local-plugins` 目录放到宿主机，并挂载进容器。例如 docker-compose：

   ```yaml
   services:
     moviepilot:
       volumes:
         - ./local-plugins:/config/local-plugins        # 新增这一行
   ```

2. 让 MoviePilot 认识这个本地插件仓。二选一：

   - **界面**：设置 → 系统设置 → 插件（`PLUGIN_LOCAL_REPO_PATHS`），填 `/config/local-plugins`；
   - **env**：容器环境变量 `-e PLUGIN_LOCAL_REPO_PATHS=/config/local-plugins`。

   多个仓库用逗号分隔。

3. 保存后重启 MoviePilot（或等配置热重载），打开「设置 → 插件」→ 本地插件/插件市场里
   会出现 **首字母归档**，点安装。安装后会落到容器内 `/app/app/plugins/pinyinsorter/`。

4. 安装完成后启用插件、填写配置、保存。

> 也可以不走本地仓：把 `plugins.v3/pinyinsorter/` 直接拷进容器 `/app/app/plugins/pinyinsorter/`，
> 但这样不会登记进「已安装列表」，宿主重启后可能不加载；本地插件仓安装最稳。

5. （可选）跑一遍离线自测，确认插件逻辑在你机器上也正常：

   ```bash
   python tests/v3/pinyinsorter/standalone_selftest.py
   ```

   输出 `ALL CHECKS PASSED` 即通过。它用桩模块替代 MoviePilot 宿主，不碰你的媒体库，
   只在临时目录里建几个假目录验证「算字母 → 搬目录 → 冲突跳过 → 刷新调用」。
   （在只读/受限沙箱里跑时，用 `PINYINSORTER_SELFTEST_DIR=<可写目录>` 指定临时目录。）

---

## 三、配置项

| 配置 | 说明 |
| --- | --- |
| 启用插件 | 总开关。关闭后事件处理直接跳过，不搬任何文件 |
| 电影库根目录 | 每行一个，如 `/media/movies`；命中多个时取**最长前缀** |
| 剧集库根目录 | 每行一个，如 `/media/tv`；按**剧名**首字母归档到 `<tv根>/<字母>/<剧名 (年份)>/` |
| 首字母来源 | `中文标题`（转拼音首字母，默认）/ `英文标题`（`en_title` 为空时回退中文标题） |
| 数字/符号归类目录 | 标题以数字/符号开头时归到这里，默认 `#` |
| 整理方式 | `移动`（默认，省空间）/ `复制`（保留源文件，适合继续做种） |
| 归档后刷新媒体库 | 默认开。搬完调用媒体服务器刷新 |
| 刷新方式 | `整库扫描`（推荐：目录搬家后旧路径失效，整库扫描会把旧条目清掉）/ `仅刷新该条目` |
| 发送通知 | 归档完成后通过 MoviePilot 通知渠道发一条消息 |
| 立即运行一次 | 打开并保存后，后台立刻扫描媒体库根目录归档一次，执行完自动关闭 |
| 下载直达字母目录 | 开：下载器直接下到 `<基准目录>/<首字母>/`（有保存目录就在其后追加，否则用下面的父目录）；关：下载仍进普通下载目录，整理后再归档 |
| 直达父目录 | 仅当调用方没指定保存目录时使用；绝对路径，支持 `{category}`/`{type}` 模板。想要「按类别 + 首字母」就填 `/Media/{category}`；**渲染后必须是目录设置里的下载目录（或其子目录）**，否则本次下载会被拒 |
| 直达生效类别 | 模糊匹配识别出的媒体类别，如 `动漫`；逗号/顿号分隔多个，留空表示所有下载都改写 |
| 下载时规范命名 | 开：搜索/订阅下载一提交，就把下载器里的任务名改成 `标题 (年份)`、电影文件放进 `<标题 (年份)>/`、剧集文件放进 `Season 01/标题 - S01E01.mkv`（字幕跟随视频名并保留语言后缀），边下边用规范名落盘；手动粘贴的磁力链接没有识别结果，保持原名 |
| 已有任务迁移 | 开（默认）：同一磁力链接已经在下载器里时，qBittorrent 会复用旧任务、不理会新目录；插件会把该任务「设置位置」搬到本次期望的字母目录，并记到状态页 |

> 插件详情页顶部还有三个按钮：**立即运行一次**（后台扫描 + 刷新）、**预览扫描**（只列计划）、
> **刷新媒体库**，不用敲命令就能手动触发。

### 归档结构

```
/media/movies/
├── 长安三万里 (2023)/        ← MoviePilot 整理出的原始位置
└── C/
    └── 长安三万里 (2023)/    ← 本插件搬到这里
        └── 长安三万里 (2023) - 2160p.mkv
```

剧集（按剧名归档，季目录保持在剧目录内）：

```
/media/tv/S/三体 (2023)/Season 1/...
```

---

## 四、首次使用：把旧库也按首字母整理（可选）

插件只处理「之后新整理完成」的媒体；已有媒体可以手动整库迁移，三种方式任选：

### 方式一：插件页按钮（最简单）

打开「设置 → 插件 → 首字母归档」，详情页顶部点：

- **预览扫描** → 只列计划，不动文件；
- **立即运行一次** → 后台扫描 + 归档 + 刷新媒体库（立即返回，跑完看日志/记录）；
- **刷新媒体库** → 只刷新，不搬文件。

也可以在配置页打开「立即运行一次（保存后执行）」再保存，效果等同点按钮。

### 方式二：扫描接口（可预览）

```bash
# 先看会搬什么（dry_run=true 不动文件）
curl -X POST "http://<moviepilot地址>/api/v1/plugin/PinyinSorter/scan?dry_run=true" \
  -H "X-API-KEY: <API_TOKEN>"

# 确认无误后执行
curl -X POST "http://<moviepilot地址>/api/v1/plugin/PinyinSorter/scan?dry_run=false" \
  -H "X-API-KEY: <API_TOKEN>"
```

只刷新媒体库、不搬文件：

```bash
curl -X POST "http://<moviepilot地址>/api/v1/plugin/PinyinSorter/refresh" \
  -H "X-API-KEY: <API_TOKEN>"
```

`<API_TOKEN>` 是 MoviePilot 容器的 `API_TOKEN` 环境变量值（docker 启动参数 `-e API_TOKEN=...`）。

---

## 五、安全与边界

- **只搬库根下的一级媒体目录**：`<库根>/<标题 (年份)>`；剧集先退掉 `Season n` 得到剧目录。
- 如果库里启用了「分类目录」（如 `<库根>/国剧/<剧名>/Season 1`），插件检测到多级结构会
  **跳过并写日志**，不会把分类目录整个搬走；这种库建议关掉分类目录，或调整库根配置。
- 目标字母目录下**已存在同名目录时跳过**，不覆盖、不合并。
- 媒体目录不在任何配置的库根下面 → 跳过。
- 失败（权限、跨盘、目标被占用）记为 `failed` 并写日志，不影响 MoviePilot 自身流程。
- 同一库根内搬移是 `rename`；跨文件系统时 `shutil.move` 会退化为「复制 + 删除」，
  大库跨盘会很慢，建议库根与下载目录同盘。

## 六、卸载

在「设置 → 插件」里卸载/停用即可（或删除容器内 `/app/app/plugins/pinyinsorter/` 并重启）；
已归档的目录结构不受影响。

---

## 七、排错

| 现象 | 排查 |
| --- | --- |
| 插件市场里看不到「首字母归档」 | `PLUGIN_LOCAL_REPO_PATHS` 是否填对；目录下是否有 `package.v3.json` 和 `plugins.v3/pinyinsorter/__init__.py` |
| 整理完成后没归档 | 插件是否启用；库根是否填了**容器内**路径；日志搜 `[PinyinSorter]` |
| 日志提示「检测到分类目录/多级结构」 | 库里启用了分类目录，关掉它或调整库根 |
| 归档成功但 Emby 没更新 | 媒体服务器是否在 MoviePilot 启用；刷新方式改「整库扫描」；Emby 库路径是否指向库根 |
| 标题首字母不对 | 插件按内置 GB18030 表取**单字默认读音**，多音字可能不是你想要的（「长安」→ C，不会按词组判成 Z）；生僻字超出 GB18030 扩展 B 时落兜底分组。可把「首字母来源」改为英文标题，或改走手动扫描后人工挪一次 |
| 搬完做种报错 | 用「复制」模式，或关掉 qB 的「移动后重新校验」 |
