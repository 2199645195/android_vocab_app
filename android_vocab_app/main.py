import json
import os
import random
import re
from datetime import datetime

from kivy.app import App
from kivy.clock import Clock
from kivy.core.window import Window
from kivy.metrics import dp
from kivy.properties import BooleanProperty, ListProperty, NumericProperty, ObjectProperty, StringProperty
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.popup import Popup
from kivy.uix.scrollview import ScrollView
from kivy.uix.spinner import Spinner


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


def parse_vocab_file(path):
    units = {}
    current_unit = None
    with open(path, "r", encoding="utf-8") as vocab_file:
        for raw in vocab_file:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            unit_match = re.match(r"^Unit\s+(\d+)", line, re.I)
            if unit_match:
                current_unit = f"Unit {unit_match.group(1)}"
                units.setdefault(current_unit, [])
                continue
            if current_unit is None:
                continue
            item = parse_word_line(line)
            if item:
                units[current_unit].append(item)
    return units


class AndroidTTS:
    def __init__(self):
        self.ready = False
        self.tts = None
        try:
            from jnius import autoclass

            PythonActivity = autoclass("org.kivy.android.PythonActivity")
            TextToSpeech = autoclass("android.speech.tts.TextToSpeech")
            Locale = autoclass("java.util.Locale")
            self.TextToSpeech = TextToSpeech
            self.tts = TextToSpeech(PythonActivity.mActivity, None)
            self.tts.setLanguage(Locale.US)
            self.ready = True
        except Exception:
            self.ready = False

    def speak(self, word):
        if not word or not self.ready:
            return
        try:
            self.tts.stop()
            self.tts.speak(word, self.TextToSpeech.QUEUE_FLUSH, None, "vocab-word")
        except Exception:
            pass


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
        super().__init__(orientation="vertical", spacing=dp(10), padding=dp(14), **kwargs)
        Window.clearcolor = (0.96, 0.97, 0.98, 1)
        self.units = {}
        self.selected_unit = "Unit 6"
        self.all_words = []
        self.test_pool = []
        self.sequential_key = None
        self.sequential_cursor = 0
        self.tts = AndroidTTS()
        self.data_dir = App.get_running_app().user_data_dir
        os.makedirs(self.data_dir, exist_ok=True)
        self.progress_path = os.path.join(self.data_dir, "progress.json")
        self.wrong_path = os.path.join(self.data_dir, "wrong_words.jsonl")
        self.stats_path = os.path.join(self.data_dir, "stats.json")
        self.load_vocab()
        self.build_menu()

    def load_vocab(self):
        app_dir = os.path.dirname(os.path.abspath(__file__))
        vocab_path = os.path.join(app_dir, "data", "vocab.txt")
        self.units = parse_vocab_file(vocab_path)
        if self.units:
            self.selected_unit = sorted(self.units, key=lambda item: int(re.search(r"\d+", item).group()))[0]

    def clear(self):
        self.clear_widgets()

    def build_menu(self, *_):
        self.clear()
        title = Label(
            text="四级单词训练",
            font_size=dp(26),
            bold=True,
            size_hint_y=None,
            height=dp(46),
            color=(0.08, 0.12, 0.18, 1),
        )
        self.add_widget(title)

        unit_names = sorted(self.units, key=lambda item: int(re.search(r"\d+", item).group()))
        self.unit_spinner = Spinner(
            text=self.selected_unit,
            values=unit_names,
            size_hint_y=None,
            height=dp(46),
            background_color=(0.13, 0.45, 0.85, 1),
        )
        self.unit_spinner.bind(text=self.set_unit)
        self.add_widget(self.unit_spinner)

        self.mode_spinner = Spinner(
            text="单词听音四选一",
            values=["单词听音四选一", "中文释义", "词性判断", "综合测试"],
            size_hint_y=None,
            height=dp(46),
        )
        self.add_widget(self.mode_spinner)

        self.order_spinner = Spinner(
            text="正序自动记录进度",
            values=["正序自动记录进度", "乱序随机练习"],
            size_hint_y=None,
            height=dp(46),
        )
        self.add_widget(self.order_spinner)

        self.count_spinner = Spinner(
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
        self.menu_info = Label(
            text=f"{self.selected_unit} 共 {total} 个词\\n正序进度：{min(learned, total)} / {total}",
            font_size=dp(17),
            halign="center",
            valign="middle",
            color=(0.2, 0.25, 0.32, 1),
        )
        self.menu_info.bind(size=lambda label, size: setattr(label, "text_size", size))
        self.add_widget(self.menu_info)

        start_button = Button(
            text="开始",
            font_size=dp(20),
            bold=True,
            size_hint_y=None,
            height=dp(54),
            background_color=(0.12, 0.62, 0.36, 1),
        )
        start_button.bind(on_release=self.start_quiz)
        self.add_widget(start_button)

        wrong_button = Button(text="查看错题数量", size_hint_y=None, height=dp(44))
        wrong_button.bind(on_release=self.show_wrong_count)
        self.add_widget(wrong_button)

    def set_unit(self, _spinner, value):
        self.selected_unit = value
        self.build_menu()

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
        self.score_label = Label(text="答对：0", color=(0.08, 0.12, 0.18, 1), bold=True)
        self.progress_label = Label(text=f"进度：0 / {self.total_questions}", color=(0.13, 0.45, 0.85, 1), bold=True)
        top.add_widget(self.score_label)
        top.add_widget(self.progress_label)
        self.add_widget(top)

        self.type_label = Label(text="", font_size=dp(19), bold=True, size_hint_y=None, height=dp(34), color=(0.13, 0.45, 0.85, 1))
        self.add_widget(self.type_label)

        self.word_label = Label(text="", font_size=dp(34), bold=True, color=(0.08, 0.12, 0.18, 1))
        self.word_label.bind(size=lambda label, size: setattr(label, "text_size", size))
        self.add_widget(self.word_label)

        speak_button = Button(text="再听一次", font_size=dp(18), size_hint_y=None, height=dp(50), background_color=(0.13, 0.45, 0.85, 1))
        speak_button.bind(on_release=lambda *_: self.repeat_pronunciation())
        self.add_widget(speak_button)

        self.extra_label = Label(text="", font_size=dp(16), size_hint_y=None, height=dp(56), color=(0.26, 0.31, 0.38, 1))
        self.extra_label.bind(size=lambda label, size: setattr(label, "text_size", size))
        self.add_widget(self.extra_label)

        self.option_buttons = []
        for index in range(4):
            button = Button(
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

        self.feedback_label = Label(text="", font_size=dp(16), size_hint_y=None, height=dp(72), color=(0.2, 0.25, 0.32, 1))
        self.feedback_label.bind(size=lambda label, size: setattr(label, "text_size", size))
        self.add_widget(self.feedback_label)

        bottom = BoxLayout(orientation="horizontal", size_hint_y=None, height=dp(46), spacing=dp(8))
        self.next_button = Button(text="下一题", disabled=True)
        self.next_button.bind(on_release=lambda *_: self.next_question())
        end_button = Button(text="结束")
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
        Clock.schedule_once(lambda *_: self.speak_word(correct["en"]), 0.25)

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
        detail = f"{self.current_word['en']}  {pos_text}\\n{self.current_word['zh']}"
        if self.current_word.get("family"):
            detail += f"\\n关联：{self.current_word['family']}"

        if is_correct:
            self.score += 1
            if self.type_label.text == "单词听音四选一":
                self.listening_correct += 1
            self.feedback_label.text = f"答对了\\n{detail}"
            self.feedback_label.color = (0.1, 0.55, 0.32, 1)
            self.score_label.text = f"答对：{self.score}"
            Clock.schedule_once(lambda *_: self.next_question(), 0.75)
        else:
            self.option_buttons[idx].background_color = (0.9, 0.18, 0.2, 1)
            self.option_buttons[idx].color = (1, 1, 1, 1)
            self.feedback_label.text = f"没选对，正确答案：\\n{detail}"
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
        self.tts.speak(word)

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
        self.show_popup("学习记录", f"累计错题：{len(wrong)}\\n听音错题：{listening}\\n释义错题：{meaning}\\n词性错题：{pos}\\n\\n听音正确率：{acc}")

    def end_test(self, *_):
        if self.total_answered == 0:
            self.build_menu()
            return
        acc = self.score / self.total_answered * 100
        message = f"本次完成：{self.total_answered} 个\\n答对：{self.score} 个\\n正确率：{acc:.1f}%"
        if self.order_mode == "sequential" and self.sequential_key:
            if self.sequential_cursor >= len(self.all_words):
                message += "\\n\\n本单元正序已学完，下次会从第 1 个词重新开始。"
            else:
                message += f"\\n\\n进度已保存，下次从第 {self.sequential_cursor + 1} 个词继续。"
        popup = Popup(title="练习报告", content=Label(text=message), size_hint=(0.86, 0.46))
        popup.bind(on_dismiss=lambda *_: self.build_menu())
        popup.open()

    def show_popup(self, title, message):
        Popup(title=title, content=Label(text=message), size_hint=(0.86, 0.46)).open()


class VocabTrainerApp(App):
    def build(self):
        self.title = "四级单词训练"
        return VocabTrainer()


if __name__ == "__main__":
    VocabTrainerApp().run()
