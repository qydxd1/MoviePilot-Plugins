"""
MoviePilot v3 插件：整理完成后按标题首字母归档媒体目录。

工作方式
--------
MoviePilot 整理（识别 → 重命名 → 搬移）完成后会广播 ``transfer.complete`` 事件，
本插件订阅该事件，取整理结果里的媒体单元目录（例如
``/media/movies/长安三万里 (2023)/``），按标题首字母计算目标字母目录（``C``），
把整个媒体单元目录移动到 ``/media/movies/C/`` 下，再请求 MoviePilot 已配置且启用的
媒体服务器（Emby / Jellyfin / Plex 等）刷新媒体库，让 Emby 立即看到新片。

首字母规则
----------
1. 内置 GB18030 汉字首字母表（纯标准库、零依赖），
   覆盖 GB18030 可编码的 4.29 万个汉字（常用字 + 扩展 A + 扩展 B + 兼容汉字）；
2. 拉丁字母取大写首字母；数字与符号归到配置的兜底分组（默认 ``#``）；
3. 认不出的一律落到兜底分组，绝不丢文件。

安全约定
--------
- 只在媒体目录位于某个「媒体库根目录」之下时才动手（前缀匹配，取最长匹配）；
- 只搬移库根目录下的「一级媒体目录」；检测到分类目录/多级结构时跳过并提示，
  以免把分类目录整体搬走；
- 目标目录已存在时跳过并记录，不覆盖；
- 已在字母目录下的媒体单元不重复搬移；
- 默认「移动」，「复制」模式保留源文件。
"""

from __future__ import annotations

import base64
import functools
import os
import re
import shutil
import threading
import time
import zlib
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional, Tuple

import anyio
from anyio import Path as AsyncPath

from app.schemas.mediaserver import RefreshMediaItem
from app.schemas.transfer import TransferInfo
from app.schemas.types import ChainEventType, EventType, MediaType
from app.sdk.events import Event, eventmanager
from app.sdk.logging import logger
from app.sdk.media import MediaInfo
from app.sdk.plugin import _PluginBase
from app.sdk.services import MediaServerHelper

_VENDOR_WHEEL = (
    Path(__file__).resolve().parent / "vendor" / "pinyinsorter_gb18030-0.1.0-py3-none-any.whl"
)


def _load_gb18030_table_b64() -> str:
    """读取 GB18030 首字母表数据。

    优先使用已安装的 ``pinyinsorter_gb18030`` 包，其次读插件 ``vendor/``
    下随包分发的 wheel，最后回退到插件目录里的 ``gb18030_table.py``，
    保证三种部署形态都能启动。
    """
    try:
        from pinyinsorter_gb18030 import GB18030_TABLE_B64 as table
    except ImportError:
        table = ""
    if table:
        return table
    try:
        import zipfile

        with zipfile.ZipFile(_VENDOR_WHEEL) as archive:
            source = archive.read("pinyinsorter_gb18030/__init__.py").decode("utf-8")
        start = source.index('GB18030_TABLE_B64 = "') + len('GB18030_TABLE_B64 = "')
        return source[start:source.index('"', start)]
    except Exception:  # pragma: no cover - vendor 缺失时回退本地模块
        pass
    from .gb18030_table import GB18030_TABLE_B64 as table

    return table


GB18030_TABLE_B64 = _load_gb18030_table_b64()

# 记录最多保留条数
_RECORD_LIMIT = 200

_LATIN = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")
_DIGITS = set("0123456789")

# 季目录名：Season 1 / S01 / Specials
_SEASON_PATTERN = re.compile(r"^(?:season\s*\d+|s\d{1,2}|specials?|sp)$", re.IGNORECASE)


def _is_cjk(char: str) -> bool:
    """判断是否为中日韩统一表意文字。"""
    code = ord(char)
    return (
        0x4E00 <= code <= 0x9FFF
        or 0x3400 <= code <= 0x4DBF
        or 0x20000 <= code <= 0x2FA1F
        or code == 0x3007  # 〇
    )


# ---------------------------------------------------------------------------
# GB18030 汉字拼音首字母表（离线内置，纯标准库）
#
# 数据来源：Unicode Unihan 数据库（kMandarin / kHanyuPinlu / kXHC1983 /
# kHanyuPinyin），覆盖 GB18030 可编码的 4.29 万个汉字：
#   - 4E00-9FFF  常用字         - 3400-4DBF  扩展 A
#   - F900-FAFF  兼容汉字       - 20000-2EBEF 扩展 B
# 存储为「字母 + 数量 + 码位差分 varint」的 zlib 压缩流再 base64，
# 运行时首次用到才解压成 dict（约 4 万项，耗时数毫秒）。
# ---------------------------------------------------------------------------
_PINYIN_INITIALS = "ABCDEFGHJKLMNOPQRSTWXYZ"  # 生成时用来校验字母合法性



def _read_varint(data: bytes, index: int) -> Tuple[int, int]:
    """读取一个 LEB128 varint，返回 (值, 新下标)。"""
    value = 0
    shift = 0
    while True:
        byte = data[index]
        index += 1
        value |= (byte & 0x7F) << shift
        if byte & 0x80:
            shift += 7
        else:
            return value, index


@functools.lru_cache(maxsize=None)
def _gb18030_first_letters() -> Dict[int, str]:
    """
    解压内置表，返回 ``{码位: 首字母}``。

    数据格式（见 `gb18030_table.py` 的 ``GB18030_TABLE_B64``）按字母分段：

    - ``字母``：1 字节 ASCII（A-Z，#）
    - ``数量 N``：LEB128 varint，本段收录多少个汉字
    - ``码位增量``：N 个 LEB128 varint，相对上一个码位的差值（升序排列，
      差值通常很小，比直接存码位省一半以上体积）
    """
    raw = zlib.decompress(base64.b64decode(GB18030_TABLE_B64))
    table: Dict[int, str] = {}
    offset = 0
    while offset < len(raw):
        letter = chr(raw[offset])
        offset += 1
        count, offset = _read_varint(raw, offset)
        codepoint = 0
        for _ in range(count):
            delta, offset = _read_varint(raw, offset)
            codepoint += delta
            table[codepoint] = letter
    return table


def _gb18030_letter(char: str) -> Optional[str]:
    """查内置 GB18030 首字母表，未收录返回 None。"""
    code = ord(char)
    if code == 0x3007:  # 〇
        return _gb18030_first_letters().get(code)
    # 只有汉字区间才查表，避免把标点/符号误认
    if not (
        0x4E00 <= code <= 0x9FFF
        or 0x3400 <= code <= 0x4DBF
        or 0xF900 <= code <= 0xFAFF
        or 0x20000 <= code <= 0x2FA1F
    ):
        return None
    return _gb18030_first_letters().get(code)


def _safe_path_segment(value: str) -> str:
    """把类别/类型这类配置值收成一个安全的路径片段（去掉路径分隔符）。"""
    cleaned = re.sub(r"[\\/:*?\"<>|]+", "", str(value or "")).strip()
    return "" if cleaned in (".", "..") else cleaned


def first_letter(text: Optional[str], fallback: str = "#") -> str:
    """
    计算标题首字母。

    :param text: 标题文本
    :param fallback: 无法判定时的归并分组
    :return: 单个大写字母或兜底分组
    """
    if not text:
        return fallback
    text = str(text).lstrip()
    for char in text:
        if char in _LATIN:
            return char.upper()
        if char in _DIGITS:
            return fallback
        if _is_cjk(char):
            letter = _gb18030_letter(char)
            if letter:
                return letter
        # 标点、空白、符号：继续看下一个字符
        continue
    return fallback


_DIRECT_TEMPLATE_RE = re.compile(r"\{(category|type)\}")

# 磁力链接里的 btih：40 位十六进制或 32 位 Base32
_MAGNET_HASH_RE = re.compile(r"urn:btih:([0-9a-fA-F]{40}|[A-Za-z2-7]{32})")

# 下载时改名：视频/字幕扩展名
_VIDEO_EXTS = {
    ".mkv", ".mp4", ".avi", ".ts", ".m2ts", ".m4v", ".wmv", ".flv", ".mov", ".rmvb",
}
_SUBTITLE_EXTS = {".ass", ".srt", ".ssa", ".vtt", ".sup"}

# 字幕文件名里常见的语言标记，改名时保留
_SUBTITLE_LANGS = {
    "chs", "cht", "sc", "tc", "chi", "zh", "zh-cn", "zh-tw", "zh-hans", "zh-hant",
    "eng", "en", "jpn", "jp", "ja", "kor", "ko",
}

# 集数：S01E02 / E02 / EP02 / 第2集
_EPISODE_RE = re.compile(
    r"(?:\bS(\d{1,2})[\s._-]*E(\d{1,3})\b|\bE(?:P)?[\s._-]*(\d{1,3})\b|第\s*(\d{1,3})\s*[集话話]|"
    r"(?:\s-\s|\s·\s)(\d{1,3})(?=[\s._\-\[\(]|$))",
    re.IGNORECASE,
)


def _torrent_hash(text: Optional[str]) -> Optional[str]:
    """
    从磁力链接里取出种子 infohash（小写十六进制）。

    :param text: 磁力链接或其它文本
    :return: 40 位十六进制 infohash；不是磁力链接时返回 None
    """
    match = _MAGNET_HASH_RE.search(str(text or ""))
    if not match:
        return None
    raw = match.group(1)
    if len(raw) == 32:
        try:
            raw = base64.b32decode(raw.upper()).hex()
        except Exception:
            return None
    return raw.lower()


