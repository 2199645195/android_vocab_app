import json
import os
import random
import re
import hashlib
import threading
import urllib.parse
import urllib.request
import ssl
from urllib.error import HTTPError, URLError
from datetime import datetime

from kivy.app import App
from kivy.clock import Clock
from kivy.core.window import Window
from kivy.core.text import LabelBase
from kivy.metrics import dp
from kivy.properties import BooleanProperty, ListProperty, NumericProperty, ObjectProperty, StringProperty
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.popup import Popup
from kivy.uix.scrollview import ScrollView
from kivy.uix.spinner import Spinner, SpinnerOption
from kivy.uix.textinput import TextInput
from kivy.utils import platform


# =========================
# 中文字体支持
# =========================
APP_DIR = os.path.dirname(os.path.abspath(__file__))
FONT_CANDIDATES = [
    os.path.join(APP_DIR, "fonts", "NotoSansSC-VariableFont_wght.ttf"),
    "/system/fonts/NotoSansCJK-Regular.ttc",
    "/system/fonts/NotoSansSC-Regular.otf",
    "/system/fonts/DroidSansFallback.ttf",
]

CHINESE_FONT_PATH = next((path for path in FONT_CANDIDATES if os.path.exists(path)), "")
CHINESE_FONT_NAME = "ChineseFont"

if CHINESE_FONT_PATH:
    LabelBase.register(
        name=CHINESE_FONT_NAME,
        fn_regular=CHINESE_FONT_PATH,
        fn_bold=CHINESE_FONT_PATH,
        fn_italic=CHINESE_FONT_PATH,
        fn_bolditalic=CHINESE_FONT_PATH,
    )


def _apply_cn_font(kwargs):
    if CHINESE_FONT_PATH:
        kwargs.setdefault("font_name", CHINESE_FONT_NAME)
    return kwargs


class CNLabel(Label):
    def __init__(self, **kwargs):
        super().__init__(**_apply_cn_font(kwargs))


class CNButton(Button):
    def __init__(self, **kwargs):
        super().__init__(**_apply_cn_font(kwargs))


class CNSpinnerOption(SpinnerOption):
    def __init__(self, **kwargs):
        super().__init__(**_apply_cn_font(kwargs))


class CNSpinner(Spinner):
    def __init__(self, **kwargs):
        kwargs = _apply_cn_font(kwargs)
        kwargs.setdefault("option_cls", CNSpinnerOption)
        super().__init__(**kwargs)


def make_popup(title, message, size_hint=(0.88, 0.38)):
    label = CNLabel(
        text=message,
        halign="center",
        valign="middle",
        font_size=dp(16),
        padding=(dp(12), dp(12)),
    )
    label.bind(size=lambda inst, value: setattr(inst, "text_size", (max(1, value[0] - dp(20)), None)))
    popup_kwargs = {
        "title": title,
        "content": label,
        "size_hint": size_hint,
    }
    if CHINESE_FONT_PATH:
        popup_kwargs["title_font"] = CHINESE_FONT_NAME
    return Popup(**popup_kwargs)


POS_LABELS = {
    "n.": "名词 n.",
    "v.": "动词 v.",
    "adj.": "形容词 adj.",
    "adv.": "副词 adv.",
    "prep.": "介词 prep.",
    "conj.": "连词 conj.",
    "pron.": "代词 pron.",
    "num.": "数词 num.",
    "int.": "感叹词 int.",
}


def normalize_pos(pos_raw):
    pos_raw = pos_raw.strip().lower().replace(" ", "")
    if not pos_raw:
        return []
    pos_raw = pos_raw.replace("vt.", "v.").replace("vi.", "v.")
    parts = re.split(r"[/,，;；]+", pos_raw)
    result = []
    for part in parts:
        if not part:
            continue
        if not part.endswith("."):
            part += "."
        if part not in result:
            result.append(part)
    return result


def parse_word_line(line):
    if "|" in line:
        parts = [part.strip() for part in line.split("|")]
        if len(parts) >= 3 and parts[0] and parts[2]:
            return {
                "en": parts[0],
                "pos": normalize_pos(parts[1]),
                "zh": parts[2],
                "family": parts[3] if len(parts) >= 4 else "",
            }

    pattern = r"^([a-zA-Z][a-zA-Z\-\(\)/\s]*?)\s+(?:(n\.|v\.|vt\.|vi\.|adj\.|adv\.|prep\.|conj\.|num\.|int\.|pron\.)\s*)?(.+)$"
    match = re.match(pattern, line)
    if not match:
        return None

    en = re.sub(r"\s+", " ", match.group(1)).strip()
    zh = match.group(3).strip()
    if en and any("\u4e00" <= char <= "\u9fff" for char in zh):
        return {"en": en, "pos": normalize_pos(match.group(2) or ""), "zh": zh, "family": ""}
    return None


def parse_vocab_text(text):
    units = {}
    current_unit = None
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        unit_match = re.match(r"^Unit\s+(\d+)", line, re.I)
        if unit_match:
            current_unit = f"Unit {int(unit_match.group(1))}"
            units.setdefault(current_unit, [])
            continue
        if current_unit is None:
            continue
        item = parse_word_line(line)
        if item:
            units[current_unit].append(item)
    return units


