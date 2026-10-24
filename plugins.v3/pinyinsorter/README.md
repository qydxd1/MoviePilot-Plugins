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
| 版本 | 1.2.0（与 `package.v3.json` 一致） |
| 入口 | `plugins.v3/pinyinsorter/__init__.py` |

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

## API（`auth: apikey`，请求头 `X-API-KEY: <API_TOKEN>`）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/plugin/PinyinSorter/records` | 最近 200 条归档记录 |
| POST | `/api/v1/plugin/PinyinSorter/scan?dry_run=true` | 扫描现有库并预演搬移（默认只预览） |
| POST | `/api/v1/plugin/PinyinSorter/refresh` | 立即刷新一次媒体库 |

## 生命周期

- `init_plugin(config)`：只读配置、加载记录，不启动任务、不访问网络；
- `stop_service()`：无外部资源需释放（事件处理器由宿主统一管理）；
- 事件处理器声明在类体内，由宿主的事件实例解析器绑定到托管实例；
  插件停用时靠 `self._enabled` 短路，不做搬运。

## 自测

```bash
python tests/v3/pinyinsorter/standalone_selftest.py   # 期望输出 ALL CHECKS PASSED
```
