import json
import io
import zipfile
import shutil
import math
import time
import os
import random
import re
import hashlib
import threading
import urllib.parse
import urllib.request
import ssl
from urllib.error import HTTPError, URLError
from datetime import datetime, date, timedelta

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
from kivy.graphics import Color, RoundedRectangle, Rectangle
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
        self.review_path = os.path.join(self.data_dir, "review_schedule.json")
        self.activity_path = os.path.join(self.data_dir, "study_activity.json")
        self.favorites_path = os.path.join(self.data_dir, "favorites.json")
        self.cards_path = os.path.join(self.data_dir, "card_schedule.json")
        self.details_path = os.path.join(self.data_dir, "word_details.json")
        self.card_queue = []
        self.card_current = None
        self.card_revealed = False
        self._saf_action = "import_vocab"

        self.review_mode = False
        self.ui_mode_text = "单词听音四选一"
        self.ui_order_text = "正序自动记录进度"
        self.ui_count_text = "每天 25 个"
        self.active_tab = "home"
        self._advance_event = None
        self.session_words = set()
        self.session_wrong = 0
        self.review_schedule = self.load_json(self.review_path, {})
        if not isinstance(self.review_schedule, dict):
            self.review_schedule = {}
        self._migrate_wrong_words()
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

    # V1.3 的 UI 专用组件：不改变词库、答题、复习与音频实现。
    def _panel(self, parent, bg=(1, 1, 1, 1), pad=15, gap=10):
        panel = BoxLayout(orientation="vertical", size_hint_y=None, padding=dp(pad), spacing=dp(gap))
        panel.bind(minimum_height=panel.setter("height"))
        with panel.canvas.before:
            shade = Color(*bg)
            rect = RoundedRectangle(pos=panel.pos, size=panel.size, radius=[dp(16)])
        panel.bind(pos=lambda inst, value: setattr(rect, "pos", value),
                   size=lambda inst, value: setattr(rect, "size", value))
        parent.add_widget(panel)
        return panel

    def _label(self, parent, value, height=33, size=16, dark=True, bold=False, align="left"):
        lab = CNLabel(text=str(value), font_size=dp(size), bold=bold, size_hint_y=None,
                      height=dp(height), color=(0.12, 0.21, 0.24, 1) if dark else (0.39, 0.46, 0.49, 1),
                      halign=align, valign="middle")
        lab.bind(size=lambda obj, val: setattr(obj, "text_size", (max(1, val[0]), None)))
        parent.add_widget(lab)
        return lab

    def _action(self, parent, label, handler, color=(0.12, 0.42, 0.35, 1), height=52):
        btn = CNButton(text=label, font_size=dp(16), size_hint_y=None, height=dp(height),
                       background_normal="", background_color=color)
        btn.bind(on_release=handler)
        parent.add_widget(btn)
        return btn

    def _spinner(self, parent, value, values, callback=None):
        spin = CNSpinner(text=value, values=values, size_hint_y=None, height=dp(49),
                         background_normal="", background_color=(0.90, 0.95, 0.93, 1),
                         color=(0.10, 0.28, 0.25, 1), font_size=dp(16))
        if callback:
            spin.bind(text=callback)
        parent.add_widget(spin)
        return spin

    def _change_tab(self, tab):
        self.active_tab = tab
        self.build_menu()

    def _change_unit(self, _spinner, name):
        if name != self.selected_unit:
            self.selected_unit = name
            self.build_menu()

    def build_menu(self, *_):
        """V1.3: 五个清晰的页面 + 常驻底部导航，业务功能保留。"""
        self.clear()
        Window.clearcolor = (0.945, 0.965, 0.961, 1)
        scroll = ScrollView(do_scroll_x=False, bar_width=dp(3))
        body = BoxLayout(orientation="vertical", size_hint_y=None, spacing=dp(12),
                         padding=[dp(2), dp(9), dp(2), dp(16)])
        body.bind(minimum_height=body.setter("height"))
        scroll.add_widget(body)
        self.add_widget(scroll)

        names = {"home": "今日学习", "library": "我的词库", "wrong": "错题与复习", "stats": "学习数据", "settings": "学习设置"}
        self._label(body, "四级单词训练", height=54, size=27, bold=True)
        self._label(body, names.get(self.active_tab, "今日学习"), height=36, size=19, bold=True)

        if self.active_tab == "home":
            summary = self._panel(body, bg=(0.12, 0.42, 0.35, 1))
            self._label(summary, "今天继续进步一点", height=35, size=19, dark=True, bold=True).color=(1,1,1,1)
            activity = self.load_json(self.activity_path, {})
            td = activity.get(date.today().isoformat(), {}) if isinstance(activity,dict) else {}
            td = td if isinstance(td,dict) else {}
            done = int(td.get('answered',0)); correct=int(td.get('correct',0))
            acc=correct/done*100 if done else 0
            target_match = re.search(r'\d+', self.ui_count_text)
            daily_goal = int(target_match.group()) if target_match else len(self.units.get(self.selected_unit,[]))
            self._label(summary, f"今日练习  {done} / {daily_goal} 题", height=50,size=23,bold=True).color=(1,1,1,1)
            self._label(summary, f"今日正确率 {acc:.1f}%     待复习 {len(self.get_due_words())} 词",height=38,size=14).color=(0.86,0.96,0.92,1)
            wordpanel = self._panel(body)
            self._label(wordpanel,"当前学习单元",size=15, dark=False)
            self.unit_spinner = self._spinner(wordpanel,self.selected_unit,self.sorted_unit_names(),self._change_unit)
            total=len(self.units.get(self.selected_unit,[]))
            done_idx=int(self.load_progress().get(self.make_progress_key(self.selected_unit,"listening"),0))
            self._label(wordpanel,f"正序进度  {min(done_idx,total)} / {total} 词",size=16,bold=True)
            actions=self._panel(body)
            self._label(actions,"开始今天的任务",size=18,bold=True)
            self._action(actions,"开始学习",self.start_quiz,height=57)
            self._action(actions,"单词卡片 · 认识/模糊/不认识",lambda *_:self.start_cards('unit'),color=(.22,.39,.57,1))
            self._action(actions,f"智能复习 · {len(self.get_due_words())} 词",self.start_review,color=(0.72,0.45,0.18,1),height=57)
            self._label(actions,"练习模式和每日数量可在底部「设置」调整",size=12,dark=False,height=25)
        elif self.active_tab == "library":
            panel=self._panel(body)
            self._label(panel,"单元选择",bold=True)
            self.unit_spinner=self._spinner(panel,self.selected_unit,self.sorted_unit_names(),self._change_unit)
            self._label(panel,f"当前单元：{len(self.units.get(self.selected_unit, []))} 个单词",dark=False)
            self._action(panel,"从手机导入 TXT 词库",self.import_vocab_file)
            self._action(panel,"手动添加单元和单词",self.show_add_unit_popup,color=(0.22,0.39,0.56,1))
            self._action(panel,"搜索单词 / 编辑音标例句 / 收藏",self.show_word_search)
            self._action(panel,"我的收藏夹",self.show_favorites,color=(.22,.39,.57,1))
            self._action(panel,"复习收藏单词卡片",lambda *_:self.start_cards('favorites'))
            self._label(panel,"导入后单元列表立即更新，新词库保存在手机内",size=13,dark=False,height=48)
        elif self.active_tab == "wrong":
            panel=self._panel(body)
            records=[v for v in self.review_schedule.values() if isinstance(v,dict) and int(v.get('wrong',0))>0]
            self._label(panel,f"历史错词  {len(records)} 个",height=54,size=23,bold=True)
            self._label(panel,f"到期复习  {len(self.get_due_words())} 个",height=38,size=18)
            self._action(panel,"查看具体错词与错误次数",self.show_wrong_book)
            self._action(panel,"开始到期复习",self.start_review,color=(0.72,0.45,0.18,1))
            self._action(panel,"到期记忆卡片复习",lambda *_:self.start_cards('due'))
            self._action(panel,"查看旧版错题分类",self.show_wrong_count,color=(0.34,0.43,0.47,1))
        elif self.active_tab == "stats":
            activity=self.load_json(self.activity_path,{})
            activity=activity if isinstance(activity,dict) else {}
            td=activity.get(date.today().isoformat(),{})
            td=td if isinstance(td,dict) else {}
            answered=int(td.get('answered',0)); correct=int(td.get('correct',0))
            panel=self._panel(body)
            self._label(panel,"今日学习概览",size=18,bold=True)
            self._label(panel,f"答题数量    {answered} 题",height=44,size=21,bold=True)
            self._label(panel,f"今日答对    {correct} 题",height=44,size=20)
            self._label(panel,f"今日正确率    {correct / answered * 100 if answered else 0:.1f}%",height=44,size=20)
            self._action(panel,"查看完整学习统计报告",self.show_dashboard)
            self._action(panel,"近28天学习日历与趋势",self.show_calendar)
        elif self.active_tab == "settings":
            panel=self._panel(body)
            self._label(panel,"练习模式",bold=True)
            self.mode_spinner=self._spinner(panel,self.ui_mode_text,["单词听音四选一","中文释义","词性判断","综合测试"],
                    lambda obj,val:setattr(self,'ui_mode_text',val))
            self._label(panel,"单词顺序",bold=True)
            self.order_spinner=self._spinner(panel,self.ui_order_text,["正序自动记录进度","乱序随机练习"],
                    lambda obj,val:setattr(self,'ui_order_text',val))
            self._label(panel,"每次练习数量",bold=True)
            self.count_spinner=self._spinner(panel,self.ui_count_text,["每天 10 个","每天 15 个","每天 20 个","每天 25 个","每天 30 个","全部"],
                    lambda obj,val:setattr(self,'ui_count_text',val))
            self._label(panel,"更改后返回首页点击「开始学习」即会生效",size=13,dark=False,height=45)
            self._label(panel,"数据安全与备份",bold=True)
            self._action(panel,"导出学习记录 ZIP 备份",lambda *_:self._launch_saf('export_backup'))
            self._action(panel,"从 ZIP 恢复学习记录",lambda *_:self._launch_saf('import_backup'),color=(.30,.42,.57,1))
            self._label(panel,"恢复会覆盖对应本地记录，请先导出当前备份",height=42,size=13,dark=False)

        # 五页导航始终位于底部，而不是随页面滚动。
        nav = BoxLayout(size_hint_y=None,height=dp(60),spacing=dp(3))
        items=[('home','首页'),('library','词库'),('wrong','错题'),('stats','统计'),('settings','设置')]
        for key,label in items:
            selected=(self.active_tab==key)
            button=CNButton(text=label,font_size=dp(14),background_normal="",
                            background_color=(0.12,0.42,0.35,1) if selected else (0.89,0.94,0.92,1),
                            color=(1,1,1,1) if selected else (0.18,0.34,0.30,1))
            button.bind(on_release=lambda _,tab=key:self._change_tab(tab))
            nav.add_widget(button)
        self.add_widget(nav)

    def set_unit(self, _spinner, value):
        self._change_unit(_spinner,value)

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
            self._saf_action = "import_vocab"
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
                raise RuntimeError("没有获取到文件")
            action = getattr(self, "_saf_action", "import_vocab")
            if action == "export_backup":
                data = self.create_backup_bytes()
                from jnius import autoclass
                PythonActivity = autoclass("org.kivy.android.PythonActivity")
                stream = PythonActivity.mActivity.getContentResolver().openOutputStream(uri)
                if stream is None:
                    raise RuntimeError("无法写入所选文件")
                try:
                    # 使用 Java OutputStream.write(int)，避免 PyJNIus 对 byte[] 的签名差异。
                    for byte in data:
                        stream.write(int(byte))
                    stream.flush()
                finally:
                    stream.close()
                Clock.schedule_once(lambda _dt: self.show_popup("备份成功", "备份 ZIP 已保存到你选择的位置。"), 0)
            elif action == "import_backup":
                payload = self._read_android_bytes_uri(uri)
                Clock.schedule_once(lambda _dt, blob=payload: self.restore_backup_bytes(blob), 0)
            else:
                content = self._read_android_text_uri(uri)
                Clock.schedule_once(lambda _dt, value=content: self._import_vocab_text(value), 0)
        except Exception as exc:
            error_message = f"文件处理失败：\n{type(exc).__name__}: {exc}"
            Clock.schedule_once(lambda _dt, msg=error_message: self.show_popup("操作失败", msg), 0)

    def _read_android_bytes_uri(self, uri):
        from jnius import autoclass
        PythonActivity = autoclass("org.kivy.android.PythonActivity")
        stream = PythonActivity.mActivity.getContentResolver().openInputStream(uri)
        if stream is None:
            raise RuntimeError("无法读取备份")
        out = bytearray()
        try:
            while True:
                value = int(stream.read())
                if value == -1:
                    break
                out.append(value)
                if len(out) > 25 * 1024 * 1024:
                    raise ValueError("备份超过 25MB，已停止读取")
        finally:
            stream.close()
        return bytes(out)

    def _launch_saf(self, action):
        if platform != "android":
            self.show_popup("提示", "请在 Android/鸿蒙兼容环境中用系统文件选择器备份或恢复。")
            return
        try:
            from jnius import autoclass
            from android import activity
            Intent = autoclass("android.content.Intent")
            PythonActivity = autoclass("org.kivy.android.PythonActivity")
            self._file_request_code = 24680
            self._saf_action = action
            self._android_activity = activity
            try:
                activity.unbind(on_activity_result=self._on_activity_result)
            except Exception:
                pass
            activity.bind(on_activity_result=self._on_activity_result)
            if action == "export_backup":
                intent = Intent(Intent.ACTION_CREATE_DOCUMENT)
                intent.addCategory(Intent.CATEGORY_OPENABLE)
                intent.setType("application/zip")
                intent.putExtra(Intent.EXTRA_TITLE, f"vocab_backup_{date.today().isoformat()}.zip")
                intent.addFlags(Intent.FLAG_GRANT_WRITE_URI_PERMISSION)
            else:
                intent = Intent(Intent.ACTION_OPEN_DOCUMENT)
                intent.addCategory(Intent.CATEGORY_OPENABLE)
                intent.setType("application/zip")
                intent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
            PythonActivity.mActivity.startActivityForResult(intent, self._file_request_code)
        except Exception as exc:
            self.show_popup("文件选择器不可用", f"{type(exc).__name__}: {exc}")

    def create_backup_bytes(self):
        names = ["progress.json", "wrong_words.jsonl", "stats.json", "custom_units.json",
                 "review_schedule.json", "study_activity.json", "favorites.json",
                 "card_schedule.json", "word_details.json"]
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("manifest.json", json.dumps({"format": "vocabtrainer-v14", "created": datetime.now().isoformat(), "files": names}))
            for name in names:
                path = os.path.join(self.data_dir, name)
                if os.path.isfile(path):
                    zf.write(path, arcname=name)
        return buf.getvalue()

    def restore_backup_bytes(self, payload):
        """Validate entire ZIP before touching any user files; never restore arbitrary paths."""
        allowed = {"progress.json", "wrong_words.jsonl", "stats.json", "custom_units.json",
                   "review_schedule.json", "study_activity.json", "favorites.json",
                   "card_schedule.json", "word_details.json"}
        try:
            if len(payload) > 25 * 1024 * 1024:
                raise ValueError("ZIP 体积过大")
            pending = {}
            with zipfile.ZipFile(io.BytesIO(payload)) as zf:
                manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
                if manifest.get("format") != "vocabtrainer-v14":
                    raise ValueError("不是本程序 V1.4 生成的备份")
                for info in zf.infolist():
                    if info.filename == "manifest.json":
                        continue
                    if info.filename not in allowed or info.is_dir() or info.file_size > 8 * 1024 * 1024:
                        raise ValueError("备份中含有非法或过大的文件")
                    content = zf.read(info)
                    if info.filename.endswith(".json"):
                        json.loads(content.decode("utf-8"))
                    else:
                        for line in content.decode("utf-8").splitlines():
                            if line.strip():
                                json.loads(line)
                    pending[info.filename] = content
            for filename, content in pending.items():
                dest = os.path.join(self.data_dir, filename)
                tmp = dest + ".restore.tmp"
                with open(tmp, "wb") as fp:
                    fp.write(content)
                os.replace(tmp, dest)
            self.review_schedule = self.load_json(self.review_path, {})
            self.load_vocab()
            self.build_menu()
            self.show_popup("恢复成功", f"已恢复 {len(pending)} 项数据。\n不会覆盖现有 MP3 缓存。")
        except Exception as exc:
            self.show_popup("恢复失败", f"数据没有通过校验：\n{type(exc).__name__}: {exc}")

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

        # 重新构建单元下拉框、数量和进度说明，立即显示新导入的单元。
        # 此方法现由 Clock 调用，处于 Kivy UI 主线程。
        self.build_menu()
        if hasattr(self, "unit_spinner"):
            self.unit_spinner.values = self.sorted_unit_names()
            self.unit_spinner.text = self.selected_unit

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
        temp = path + ".tmp"
        with open(temp, "w", encoding="utf-8") as json_file:
            json.dump(data, json_file, ensure_ascii=False, indent=2)
        os.replace(temp, path)

    def load_progress(self):
        return self.load_json(self.progress_path, {})

    def save_progress(self, key, index):
        data = self.load_progress()
        data[key] = index
        self.save_json(self.progress_path, data)

    @staticmethod
    def word_key(word):
        return re.sub(r"\s+", " ", str(word.get("en", "")).strip().lower())

    def _migrate_wrong_words(self):
        """旧版 jsonl 只读迁移，不删除用户数据。"""
        if not os.path.exists(self.wrong_path):
            return
        changed = False
        try:
            with open(self.wrong_path, "r", encoding="utf-8") as fp:
                for raw in fp:
                    try:
                        word = json.loads(raw)
                    except (ValueError, TypeError):
                        continue
                    if not isinstance(word, dict):
                        continue
                    key = self.word_key(word)
                    if key and key not in self.review_schedule:
                        self.review_schedule[key] = {
                            "word": {k: word.get(k) for k in ("en", "zh", "pos", "family")},
                            "wrong": 1, "correct": 0, "streak": 0, "level": 0,
                            "due": date.today().isoformat(), "last": "", "last_wrong": True,
                        }
                        changed = True
        except OSError:
            return
        if changed:
            self._save_review_schedule()

    def _save_review_schedule(self):
        self.save_json(self.review_path, self.review_schedule)

    def get_due_words(self):
        today = date.today().isoformat()
        return [item["word"] for item in self.review_schedule.values()
                if isinstance(item, dict) and item.get("due", "9999") <= today
                and isinstance(item.get("word"), dict) and item["word"].get("en")]

    def record_answer(self, word, is_correct):
        key = self.word_key(word)
        if not key:
            return
        today = date.today()
        record = self.review_schedule.get(key, {})
        record["word"] = {k: word.get(k) for k in ("en", "zh", "pos", "family")}
        record["correct"] = int(record.get("correct", 0)) + int(is_correct)
        record["wrong"] = int(record.get("wrong", 0)) + int(not is_correct)
        record["last"] = today.isoformat()
        record["last_wrong"] = not is_correct
        if is_correct:
            streak = int(record.get("streak", 0)) + 1
            record["streak"] = streak
            level = min(6, int(record.get("level", 0)) + 1)
            record["level"] = level
            # 简单透明的间隔复习：1、3、7、14、30、60 天。
            days = (1, 3, 7, 14, 30, 60)[level - 1]
            record["due"] = (today + timedelta(days=days)).isoformat()
        else:
            record["streak"] = 0
            record["level"] = 0
            # 当天继续复习错词；下次开始复习将自动出现。
            record["due"] = today.isoformat()
        self.review_schedule[key] = record
        self._save_review_schedule()

        activity = self.load_json(self.activity_path, {})
        if not isinstance(activity, dict):
            activity = {}
        daily = activity.setdefault(today.isoformat(), {"answered": 0, "correct": 0, "wrong": 0, "unique": []})
        daily["answered"] = int(daily.get("answered", 0)) + 1
        daily["correct"] = int(daily.get("correct", 0)) + int(is_correct)
        daily["wrong"] = int(daily.get("wrong", 0)) + int(not is_correct)
        unique = daily.get("unique", [])
        if not isinstance(unique, list):
            unique = []
        if key not in unique:
            unique.append(key)
        daily["unique"] = unique
        self.save_json(self.activity_path, activity)
        self.session_words.add(key)
        self.session_wrong += int(not is_correct)

    def start_review(self, *_):
        due = self.get_due_words()
        if not due:
            self.show_popup("复习完成", "目前没有到期的复习单词。\n继续正常练习，系统会按你的答题情况安排下次复习。")
            return
        # 错词可能来自多个单元；从完整词库补充干扰项。
        vocabulary = {}
        for words in self.units.values():
            for word in words:
                vocabulary[self.word_key(word)] = word
        for word in due:
            vocabulary[self.word_key(word)] = word
        self.all_words = list(vocabulary.values())
        if len(self.all_words) < 4:
            self.show_popup("无法开始", "至少需要 4 个不同单词才能生成四选一练习。")
            return
        if self._advance_event is not None:
            self._advance_event.cancel()
            self._advance_event = None
        self.review_mode = True
        self.quiz_mode = "mixed"
        self.order_mode = "shuffle"
        self.sequential_key = None
        self.total_answered = self.score = self.listening_total = self.listening_correct = 0
        self.session_wrong = 0
        self.session_words = set()
        # 一次复习上限 25 词，避免课程过长。
        self.test_pool = due[:25]
        random.shuffle(self.test_pool)
        self.total_questions = len(self.test_pool)
        self.build_quiz_ui()
        self.next_question()

    def show_dashboard(self, *_):
        fresh = self.load_json(self.review_path, {})
        if isinstance(fresh, dict):
            self.review_schedule = fresh
        activity = self.load_json(self.activity_path, {})
        if not isinstance(activity, dict):
            activity = {}
        today = date.today().isoformat()
        td = activity.get(today, {})
        answered = sum(int(day.get("answered", 0)) for day in activity.values() if isinstance(day, dict))
        correct = sum(int(day.get("correct", 0)) for day in activity.values() if isinstance(day, dict))
        unique_all = set()
        for day in activity.values():
            if isinstance(day, dict):
                unique_all.update(day.get("unique", []))
        due = len(self.get_due_words())
        ever_wrong = [r for r in self.review_schedule.values() if isinstance(r, dict) and int(r.get("wrong", 0)) > 0]
        mastered = sum(1 for r in self.review_schedule.values() if int(r.get("level", 0)) >= 4)
        total_vocab = len({self.word_key(w) for words in self.units.values() for w in words})
        streak = 0
        cursor = date.today()
        while activity.get(cursor.isoformat(), {}).get("answered", 0):
            streak += 1
            cursor -= timedelta(days=1)
        if not streak:
            cursor = date.today() - timedelta(days=1)
            while activity.get(cursor.isoformat(), {}).get("answered", 0):
                streak += 1
                cursor -= timedelta(days=1)
        daily_acc = (int(td.get("correct", 0)) / int(td.get("answered", 1)) * 100) if td.get("answered") else 0
        report = (
            f"今天答题：{td.get('answered', 0)} 题 | 答对 {td.get('correct', 0)} 题\n"
            f"今日正确率：{daily_acc:.1f}%  | 今日不同单词：{len(td.get('unique', []))}\n"
            f"连续学习：{streak} 天\n\n"
            f"累计答题：{answered} 题 | 总正确率：{correct / answered * 100 if answered else 0:.1f}%\n"
            f"累计练过：{len(unique_all)} / {total_vocab} 个不同单词\n"
            f"已达到强化掌握等级：{mastered} 个\n"
            f"历史错词：{len(ever_wrong)} 个 | 今日待复习：{due} 个\n\n"
            "复习间隔：1→3→7→14→30→60 天；答错归零。"
        )
        self.show_report_popup("学习进度与复习报告", report)

    def show_report_popup(self, title, report):
        """用可滚动的只读 TextInput 展示记录，避免 Label 计算高度后空白。"""
        view = TextInput(
            text=str(report), readonly=True, multiline=True,
            font_size=dp(17), foreground_color=(0.10, 0.15, 0.22, 1),
            background_color=(1, 1, 1, 1),
            cursor_blink=False, padding=[dp(14), dp(16), dp(14), dp(16)],
        )
        if CHINESE_FONT_PATH:
            view.font_name = CHINESE_FONT_NAME

        wrapper = BoxLayout(orientation="vertical", padding=dp(8), spacing=dp(8))
        wrapper.add_widget(view)
        close_btn = CNButton(
            text="关闭", size_hint_y=None, height=dp(48),
            background_color=(0.13, 0.40, 0.65, 1),
        )
        wrapper.add_widget(close_btn)
        kwargs = {"title": title, "content": wrapper,
                  "size_hint": (0.94, 0.80), "auto_dismiss": True}
        if CHINESE_FONT_PATH:
            kwargs["title_font"] = CHINESE_FONT_NAME
        popup = Popup(**kwargs)
        close_btn.bind(on_release=popup.dismiss)
        popup.open()

    def show_wrong_book(self, *_):
        fresh = self.load_json(self.review_path, {})
        if isinstance(fresh, dict):
            self.review_schedule = fresh
        records = sorted(
            (item for item in self.review_schedule.values() if isinstance(item, dict) and int(item.get("wrong", 0)) > 0),
            key=lambda item: (item.get("due", "9999"), -int(item.get("wrong", 0))),
        )
        if not records:
            self.show_popup("错题本", "还没有错题。答错的单词会自动记录在这里。")
            return
        text_rows = []
        today = date.today().isoformat()
        for item in records[:250]:
            word = item.get("word", {})
            due = item.get("due", "")
            state = "待复习" if due <= today else "下次复习 " + due
            text_rows.append(
                f"{word.get('en', '')}  |  {word.get('zh', '')}\n"
                f"答错 {item.get('wrong', 0)} 次 · 连续答对 {item.get('streak', 0)} 次 · {state}"
            )
        caption = f"错词共 {len(records)} 个（显示前 {min(len(records), 250)} 个）\n\n"
        self.show_report_popup("错题本", caption + "\n\n".join(text_rows))

    # V1.4: card study, learning ratings, collection, searching and word details.
    def all_vocabulary(self):
        items = {}
        for unit in self.sorted_unit_names():
            for word in self.units[unit]:
                items.setdefault(self.word_key(word), word)
        return list(items.values())

    def _favorite_keys(self):
        values = self.load_json(self.favorites_path, [])
        return set(values) if isinstance(values, list) else set()

    def toggle_favorite(self, word, *_):
        key = self.word_key(word)
        favorites = self._favorite_keys()
        if key in favorites:
            favorites.remove(key)
        else:
            favorites.add(key)
        self.save_json(self.favorites_path, sorted(favorites))
        self.show_popup("收藏夹", f"{word.get('en', '')} 已{'收藏' if key in favorites else '取消收藏'}")

    def _card_grade(self, word, rating, today=None):
        """SM-2-inspired transparent intervals; independent of the old quiz schedule."""
        today = today or date.today()
        key = self.word_key(word)
        cards = self.load_json(self.cards_path, {})
        if not isinstance(cards, dict):
            cards = {}
        record = cards.get(key, {})
        rep = int(record.get("repetitions", 0))
        ease = max(1.3, float(record.get("ease", 2.5)))
        interval = int(record.get("interval", 0))
        if rating == "again":
            rep = 0
            interval = 0
            ease = max(1.3, ease - 0.2)
        elif rating == "hard":
            rep = max(1, rep)
            interval = max(1, round(interval * 1.2))
            ease = max(1.3, ease - 0.15)
        else:
            rep += 1
            interval = 1 if rep == 1 else (3 if rep == 2 else max(4, round(interval * ease)))
            ease = min(3.0, ease + 0.05)
        record.update({"word": word, "repetitions": rep, "ease": round(ease, 2),
                       "interval": interval, "due": (today + timedelta(days=interval)).isoformat(),
                       "last": today.isoformat(), "rating": rating})
        cards[key] = record
        self.save_json(self.cards_path, cards)
        return record

    def start_cards(self, mode="unit", *_):
        pool = self.all_vocabulary() if mode != "unit" else list(self.units.get(self.selected_unit, []))
        if mode == "favorites":
            keys = self._favorite_keys()
            pool = [w for w in pool if self.word_key(w) in keys]
        elif mode == "due":
            cards = self.load_json(self.cards_path, {})
            today = date.today().isoformat()
            pool = [w for w in pool if self.word_key(w) in cards and cards[self.word_key(w)].get("due", "9999") <= today]
        if not pool:
            self.show_popup("暂时没有单词", "当前没有符合条件的卡片，可以先学习或收藏单词。")
            return
        self.card_queue = list(pool[:100])
        self.card_position = 0
        self._show_next_card()

    def _show_next_card(self):
        if self.card_position >= len(self.card_queue):
            self.active_tab = "home"
            self.build_menu()
            self.show_popup("卡片练习完成", "本次卡片已学完，熟悉度和复习日期已保存。")
            return
        self.card_current = self.card_queue[self.card_position]
        self.card_revealed = False
        self.clear()
        word = self.card_current
        scroll = ScrollView(do_scroll_x=False)
        body = BoxLayout(orientation="vertical", spacing=dp(12), padding=dp(12), size_hint_y=None)
        body.bind(minimum_height=body.setter("height"))
        scroll.add_widget(body)
        self.add_widget(scroll)
        self._label(body, f"单词卡片   {self.card_position + 1} / {len(self.card_queue)}", 48, 21, bold=True)
        pane = self._panel(body, pad=16)
        self._label(pane, word.get("en", ""), 74, 32, bold=True, align="center")
        detail = self.load_json(self.details_path, {}).get(self.word_key(word), {})
        self._label(pane, detail.get("ipa", "") or "音标待补充", 34, 17, align="center")
        self._action(pane, "播放英文发音", lambda *_: self.speak_word(word.get("en", "")), height=48)
        self.card_meaning_label = self._label(pane, "点击下方按钮揭晓释义", 85, 18, align="center")
        self.card_detail_label = self._label(pane, "", 100, 15, dark=False, align="center")
        self._action(body, "翻开卡片 / 查看释义", self.reveal_card)
        self._action(body, "收藏 / 取消收藏", lambda *_: self.toggle_favorite(word), color=(0.30, 0.43, 0.56, 1))
        self._label(body, "回忆后选择熟悉程度", 34, 16)
        buttons = BoxLayout(size_hint_y=None, height=dp(54), spacing=dp(6))
        for label, rating, color in [("不认识", "again", (.74,.25,.23,1)),
                                     ("模糊", "hard", (.75,.49,.18,1)),
                                     ("认识", "good", (.12,.43,.35,1))]:
            b = CNButton(text=label, background_normal="", background_color=color)
            b.bind(on_release=lambda _, r=rating: self.rate_card(r))
            buttons.add_widget(b)
        body.add_widget(buttons)
        self._action(body, "退出卡片学习", lambda *_: self.build_menu(), color=(.35,.42,.46,1))

    def reveal_card(self, *_):
        if not self.card_current:
            return
        word = self.card_current
        self.card_revealed = True
        self.card_meaning_label.text = f"{' / '.join(word.get('pos') or [])}\n{word.get('zh', '')}"
        detail = self.load_json(self.details_path, {}).get(self.word_key(word), {})
        self.card_detail_label.text = (f"词族：{word.get('family') or '暂无'}\n"
                                       f"例句：{detail.get('example') or '暂无（可在单词详情中手动补充）'}")

    def rate_card(self, rating):
        if not self.card_current:
            return
        if not self.card_revealed:
            self.reveal_card()
            self.show_popup("先看答案", "已为你翻开卡片，请确认释义后再选择熟悉程度。")
            return
        self._card_grade(self.card_current, rating)
        self.card_position += 1
        self._show_next_card()

    def show_word_search(self, *_):
        wrapper = BoxLayout(orientation="vertical", spacing=dp(8), padding=dp(10))
        keyword = TextInput(hint_text="输入英文或中文搜索", multiline=False, size_hint_y=None, height=dp(50))
        if CHINESE_FONT_PATH:
            keyword.font_name = CHINESE_FONT_NAME
        wrapper.add_widget(keyword)
        results = TextInput(readonly=True, multiline=True, font_size=dp(16))
        if CHINESE_FONT_PATH:
            results.font_name = CHINESE_FONT_NAME
        wrapper.add_widget(results)
        actions = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(6))
        view_btn = CNButton(text="单词详情")
        fav_btn = CNButton(text="收藏选中")
        actions.add_widget(view_btn)
        actions.add_widget(fav_btn)
        wrapper.add_widget(actions)
        items = self.all_vocabulary()
        shown = []
        def update(*_):
            value = keyword.text.strip().lower()
            shown[:] = [w for w in items if value in w.get('en','').lower() or value in w.get('zh','').lower()][:80] if value else []
            results.text = "\n".join(f"{i+1}. {w.get('en')}  —  {w.get('zh')}" for i,w in enumerate(shown)) or "输入关键词后显示结果"
        keyword.bind(text=update)
        update()
        kw = {"title":"搜索与收藏", "content":wrapper,"size_hint":(.94,.82)}
        if CHINESE_FONT_PATH:
            kw["title_font"] = CHINESE_FONT_NAME
        popup = Popup(**kw)
        def selected():
            # 精确搜索时选择匹配单词；多结果默认列表第一条。
            value = keyword.text.strip().lower()
            return next((w for w in shown if w.get('en','').lower() == value), shown[0] if shown else None)
        def details(*_):
            word = selected()
            if word:
                popup.dismiss()
                self.show_word_details(word)
        def favorite(*_):
            word = selected()
            if word:
                self.toggle_favorite(word)
        view_btn.bind(on_release=details)
        fav_btn.bind(on_release=favorite)
        popup.open()

    def show_word_details(self, word, *_):
        key = self.word_key(word)
        details = self.load_json(self.details_path, {})
        existing = details.get(key, {})
        wrapper = BoxLayout(orientation='vertical',padding=dp(10),spacing=dp(8))
        title = CNLabel(text=f"{word.get('en','')}\n{word.get('zh','')}",size_hint_y=None,height=dp(86))
        wrapper.add_widget(title)
        ipa = TextInput(text=existing.get('ipa',''), hint_text='音标（可手动填写）',multiline=False,size_hint_y=None,height=dp(46))
        example = TextInput(text=existing.get('example',''), hint_text='英语例句（可手动填写）',multiline=True)
        if CHINESE_FONT_PATH:
            ipa.font_name = example.font_name = CHINESE_FONT_NAME
        wrapper.add_widget(ipa)
        wrapper.add_widget(example)
        self._label(wrapper, f"词族/关联词：{word.get('family') or '暂无'}",height=55,size=15)
        buttons = BoxLayout(size_hint_y=None,height=dp(48),spacing=dp(8))
        save = CNButton(text='保存详情'); play = CNButton(text='播放发音')
        buttons.add_widget(save); buttons.add_widget(play)
        wrapper.add_widget(buttons)
        kw={"title":"单词详情", "content":wrapper,"size_hint":(.94,.75)}
        if CHINESE_FONT_PATH:
            kw['title_font']=CHINESE_FONT_NAME
        popup=Popup(**kw)
        def save_details(*_):
            details[key]={"ipa":ipa.text.strip(),"example":example.text.strip()}
            self.save_json(self.details_path, details)
            popup.dismiss()
            self.show_popup("已保存", "音标和例句已保存到本地，并包含在数据备份中。")
        save.bind(on_release=save_details)
        play.bind(on_release=lambda *_:self.speak_word(word.get('en','')))
        popup.open()

    def show_favorites(self, *_):
        keys = self._favorite_keys()
        words = [w for w in self.all_vocabulary() if self.word_key(w) in keys]
        if not words:
            self.show_popup('收藏夹', '还没有收藏。搜索单词或翻卡片时可添加收藏。')
            return
        self.show_report_popup('我的收藏夹', '\n'.join(f"{w['en']}  —  {w['zh']}" for w in words))

    def show_calendar(self, *_):
        activity = self.load_json(self.activity_path, {})
        today = date.today()
        lines = []
        for i in range(27, -1, -1):
            day = today - timedelta(days=i)
            r = activity.get(day.isoformat(), {})
            n = int(r.get('answered', 0)) if isinstance(r, dict) else 0
            c = int(r.get('correct', 0)) if isinstance(r, dict) else 0
            bar = '■' * min(12, math.ceil(n / 5)) if n else '·'
            lines.append(f"{day.strftime('%m-%d')}  {bar}  {n}题  {c / n * 100 if n else 0:.0f}%")
        self.show_report_popup('近28天学习日历与趋势', '\n'.join(lines))

    def start_quiz(self, *_):
        mode_map = {
            "单词听音四选一": "listening",
            "中文释义": "meaning",
            "词性判断": "pos",
            "综合测试": "mixed",
        }
        self.quiz_mode = mode_map[self.ui_mode_text]
        self.order_mode = "sequential" if self.ui_order_text.startswith("正序") else "shuffle"
        words = list(self.units.get(self.selected_unit, []))
        if len(words) < 4:
            self.show_popup("提示", "当前词库至少需要 4 个单词。")
            return

        if self.ui_count_text == "全部":
            requested = len(words)
        else:
            requested = int(re.search(r"\d+", self.ui_count_text).group())

        if self._advance_event is not None:
            self._advance_event.cancel()
            self._advance_event = None
        self.review_mode = False
        self.session_words = set()
        self.session_wrong = 0
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
        self._advance_event = None
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

        self.record_answer(self.current_word, is_correct)
        if is_correct:
            self.score += 1
            if self.type_label.text == "单词听音四选一":
                self.listening_correct += 1
            self.feedback_label.text = f"答对了\n{detail}"
            self.feedback_label.color = (0.1, 0.55, 0.32, 1)
            self.score_label.text = f"答对：{self.score}"
            self._advance_event = Clock.schedule_once(lambda *_: self.next_question(), 0.75)
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
        if self._advance_event is not None:
            self._advance_event.cancel()
            self._advance_event = None
        if self.total_answered == 0:
            self.build_menu()
            return
        acc = self.score / self.total_answered * 100
        message = (f"本次完成：{self.total_answered} 题\n答对：{self.score} 题\n"
                   f"正确率：{acc:.1f}%\n错题：{self.session_wrong} 题\n"
                   f"涉及单词：{len(self.session_words)} 个\n"
                   f"当前待复习：{len(self.get_due_words())} 个")
        if self.order_mode == "sequential" and self.sequential_key and not self.review_mode:
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
