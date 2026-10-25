# 首字母归档（PinyinSorter）

MoviePilot v3 插件。订阅 `transfer.complete` 事件，在 MoviePilot 完成「识别 → 分类 → 重命名 → 搬移」
之后，把媒体单元目录按标题首字母搬到 `<库根>/<字母>/<标题 (年份)>/`，再刷新 Emby / Jellyfin / Plex 媒体库。

```
磁力 → qBittorrent → MoviePilot 整理 → transfer.complete
     → 本插件搬进字母目录 → 刷新媒体服务器
```

## 依赖

无第三方依赖。首字母由插件内置的 GB18030 汉字表计算（42915 个汉字，含扩展 A/B 与兼容汉字，
约 50KB base64 压缩在源码里，首次使用时解压一次）。

## 类属性

| 属性 | 值 |
| --- | --- |
| 插件 ID | `PinyinSorter`（= 类名） |
| 名称 | 首字母归档 |
| 版本 | 1.5.0（与 `package.v3.json` 一致） |
| 入口 | `plugins.v3/pinyinsorter/__init__.py` |

## 下载直达字母目录

开启后插件订阅 `resource.download` 链式事件，在种子提交给下载器之前把保存目录改成
`<直达父目录>/<首字母>/`，下载任务从第一秒就落在字母目录里。

「直达父目录」支持模板占位符，按类别自动分流：

| 配置值 | 效果 |
| --- | --- |
| `/Media/{category}` | 动漫 → `/Media/动漫/J/`，电影 → `/Media/电影/G/` |
| `/Media/{type}` | 电影 → `/Media/电影/…`，电视剧 → `/Media/电视剧/…`（`{type}` 无「动漫」维度） |
| `/Media/动漫` | 固定一个父目录，不分类别 |

占位符取不到值时插件放弃本次覆盖并写日志（不会拼出 `/Media//J` 这种残缺路径），
保证下载至少还能按宿主默认目录走，而不是直接失败。

```
磁力 → resource.download（插件改写 save_path）→ qBittorrent 直接下到 /media/动漫/J/
     → MoviePilot 整理（同盘 rename）→ 本插件把媒体目录归档到 /media/动漫/J/<标题>/
```

- 只在 **调用方未显式指定保存目录** 时改写，订阅里手动选的目录优先，不覆盖；
- 「生效媒体类别」按识别出的类别做模糊匹配（默认 `动漫`），留空表示对所有下载生效；
- **直达父目录（渲染后）必须落在目录设置里某条「下载目录」范围内**，宿主的 `save_path` 校验会拒绝范围外的路径，
  本次下载会直接失败并提示「保存路径不在允许的下载目录范围内」，所以填之前先在
  MoviePilot「设置 → 目录」里确认该路径是下载目录（或它的子目录）；
- 整理阶段仍按媒体库目录归档一次（同盘是 rename，不产生拷贝），最终结构仍是 `<库根>/<首字母>/<媒体目录>/`；
  如果只是想最终落到字母目录、并不要求下载器路径本身带字母，用原来的 `transfer.complete` 归档即可。

## 立即运行一次

三种触发方式，效果相同（扫描媒体库根目录 → 归档散落目录 → 刷新媒体库）：

1. **插件页按钮**：打开插件详情页，点「立即运行一次」（后台执行，接口立即返回）；
   同页还有「预览扫描」（只列计划、不动文件）和「刷新媒体库」。
2. **配置开关**：设置页打开「立即运行一次（保存后执行）」并保存，插件初始化时提交一次后台扫描，
   然后自动把开关写回关闭。
3. **API**：`POST /api/v1/plugin/PinyinSorter/run_now`（浏览器登录态，`auth: bear`）。

后台任务优先交给宿主调度器（`app.sdk.scheduler.add_plugin_once_job`），
调度器不可用时退回独立线程；同一时间只允许一个扫描在跑。

## 配置项

| key | 类型 | 说明 |
| --- | --- | --- |
| `enabled` | bool | 总开关 |
| `movie_roots` | string（换行分隔） | 电影库根目录（**容器内路径**） |
| `tv_roots` | string（换行分隔） | 剧集库根目录 |
| `letter_source` | `title` / `en_title` | 首字母取中文标题还是英文标题 |
| `fallback_letter` | string | 数字/符号开头的归类目录，默认 `#` |
| `copy_mode` | `move` / `copy` | 移动或复制（复制适合继续做种） |
| `refresh` | bool | 归档后是否刷新媒体库 |
| `refresh_mode` | `library` / `item` | 整库扫描或只刷该条目 |
| `notify` | bool | 归档后是否通过 MoviePilot 通知渠道发消息 |
| `direct_letter` | bool | 下载直达：把下载器保存目录改成 `<直达父目录>/<首字母>` |
| `direct_letter_parent` | string | 直达父目录，绝对路径，支持 `{category}`/`{type}` 模板（例：`/Media/{category}`），渲染后须在下载目录范围内 |
| `direct_letter_categories` | string | 生效媒体类别（模糊匹配，`、`/逗号分隔，留空=全部） |
| `onlyonce` | bool | 保存后立即运行一次；执行后自动关闭 |

## API

外部脚本（`auth: apikey`，请求头 `X-API-KEY: <API_TOKEN>`）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/plugin/PinyinSorter/records` | 最近 200 条归档记录 |
| POST | `/api/v1/plugin/PinyinSorter/scan?dry_run=true` | 扫描现有库并预演搬移（默认只预览） |
| POST | `/api/v1/plugin/PinyinSorter/refresh` | 立即刷新一次媒体库 |

插件页面按钮用（`auth: bear`，浏览器登录态）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/v1/plugin/PinyinSorter/run_now` | 后台立即运行一次（立即返回） |
| POST | `/api/v1/plugin/PinyinSorter/preview` | 预览扫描，返回计划归档的目录 |
| POST | `/api/v1/plugin/PinyinSorter/refresh_library` | 刷新媒体库 |

## 生命周期

- `init_plugin(config)`：只读配置、加载记录，不启动任务、不访问网络；
- `stop_service()`：无外部资源需释放（事件处理器由宿主统一管理）；
- 事件处理器声明在类体内，由宿主的事件实例解析器绑定到托管实例；
  插件停用时靠 `self._enabled` 短路，不做搬运。

## 自测

```bash
python tests/v3/pinyinsorter/standalone_selftest.py   # 期望输出 ALL CHECKS PASSED
```
