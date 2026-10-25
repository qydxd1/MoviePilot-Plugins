"""PinyinSorter 离线自测：用桩模块替代 MoviePilot 宿主，验证纯逻辑与文件操作。"""
import asyncio
import importlib.util
import os
import pathlib
import shutil
import sys
import tempfile
import time
import types


def mod(name, **attrs):
    m = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(m, key, value)
    sys.modules[name] = m
    return m


class EnumValue:
    def __init__(self, value):
        self.value = value

    def __eq__(self, other):
        return self.value == getattr(other, "value", other)

    def __hash__(self):
        return hash(self.value)

    def __repr__(self):
        return f"<{self.value}>"


MediaType = types.SimpleNamespace(MOVIE=EnumValue("电影"), TV=EnumValue("电视剧"), UNKNOWN=EnumValue("未知"))
EventType = types.SimpleNamespace(TransferComplete="transfer.complete")
ChainEventType = types.SimpleNamespace(ResourceDownload="resource.download")


class Event:
    def __init__(self, data):
        self.event_data = data


class EventManager:
    def register(self, _type):
        def deco(fn):
            return fn

        return deco


eventmanager = EventManager()


class Logger:
    def __init__(self):
        self.lines = []

    def info(self, *a):
        self.lines.append(("I", a))

    def warning(self, *a):
        self.lines.append(("W", a))

    def debug(self, *a):
        self.lines.append(("D", a))

    def error(self, *a):
        self.lines.append(("E", a))


logger = Logger()


class MediaInfo:
    def __init__(
        self,
        title="",
        en_title="",
        year=2023,
        type=None,
        category=None,
        season=None,
        seasons=None,
    ):
        self.title = title
        self.en_title = en_title
        self.year = year
        self.type = type
        self.category = category
        self.season = season
        self.seasons = seasons


class PluginBase:
    def __init__(self):
        self._data = {}
        self._config = {}
        self.messages = []

    def get_data(self, key):
        return self._data.get(key)

    def save_data(self, key, value):
        self._data[key] = value

    def post_message(self, **kwargs):
        self.messages.append(kwargs)

    def update_config(self, config, plugin_id=None):
        self._config = config


class RefreshMediaItem:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class FakeServer:
    def __init__(self):
        self.calls = []

    def refresh_library_by_items(self, items):
        self.calls.append(("items", items))

    def refresh_root_library(self):
        self.calls.append(("root", None))


class MediaServerHelper:
    shared = {}

    def __init__(self):
        self.services = MediaServerHelper.shared

    def get_services(self, *a, **kw):
        return self.services


class TransferInfo:
    def __init__(self, success=True, target_diritem=None, target_item=None, file_list_new=None):
        self.success = success
        self.target_diritem = target_diritem
        self.target_item = target_item
        self.file_list_new = file_list_new or []


class FileItem:
    def __init__(self, path):
        self.path = path


class APath:
    def __init__(self, p):
        self._p = pathlib.Path(p)

    def __str__(self):
        return str(self._p)

    @property
    def name(self):
        return self._p.name

    def __fspath__(self):
        return str(self._p)

    async def is_dir(self):
        return self._p.is_dir()

    async def iterdir(self):
        for child in sorted(self._p.iterdir()):
            yield APath(child)


async def run_sync(fn, *args, **kwargs):
    return fn(*args, **kwargs)


mod("app")
mod("app.schemas")
mod("app.sdk")
mod("app.schemas.types", MediaType=MediaType, EventType=EventType, ChainEventType=ChainEventType)
mod("app.schemas.transfer", TransferInfo=TransferInfo, FileItem=FileItem)
mod("app.schemas.mediaserver", RefreshMediaItem=RefreshMediaItem)
mod("app.sdk.events", Event=Event, eventmanager=eventmanager)
mod("app.sdk.logging", logger=logger)
mod("app.sdk.media", MediaInfo=MediaInfo)
mod("app.sdk.plugin", _PluginBase=PluginBase)
mod("app.sdk.services", MediaServerHelper=MediaServerHelper, RefreshMediaItem=RefreshMediaItem)
mod("anyio", Path=APath, to_thread=types.SimpleNamespace(run_sync=run_sync))