def parse_vocab_file(path):
    with open(path, "r", encoding="utf-8-sig") as vocab_file:
        return parse_vocab_text(vocab_file.read())


class CachedWordAudio:
    """有道在线发音：后台下载、私有目录缓存、Android MediaPlayer 播放。"""

    def __init__(self, cache_dir, on_error=None):
        self.cache_dir = cache_dir
        os.makedirs(cache_dir, exist_ok=True)
        self.on_error = on_error
        self._lock = threading.Lock()
        self._generation = 0
        self._player = None
        self._closed = False
        self._inflight = set()
        self.error_message = ""

    def _cache_path(self, word):
        name = hashlib.sha256(word.lower().encode("utf-8")).hexdigest()
        return os.path.join(self.cache_dir, name + ".mp3")

    def _stop_player(self):
        if self._player is not None:
            try:
                self._player.stop()
            except Exception:
                pass
            try:
                self._player.release()
            except Exception:
                pass
            self._player = None

    def _notify_error(self, message, generation):
        def update_ui(_dt):
            if self._closed or generation != self._generation:
                return
            self.error_message = message
            if callable(self.on_error):
                self.on_error(message)
        Clock.schedule_once(update_ui, 0)

    def speak(self, word):
        word = (word or "").strip()
        if not word:
            return False
        self._generation += 1
        generation = self._generation
        self._stop_player()
        path = self._cache_path(word)
        if os.path.isfile(path) and os.path.getsize(path) > 100:
            self._play(path, generation)
        else:
            threading.Thread(target=self._download, args=(word, path, generation), daemon=True).start()
        return True

    def _play(self, path, generation):
        if self._closed or generation != self._generation:
            return
        self._stop_player()
        if platform == "android":
            try:
                from jnius import autoclass
                player = autoclass("android.media.MediaPlayer")()
                player.setDataSource(path)
                player.prepare()
                player.start()
                self._player = player
                return
            except Exception as exc:
                self._notify_error("下载成功但播放失败，请检查媒体音量。", generation)
                return
        try:
            from kivy.core.audio import SoundLoader
            player = SoundLoader.load(path)
            if player is None:
                raise RuntimeError("no local mp3 decoder")
            player.play()
            self._player = player
        except Exception:
            self._notify_error("下载成功，但当前设备无法播放 MP3 音频。", generation)

    @staticmethod
    def _validate_mp3(payload):
        if not (100 <= len(payload) <= 2 * 1024 * 1024):
            raise ValueError("返回的音频文件过小或过大")
        if not (payload.startswith(b"ID3") or
                (len(payload) >= 2 and payload[0] == 0xff and (payload[1] & 0xe0) == 0xe0)):
            raise ValueError("响应不是 MP3 音频（可能是服务器错误页面）")
        return payload

    @staticmethod
    def _error_detail(exc):
        """短错误文本，不显示 Java 堆栈或占满屏幕。"""
        if isinstance(exc, HTTPError):
            return "HTTP " + str(exc.code)
        if isinstance(exc, URLError):
            cause = getattr(exc, "reason", None)
            if isinstance(cause, ssl.SSLCertVerificationError):
                return "证书校验失败：请检查手机日期、VPN/网络代理或证书链"
            return "URLError: " + str(cause or exc)[:110]
        return (type(exc).__name__ + ": " + str(exc))[:125]

    def _fetch_python(self, url):
        request = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Linux; Android 12) AppleWebKit/537.36 Chrome/120.0 Mobile Safari/537.36",
            "Accept": "audio/mpeg,audio/*;q=0.9,*/*;q=0.5",
            "Referer": "https://dict.youdao.com/",
        })
        # Use the maintained certifi CA bundle, not an unverified SSL context.
        # On Python-for-Android, the built-in OpenSSL CA path can be empty.
        try:
            import certifi
        except ImportError as exc:
            raise RuntimeError("缺少 certifi 证书包，请在 buildozer.spec 的 requirements 中添加 certifi") from exc
        context = ssl.create_default_context(cafile=certifi.where())
        with urllib.request.urlopen(request, timeout=12, context=context) as response:
            return self._validate_mp3(response.read(2 * 1024 * 1024 + 1))

    def _fetch_android(self, url):
        """通过 Android Java HTTPS 栈下载，避免部分兼容环境中的 Python SSL/DNS 问题。"""
        from jnius import autoclass, jarray

        URL = autoclass("java.net.URL")
        conn = URL(url).openConnection()
        stream = None
        try:
            conn.setConnectTimeout(12000)
            conn.setReadTimeout(12000)
            conn.setRequestProperty("User-Agent", "Mozilla/5.0 (Linux; Android) AppleWebKit/537.36 Chrome/120.0 Mobile Safari/537.36")
            conn.setRequestProperty("Accept", "audio/mpeg,audio/*;q=0.9,*/*;q=0.5")
            conn.setRequestProperty("Referer", "https://dict.youdao.com/")
            conn.setInstanceFollowRedirects(True)
            status = conn.getResponseCode()
            if status != 200:
                raise ValueError("HTTP " + str(status))
            stream = conn.getInputStream()
            buf = jarray('b')(8192)
            content = bytearray()
            while True:
                n = stream.read(buf)
                if n == -1:
                    break
                if n == 0:
                    continue
                content.extend((int(b) & 0xff) for b in buf[:n])
                if len(content) > 2 * 1024 * 1024:
                    raise ValueError("音频超过 2MB 安全上限")
            return self._validate_mp3(bytes(content))
        finally:
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass
            try:
                conn.disconnect()
            except Exception:
                pass

    def _download(self, word, path, generation):
        """优先 Android 原生网络栈；使用有道发音并缓存。"""
        temp_path = None
        try:
            urls = [
                "https://dict.youdao.com/dictvoice?" + urllib.parse.urlencode({"audio": word, "type": kind})
                for kind in ("2", "1")
            ]
            errors = []
            content = None
            fetchers = ([ ("安卓网络", self._fetch_android), ("Python网络", self._fetch_python) ]
                        if platform == "android" else [("Python网络", self._fetch_python)])
            for url in urls:
                if self._closed or generation != self._generation:
                    return
                for label, fetch in fetchers:
                    try:
                        content = fetch(url)
                        break
                    except Exception as exc:
                        errors.append(label + " " + self._error_detail(exc))
                if content is not None:
                    break
            if content is None:
                # 只有一个简短的关键错误提示，方便手机截图反馈。
                unique = list(dict.fromkeys(errors))
                if any("证书" in e or "SSL" in e or "CertPath" in e for e in unique):
                    raise RuntimeError("HTTPS 证书校验失败。请确认手机日期正确，关闭 VPN/代理后重试。详情：" + "；".join(unique[:2])[:185])
                raise RuntimeError("有道下载失败：" + "；".join(unique[:3])[:285])
            if self._closed or generation != self._generation:
                return
            temp_path = path + "." + str(generation) + ".tmp"
            with open(temp_path, "wb") as output:
                output.write(content)
            with self._lock:
                os.replace(temp_path, path)
            temp_path = None
            Clock.schedule_once(lambda _dt: self._play(path, generation), 0)
        except Exception as exc:
            self._notify_error("发音失败：" + str(exc)[:300], generation)
        finally:
            if temp_path:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass

    def shutdown(self):
        self._closed = True
        self._generation += 1
        self._stop_player()


