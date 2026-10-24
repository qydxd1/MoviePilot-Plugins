"""PinyinSorter 离线自测：用桩模块替代 MoviePilot 宿主，验证纯逻辑与文件操作。"""
import asyncio
import importlib.util
import os
import pathlib
import shutil
import sys
import tempfile
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
    def __init__(self, title="", en_title="", year=2023, type=None, category=None):
        self.title = title
        self.en_title = en_title
        self.year = year
        self.type = type
        self.category = category


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
mod("app.schemas.types", MediaType=MediaType, EventType=EventType)
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

    # ---- 界面 ----
    form, config = plugin.get_form()
    check("form component", form[0]["component"], "VForm")
    check("config enabled", config["enabled"], True)
    check("config refresh_mode", config["refresh_mode"], "item")
    page = plugin.get_page()
    check("page list", isinstance(page, list) and len(page) >= 2, True)
finally:
    shutil.rmtree(workdir, ignore_errors=True)

log = "\n".join(f"{level} {args}" for level, args in logger.lines[-5:])
print("recent log:\n" + log)
print("FAILURES:" if failures else "ALL CHECKS PASSED")
for failure in failures:
    print(" -", failure)