PLUGIN_ROOT = pathlib.Path(__file__).resolve().parents[3]
PLUGIN_PATH = PLUGIN_ROOT / "plugins.v3" / "pinyinsorter" / "__init__.py"
spec = importlib.util.spec_from_file_location("pinyinsorter", PLUGIN_PATH)
plugin_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin_module)
print("import ok")

failures = []


def check(label, actual, expected):
    if actual != expected:
        failures.append(f"{label}: got {actual!r}, expected {expected!r}")


letter = plugin_module.first_letter
for text, expected in {
    "长安三万里": "C",
    "霸王别姬": "B",
    "ABC Song": "A",
    "1917": "#",
    "三体": "S",
    "三_体": "S",
    "·某片": "M",
    "《琅琊榜》": "L",
    "Warrior": "W",
    "-": "#",
    "": "#",
    None: "#",
    "山河令": "S",
    "哪吒之魔童降世": "N",
    "阿凡达": "A",
    "肖申克的救赎": "X",
    "我不是药神": "W",
    # GB18030 扩展区抽样（GB2312 表覆盖不到的字）
    "彧": "Y",
    "澔": "H",
    "龘": "D",
    "〇": "L",
    "㐀": "Q",
    "𠀀": "H",
}.items():
    check(f"first_letter({text!r})", letter(text), expected)

table = plugin_module._gb18030_first_letters()
check("GB18030 表字符数", len(table) > 42000, True)
check("GB18030 表含扩展B", 0x20000 in table, True)

plugin = plugin_module.PinyinSorter()

check(
    "parse_roots",
    plugin._parse_roots("/media/movies\n/media/other, /media/x；/media/movies"),
    ["/media/movies", "/media/other", "/media/x"],
)

# ---- 目录解析 ----
root = pathlib.Path("/media/movies")
check(
    "_resolve_unit_dir movie",
    plugin._resolve_unit_dir(root, pathlib.Path("/media/movies/长安三万里 (2023)")),
    root / "长安三万里 (2023)",
)
check(
    "_resolve_unit_dir tv season",
    plugin._resolve_unit_dir(pathlib.Path("/media/tv"), pathlib.Path("/media/tv/Show (2020)/Season 1")),
    pathlib.Path("/media/tv/Show (2020)"),
)
check(
    "_resolve_unit_dir categorized",
    plugin._resolve_unit_dir(pathlib.Path("/media/tv"), pathlib.Path("/media/tv/国剧/Show (2020)/Season 1")),
    None,
)
check(
    "_resolve_unit_dir root itself",
    plugin._resolve_unit_dir(root, root),
    None,
)

# ---- 移动 ----
# 沙箱/CI 里可指定临时目录；默认用系统临时目录
workdir = pathlib.Path(os.environ.get("PINYINSORTER_SELFTEST_DIR")
                       or tempfile.mkdtemp(prefix="pinyinsorter-selftest-"))