def _media_folder_name(title: str, year: Any = None) -> str:
    """
    生成下载器里的媒体目录名：``标题 (年份)``，没有年份时只用标题。

    :param title: 媒体标题
    :param year: 发行年份
    :return: 安全的目录名；标题为空时返回空串
    """
    cleaned = _safe_path_segment(str(title or "").strip())
    if not cleaned:
        return ""
    year_text = str(year or "").strip()
    return f"{cleaned} ({year_text})" if year_text.isdigit() else cleaned


def _parse_episode(text: str) -> Tuple[Optional[int], Optional[int]]:
    """
    从文件名里解析季号与集号。

    :param text: 文件名或路径
    :return: ``(季号, 集号)``，解析不到的位为 None
    """
    match = _EPISODE_RE.search(str(text or ""))
    if not match:
        return None, None
    if match.group(2):
        return int(match.group(1)), int(match.group(2))
    if match.group(3):
        return None, int(match.group(3))
    if match.group(4):
        return None, int(match.group(4))
    if match.group(5):
        return None, int(match.group(5))
    return None, None


def _episode_name(title: str, season: Any, episode: Any, suffix: str) -> str:
    """
    生成规范的单集文件名：``标题 - S01E02.mkv``。

    没有集号时退化成 ``标题.mkv``（电影）。

    :param title: 媒体标题
    :param season: 季号，缺省按第 1 季
    :param episode: 集号
    :param suffix: 原文件扩展名（含点）
    :return: 新文件名
    """
    if episode is None:
        return f"{title}{suffix}"
    season_no = int(season or 1)
    return f"{title} - S{season_no:02d}E{int(episode):02d}{suffix}"


def _relative_dir(path: "PurePosixPath") -> str:
    """返回相对路径里的目录部分，顶层文件返回空串。"""
    parent = str(path.parent)
    return "" if parent in ("", ".") else parent


def _join_relative(directory: str, name: str) -> str:
    """把目录与文件名拼回相对路径。"""
    return f"{directory}/{name}" if directory else name


def _season_folder(season: Any) -> str:
    """
    生成下载器里的分季目录名：``Season 01``。

    :param season: 季号，缺省按第 1 季
    :return: 目录名
    """
    try:
        number = int(season)
    except (TypeError, ValueError):
        number = 1
    return f"Season {number:02d}"


def _torrent_root(paths: List[str]) -> str:
    """
    判断种子是否自带顶层目录（多文件种子通常有，单文件没有）。

    :param paths: 种子文件清单
    :return: 顶层目录名；散文件种子返回空串
    """
    for path in paths:
        pure = PurePosixPath(path)
        if len(pure.parts) > 1:
            return pure.parts[0]
    return ""


def _build_rename_plan(
    paths: List[str],
    title: str,
    season: Any = None,
    episodes: Optional[List[int]] = None,
    media_type: Any = None,
    folder: str = "",
) -> List[Tuple[str, str]]:
    """
    为种子内的文件生成改名计划（只动视频与配套字幕）。

    目录约定与媒体库保持一致：

    - 电影：文件放进 ``<标题 (年份)>/`` 子目录（种子自带顶层目录时用原目录，
      之后由任务改名统一成标题目录）；
    - 剧集：文件按季放进 ``Season 01/``、``Season 02/`` 子目录。

    :param paths: 种子文件清单（相对路径，``/`` 分隔）
    :param title: 媒体标题
    :param season: 识别出的季号
    :param episodes: 本次下载涉及的集号（按顺序回填解析不到集号的文件）
    :param media_type: 媒体类型（``MediaType`` 或字符串），用来区分电影/剧集
    :param folder: 电影目录名（``标题 (年份)``），散文件种子用它建目录
    :return: ``[(旧相对路径, 新相对路径), ...]``
    """
    videos = [
        path for path in paths
        if PurePosixPath(path).suffix.lower() in _VIDEO_EXTS
    ]
    if not videos:
        return []
    type_text = str(getattr(media_type, "value", media_type) or "").strip().lower()
    is_tv = type_text in ("tv", "电视剧")
    root = _torrent_root(paths)
    ordered_videos = sorted(videos)
    season_fallback = season
    episode_pool = list(episodes or [])
    video_targets: Dict[str, Tuple[str, str, str]] = {}
    plan: List[Tuple[str, str]] = []
    for index, path in enumerate(ordered_videos):
        pure = PurePosixPath(path)
        parsed_season, parsed_episode = _parse_episode(pure.stem)
        episode = parsed_episode
        if episode is None and index < len(episode_pool):
            episode = episode_pool[index]
        effective_season = parsed_season if parsed_season is not None else season_fallback
        if is_tv:
            new_name = _episode_name(title, effective_season, episode, pure.suffix)
            base = root
            if effective_season is not None:
                base = _join_relative(base, _season_folder(effective_season))
        else:
            new_name = f"{folder or title}{pure.suffix}"
            base = root or folder
        new_path = _join_relative(base, new_name)
        video_targets[path] = (base, pure.stem, PurePosixPath(new_name).stem)
        if new_path != path:
            plan.append((path, new_path))
    # 字幕按视频名配对：同目录、文件名以视频名开头
    handled = {old for old, _ in plan}
    for path in paths:
        if path in handled or path in video_targets:
            continue
        pure = PurePosixPath(path)
        if pure.suffix.lower() not in _SUBTITLE_EXTS:
            continue
        directory = _relative_dir(pure)
        match: Optional[Tuple[str, str, str]] = None
        for old_video, (base, old_stem, new_stem) in video_targets.items():
            if _relative_dir(PurePosixPath(old_video)) != directory:
                continue
            if pure.stem == old_stem or pure.stem.startswith(old_stem):
                if match is None or len(old_stem) > len(match[1]):
                    match = (base, old_stem, new_stem)
        if not match:
            continue
        base, old_stem, new_stem = match
        tail = pure.stem[len(old_stem):].strip(".-_ ")
        lang = tail if tail and tail.lower() in _SUBTITLE_LANGS else ""
        new_name = f"{new_stem}{'.' + tail if lang else ''}{pure.suffix}"
        new_path = _join_relative(base, new_name)
        if new_path != path:
            plan.append((path, new_path))
    return plan


def _rename_torrent(client: Any, hash_value: str, new_name: str) -> bool:
    """
    改下载器里的任务名。

    qBittorrent WebAPI v2.4+ 是 ``POST /api/v2/torrents/rename``（``new_torrent_name``），
    更老的封装用 ``name``，两种都试。

    :param client: qbittorrentapi 客户端
    :param hash_value: 种子 infohash
    :param new_name: 新任务名
    :return: 是否成功
    """
    call = getattr(client, "torrents_rename", None)
    if call is None:
        return False
    for kwargs in (
        {"torrent_hash": hash_value, "new_torrent_name": new_name},
        {"torrent_hash": hash_value, "name": new_name},
    ):
        try:
            call(**kwargs)
            return True
        except TypeError:
            continue
        except Exception as error:  # pragma: no cover - 下载器接口异常
            logger.warning("[PinyinSorter] 任务改名失败：%s", error)
            return False
    return False


def _rename_torrent_file(client: Any, hash_value: str, old_path: str, new_path: str) -> bool:
    """
    改种子内部的文件名（qBittorrent WebAPI v2.4+ 的 ``torrents/rename``）。

    :param client: qbittorrentapi 客户端
    :param hash_value: 种子 infohash
    :param old_path: 种子内的旧相对路径
    :param new_path: 种子内的新相对路径
    :return: 是否成功
    """
    call = getattr(client, "torrents_rename_file", None)
    if call is None:
        return False
    try:
        call(torrent_hash=hash_value, old_path=old_path, new_path=new_path)
        return True
    except Exception as error:  # pragma: no cover - 下载器接口异常
        logger.warning("[PinyinSorter] 文件改名失败 %s → %s：%s", old_path, new_path, error)
        return False