class VocabTrainer(BoxLayout):
    current_word = ObjectProperty(None, allownone=True)
    current_options = ListProperty([])
    quiz_mode = StringProperty("listening")
    order_mode = StringProperty("sequential")
    total_questions = NumericProperty(25)
    total_answered = NumericProperty(0)
    score = NumericProperty(0)
    listening_total = NumericProperty(0)
    listening_correct = NumericProperty(0)
    is_answering = BooleanProperty(False)

    def __init__(self, **kwargs):
        super().__init__(orientation="vertical", spacing=dp(10), padding=[dp(14), dp(58), dp(14), dp(18)], **kwargs)
        Window.clearcolor = (0.96, 0.97, 0.98, 1)
        self.units = {}
        self.selected_unit = "Unit 6"
        self.all_words = []
        self.test_pool = []
        self.sequential_key = None
        self.sequential_cursor = 0
        self.data_dir = App.get_running_app().user_data_dir
        os.makedirs(self.data_dir, exist_ok=True)
        self.audio = CachedWordAudio(os.path.join(self.data_dir, "pronunciation_cache"), on_error=self.on_audio_error)
        self.progress_path = os.path.join(self.data_dir, "progress.json")
        self.wrong_path = os.path.join(self.data_dir, "wrong_words.jsonl")
        self.stats_path = os.path.join(self.data_dir, "stats.json")
        self.custom_units_path = os.path.join(self.data_dir, "custom_units.json")
        self.load_vocab()
        self.build_menu()

    def load_vocab(self):
        app_dir = os.path.dirname(os.path.abspath(__file__))
        vocab_path = os.path.join(app_dir, "data", "vocab.txt")

        builtin_units = parse_vocab_file(vocab_path) if os.path.exists(vocab_path) else {}
        custom_units = self.load_json(self.custom_units_path, {}) if hasattr(self, "custom_units_path") else {}

        self.units = {name: list(words) for name, words in builtin_units.items()}
        for unit_name, words in custom_units.items():
            if not isinstance(words, list):
                continue
            self.units.setdefault(unit_name, [])
            existing = {item.get("en", "").lower() for item in self.units[unit_name]}
            for item in words:
                if not isinstance(item, dict) or not item.get("en") or not item.get("zh"):
                    continue
                if item.get("en", "").lower() not in existing:
                    self.units[unit_name].append(item)
                    existing.add(item.get("en", "").lower())

        if self.units:
            names = self.sorted_unit_names()
            if self.selected_unit not in self.units:
                self.selected_unit = names[0]

    def sorted_unit_names(self):
        def sort_key(name):
            match = re.search(r"\d+", name)
            return (0, int(match.group())) if match else (1, name.lower())
        return sorted(self.units, key=sort_key)

    def clear(self):
        self.clear_widgets()

    def build_menu(self, *_):
        self.clear()
        title = CNLabel(
            text="四级单词训练",
            font_size=dp(26),
            bold=True,
            size_hint_y=None,
            height=dp(46),
            color=(0.08, 0.12, 0.18, 1),
        )
        self.add_widget(title)

        unit_names = self.sorted_unit_names()
        self.unit_spinner = CNSpinner(
            text=self.selected_unit,
            values=unit_names,
            size_hint_y=None,
            height=dp(46),
            background_color=(0.13, 0.45, 0.85, 1),
        )
        self.unit_spinner.bind(text=self.set_unit)
        self.add_widget(self.unit_spinner)

        import_button = CNButton(
            text="📄 导入词库文件（TXT）",
            font_size=dp(17),
            size_hint_y=None,
            height=dp(44),
            background_color=(0.16, 0.48, 0.68, 1),
        )
        import_button.bind(on_release=self.import_vocab_file)
        self.add_widget(import_button)

        add_unit_button = CNButton(
            text="＋ 手动添加单元 / 单词",
            font_size=dp(17),
            size_hint_y=None,
            height=dp(44),
            background_color=(0.20, 0.38, 0.62, 1),
        )
        add_unit_button.bind(on_release=self.show_add_unit_popup)
        self.add_widget(add_unit_button)

        self.mode_spinner = CNSpinner(
            text="单词听音四选一",
            values=["单词听音四选一", "中文释义", "词性判断", "综合测试"],
            size_hint_y=None,
            height=dp(46),
        )
        self.add_widget(self.mode_spinner)

        self.order_spinner = CNSpinner(
            text="正序自动记录进度",
            values=["正序自动记录进度", "乱序随机练习"],
            size_hint_y=None,
            height=dp(46),
        )
        self.add_widget(self.order_spinner)

        self.count_spinner = CNSpinner(
            text="每天 25 个",
            values=["每天 10 个", "每天 15 个", "每天 20 个", "每天 25 个", "每天 30 个", "全部"],
            size_hint_y=None,
            height=dp(46),
        )
        self.add_widget(self.count_spinner)

        progress = self.load_progress()
        preview_key = self.make_progress_key(self.selected_unit, "listening")
        learned = int(progress.get(preview_key, 0))
        total = len(self.units.get(self.selected_unit, []))
        self.menu_info = CNLabel(
            text=f"{self.selected_unit} 共 {total} 个词\n正序进度：{min(learned, total)} / {total}",
            font_size=dp(17),
            halign="center",
            valign="middle",
            color=(0.2, 0.25, 0.32, 1),
        )
        self.menu_info.bind(size=lambda label, size: setattr(label, "text_size", size))
        self.add_widget(self.menu_info)

        start_button = CNButton(
            text="开始",
            font_size=dp(20),
            bold=True,
            size_hint_y=None,
            height=dp(54),
            background_color=(0.12, 0.62, 0.36, 1),
        )
        start_button.bind(on_release=self.start_quiz)
        self.add_widget(start_button)

        wrong_button = CNButton(text="查看错题数量", size_hint_y=None, height=dp(44))
        wrong_button.bind(on_release=self.show_wrong_count)
        self.add_widget(wrong_button)

    def set_unit(self, _spinner, value):
        self.selected_unit = value
        self.build_menu()

    def on_audio_error(self, message):
        # 听音失败时不打断答题；明确提示当前题可选择再试。
        if hasattr(self, "feedback_label") and self.current_word:
            if self.is_answering:
                self.extra_label.text = message

    def import_vocab_file(self, *_):
        """在 Android 上调用系统文件选择器，选择并导入 .txt 词库。"""
        if platform != "android":
            self.show_popup("提示", "文件选择导入功能需要在 Android 手机上使用。")
            return

        try:
            from jnius import autoclass
            from android import activity

            Intent = autoclass("android.content.Intent")
            PythonActivity = autoclass("org.kivy.android.PythonActivity")

            self._file_request_code = 24680
            self._android_activity = activity
            try:
                activity.unbind(on_activity_result=self._on_activity_result)
            except Exception:
                pass
            activity.bind(on_activity_result=self._on_activity_result)

            intent = Intent(Intent.ACTION_OPEN_DOCUMENT)
            intent.addCategory(Intent.CATEGORY_OPENABLE)
            intent.setType("text/plain")
            intent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
            PythonActivity.mActivity.startActivityForResult(intent, self._file_request_code)
        except Exception as exc:
            self.show_popup("导入失败", f"无法打开系统文件选择器：\n{exc}")

    def _on_activity_result(self, request_code, result_code, intent):
        if request_code != getattr(self, "_file_request_code", 24680):
            return

        activity_module = getattr(self, "_android_activity", None)
        if activity_module is not None:
            try:
                activity_module.unbind(on_activity_result=self._on_activity_result)
            except Exception:
                pass

        try:
            from jnius import autoclass

            Activity = autoclass("android.app.Activity")
            if int(result_code) != int(Activity.RESULT_OK) or intent is None:
                return

            uri = intent.getData()
            if uri is None:
                self.show_popup("导入失败", "没有获取到所选文件。")
                return

            text = self._read_android_text_uri(uri)
            self._import_vocab_text(text)
        except Exception as exc:
            self.show_popup("导入失败", f"读取词库文件失败：\n{exc}")

    def _read_android_text_uri(self, uri):
        from jnius import autoclass

        PythonActivity = autoclass("org.kivy.android.PythonActivity")
        InputStreamReader = autoclass("java.io.InputStreamReader")
        BufferedReader = autoclass("java.io.BufferedReader")

        resolver = PythonActivity.mActivity.getContentResolver()
        stream = resolver.openInputStream(uri)
        if stream is None:
            raise RuntimeError("无法打开文件流")

        reader = None
        try:
            reader = BufferedReader(InputStreamReader(stream, "UTF-8"))
            lines = []
            while True:
                line = reader.readLine()
                if line is None:
                    break
                lines.append(str(line))
            return "\n".join(lines)
        finally:
            try:
                if reader is not None:
                    reader.close()
                else:
                    stream.close()
            except Exception:
                pass

    def _import_vocab_text(self, text):
        units = parse_vocab_text(text)
        units = {name: words for name, words in units.items() if words}
        if not units:
            self.show_popup(
                "无法识别",
                "没有识别到有效单元。\n\nTXT 文件需要包含例如：\nUnit 1\nword | n. | 中文释义",
            )
            return

        custom_units = self.load_json(self.custom_units_path, {})
        total_added = 0
        imported_names = []

        for unit_name in self._sort_unit_dict_names(units):
            words = units[unit_name]
            custom_units.setdefault(unit_name, [])
            existing = {
                item.get("en", "").strip().lower()
                for item in custom_units[unit_name]
                if isinstance(item, dict)
            }
            added_this_unit = 0
            for item in words:
                key = item.get("en", "").strip().lower()
                if key and key not in existing:
                    custom_units[unit_name].append(item)
                    existing.add(key)
                    total_added += 1
                    added_this_unit += 1
            imported_names.append(f"{unit_name}（新增 {added_this_unit}）")

        self.save_json(self.custom_units_path, custom_units)
        first_unit = self._sort_unit_dict_names(units)[0]
        self.selected_unit = first_unit
        self.load_vocab()
        self.build_menu()
        self.show_popup(
            "导入成功",
            "已导入：\n" + "\n".join(imported_names) + f"\n\n共新增 {total_added} 个单词。",
        )

    @staticmethod
    def _sort_unit_dict_names(mapping):
        def key(name):
            match = re.search(r"\d+", name)
            return (0, int(match.group())) if match else (1, name.lower())
        return sorted(mapping, key=key)

    def show_add_unit_popup(self, *_):
        content = BoxLayout(orientation="vertical", spacing=dp(10), padding=dp(12))

        tip = CNLabel(
            text="输入单元编号和单词。\n单词格式：英文 | 词性 | 中文释义 | 关联词(可选)",
            size_hint_y=None,
            height=dp(70),
            halign="left",
            valign="middle",
            color=(0.12, 0.16, 0.22, 1),
        )
        tip.bind(size=lambda label, size: setattr(label, "text_size", size))
        content.add_widget(tip)

        unit_input = TextInput(
            hint_text="例如：7 或 Unit 7",
            multiline=False,
            size_hint_y=None,
            height=dp(46),
        )
        if CHINESE_FONT_PATH:
            unit_input.font_name = CHINESE_FONT_NAME
        content.add_widget(unit_input)

        words_input = TextInput(
            hint_text="ability | n. | 能力\naccept | v. | 接受\nactive | adj. | 积极的",
            multiline=True,
        )
        if CHINESE_FONT_PATH:
            words_input.font_name = CHINESE_FONT_NAME
        content.add_widget(words_input)

        button_row = BoxLayout(size_hint_y=None, height=dp(46), spacing=dp(8))
        cancel_button = CNButton(text="取消")
        save_button = CNButton(text="保存", background_color=(0.12, 0.62, 0.36, 1))
        button_row.add_widget(cancel_button)
        button_row.add_widget(save_button)
        content.add_widget(button_row)

        popup_kwargs = {
            "title": "添加单元 / 单词",
            "content": content,
            "size_hint": (0.92, 0.78),
        }
        if CHINESE_FONT_PATH:
            popup_kwargs["title_font"] = CHINESE_FONT_NAME
        popup = Popup(**popup_kwargs)

        cancel_button.bind(on_release=popup.dismiss)
        save_button.bind(on_release=lambda *_: self.save_custom_unit(unit_input.text, words_input.text, popup))
        popup.open()

    def save_custom_unit(self, unit_text, words_text, popup):
        unit_text = (unit_text or "").strip()
        number_match = re.search(r"\d+", unit_text)
        if not number_match:
            self.show_popup("提示", "请输入单元编号，例如 7 或 Unit 7。")
            return

        unit_name = f"Unit {int(number_match.group())}"
        parsed_words = []
        invalid_lines = []
        for raw in (words_text or "").splitlines():
            line = raw.strip()
            if not line:
                continue
            item = parse_word_line(line)
            if item:
                parsed_words.append(item)
            else:
                invalid_lines.append(line)

        if not parsed_words:
            self.show_popup("提示", "没有识别到有效单词。\n请按：英文 | 词性 | 中文释义")
            return

        custom_units = self.load_json(self.custom_units_path, {})
        custom_units.setdefault(unit_name, [])
        existing = {item.get("en", "").lower() for item in custom_units[unit_name] if isinstance(item, dict)}
        added = 0
        for item in parsed_words:
            key = item["en"].lower()
            if key not in existing:
                custom_units[unit_name].append(item)
                existing.add(key)
                added += 1

        self.save_json(self.custom_units_path, custom_units)
        self.selected_unit = unit_name
        self.load_vocab()
        popup.dismiss()
        self.build_menu()

        message = f"{unit_name} 已保存，新增 {added} 个单词。"
        if invalid_lines:
            message += f"\n有 {len(invalid_lines)} 行格式无法识别，已跳过。"
        self.show_popup("保存成功", message)

    def make_progress_key(self, unit, mode):
        return f"builtin_vocab|{unit}|{mode}"

    def load_json(self, path, default):
        if not os.path.exists(path):
            return default
        try:
            with open(path, "r", encoding="utf-8") as json_file:
                return json.load(json_file)
        except Exception:
            return default

    def save_json(self, path, data):
        with open(path, "w", encoding="utf-8") as json_file:
            json.dump(data, json_file, ensure_ascii=False, indent=2)

    def load_progress(self):
        return self.load_json(self.progress_path, {})

    def save_progress(self, key, index):
        data = self.load_progress()
        data[key] = index
        self.save_json(self.progress_path, data)

    def start_quiz(self, *_):
        mode_map = {
            "单词听音四选一": "listening",
            "中文释义": "meaning",
            "词性判断": "pos",
            "综合测试": "mixed",
        }
        self.quiz_mode = mode_map[self.mode_spinner.text]
        self.order_mode = "sequential" if self.order_spinner.text.startswith("正序") else "shuffle"
        words = list(self.units.get(self.selected_unit, []))
        if len(words) < 4:
            self.show_popup("提示", "当前词库至少需要 4 个单词。")
            return

        if self.count_spinner.text == "全部":
            requested = len(words)
        else:
            requested = int(re.search(r"\d+", self.count_spinner.text).group())

        self.all_words = words
        self.total_answered = 0
        self.score = 0
        self.listening_total = 0
        self.listening_correct = 0

        if self.order_mode == "sequential":
            self.sequential_key = self.make_progress_key(self.selected_unit, self.quiz_mode)
            progress = self.load_progress()
            start = int(progress.get(self.sequential_key, 0))
            if start >= len(words):
                start = 0
                self.save_progress(self.sequential_key, 0)
            end = min(start + requested, len(words))
            self.sequential_cursor = start
            self.test_pool = words[start:end]
        else:
            self.sequential_key = None
            self.test_pool = random.sample(words, min(requested, len(words)))
            random.shuffle(self.test_pool)

        self.total_questions = len(self.test_pool)
        self.build_quiz_ui()
        self.next_question()

    def build_quiz_ui(self):
        self.clear()
        top = BoxLayout(orientation="horizontal", size_hint_y=None, height=dp(38), spacing=dp(8))
        self.score_label = CNLabel(text="答对：0", color=(0.08, 0.12, 0.18, 1), bold=True)
        self.progress_label = CNLabel(text=f"进度：0 / {self.total_questions}", color=(0.13, 0.45, 0.85, 1), bold=True)
        top.add_widget(self.score_label)
        top.add_widget(self.progress_label)
        self.add_widget(top)

        self.type_label = CNLabel(text="", font_size=dp(19), bold=True, size_hint_y=None, height=dp(34), color=(0.13, 0.45, 0.85, 1))
        self.add_widget(self.type_label)

        self.word_label = CNLabel(text="", font_size=dp(34), bold=True, color=(0.08, 0.12, 0.18, 1))
        self.word_label.bind(size=lambda label, size: setattr(label, "text_size", size))
        self.add_widget(self.word_label)

        speak_button = CNButton(text="再听一次", font_size=dp(18), size_hint_y=None, height=dp(50), background_color=(0.13, 0.45, 0.85, 1))
        speak_button.bind(on_release=lambda *_: self.repeat_pronunciation())
        self.add_widget(speak_button)

        self.extra_label = CNLabel(text="", font_size=dp(16), size_hint_y=None, height=dp(56), color=(0.26, 0.31, 0.38, 1))
        self.extra_label.bind(size=lambda label, size: setattr(label, "text_size", size))
        self.add_widget(self.extra_label)

        self.option_buttons = []
        for index in range(4):
            button = CNButton(
                text="",
                font_size=dp(17),
                size_hint_y=None,
                height=dp(58),
                halign="left",
                valign="middle",
                background_normal="",
                background_color=(1, 1, 1, 1),
                color=(0.08, 0.12, 0.18, 1),
            )
            button.bind(size=lambda btn, size: setattr(btn, "text_size", (size[0] - dp(24), size[1])))
            button.bind(on_release=lambda btn, idx=index: self.check_answer(idx))
            self.option_buttons.append(button)
            self.add_widget(button)

        self.feedback_label = CNLabel(text="", font_size=dp(16), size_hint_y=None, height=dp(72), color=(0.2, 0.25, 0.32, 1))
        self.feedback_label.bind(size=lambda label, size: setattr(label, "text_size", size))
        self.add_widget(self.feedback_label)

        bottom = BoxLayout(orientation="horizontal", size_hint_y=None, height=dp(46), spacing=dp(8))
        self.next_button = CNButton(text="下一题", disabled=True)
        self.next_button.bind(on_release=lambda *_: self.next_question())
        end_button = CNButton(text="结束")
        end_button.bind(on_release=lambda *_: self.end_test())
        bottom.add_widget(self.next_button)
        bottom.add_widget(end_button)
        self.add_widget(bottom)

    def next_question(self, *_):
        if not self.test_pool:
            self.end_test()
            return

        self.current_word = self.test_pool.pop(0)
        self.is_answering = True
        self.next_button.disabled = True
        self.feedback_label.text = ""
        for button in self.option_buttons:
            button.disabled = False
            button.background_color = (1, 1, 1, 1)
            button.color = (0.08, 0.12, 0.18, 1)

        if self.quiz_mode == "listening":
            self.build_listening_question()
        elif self.quiz_mode == "meaning":
            self.build_meaning_question()
        elif self.quiz_mode == "pos":
            self.build_pos_question()
        else:
            random.choice([self.build_meaning_question, self.build_pos_question])()

        self.progress_label.text = f"进度：{self.total_answered} / {self.total_questions}"

    def build_listening_question(self):
        self.type_label.text = "单词听音四选一"
        self.word_label.text = "听发音，选单词"
        self.extra_label.text = "听音题只显示英文选项。"
        correct = self.current_word
        pool = [word for word in self.all_words if word["en"] != correct["en"]]
        options = random.sample(pool, 3) + [correct]
        random.shuffle(options)
        self.current_options = options
        for index, button in enumerate(self.option_buttons):
            button.text = f"{chr(65 + index)}. {options[index]['en']}"
        Clock.schedule_once(lambda *_: self.speak_word(correct["en"]) if self.current_word and self.current_word.get("en") == correct["en"] else None, 0.25)

    def build_meaning_question(self):
        self.type_label.text = "中文释义"
        self.word_label.text = self.current_word["en"]
        pos_text = " / ".join(self.current_word.get("pos", [])) or "未标注"
        self.extra_label.text = f"词性：{pos_text}"
        correct = self.current_word
        pool = [word for word in self.all_words if word["en"] != correct["en"] and word["zh"] != correct["zh"]]
        distractors = random.sample(pool, min(3, len(pool)))
        if len(distractors) < 3:
            backup = [word for word in self.all_words if word["en"] != correct["en"] and word not in distractors]
            distractors += random.sample(backup, 3 - len(distractors))
        options = distractors + [correct]
        random.shuffle(options)
        self.current_options = options
        for index, button in enumerate(self.option_buttons):
            button.text = f"{chr(65 + index)}. {options[index]['zh']}"
        self.speak_word(correct["en"])

    def build_pos_question(self):
        if not self.current_word.get("pos"):
            self.build_meaning_question()
            return
        self.type_label.text = "词性判断"
        self.word_label.text = self.current_word["en"]
        self.extra_label.text = f"中文：{self.current_word['zh']}"
        correct_text = " / ".join(POS_LABELS.get(pos, pos) for pos in self.current_word["pos"])
        candidates = []
        for pos in ["n.", "v.", "adj.", "adv.", "prep.", "conj.", "pron."]:
            text = POS_LABELS.get(pos, pos)
            if text != correct_text and pos not in self.current_word["pos"]:
                candidates.append(text)
        random.shuffle(candidates)
        options = candidates[:3] + [correct_text]
        random.shuffle(options)
        self.current_options = options
        for index, button in enumerate(self.option_buttons):
            button.text = f"{chr(65 + index)}. {options[index]}"
        self.speak_word(self.current_word["en"])

    def check_answer(self, idx):
        if not self.is_answering:
            return
        self.is_answering = False
        self.total_answered += 1
        if self.order_mode == "sequential" and self.sequential_key:
            self.sequential_cursor += 1
            self.save_progress(self.sequential_key, self.sequential_cursor)

        correct_idx = 0
        is_correct = False
        if self.type_label.text == "单词听音四选一":
            self.listening_total += 1
            correct_idx = next(i for i, option in enumerate(self.current_options) if option["en"] == self.current_word["en"])
            is_correct = self.current_options[idx]["en"] == self.current_word["en"]
        elif self.type_label.text == "中文释义":
            correct_idx = next(i for i, option in enumerate(self.current_options) if option["en"] == self.current_word["en"])
            is_correct = self.current_options[idx]["en"] == self.current_word["en"]
        else:
            correct_text = " / ".join(POS_LABELS.get(pos, pos) for pos in self.current_word["pos"])
            correct_idx = self.current_options.index(correct_text)
            is_correct = self.current_options[idx] == correct_text

        for button in self.option_buttons:
            button.disabled = True
        self.option_buttons[correct_idx].background_color = (0.1, 0.62, 0.36, 1)
        self.option_buttons[correct_idx].color = (1, 1, 1, 1)

        pos_text = " / ".join(self.current_word.get("pos", [])) or "未标注"
        detail = f"{self.current_word['en']}  {pos_text}\n{self.current_word['zh']}"
        if self.current_word.get("family"):
            detail += f"\n关联：{self.current_word['family']}"

        if is_correct:
            self.score += 1
            if self.type_label.text == "单词听音四选一":
                self.listening_correct += 1
            self.feedback_label.text = f"答对了\n{detail}"
            self.feedback_label.color = (0.1, 0.55, 0.32, 1)
            self.score_label.text = f"答对：{self.score}"
            Clock.schedule_once(lambda *_: self.next_question(), 0.75)
        else:
            self.option_buttons[idx].background_color = (0.9, 0.18, 0.2, 1)
            self.option_buttons[idx].color = (1, 1, 1, 1)
            self.feedback_label.text = f"没选对，正确答案：\n{detail}"
            self.feedback_label.color = (0.75, 0.12, 0.14, 1)
            self.save_wrong_word(self.current_word, self.type_label.text)
            self.next_button.disabled = False

        self.progress_label.text = f"进度：{self.total_answered} / {self.total_questions}"
        if self.type_label.text == "单词听音四选一":
            self.save_stats()

    def repeat_pronunciation(self):
        if self.current_word:
            self.speak_word(self.current_word["en"])

    def speak_word(self, word):
        return self.audio.speak(word)

    def save_wrong_word(self, word, qtype):
        existing = self.load_wrong_words()
        key = (word["en"], qtype)
        if any((item.get("en"), item.get("question_type")) == key for item in existing):
            return
        item = dict(word)
        item["question_type"] = qtype
        item["created_at"] = datetime.now().isoformat(timespec="seconds")
        with open(self.wrong_path, "a", encoding="utf-8") as wrong_file:
            wrong_file.write(json.dumps(item, ensure_ascii=False) + "\n")

    def load_wrong_words(self):
        if not os.path.exists(self.wrong_path):
            return []
        items = []
        with open(self.wrong_path, "r", encoding="utf-8") as wrong_file:
            for line in wrong_file:
                if line.strip():
                    items.append(json.loads(line))
        return items

    def save_stats(self):
        stats = self.load_json(self.stats_path, {"listening_total": 0, "listening_correct": 0})
        stats["listening_total"] = int(stats.get("listening_total", 0)) + 1
        if self.current_options and self.option_buttons:
            stats["listening_correct"] = int(stats.get("listening_correct", 0)) + (1 if self.feedback_label.text.startswith("答对") else 0)
        self.save_json(self.stats_path, stats)

    def show_wrong_count(self, *_):
        wrong = self.load_wrong_words()
        listening = sum(1 for item in wrong if item.get("question_type") == "单词听音四选一")
        meaning = sum(1 for item in wrong if item.get("question_type") == "中文释义")
        pos = sum(1 for item in wrong if item.get("question_type") == "词性判断")
        stats = self.load_json(self.stats_path, {"listening_total": 0, "listening_correct": 0})
        total = int(stats.get("listening_total", 0))
        correct = int(stats.get("listening_correct", 0))
        acc = f"{correct / total * 100:.1f}%" if total else "暂无"
        self.show_popup("学习记录", f"累计错题：{len(wrong)}\n听音错题：{listening}\n释义错题：{meaning}\n词性错题：{pos}\n\n听音正确率：{acc}")

    def end_test(self, *_):
        if self.total_answered == 0:
            self.build_menu()
            return
        acc = self.score / self.total_answered * 100
        message = f"本次完成：{self.total_answered} 个\n答对：{self.score} 个\n正确率：{acc:.1f}%"
        if self.order_mode == "sequential" and self.sequential_key:
            if self.sequential_cursor >= len(self.all_words):
                message += "\n\n本单元正序已学完，下次会从第 1 个词重新开始。"
            else:
                message += f"\n\n进度已保存，下次从第 {self.sequential_cursor + 1} 个词继续。"
        popup = make_popup("练习报告", message)
        popup.bind(on_dismiss=lambda *_: self.build_menu())
        popup.open()

    def show_popup(self, title, message):
        make_popup(title, message).open()


class VocabTrainerApp(App):
    def build(self):
        self.title = "四级单词训练"
        self.trainer = VocabTrainer()
        return self.trainer

    def on_stop(self):
        try:
            self.trainer.audio.shutdown()
        except Exception:
            pass


if __name__ == "__main__":
    VocabTrainerApp().run()