shutil.rmtree(workdir, ignore_errors=True)
workdir.mkdir(parents=True, exist_ok=True)
try:
    movies = workdir / "movies"
    src = movies / "长安三万里 (2023)"
    src.mkdir(parents=True)
    (src / "movie.mkv").write_text("x", encoding="utf-8")
    status, message = plugin._move_dir(src, movies / "C" / "长安三万里 (2023)")
    check("move status", status, "success")
    check("moved exists", (movies / "C" / "长安三万里 (2023)" / "movie.mkv").exists(), True)
    # 目标已存在 → 跳过
    again = movies / "重名 (2024)"
    again.mkdir(parents=True)
    (again / "a.mkv").write_text("x", encoding="utf-8")
    existing = movies / "C" / "重名 (2024)"
    existing.mkdir(parents=True)
    check("conflict skip", plugin._move_dir(again, existing)[0], "skipped")

    # ---- 事件处理（电影 + 刷新）----
    plugin.init_plugin(
        {
            "enabled": True,
            "movie_roots": str(movies),
            "tv_roots": "",
            "letter_source": "title",
            "fallback_letter": "#",
            "copy_mode": "move",
            "refresh": True,
        }
    )
    server = FakeServer()
    MediaServerHelper.shared = {"Emby": types.SimpleNamespace(instance=server)}

    new_dir = movies / "肖申克的救赎 (1994)"
    new_dir.mkdir(parents=True)
    (new_dir / "x.mkv").write_text("y", encoding="utf-8")
    info = MediaInfo(title="肖申克的救赎", year=1994, type=MediaType.MOVIE, category="电影")
    transfer = TransferInfo(target_diritem=FileItem(str(new_dir)))
    event = Event({"mediainfo": info, "transferinfo": transfer})

    asyncio.run(plugin.on_transfer_complete(event))
    check("event moved", (movies / "X" / "肖申克的救赎 (1994)" / "x.mkv").exists(), True)
    check("event refresh default library", server.calls[0][0] if server.calls else None, "root")
    check("record saved", len(plugin.get_records()), 1)

    # 按条目刷新模式
    plugin.init_plugin(
        {
            "enabled": True,
            "movie_roots": str(movies),
            "tv_roots": "",
            "refresh": True,
            "refresh_mode": "item",
        }
    )
    server.calls.clear()
    item_dir = movies / "阿凡达 (2009)"
    item_dir.mkdir(parents=True)
    (item_dir / "a.mkv").write_text("a", encoding="utf-8")
    info2 = MediaInfo(title="阿凡达", year=2009, type=MediaType.MOVIE, category="电影")
    transfer2 = TransferInfo(target_diritem=FileItem(str(item_dir)))
    asyncio.run(plugin.on_transfer_complete(Event({"mediainfo": info2, "transferinfo": transfer2})))
    check("item mode refresh", server.calls[0][0] if server.calls else None, "items")
    check("item mode moved", (movies / "A" / "阿凡达 (2009)").exists(), True)

    # ---- 电影不建文件夹：整理结果直接落在库根下的单个文件 ----
    root_file = movies / "My Movie (2020).mkv"
    root_file.write_text("r", encoding="utf-8")
    info3 = MediaInfo(title="My Movie", year=2020, type=MediaType.MOVIE, category="电影")
    transfer3 = TransferInfo(file_list_new=[str(root_file)])
    asyncio.run(plugin.on_transfer_complete(Event({"mediainfo": info3, "transferinfo": transfer3})))
    check("root file archived", (movies / "M" / "My Movie (2020).mkv").exists(), True)
    check("root file source gone", root_file.exists(), False)

    # ---- 手动扫描 dry_run ----
    loose = movies / "Zeta (2001)"
    loose.mkdir(parents=True)
    (loose / "z.mkv").write_text("z", encoding="utf-8")
    summary = asyncio.run(plugin.scan_existing(dry_run=True))
    planned = [item for item in summary["items"] if item["source"].endswith("Zeta (2001)")]
    check("scan dry_run planned", planned[0]["letter"] if planned else None, "Z")
    check("scan dry_run untouched", loose.exists(), True)

    summary = asyncio.run(plugin.scan_existing(dry_run=False))
    check("scan moved", (movies / "Z" / "Zeta (2001)").exists(), True)

    # ---- 下载直达字母目录 ----
    plugin.init_plugin(
        {
            "enabled": True,
            "movie_roots": str(movies),
            "direct_letter": True,
            "direct_letter_parent": "/media/动漫",
            "direct_letter_categories": "动漫",
        }
    )

    def _resource_event(title, category="动漫", save_path=None):
        options = {"media_category": category}
        if save_path:
            options["save_path"] = save_path
        data = types.SimpleNamespace(
            context=types.SimpleNamespace(media_info=MediaInfo(title=title)),
            options=options,
            cancel=False,
        )
        return Event(data), options

    event_data, options = _resource_event("进击的巨人")
    plugin.on_resource_download(event_data)
    check("direct save_path", options.get("save_path"), "/media/动漫/J")

    # ---- 模板：按类别直达 /Media/<类别>/<首字母> ----
    plugin.init_plugin(
        {
            "enabled": True,
            "movie_roots": str(movies),
            "direct_letter": True,
            "direct_letter_parent": "/Media/{category}",
            "direct_letter_categories": "",
        }
    )
    event_data, options = _resource_event("进击的巨人", category="动漫")
    plugin.on_resource_download(event_data)
    check("direct template category", options.get("save_path"), "/Media/动漫/J")

    event_data, options = _resource_event("鬼灭之刃", category="电影")
    plugin.on_resource_download(event_data)
    check("direct template category 2", options.get("save_path"), "/Media/电影/G")

    event_data, options = _resource_event("进击的巨人", category="")
    plugin.on_resource_download(event_data)
    check("direct template missing category skipped", options.get("save_path"), None)

    plugin.init_plugin(
        {
            "enabled": True,
            "movie_roots": str(movies),
            "direct_letter": True,
            "direct_letter_parent": "/Media/{unknown}",
            "direct_letter_categories": "",
        }
    )
    event_data, options = _resource_event("进击的巨人", category="动漫")
    plugin.on_resource_download(event_data)
    check("direct unknown placeholder skipped", options.get("save_path"), None)

    plugin.init_plugin(
        {
            "enabled": True,
            "movie_roots": str(movies),
            "direct_letter": True,
            "direct_letter_parent": "/media/动漫",
            "direct_letter_categories": "动漫",
        }
    )

    event_data, options = _resource_event("鬼灭之刃", category="电影")
    plugin.on_resource_download(event_data)
    check("direct category filtered", options.get("save_path"), None)

    event_data, options = _resource_event("进击的巨人", save_path="/downloads/explicit")
    plugin.on_resource_download(event_data)
    check(
        "direct appends letter to explicit save_path",
        options.get("save_path"),
        "/downloads/explicit/J",
    )

    event_data, options = _resource_event("我对井底之鱼的无限憧憬", save_path="/Media/动画电影")
    plugin.on_resource_download(event_data)
    check(
        "direct appends letter to category save_path",
        options.get("save_path"),
        "/Media/动画电影/W",
    )

    plugin.init_plugin({"enabled": True, "movie_roots": str(movies), "direct_letter": True})
    event_data, options = _resource_event("进击的巨人")
    plugin.on_resource_download(event_data)
    check("direct blank parent ignored", options.get("save_path"), None)

    plugin.init_plugin({"enabled": True, "movie_roots": str(movies), "direct_letter": False})
    event_data, options = _resource_event("进击的巨人")
    plugin.on_resource_download(event_data)
    check("direct disabled", options.get("save_path"), None)

    check(
        "parse_categories",
        plugin._parse_categories("动漫、国创, 日本动画"),
        ["动漫", "国创", "日本动画"],
    )

    # ---- 下载时规范命名 ----
    check(
        "torrent hash hex",
        plugin_module._torrent_hash(
            "magnet:?xt=urn:btih:0123456789ABCDEF0123456789ABCDEF01234567&dn=x"
        ),
        "0123456789abcdef0123456789abcdef01234567",
    )
    check(
        "torrent hash base32",
        plugin_module._torrent_hash("magnet:?xt=urn:btih:AEBAGBAFAYDQQCIKBMQEAYDQQCIKBMQE"),
        "0102030405060708090a0b204060708090a0b204",
    )
    check("torrent hash bad base32", plugin_module._torrent_hash("magnet:?xt=urn:btih:SHORT"), None)
    check("torrent hash not magnet", plugin_module._torrent_hash("https://x/y.torrent"), None)
    check("media folder name", plugin_module._media_folder_name("进击的巨人", 2013), "进击的巨人 (2013)")
    check("media folder name no year", plugin_module._media_folder_name("进击的巨人", None), "进击的巨人")
    check("parse episode SxxEyy", plugin_module._parse_episode("Show.S02E07.1080p"), (2, 7))
    check("parse episode Exx", plugin_module._parse_episode("[Group] Show - 05 [1080p]"), (None, 5))
    check("parse episode 第N集", plugin_module._parse_episode("Show 第12集"), (None, 12))
    check(
        "episode name",
        plugin_module._episode_name("进击的巨人", 1, 7, ".mkv"),
        "进击的巨人 - S01E07.mkv",
    )
    plan = plugin_module._build_rename_plan(
        [
            "Show/Show.S01E01.1080p.mkv",
            "Show/Show.S01E01.1080p.chs.ass",
            "Show/Show.S01E02.1080p.mkv",
        ],
        "进击的巨人",
        1,
        [1, 2],
    )
    check(
        "rename plan video",
        ("Show/Show.S01E01.1080p.mkv", "Show/进击的巨人 - S01E01.mkv") in plan,
        True,
    )
    check(
        "rename plan subtitle keeps lang",
        ("Show/Show.S01E01.1080p.chs.ass", "Show/进击的巨人 - S01E01.chs.ass") in plan,
        True,
    )
    check(
        "rename plan second episode",
        ("Show/Show.S01E02.1080p.mkv", "Show/进击的巨人 - S01E02.mkv") in plan,
        True,
    )
    check("rename plan movie single file", plugin_module._build_rename_plan(["A.mkv"], "电影", None, []), [("A.mkv", "电影.mkv")])

    def _rename_event(title="进击的巨人", enclosure=None, episodes=None):
        media = MediaInfo(title=title, year=2013, type=MediaType.TV, season=1)
        context = types.SimpleNamespace(
            media_info=media,
            torrent_info=types.SimpleNamespace(enclosure=enclosure),
        )
        data = types.SimpleNamespace(
            context=context,
            options={"media_category": "动漫"},
            cancel=False,
            episodes=set(episodes or [1, 2]),
            downloader="qb",
        )
        return Event(data)

    plugin.init_plugin(
        {
            "enabled": True,
            "movie_roots": str(movies),
            "rename_download": True,
        }
    )
    scheduled = []
    original_rename_job = plugin._download_rename_job
    plugin._download_rename_job = lambda *args, **kwargs: scheduled.append(args)
    plugin._rename_threads.clear()
    plugin.on_resource_download(
        _rename_event(
            enclosure="magnet:?xt=urn:btih:0123456789ABCDEF0123456789ABCDEF01234567&dn=x"
        )
    )
    for _ in range(50):
        if scheduled:
            break
        time.sleep(0.02)
    check("rename scheduled", len(scheduled), 1)
    check("rename folder arg", scheduled[0][1] if scheduled else None, "进击的巨人 (2013)")
    check("rename episodes arg", scheduled[0][3] if scheduled else None, [1, 2])
    plugin._download_rename_job = original_rename_job
    plugin._rename_threads.clear()

    plugin.on_resource_download(_rename_event(enclosure="https://site/download/1.torrent"))
    check("rename skipped without magnet", len(scheduled), 1)

    plugin.init_plugin(
        {
            "enabled": True,
            "movie_roots": str(movies),
            "rename_download": False,
        }
    )
    plugin._download_rename_job = lambda *args, **kwargs: scheduled.append(args)
    plugin.on_resource_download(
        _rename_event(
            enclosure="magnet:?xt=urn:btih:0123456789ABCDEF0123456789ABCDEF01234567&dn=x"
        )
    )
    check("rename disabled", len(scheduled), 1)
    plugin.__dict__.pop("_download_rename_job", None)
    plugin.init_plugin(
        {
            "enabled": True,
            "movie_roots": str(movies),
            "tv_roots": "",
            "refresh": True,
            "refresh_mode": "item",
        }
    )

    # ---- 下载时改名任务本身 ----
    class _FakeQb:
        def __init__(self):
            self.torrent_renames = []
            self.file_renames = []

        def torrents_rename(self, torrent_hash=None, new_torrent_name=None, name=None, **kwargs):
            self.torrent_renames.append(new_torrent_name or name)

        def torrents_rename_file(self, torrent_hash=None, old_path=None, new_path=None, **kwargs):
            self.file_renames.append((old_path, new_path))

    fake = _FakeQb()
    plugin._qb_client = lambda downloader_name: fake
    plugin._wait_torrent_files = lambda hash_value, downloader_name, timeout=300.0: [
        {"name": "Show/Show.S01E01.1080p.mkv"},
        {"name": "Show/Show.S01E01.1080p.chs.ass"},
        {"name": "Show/Show.S01E02.1080p.mkv"},
    ]
    plugin._rename_stop = None
    plugin._download_rename_job(
        "0123456789abcdef0123456789abcdef01234567",
        "进击的巨人 (2013)",
        MediaInfo(title="进击的巨人", year=2013, type=MediaType.TV, season=1),
        [1, 2],
        "qb",
    )
    check("job torrent renamed", fake.torrent_renames, ["进击的巨人 (2013)"])
    check(
        "job file renames",
        fake.file_renames,
        [
            ("Show/Show.S01E01.1080p.mkv", "Show/进击的巨人 - S01E01.mkv"),
            ("Show/Show.S01E02.1080p.mkv", "Show/进击的巨人 - S01E02.mkv"),
            ("Show/Show.S01E01.1080p.chs.ass", "Show/进击的巨人 - S01E01.chs.ass"),
        ],
    )

    fake = _FakeQb()
    plugin._qb_client = lambda downloader_name: fake
    plugin._wait_torrent_files = lambda hash_value, downloader_name, timeout=300.0: [
        {"name": "My.Movie.2020.1080p.mkv"},
    ]
    plugin._download_rename_job(
        "0123456789abcdef0123456789abcdef01234567",
        "我的电影 (2020)",
        MediaInfo(title="我的电影", year=2020, type=MediaType.MOVIE),
        [],
        "qb",
    )
    check("job single file torrent renamed", fake.torrent_renames, ["我的电影 (2020).mkv"])
    check(
        "job single file renamed",
        fake.file_renames,
        [("My.Movie.2020.1080p.mkv", "我的电影 (2020).mkv")],
    )

    # ---- 界面 ----
    form, config = plugin.get_form()
    check("form component", form[0]["component"], "VForm")
    check("config enabled", config["enabled"], True)
    check("config refresh_mode", config["refresh_mode"], "item")
    page = plugin.get_page()
    check("page list", isinstance(page, list) and len(page) >= 2, True)

    # ---- 立即运行一次 ----
    apis = {item["path"]: item for item in plugin.get_api()}
    check("api /run_now auth", apis.get("/run_now", {}).get("auth"), "bear")
    check("api /run_now methods", apis.get("/run_now", {}).get("methods"), ["POST"])
    check("api /preview auth", apis.get("/preview", {}).get("auth"), "bear")
    check("api /refresh_library auth", apis.get("/refresh_library", {}).get("auth"), "bear")
    check("config onlyonce default", config.get("onlyonce"), False)

    buttons = []

    def _collect_buttons(node):
        if isinstance(node, dict):
            if node.get("component") == "VBtn":
                buttons.append(node)
            for value in node.values():
                _collect_buttons(value)
        elif isinstance(node, list):
            for value in node:
                _collect_buttons(value)

    _collect_buttons(page)
    run_button = next((b for b in buttons if b.get("text") == "立即运行一次"), None)
    check("page has run button", run_button is not None, True)
    check(
        "run button api",
        (run_button or {}).get("events", {}).get("click", {}).get("api"),
        "plugin/PinyinSorter/run_now",
    )
    check("page button count", len(buttons), 3)

    submitted = []
    original_schedule = plugin._schedule_once_run
    plugin._schedule_once_run = lambda: (submitted.append(1) or True)
    result = asyncio.run(plugin.run_now())
    check("run_now success", result.get("success"), True)
    check("run_now scheduled", len(submitted), 1)

    plugin.init_plugin({"enabled": True, "movie_roots": str(movies), "onlyonce": True})
    check("onlyonce submitted", len(submitted), 2)
    check("onlyonce snapshot reset", plugin._config_snapshot.get("onlyonce"), False)
    check("onlyonce config saved", plugin._config.get("onlyonce"), False)
    plugin._schedule_once_run = original_schedule
finally:
    shutil.rmtree(workdir, ignore_errors=True)

log = "\n".join(f"{level} {args}" for level, args in logger.lines[-5:])
print("recent log:\n" + log)
print("FAILURES:" if failures else "ALL CHECKS PASSED")
for failure in failures:
    print(" -", failure)