class PinyinSorter(_PluginBase):
    """按首字母归档 + 刷新媒体库。"""

    # 插件名称
    plugin_name = "首字母归档"
    # 插件描述
    plugin_desc = "整理完成后按标题首字母归档并刷新 Emby/Jellyfin/Plex；支持下载直达字母目录、下载时规范命名（电影建标题目录、剧集分季）、硬链接归档"
    # 插件版本
    plugin_version = "2.0.0"
    # 插件作者
    plugin_author = "qydxd1"
    # 作者主页
    author_url = "https://github.com/qydxd1"
    # 插件配置项ID前缀
    plugin_config_prefix = "pinyinsorter_"
    # 加载顺序
    plugin_order = 21
    # 可使用的用户级别
    auth_level = 1

    _enabled: bool = False
    _movie_roots: List[str] = []
    _tv_roots: List[str] = []
    _letter_source: str = "title"
    _fallback_letter: str = "#"
    _copy_mode: str = "move"
    _refresh_delay: int = 0
    _refresh: bool = True
    _refresh_mode: str = "library"
    _notify: bool = False
    _direct_letter: bool = False
    _direct_letter_parent: str = ""
    _direct_letter_categories: List[str] = []
    _rename_download: bool = False
    _relocate_existing: bool = True
    _rename_threads: set = set()
    _rename_stop: Optional[threading.Event] = None
    _records: List[Dict[str, Any]] = []
    _resource_events: List[Dict[str, Any]] = []
    _handled_hashes: Dict[str, float] = {}
    _running: bool = False
    _config_snapshot: Dict[str, Any] = {}

    def init_plugin(self, config: Optional[Dict[str, Any]] = None) -> None:
        """
        读取配置，建立运行状态。

        :param config: 插件配置字典，None 时使用默认值
        """
        config = config or {}
        self._enabled = bool(config.get("enabled", False))
        self._movie_roots = self._parse_roots(config.get("movie_roots"))
        self._tv_roots = self._parse_roots(config.get("tv_roots"))
        self._letter_source = (
            "en_title"
            if str(config.get("letter_source") or "title") == "en_title"
            else "title"
        )
        fallback = str(config.get("fallback_letter") or "#").strip()
        self._fallback_letter = fallback[:4] or "#"
        mode = str(config.get("copy_mode") or "move")
        self._copy_mode = mode if mode in ("copy", "hardlink") else "move"
        try:
            self._refresh_delay = int(str(config.get("refresh_delay") or 0))
        except (TypeError, ValueError):
            self._refresh_delay = 0
        self._refresh_delay = max(0, min(3600, self._refresh_delay))
        self._refresh = bool(config.get("refresh", True))
        self._refresh_mode = (
            "item" if str(config.get("refresh_mode") or "library") == "item" else "library"
        )
        self._notify = bool(config.get("notify", False))
        self._direct_letter = bool(config.get("direct_letter", False))
        self._direct_letter_parent = str(config.get("direct_letter_parent") or "").strip()
        self._direct_letter_categories = self._parse_categories(
            config.get("direct_letter_categories")
        )
        self._rename_download = bool(config.get("rename_download", False))
        self._relocate_existing = bool(config.get("relocate_existing", True))
        if self._rename_stop is None:
            self._rename_stop = threading.Event()
        else:
            self._rename_stop.clear()
        self._records = self._load_records()
        self._config_snapshot = dict(config)
        if config.get("onlyonce"):
            if self._enabled and self._schedule_once_run():
                self._config_snapshot["onlyonce"] = False
                try:
                    self.update_config(self._config_snapshot)
                except Exception as error:  # pragma: no cover - 保存失败不阻断
                    logger.warning("[PinyinSorter] 关闭一次性开关失败：%s", error)
            elif not self._enabled:
                logger.warning("[PinyinSorter] 插件未启用，「立即运行一次」已忽略")
        if self._enabled:
            logger.info(
                "[PinyinSorter] 已启用：电影库 %s，剧集库 %s，首字母来源 %s",
                self._movie_roots,
                self._tv_roots,
                self._letter_source,
            )

    def get_state(self) -> bool:
        """返回插件启用状态。"""
        return self._enabled

    def stop_service(self) -> None:
        """插件停用/重载/卸载时释放运行状态：停止改名线程。"""
        self._enabled = False
        self._running = False
        if self._rename_stop is not None:
            self._rename_stop.set()
        for thread in list(self._rename_threads):
            try:
                thread.join(timeout=1.0)
            except Exception:  # pragma: no cover - 线程状态异常
                pass
        self._rename_threads.clear()

    # ------------------------------------------------------------------
    # 立即运行一次
    # ------------------------------------------------------------------

    def _schedule_once_run(self) -> bool:
        """
        提交一次后台归档扫描（dry_run=False，含媒体库刷新）。

        优先交给宿主调度器，调度器不可用时退回独立线程，保证按钮点下去就有反应。

        :return: 是否已接受本次执行
        """
        if self._running:
            logger.info("[PinyinSorter] 已有扫描在运行，忽略本次「立即运行一次」")
            return False
        try:
            from app.sdk.scheduler import add_plugin_once_job

            accepted = add_plugin_once_job(
                plugin_id=self.__class__.__name__,
                job_id=f"{self.__class__.__name__}_run_once",
                func=self._run_scan_job,
                name="首字母归档-立即运行一次",
                delay_seconds=0,
            )
            if accepted:
                logger.info("[PinyinSorter] 已提交后台扫描任务")
                return True
        except Exception as error:  # pragma: no cover - 宿主接口变化
            logger.warning("[PinyinSorter] 宿主调度器不可用，改用线程执行：%s", error)
        threading.Thread(
            target=self._run_scan_job,
            name="PinyinSorterRunOnce",
            daemon=True,
        ).start()
        return True

    def _run_scan_job(self) -> None:
        """在后台线程中执行一次完整扫描，完成后可选发送通知。"""
        if self._running:
            return
        self._running = True
        try:
            summary = anyio.run(self.scan_existing, False)
            logger.info(
                "[PinyinSorter] 立即运行完成：扫描根目录 %s，候选 %s，完成 %s，跳过 %s，失败 %s",
                summary.get("scanned_roots"),
                summary.get("candidates"),
                summary.get("done"),
                summary.get("skipped"),
                summary.get("failed"),
            )
            if self._notify:
                try:
                    self.post_message(
                        mtype="整理完成",
                        title="【首字母归档】立即运行完成",
                        text=(
                            f"扫描根目录 {summary.get('scanned_roots', 0)} 个，"
                            f"候选 {summary.get('candidates', 0)} 个，"
                            f"完成 {summary.get('done', 0)}，"
                            f"跳过 {summary.get('skipped', 0)}，"
                            f"失败 {summary.get('failed', 0)}"
                        ),
                    )
                except Exception as error:  # pragma: no cover - 通知失败不影响主流程
                    logger.warning("[PinyinSorter] 发送通知失败：%s", error)
        except Exception as error:  # pragma: no cover - 文件系统异常
            logger.error("[PinyinSorter] 立即运行失败：%s", error)
        finally:
            self._running = False

    # ------------------------------------------------------------------
    # 事件处理
    # ------------------------------------------------------------------

    @eventmanager.register(EventType.TransferComplete)
    async def on_transfer_complete(self, event: Event) -> None:
        """
        整理完成事件入口：入库后刷新媒体库（不论是否发生归档搬动）。

        刷新时机是「入库」，不是「归档成功」：媒体已经进库就算数，哪怕目录
        本来就在字母目录里、或者不在本插件管理的库根之下，也应当通知媒体
        服务器刷新，避免 Emby/Jellyfin 看不到新内容。

        :param event: transfer.complete 事件
        """
        if not self._enabled:
            return
        data = event.event_data or {}
        mediainfo: Optional[MediaInfo] = data.get("mediainfo")
        transferinfo: Optional[TransferInfo] = data.get("transferinfo")
        if not mediainfo or not transferinfo or not transferinfo.success:
            return
        record = await anyio.to_thread.run_sync(
            functools.partial(self._handle_media_unit, mediainfo, transferinfo, False)
        )
        if not self._refresh:
            return
        target = self._refresh_target_dir(record, transferinfo)
        if self._refresh_delay <= 0:
            await anyio.to_thread.run_sync(
                functools.partial(self._refresh_media_servers, mediainfo, target)
            )
            return
        timer = threading.Timer(
            self._refresh_delay,
            functools.partial(self._refresh_media_servers, mediainfo, target),
        )
        timer.daemon = True
        timer.start()
        logger.info(
            "[PinyinSorter] 已安排 %s 秒后刷新媒体库：%s",
            self._refresh_delay,
            target,
        )

    @staticmethod
    def _refresh_target_dir(
        record: Optional[Dict[str, Any]],
        transferinfo: TransferInfo,
    ) -> Optional[Path]:
        """算出入库后要刷新给媒体服务器的目标路径。"""
        if record and record.get("status") == "success" and record.get("target"):
            target = Path(str(record["target"]))
            return target.parent if target.suffix else target
        return PinyinSorter._resolve_target_dir(transferinfo)

    @eventmanager.register(ChainEventType.ResourceDownload)
    def on_resource_download(self, event: Event) -> None:
        """
        资源下载提交阶段：把保存目录改写为「基准目录/首字母」。

        只在开启「下载直达字母目录」、且媒体类别命中过滤条件时生效。

        基准目录的取法：

        - 调用方已指定保存目录（搜索/订阅下载时很常见）→ 在该目录后追加首字母，
          这样「保存到 /Media/动画电影」的下载会落到 ``/Media/动画电影/首字母``；
        - 调用方未指定、但填了「直达父目录」→ 用父目录（支持 ``{category}`` 模板）；
        - 两者都没有 → 跳过。

        路径必须落在已配置的下载目录内，否则宿主的 save_path 校验会拒绝本次覆盖。

        这里同时挂上「下载时规范命名」：本事件带识别结果，正好在建任务前拿到
        媒体标题/年份/季集，稍后由后台线程去下载器里改名。

        :param event: resource.download 链式事件
        """
        if not self._enabled:
            return
        data = getattr(event, "event_data", None)
        if not data or getattr(data, "cancel", False):
            return
        options = getattr(data, "options", None)
        before = str(options.get("save_path") or "") if isinstance(options, dict) else ""
        reason = ""
        target: Optional[Path] = None
        if not self._direct_letter:
            reason = "未开启下载直达"
        elif not isinstance(options, dict):
            reason = "事件没有 options"
        elif not self._direct_category_allowed(options.get("media_category")):
            reason = "媒体类别不在过滤范围"
        else:
            title = self._resource_title(data)
            if not title:
                reason = "取不到媒体标题"
            else:
                base = self._direct_base_path(options.get("save_path"), data)
                letter = first_letter(title, self._fallback_letter) if base else ""
                if not base:
                    reason = "保存目录无法解析"
                elif not letter:
                    reason = "首字母计算为空"
                else:
                    target = base / letter
                    options["save_path"] = target.as_posix()
                    reason = "已改写保存目录"
                    logger.info(
                        "[PinyinSorter] 下载直达：%s → %s", title, options["save_path"]
                    )
        self._remember_resource_event(data, before, reason)
        self._maybe_schedule_download_rename(data, target)

    def _remember_resource_event(self, data: Any, before: str, reason: str) -> None:
        """
        记录最近收到的 resource.download 事件，便于排查「为什么没落字母目录」。

        :param data: 事件数据
        :param before: 改写前的保存目录
        :param reason: 处理结论
        """
        context = getattr(data, "context", None)
        media = getattr(context, "media_info", None)
        torrent = getattr(context, "torrent_info", None)
        options = getattr(data, "options", None)
        target = str(options.get("save_path") or "") if isinstance(options, dict) else ""
        events = list(self._resource_events)
        events.append(
            {
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "title": str(getattr(media, "title", "") or ""),
                "category": str(getattr(media, "category", "") or ""),
                "hash": _torrent_hash(getattr(torrent, "enclosure", None)) or "",
                "save_path_before": before,
                "save_path_after": target,
                "reason": reason,
            }
        )
        self._resource_events = events[-20:]

    def get_resource_events(self) -> Dict[str, Any]:
        """返回最近 20 次 resource.download 事件的处理情况。"""
        return self._envelope(True, "", list(self._resource_events))

    def _direct_base_path(self, save_path: Any, data: Any) -> Optional[Path]:
        """
        解析「下载直达」的基准目录。

        优先用调用方指定的保存目录（在其后追加首字母）；
        没有指定时才回落到「直达父目录」配置（支持模板）。

        :param save_path: 事件里已有的保存目录
        :param data: resource.download 事件数据
        :return: 基准目录；解析不出来时返回 None
        """
        explicit = str(save_path or "").strip()
        if explicit:
            base = Path(explicit)
            if not base.is_absolute() and not explicit.startswith("/"):
                logger.warning(
                    "[PinyinSorter] 保存目录不是绝对路径，跳过下载直达：%s", explicit
                )
                return None
            return base
        return self._direct_parent_path(data)

    def _resource_title(self, data: Any) -> str:
        """从 resource.download 事件数据里取媒体标题（取不到时退回种子标题）。"""
        context = getattr(data, "context", None)
        media = getattr(context, "media_info", None)
        for attr in ("title", "title_year"):
            value = str(getattr(media, attr, "") or "").strip()
            if value:
                return value
        torrent = getattr(context, "torrent_info", None)
        return str(getattr(torrent, "title", "") or "").strip()

    def _direct_category_allowed(self, category: Any) -> bool:
        """按「媒体类别过滤」判断是否对本次下载生效；过滤为空表示不限制。"""
        if not self._direct_letter_categories:
            return True
        value = str(category or "").strip()
        if not value:
            return False
        return any(key and key in value for key in self._direct_letter_categories)

    def _direct_parent_path(self, data: Any = None) -> Optional[Path]:
        """
        解析「直达父目录」。

        支持模板占位符：``{category}`` 替换为识别出的媒体类别（如「动漫」），
        ``{type}`` 替换为媒体类型（如「电视剧」）；不含占位符时按普通绝对路径处理。

        :param data: resource.download 事件数据，用于取作用户模板变量的类别/类型
        :return: 解析后的父目录；模板无法解析或不是绝对路径时返回 None
        """
        raw = self._direct_letter_parent
        if not raw:
            logger.warning("[PinyinSorter] 下载直达已开启但未填写直达父目录，跳过")
            return None
        rendered = self._render_direct_template(raw, data)
        if not rendered:
            return None
        parent = Path(rendered)
        if not parent.is_absolute() and not rendered.startswith("/"):
            logger.warning("[PinyinSorter] 直达父目录必须是绝对路径：%s", raw)
            return None
        return parent

    def _render_direct_template(self, raw: str, data: Any = None) -> Optional[str]:
        """
        把直达父目录里的 ``{category}`` / ``{type}`` 占位符换成实际值。

        占位符取不到值时放弃本次覆盖（而不是拼出一个错误的路径），
        避免把文件写进 ``/Media//J`` 这类残缺目录。

        :param raw: 配置的直达父目录
        :param data: resource.download 事件数据
        :return: 渲染后的绝对路径；无法渲染时返回 None
        """
        if "{" not in raw:
            return raw
        values = self._direct_template_values(data)
        rendered = _DIRECT_TEMPLATE_RE.sub(
            lambda match: values.get(match.group(1)) or "", raw
        )
        if "{" in rendered or "}" in rendered:
            logger.warning("[PinyinSorter] 直达父目录模板含未知占位符：%s", raw)
            return None
        for name in _DIRECT_TEMPLATE_RE.findall(raw):
            if not values.get(name):
                logger.warning("[PinyinSorter] 直达父目录模板的 {%s} 取不到值，跳过直达", name)
                return None
        segments = [segment for segment in rendered.split("/") if segment]
        if not segments:
            logger.warning("[PinyinSorter] 直达父目录模板渲染为空：%s", raw)
            return None
        return "/" + "/".join(segments)

    def _direct_template_values(self, data: Any = None) -> Dict[str, str]:
        """
        取模板变量：媒体类别与媒体类型。

        :param data: resource.download 事件数据
        :return: 形如 ``{"category": "动漫", "type": "电视剧"}`` 的字典
        """
        context = getattr(data, "context", None)
        media = getattr(context, "media_info", None)
        category = ""
        if data is not None:
            options = getattr(data, "options", None)
            if isinstance(options, dict):
                category = str(options.get("media_category") or "").strip()
        if not category:
            category = str(getattr(media, "category", "") or "").strip()
        media_type_obj = getattr(media, "type", None)
        media_type = str(getattr(media_type_obj, "value", media_type_obj) or "").strip()
        return {
            "category": _safe_path_segment(category),
            "type": _safe_path_segment(media_type),
        }

    # ------------------------------------------------------------------
    # 下载时规范命名
    # ------------------------------------------------------------------

    def _maybe_schedule_download_rename(self, data: Any, target: Optional[Path] = None) -> None:
        """
        下载提交阶段安排一次「下载器内改名」。

        只有本事件带着识别结果（搜索/订阅下载）且种子是磁力链接时才安排：
        手动粘贴的磁力链接没有识别结果，保持原名。

        :param data: resource.download 事件数据
        :param target: 本次下载期望的保存目录（下载直达算出来的），用于迁移已有任务
        """
        if not self._rename_download:
            return
        context = getattr(data, "context", None)
        media = getattr(context, "media_info", None)
        title = str(getattr(media, "title", "") or "").strip()
        if not media or not title:
            logger.info("[PinyinSorter] 本次下载没有识别结果，跳过下载时改名")
            return
        torrent = getattr(context, "torrent_info", None)
        hash_value = _torrent_hash(getattr(torrent, "enclosure", None))
        if not hash_value:
            logger.info("[PinyinSorter] 拿不到种子 infohash（不是磁力链接），跳过下载时改名")
            return
        folder = _media_folder_name(title, getattr(media, "year", None))
        if not folder:
            return
        episodes: List[int] = []
        for item in getattr(data, "episodes", None) or []:
            try:
                episodes.append(int(str(item)))
            except (TypeError, ValueError):
                continue
        thread = threading.Thread(
            target=self._download_rename_job,
            args=(
                hash_value,
                folder,
                media,
                sorted(episodes),
                str(getattr(data, "downloader", "") or ""),
                target.as_posix() if target else "",
                first_letter(title, self._fallback_letter) if self._direct_letter else "",
            ),
            name="PinyinSorterRename",
            daemon=True,
        )
        self._rename_threads.add(thread)
        thread.start()
        PinyinSorter._handled_hashes[hash_value] = time.time()
        logger.info("[PinyinSorter] 已安排下载时改名：%s（%s）", folder, hash_value)

    @staticmethod
    def _qb_client(downloader_name: str) -> Any:
        """
        取 qBittorrent 客户端实例（改名要走下载器自己的 API）。

        :param downloader_name: 任务提交到的下载器名称，空则取第一个 qBittorrent 实例
        :return: qbittorrentapi 客户端；没有可用实例时返回 None
        """
        from app.sdk.services import DownloaderHelper

        helper = DownloaderHelper()
        services: Dict[Optional[str], Any] = {}
        if downloader_name:
            service = helper.get_service(name=downloader_name)
            if service is not None and str(getattr(service, "type", "")) == "qbittorrent":
                services = {service.name: service}
        if not services:
            services = helper.get_services(type_filter="qbittorrent") or {}
        for service in services.values():
            instance = getattr(service, "instance", None)
            client = getattr(instance, "qbc", None)
            if client is not None:
                return client
        return None

    def _wait_torrent_files(
        self,
        hash_value: str,
        downloader_name: str,
        timeout: float = 300.0,
    ) -> List[Dict[str, Any]]:
        """
        等下载器拿到种子元数据（文件清单）。

        :param hash_value: 种子 infohash
        :param downloader_name: 下载器名称
        :param timeout: 最长等待秒数
        :return: 文件清单；超时或任务不存在时为空列表
        """
        stop = self._rename_stop or threading.Event()
        deadline = time.time() + timeout
        while not stop.is_set() and time.time() < deadline:
            client = self._qb_client(downloader_name)
            if client is not None:
                try:
                    infos = client.torrents_info(torrent_hashes=hash_value) or []
                    if infos:
                        files = client.torrents_files(torrent_hash=hash_value) or []
                        if files:
                            return [item for item in files if isinstance(item, dict)]
                except Exception as error:  # pragma: no cover - 下载器接口异常
                    logger.debug("[PinyinSorter] 读取下载任务失败：%s", error)
            stop.wait(5)
        return []

    def _download_rename_job(
        self,
        hash_value: str,
        folder: str,
        media: MediaInfo,
        episodes: List[int],
        downloader_name: str,
        expected_dir: str = "",
        letter: str = "",
    ) -> None:
        """
        后台任务：等元数据到位后，把任务名与视频/字幕文件改成规范命名。

        顺手把下载器里「已经存在、但保存目录不是本次期望目录」的任务迁移过去，
        避免重下同一资源时因为 qB 直接复用旧任务而落在老目录。

        :param hash_value: 种子 infohash
        :param folder: 目标目录名（``标题 (年份)``）
        :param media: 识别出的媒体信息
        :param episodes: 本次下载涉及的集号
        :param downloader_name: 下载器名称
        :param expected_dir: 本次期望的保存目录（空字符串表示不做迁移）
        :param letter: 首字母目录名（给了就在当前保存目录下补一层字母目录）
        """
        title = str(getattr(media, "title", "") or "").strip()
        season = getattr(media, "season", None)
        try:
            files = self._wait_torrent_files(hash_value, downloader_name)
            stop = self._rename_stop or threading.Event()
            if stop.is_set():
                return
            if not files:
                self._record_rename(folder, hash_value, "没等到任务元数据", "skipped")
                return
            client = self._qb_client(downloader_name)
            if client is None:
                self._record_rename(folder, hash_value, "没有可用的 qBittorrent 实例", "failed")
                return
            if not expected_dir and letter:
                expected_dir = self._letter_target_of(client, hash_value, letter)
            self._relocate_torrent_if_needed(client, hash_value, folder, expected_dir)
            paths = [
                str(item.get("name") or "")
                for item in files
                if str(item.get("name") or "").strip()
            ]
            plan = _build_rename_plan(
                paths,
                title,
                season,
                episodes,
                media_type=getattr(media, "type", None),
                folder=folder,
            )
            renamed = 0
            for old_path, new_path in plan:
                if stop.is_set():
                    return
                if _rename_torrent_file(client, hash_value, old_path, new_path):
                    renamed += 1
            _rename_torrent(client, hash_value, folder)
            logger.info(
                "[PinyinSorter] 下载时改名：%s（文件 %s 个）",
                folder,
                renamed,
            )
            message = f"任务名改为 {folder}，文件改名 {renamed} 个"
            self._record_rename(folder, hash_value, message, "success")
        except Exception as error:
            logger.warning("[PinyinSorter] 下载时改名失败：%s", error)
            self._record_rename(folder, hash_value, f"改名失败：{error}", "failed")

    @eventmanager.register(EventType.DownloadAdded)
    def on_download_added(self, event: Event) -> None:
        """
        兜底：resource.download 没生效时，任务进下载器后再补一次字母目录。

        订阅、批量下载等链路偶尔拿不到改写后的保存目录，这里按下载器里的实际
        保存目录补一层首字母目录，不依赖前面事件是否生效。
        """
        if not self._enabled or not self._rename_download or not self._direct_letter:
            return
        data = event.event_data
        context = self._payload_field(data, "context")
        media = getattr(context, "media_info", None)
        title = str(getattr(media, "title", "") or "").strip()
        hash_value = str(self._payload_field(data, "hash", "") or "").strip().lower()
        if not media or not title or not hash_value:
            return
        if time.time() - float(PinyinSorter._handled_hashes.get(hash_value) or 0) < 600:
            return
        letter = first_letter(title, self._fallback_letter)
        if not letter:
            return
        if not self._direct_category_allowed(str(getattr(media, "category", "") or "")):
            return
        folder = _media_folder_name(title, getattr(media, "year", None))
        if not folder:
            return
        episodes: List[int] = []
        for item in self._payload_field(data, "episodes", []) or []:
            try:
                episodes.append(int(str(item)))
            except (TypeError, ValueError):
                continue
        thread = threading.Thread(
            target=self._download_rename_job,
            args=(
                hash_value,
                folder,
                media,
                sorted(episodes),
                str(self._payload_field(data, "downloader", "") or ""),
                "",
                letter,
            ),
            name="PinyinSorterRename",
            daemon=True,
        )
        self._rename_threads.add(thread)
        thread.start()
        PinyinSorter._handled_hashes[hash_value] = time.time()
        logger.info("[PinyinSorter] 下载器里补字母目录：%s（%s）", folder, hash_value)

    @staticmethod
    def _payload_field(payload: Any, key: str, default: Any = None) -> Any:
        """广播事件 payload 可能是对象也可能是 dict，统一按 key 取值。"""
        if isinstance(payload, dict):
            return payload.get(key, default)
        return getattr(payload, key, default)

    @staticmethod
    def _letter_target_of(client: Any, hash_value: str, letter: str) -> str:
        """按下载器里的实际保存目录，算出它下面该有的首字母目录。"""
        try:
            infos = client.torrents_info(torrent_hashes=hash_value) or []
        except Exception as error:  # pragma: no cover - 只影响迁移
            logger.debug("[PinyinSorter] 读取任务保存目录失败：%s", error)
            return ""
        if not infos:
            return ""
        save_path = str(getattr(infos[0], "save_path", "") or "").strip().rstrip("/")
        if not save_path:
            return ""
        current = PurePosixPath(save_path)
        if current.name.upper() == letter.upper():
            return ""
        return (current / letter).as_posix()

    def _relocate_torrent_if_needed(
        self,
        client: Any,
        hash_value: str,
        folder: str,
        expected_dir: str,
    ) -> None:
        """
        把下载器里已经存在、但保存目录不对的任务迁到本次期望目录。

        qBittorrent 遇到重复 infohash 会直接返回已有任务、不理会新的 save_path，
        所以「重下同一资源」时实际目录仍是旧的；这里用 set_location 搬一次。

        :param client: qBittorrent 客户端
        :param hash_value: 种子 infohash
        :param folder: 目标目录名（只用于记录）
        :param expected_dir: 本次期望的保存目录
        """
        if not self._relocate_existing or not expected_dir:
            return
        try:
            infos = client.torrents_info(torrent_hashes=hash_value) or []
        except Exception as error:  # pragma: no cover - 只影响迁移，不阻断改名
            logger.warning("[PinyinSorter] 读取已有任务目录失败：%s", error)
            return
        if not infos:
            return
        actual = str(getattr(infos[0], "save_path", "") or "").strip().rstrip("/")
        expected = expected_dir.strip().rstrip("/")
        if not expected or (actual and actual == expected):
            return
        setter = getattr(client, "torrents_set_location", None)
        if setter is None:
            logger.warning("[PinyinSorter] 下载器不支持迁移已有任务，跳过")
            return
        try:
            setter(location=expected, torrent_hashes=hash_value)
        except Exception as error:
            logger.warning("[PinyinSorter] 迁移已有任务失败：%s", error)
            self._record_rename(
                folder, hash_value, f"迁移已有任务失败：{error}", "failed"
            )
            return
        message = f"已有任务从 {actual or '未知目录'} 迁到 {expected}"
        logger.info("[PinyinSorter] %s", message)
        self._record_rename(folder, hash_value, message, "success")

    def _record_rename(self, folder: str, hash_value: str, message: str, status: str) -> None:
        """把一次下载时改名记到状态页。"""
        self._append_record(
            {
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "title": folder or hash_value,
                "letter": "下载时改名",
                "status": status,
                "source": hash_value,
                "target": folder,
                "message": message,
            }
        )

    def _handle_media_unit(
        self,
        mediainfo: MediaInfo,
        transferinfo: TransferInfo,
        refresh: bool,
    ) -> Optional[Dict[str, Any]]:
        """
        按首字母归档单个媒体单元，并记录结果。

        :param mediainfo: 识别出的媒体信息
        :param transferinfo: 整理结果
        :param refresh: 是否由本函数直接刷新媒体库（事件链路已统一改为入库后刷新）
        :return: 记录字典；跳过时返回 None
        """
        target_dir = self._resolve_target_dir(transferinfo)
        if not target_dir:
            logger.debug("[PinyinSorter] 整理结果没有可用的目标目录，跳过")
            return None

        root = self._match_root(mediainfo.type, target_dir)
        if not root:
            logger.debug(
                "[PinyinSorter] %s 不在已配置的媒体库目录内，跳过：%s",
                mediainfo.type.value,
                target_dir,
            )
            return None

        unit = self._resolve_unit_dir(root, target_dir)
        if not unit:
            record = self._handle_root_file(mediainfo, transferinfo, root, refresh)
            if record is not None:
                return record
            logger.warning(
                "[PinyinSorter] 检测到分类目录/多级结构，未自动归档：%s（媒体库根目录 %s）",
                target_dir,
                root,
            )
            return None
        if not unit.is_dir():
            if unit.is_file():
                # 电影常见形态：单个视频文件直接落在库根或首字母目录下
                return self._archive_file(mediainfo, unit, root, refresh)
            logger.debug("[PinyinSorter] 媒体单元目录不存在，跳过：%s", unit)
            return None

        title = self._pick_title(mediainfo)
        letter = first_letter(title or unit.name, self._fallback_letter)
        if unit.parent == root / letter:
            logger.debug("[PinyinSorter] 已归档到 %s，跳过：%s", letter, unit)
            return None

        target = root / letter / unit.name
        status, message = self._move_dir(unit, target)
        record = {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "title": title or unit.name,
            "letter": letter,
            "source": str(unit),
            "target": str(target),
            "status": status,
            "message": message,
        }
        self._append_record(record)
        if status == "success":
            logger.info("[PinyinSorter] %s → %s", unit, target)
            if refresh:
                self._refresh_media_servers(mediainfo, target)
            if self._notify:
                self.post_message(
                    title="首字母归档完成",
                    text=f"{record['title']}\n{unit.name} → {letter}/\n{message}",
                )
        else:
            logger.warning(
                "[PinyinSorter] 归档失败：%s → %s（%s）", unit, target, message
            )
        return record

    def _archive_file(
        self,
        mediainfo: MediaInfo,
        source: Path,
        root: Path,
        refresh: bool,
    ) -> Optional[Dict[str, Any]]:
        """
        把单个视频文件包进 ``<首字母>/<标题 (年份)>/`` 目录后再归档。

        电影下载常常只有单个视频文件，直接放进首字母目录会让媒体库出现散文件；
        这里统一建一个与媒体库一致的标题目录。

        :param mediainfo: 识别出的媒体信息
        :param source: 待归档的文件
        :param root: 命中的媒体库根目录
        :param refresh: 是否由本函数直接刷新媒体库（事件链路已统一改为入库后刷新）
        :return: 记录字典；跳过时返回 None
        """
        title = self._pick_title(mediainfo)
        letter = first_letter(title or source.stem, self._fallback_letter)
        if not letter:
            return None
        folder = _media_folder_name(title or source.stem, getattr(mediainfo, "year", None))
        target_dir = root / letter / folder if folder else root / letter
        target = target_dir / source.name
        if target == source:
            logger.debug("[PinyinSorter] 已归档到 %s，跳过：%s", letter, source)
            return None
        status, message = self._move_file(source, target)
        record = {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "title": title or source.stem,
            "letter": letter,
            "source": str(source),
            "target": str(target),
            "status": status,
            "message": message,
        }
        self._append_record(record)
        if status == "success":
            logger.info("[PinyinSorter] %s → %s", source, target)
            if refresh:
                self._refresh_media_servers(mediainfo, target_dir)
            if self._notify:
                self.post_message(
                    title="首字母归档完成",
                    text=f"{record['title']}\n{source.name} → {letter}/{folder}/\n{message}",
                )
        else:
            logger.warning("[PinyinSorter] 归档失败：%s → %s（%s）", source, target, message)
        return record

    @staticmethod
    def _resolve_target_dir(transferinfo: TransferInfo) -> Optional[Path]:
        """从整理结果里推导整理完成后的目标目录。"""
        for item in (transferinfo.target_diritem, transferinfo.target_item):
            if item and item.path:
                candidate = Path(item.path)
                return candidate.parent if candidate.suffix else candidate
        if transferinfo.file_list_new:
            return Path(transferinfo.file_list_new[0]).parent
        return None

    def _resolve_unit_dir(self, root: Path, target_dir: Path) -> Optional[Path]:
        """
        找出要归档的「一级媒体目录」。

        - 电影：``<root>/<标题 (年份)>``；
        - 剧集：目标目录通常是 ``<root>/<标题 (年份)>/Season 1``，先退掉季目录；
        - 若剩下的目录仍不是根目录的一级子目录（说明库里有分类目录等层级），
          返回 None，交给调用方跳过，避免误搬分类目录。

        :param root: 命中的媒体库根目录
        :param target_dir: 整理完成后的目标目录
        :return: 一级媒体目录；结构不符合预期时返回 None
        """
        directory = target_dir
        if _SEASON_PATTERN.match(directory.name):
            directory = directory.parent
        try:
            relative = directory.relative_to(root)
        except ValueError:
            return None
        if not relative.parts:
            return None
        unit = root / relative.parts[0]
        return unit if unit == directory else None

    def _handle_root_file(
        self,
        mediainfo: MediaInfo,
        transferinfo: TransferInfo,
        root: Path,
        refresh: bool,
    ) -> Optional[Dict[str, Any]]:
        """
        处理整理结果直接落在库根下的单个文件（电影不建文件夹时常见）。

        这种文件没有「一级媒体目录」可搬，改为把文件本身移进
        ``<root>/<首字母>/``，让它和建了文件夹的电影保持同一层级。

        :param mediainfo: 识别出的媒体信息
        :param transferinfo: 整理结果
        :param root: 命中的媒体库根目录
        :param refresh: 是否由本函数直接刷新媒体库（事件链路已统一改为入库后刷新）
        :return: 记录字典；不适用时返回 None
        """
        media_file: Optional[Path] = None
        for raw in transferinfo.file_list_new or []:
            candidate = Path(str(raw))
            if candidate.parent == root and candidate.is_file():
                media_file = candidate
                break
        if media_file is None:
            return None

        title = self._pick_title(mediainfo)
        letter = first_letter(title or media_file.stem, self._fallback_letter)
        if not letter:
            return None
        folder = _media_folder_name(title or media_file.stem, getattr(mediainfo, "year", None))
        target_dir = root / letter / folder if folder else root / letter
        target = target_dir / media_file.name
        if target == media_file:
            logger.debug("[PinyinSorter] 已归档到 %s，跳过：%s", letter, media_file)
            return None

        status, message = self._move_file(media_file, target)
        record = {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "title": title or media_file.stem,
            "letter": letter,
            "source": str(media_file),
            "target": str(target),
            "status": status,
            "message": message,
        }
        self._append_record(record)
        if status == "success":
            logger.info("[PinyinSorter] %s → %s", media_file, target)
            if refresh:
                self._refresh_media_servers(mediainfo, target.parent)
            if self._notify:
                self.post_message(
                    title="首字母归档完成",
                    text=f"{record['title']}\n{media_file.name} → {letter}/\n{message}",
                )
        else:
            logger.warning(
                "[PinyinSorter] 归档失败：%s → %s（%s）", media_file, target, message
            )
        return record

    def _move_file(self, source: Path, target: Path) -> Tuple[str, str]:
        """
        把库根下的单个文件搬进字母目录。

        :return: (状态, 说明)，状态取值 success / skipped / failed
        """
        if source == target:
            return "skipped", "源文件与目标文件相同"
        if target.exists():
            return "skipped", "目标文件已存在，未覆盖"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if self._copy_mode == "copy":
                shutil.copy2(source, target)
                return "success", "已复制"
            if self._copy_mode == "hardlink":
                os.link(source, target)
                try:
                    source.unlink()
                    return "success", "已硬链接（源文件已删除，省空间）"
                except OSError:
                    return "success", "已硬链接（源文件删除失败，已保留）"
            shutil.move(str(source), str(target))
            return "success", "已移动"
        except Exception as error:  # pragma: no cover - IO 错误路径
            return "failed", f"{type(error).__name__}: {error}"

    def _pick_title(self, mediainfo: MediaInfo) -> str:
        """按配置挑选用于计算首字母的标题。"""
        title = getattr(mediainfo, "title", None) or ""
        en_title = getattr(mediainfo, "en_title", None) or ""
        if self._letter_source == "en_title":
            return en_title or title
        return title or en_title

    def _match_root(self, media_type: MediaType, target_dir: Path) -> Optional[Path]:
        """
        找出目标目录所属的媒体库根目录，取最长前缀匹配。

        :param media_type: 媒体类型，决定候选根目录集合
        :param target_dir: 整理完成后的目标目录
        :return: 命中的根目录；未命中返回 None
        """
        roots = list(self._tv_roots if media_type == MediaType.TV else self._movie_roots)
        best: Optional[Path] = None
        for raw in roots:
            root = Path(raw).expanduser()
            try:
                target_dir.relative_to(root)
            except ValueError:
                continue
            if best is None or len(str(root)) > len(str(best)):
                best = root
        return best

    def _move_dir(self, source: Path, target: Path) -> Tuple[str, str]:
        """
        把媒体单元目录搬到字母目录下。

        :return: (状态, 说明)，状态取值 success / skipped / failed
        """
        if source == target:
            return "skipped", "源目录与目标目录相同"
        if target.exists():
            return "skipped", "目标目录已存在，未覆盖"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if self._copy_mode == "copy":
                shutil.copytree(source, target, symlinks=True)
                return "success", "已复制"
            if self._copy_mode == "hardlink":
                for path in sorted(source.rglob("*")):
                    dest = target / path.relative_to(source)
                    if path.is_dir():
                        dest.mkdir(parents=True, exist_ok=True)
                        continue
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    os.link(path, dest)
                shutil.rmtree(source, ignore_errors=False)
                return "success", "已硬链接（源目录已删除，省空间）"
            shutil.move(str(source), str(target))
            return "success", "已移动"
        except Exception as error:  # pragma: no cover - IO 错误路径
            return "failed", f"{type(error).__name__}: {error}"

    # ------------------------------------------------------------------
    # 媒体库刷新
    # ------------------------------------------------------------------

    def _refresh_media_servers(
        self,
        mediainfo: Optional[MediaInfo] = None,
        media_dir: Optional[Path] = None,
    ) -> None:
        """
        请求所有已启用的媒体服务器刷新媒体库。

        优先按条目刷新（Emby/Jellyfin 支持），不可用时退回整库刷新。

        :param mediainfo: 媒体信息，用于构造刷新条目
        :param media_dir: 媒体单元目录（target_path）
        """
        item: Optional[RefreshMediaItem] = None
        if mediainfo is not None:
            item = RefreshMediaItem(
                title=getattr(mediainfo, "title", None),
                year=getattr(mediainfo, "year", None),
                type=getattr(mediainfo, "type", None),
                category=getattr(mediainfo, "category", None),
                target_path=media_dir,
            )
        try:
            services = MediaServerHelper().get_services() or {}
        except Exception as error:  # pragma: no cover - 宿主接口变化
            logger.warning("[PinyinSorter] 获取媒体服务器失败：%s", error)
            return
        if not services:
            logger.debug("[PinyinSorter] 未配置可用的媒体服务器，跳过刷新")
            return
        use_item = self._refresh_mode == "item" and item is not None
        for name, service in services.items():
            instance = getattr(service, "instance", None)
            if instance is None:
                continue
            try:
                if use_item and hasattr(instance, "refresh_library_by_items"):
                    instance.refresh_library_by_items([item])
                    logger.info("[PinyinSorter] 已请求 %s 按条目刷新：%s", name, item.title)
                elif hasattr(instance, "refresh_root_library"):
                    instance.refresh_root_library()
                    logger.info("[PinyinSorter] 已请求 %s 刷新整库", name)
                else:
                    logger.debug("[PinyinSorter] %s 不支持主动刷新", name)
            except Exception as error:  # pragma: no cover - 服务端异常
                logger.warning("[PinyinSorter] %s 刷新失败：%s", name, error)

    # ------------------------------------------------------------------
    # 手动扫描（首次迁移用）
    # ------------------------------------------------------------------

    async def scan_existing(self, dry_run: bool = True) -> Dict[str, Any]:
        """
        扫描已配置的媒体库根目录，把散落的一级媒体目录归档到字母目录下。

        :param dry_run: True 时只返回将要执行的动作，不移动文件
        :return: 汇总结果
        """
        summary: Dict[str, Any] = {
            "dry_run": dry_run,
            "scanned_roots": 0,
            "candidates": 0,
            "done": 0,
            "skipped": 0,
            "failed": 0,
            "items": [],
        }
        for raw in self._movie_roots + self._tv_roots:
            root = Path(raw).expanduser()
            if not root.is_dir():
                logger.warning("[PinyinSorter] 媒体库目录不存在：%s", root)
                continue
            summary["scanned_roots"] += 1
            async for entry in AsyncPath(root).iterdir():
                if not await entry.is_dir():
                    continue
                name = entry.name
                # 已经是字母/数字分组目录，跳过
                if len(name) == 1 and (name.isalpha() or name.isdigit()):
                    continue
                summary["candidates"] += 1
                letter = first_letter(name, self._fallback_letter)
                target = root / letter / name
                item = {"source": str(entry), "letter": letter, "target": str(target)}
                if dry_run:
                    summary["items"].append({**item, "status": "plan"})
                    continue
                status, message = await anyio.to_thread.run_sync(
                    self._move_dir, Path(str(entry)), target
                )
                if status == "success":
                    summary["done"] += 1
                elif status == "failed":
                    summary["failed"] += 1
                else:
                    summary["skipped"] += 1
                summary["items"].append({**item, "status": status, "message": message})
        if not dry_run and summary["done"] and self._refresh:
            await anyio.to_thread.run_sync(self._refresh_media_servers)
        logger.info(
            "[PinyinSorter] 扫描完成：根目录 %s，候选 %s，完成 %s，跳过 %s，失败 %s",
            summary["scanned_roots"],
            summary["candidates"],
            summary["done"],
            summary["skipped"],
            summary["failed"],
        )
        return summary

    # ------------------------------------------------------------------
    # 数据持久化
    # ------------------------------------------------------------------

    def _load_records(self) -> List[Dict[str, Any]]:
        """读取最近归档记录。"""
        try:
            value = self.get_data("records")
        except Exception:  # pragma: no cover - 首次运行无数据
            return []
        return value if isinstance(value, list) else []

    def _append_record(self, record: Dict[str, Any]) -> None:
        """追加一条记录，超出上限时丢弃最旧的。"""
        records = [record] + [item for item in self._records if isinstance(item, dict)]
        self._records = records[:_RECORD_LIMIT]
        try:
            self.save_data("records", self._records)
        except Exception as error:  # pragma: no cover - 存储异常
            logger.warning("[PinyinSorter] 保存记录失败：%s", error)

    # ------------------------------------------------------------------
    # 插件接口
    # ------------------------------------------------------------------

    def get_api(self) -> List[Dict[str, Any]]:
        """注册插件接口：记录查询、手动扫描、手动刷新、页面按钮。"""
        return [
            {
                "path": "/records",
                "endpoint": self.get_records,
                "methods": ["GET"],
                "auth": "apikey",
                "summary": "查询归档记录",
                "description": "返回最近的首字母归档记录",
            },
            {
                "path": "/scan",
                "endpoint": self.scan,
                "methods": ["POST"],
                "auth": "apikey",
                "summary": "扫描并归档已存在的媒体目录",
                "description": "dry_run=true 时只预览，不移动文件",
            },
            {
                "path": "/refresh",
                "endpoint": self.refresh_now,
                "methods": ["POST"],
                "auth": "apikey",
                "summary": "立即刷新媒体库",
                "description": "手动触发一次媒体库刷新",
            },
            {
                "path": "/run_now",
                "endpoint": self.run_now,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "立即运行一次",
                "description": "提交一次后台归档扫描（含媒体库刷新），立即返回",
            },
            {
                "path": "/preview",
                "endpoint": self.preview_scan,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "预览扫描",
                "description": "只列出将要归档的目录，不移动文件",
            },
            {
                "path": "/refresh_library",
                "endpoint": self.refresh_library,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "刷新媒体库",
                "description": "给插件页面按钮用：立即刷新一次媒体库",
            },
            {
                "path": "/download_events",
                "endpoint": self.get_resource_events,
                "methods": ["GET"],
                "auth": "apikey",
                "summary": "最近的下载事件",
                "description": "返回最近 20 次 resource.download 事件的处理结论，用于排查下载未落字母目录",
            },
        ]

    @staticmethod
    def _envelope(success: bool, message: str = "", data: Any = None) -> Dict[str, Any]:
        """宿主前端只认严格三键 envelope：success / message / data。"""
        return {"success": success, "message": message, "data": data}

    def get_records(self) -> Dict[str, Any]:
        """返回最近归档记录。"""
        return self._envelope(True, "", list(self._records))

    async def scan(self, dry_run: bool = True) -> Dict[str, Any]:
        """扫描并归档已存在的媒体目录（首次迁移用）。"""
        summary = await self.scan_existing(dry_run=dry_run)
        return self._envelope(True, "扫描完成", summary)

    async def refresh_now(self) -> Dict[str, Any]:
        """手动刷新媒体库。"""
        try:
            await anyio.to_thread.run_sync(self._refresh_media_servers)
            return self._envelope(True, "媒体库刷新完成")
        except Exception as error:
            logger.warning("[PinyinSorter] 手动刷新媒体库失败：%s", error)
            return self._envelope(False, f"媒体库刷新失败：{error}")

    async def run_now(self) -> Dict[str, Any]:
        """页面「立即运行一次」按钮：提交后台扫描。"""
        accepted = self._schedule_once_run()
        return self._envelope(
            accepted,
            "已提交后台任务" if accepted else "已有扫描在运行",
            {"accepted": accepted},
        )

    async def preview_scan(self) -> Dict[str, Any]:
        """页面「预览扫描」按钮：返回将要归档的目录列表，不移动文件。"""
        summary = await self.scan_existing(dry_run=True)
        items = summary.get("items") or []
        return self._envelope(
            True,
            "预览完成",
            {
                "scanned_roots": summary.get("scanned_roots", 0),
                "candidates": summary.get("candidates", 0),
                "items": items[:_RECORD_LIMIT],
            },
        )

    async def refresh_library(self) -> Dict[str, Any]:
        """页面「刷新媒体库」按钮。"""
        try:
            await anyio.to_thread.run_sync(self._refresh_media_servers)
            return self._envelope(True, "媒体库刷新完成")
        except Exception as error:
            logger.warning("[PinyinSorter] 刷新媒体库失败：%s", error)
            return self._envelope(False, f"媒体库刷新失败：{error}")

    # ------------------------------------------------------------------
    # 插件界面
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_roots(value: Any) -> List[str]:
        """把换行/逗号分隔的路径文本解析成去重后的列表。"""
        items: List[str] = []
        if isinstance(value, (list, tuple)):
            candidates = [str(item) for item in value]
        else:
            text = str(value or "").replace(",", "\n").replace("；", "\n")
            candidates = text.splitlines()
        for raw in candidates:
            path = raw.strip()
            if path and path not in items:
                items.append(path)
        return items

    @staticmethod
    def _parse_categories(value: Any) -> List[str]:
        """把逗号/顿号/空格分隔的类别文本解析成去重后的列表。"""
        if isinstance(value, (list, tuple)):
            candidates = [str(item) for item in value]
        else:
            text = str(value or "")
            for sep in (",", "，", "、", ";", "；", " "):
                text = text.replace(sep, "\n")
            candidates = text.splitlines()
        items: List[str] = []
        for raw in candidates:
            category = raw.strip()
            if category and category not in items:
                items.append(category)
        return items

    def get_form(self) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """返回配置表单与当前配置。"""
        return self._build_form(), {
            "enabled": self._enabled,
            "movie_roots": "\n".join(self._movie_roots),
            "tv_roots": "\n".join(self._tv_roots),
            "letter_source": self._letter_source,
            "fallback_letter": self._fallback_letter,
            "copy_mode": self._copy_mode,
            "refresh_delay": self._refresh_delay,
            "refresh": self._refresh,
            "refresh_mode": self._refresh_mode,
            "notify": self._notify,
            "direct_letter": self._direct_letter,
            "direct_letter_parent": self._direct_letter_parent,
            "direct_letter_categories": "、".join(self._direct_letter_categories),
            "rename_download": self._rename_download,
            "relocate_existing": self._relocate_existing,
            "onlyonce": False,
        }

    def _build_form(self) -> List[Dict[str, Any]]:
        """构建 Vuetify 配置表单。"""
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "enabled", "label": "启用插件"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "refresh", "label": "入库后自动刷新媒体库"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "notify", "label": "发送通知"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "copy_mode",
                                            "label": "整理方式",
                                            "items": [
                                                {"title": "移动（省空间）", "value": "move"},
                                                {"title": "复制（保留原文件）", "value": "copy"},
                                                {"title": "硬链接（同盘不占空间，删源）", "value": "hardlink"},
                                            ],
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "refresh_delay",
                                            "label": "入库后刷新延迟（秒）",
                                            "type": "number",
                                            "hint": "0 = 立即刷新；媒体服务器来不及扫到新文件时可设 10~60 秒",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "refresh_mode",
                                            "label": "刷新方式",
                                            "items": [
                                                {"title": "整库扫描（推荐）", "value": "library"},
                                                {"title": "仅刷新该条目", "value": "item"},
                                            ],
                                            "hint": "目录搬家后旧路径会失效，整库扫描更干净",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "onlyonce",
                                            "label": "立即运行一次（保存后执行）",
                                            "hint": "开启并保存后，后台扫描媒体库根目录、把散落目录归档一次，"
                                                    "完成后自动关闭该开关",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "movie_roots",
                                            "label": "电影库根目录",
                                            "placeholder": "/media/movies",
                                            "rows": 3,
                                            "hint": "每行一个路径，须与 MoviePilot 媒体目录设置一致",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "tv_roots",
                                            "label": "剧集库根目录",
                                            "placeholder": "/media/tv",
                                            "rows": 3,
                                            "hint": "每行一个路径；剧集按剧名首字母归档",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "direct_letter",
                                            "label": "下载直达字母目录",
                                            "hint": "下载时直接落到 <基准目录>/<首字母>："
                                                    "有指定保存目录就在其后追加首字母，没有才用下面的父目录",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "direct_letter_parent",
                                            "label": "直达父目录（仅在没有指定保存目录时使用，支持 {category}/{type}）",
                                            "placeholder": "/Media/{category}",
                                            "hint": "搜索/订阅下载会带上保存目录，那种情况直接在其后追加首字母，"
                                                    "不需要这里。这里只兜底「调用方没指定保存目录」的下载："
                                                    "填 /Media/{category} 就是 /Media/动漫/<首字母>/。"
                                                    "路径必须落在目录设置里某条「下载目录」内，"
                                                    "否则本次下载会被拒绝（保存路径不在允许的下载目录范围内）",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 5},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "direct_letter_categories",
                                            "label": "生效媒体类别（留空=全部）",
                                            "placeholder": "动漫",
                                            "hint": "按识别出的类别模糊匹配，用、或逗号分隔",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 5},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "rename_download",
                                            "label": "下载时规范命名",
                                            "hint": "任务一提交就按识别结果改名：目录改成「标题 (年份)」，"
                                                    "剧集文件改成「标题 - S01E01」，边下边用新名落盘。"
                                                    "只对 MoviePilot 自己搜索/订阅发起的下载生效；"
                                                    "手动粘贴磁力链接没有识别结果，保持原名",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 5},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "relocate_existing",
                                            "label": "已有任务迁移",
                                            "hint": "qBittorrent 遇到同一磁力链接会直接复用已有任务、"
                                                    "不理会新的保存目录。开启后，插件会把这个已存在任务"
                                                    "用「设置位置」搬到本次期望的字母目录；"
                                                    "关闭则只改名、不搬目录",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "letter_source",
                                            "label": "首字母来源",
                                            "items": [
                                                {"title": "中文标题（转拼音）", "value": "title"},
                                                {"title": "英文标题", "value": "en_title"},
                                            ],
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "fallback_letter",
                                            "label": "数字/符号归类目录",
                                            "placeholder": "#",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "text": (
                                                "归档结构：<库根目录>/<首字母>/<媒体目录>/。"
                                                "Emby 等媒体库必须指向库根目录（如 /media/movies），"
                                                "不要指向单个影片目录；若库里已配置分类目录导致多级结构，"
                                                "插件会跳过并写日志，避免误搬分类目录。"
                                                "开启「下载直达字母目录」后，下载任务一开始就保存到"
                                                "<基准目录>/<首字母>/：搜索/订阅下载会带上保存目录，"
                                                "直接在其后追加首字母；只有调用方没指定保存目录时才用"
                                                "「直达父目录」（可写 {category} 占位符，如 /Media/动漫/J/）。"
                                                "整理完成后仍按上述结构归档。"
                                                "注意：宿主会在目录设置的下载根下自动追加「类别目录」，"
                                                "所以保存目录本身带类别时（如 /Media/动画电影），"
                                                "落盘就是 /Media/动画电影/<首字母>/，不会再套一层类别。"
                                                "开启「下载时规范命名」后，任务一提交就按识别结果改名："
                                                "目录为「标题 (年份)」，剧集文件为「标题 - S01E01」，"
                                                "下载器里看到的就是规范名（手动磁力链接不改）。"
                                            ),
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                ],
            }
        ]

    def get_page(self) -> Optional[List[Dict[str, Any]]]:
        """返回归档记录状态页。"""
        return self._build_page()

    def _build_page(self) -> List[Dict[str, Any]]:
        """构建 Vuetify 状态页：顶部操作按钮 + 最近归档记录。"""
        page: List[Dict[str, Any]] = [self._build_page_actions()]
        records = self._records
        if not records:
            page.append(
                {
                    "component": "VAlert",
                    "props": {
                        "type": "info",
                        "variant": "tonal",
                        "text": (
                            "暂无归档记录。点上面的「立即运行一次」扫描已有媒体目录，"
                            "或等下一次整理完成后自动归档。"
                        ),
                    },
                }
            )
            return page

        success = sum(1 for item in records if item.get("status") == "success")
        failed = sum(1 for item in records if item.get("status") == "failed")
        skipped = len(records) - success - failed
        page.append(
            {
                "component": "VAlert",
                "props": {
                    "type": "success" if failed == 0 else "warning",
                    "variant": "tonal",
                    "text": (
                        f"最近 {len(records)} 条记录：成功 {success}，"
                        f"跳过 {skipped}，失败 {failed}。最多保留 {_RECORD_LIMIT} 条。"
                    ),
                    "class": "mb-2",
                },
            }
        )
        for record in records:
            status = record.get("status")
            icon = "✓" if status == "success" else ("→" if status == "skipped" else "✗")
            color = (
                "text-success"
                if status == "success"
                else ("text-warning" if status == "skipped" else "text-error")
            )
            page.append(
                {
                    "component": "VCard",
                    "props": {"class": "mb-2"},
                    "content": [
                        {
                            "component": "VCardText",
                            "content": [
                                {
                                    "component": "span",
                                    "props": {"class": f"{color} font-weight-bold"},
                                    "text": (
                                        f"{icon} [{record.get('time', '')}] "
                                        f"{record.get('title', '')}"
                                    ),
                                },
                                {"component": "br", "props": {}},
                                {
                                    "component": "span",
                                    "props": {"class": "text-caption text-grey"},
                                    "text": (
                                        f"分组 {record.get('letter', '')} · "
                                        f"{record.get('source', '')} → "
                                        f"{record.get('target', '')} · "
                                        f"{record.get('message', '')}"
                                    ),
                                },
                            ],
                        }
                    ],
                }
            )
        return page

    def _build_page_actions(self) -> Dict[str, Any]:
        """构建页面顶部的操作按钮行（点击后由前端调用插件接口）。"""
        pid = self.__class__.__name__
        return {
            "component": "VRow",
            "props": {"class": "mb-2"},
            "content": [
                {
                    "component": "VCol",
                    "props": {"cols": 12, "md": 4},
                    "content": [
                        self._page_button(
                            "立即运行一次",
                            "mdi-play-circle-outline",
                            f"{pid}/run_now",
                            "primary",
                            "扫描媒体库根目录、归档散落目录并刷新媒体库（后台执行）",
                        )
                    ],
                },
                {
                    "component": "VCol",
                    "props": {"cols": 12, "md": 4},
                    "content": [
                        self._page_button(
                            "预览扫描",
                            "mdi-magnify-scan",
                            f"{pid}/preview",
                            "secondary",
                            "只列出将要归档的目录，不移动任何文件",
                        )
                    ],
                },
                {
                    "component": "VCol",
                    "props": {"cols": 12, "md": 4},
                    "content": [
                        self._page_button(
                            "刷新媒体库",
                            "mdi-refresh",
                            f"{pid}/refresh_library",
                            "secondary",
                            "立即让 Emby/Jellyfin/Plex 重新扫描媒体库",
                        )
                    ],
                },
            ],
        }

    @staticmethod
    def _page_button(
        label: str,
        icon: str,
        api_path: str,
        color: str,
        tooltip: str,
    ) -> Dict[str, Any]:
        """生成一个会调用插件接口的按钮组件。"""
        return {
            "component": "VBtn",
            "props": {
                "color": color,
                "variant": "tonal",
                "block": True,
                "prepend-icon": icon,
                "title": tooltip,
            },
            "text": label,
            "events": {
                "click": {
                    "api": f"plugin/{api_path}",
                    "method": "POST",
                    "params": {},
                }
            },
        }
