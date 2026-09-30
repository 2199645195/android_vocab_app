# 四级单词训练 Android 版

这个目录已经是一个 Kivy Android 工程，内置了 `data/vocab.txt` 词库。

已保留的功能：

- Unit 词库选择
- 中文释义四选一，显示词性
- 词性判断
- 综合测试
- 单词听音四选一
- 听音题只显示四个英文选项
- “再听一次”
- 每天默认 25 个
- 正序 / 乱序
- 正序自动保存进度
- 错题记录
- 听音正确率统计
- 手机本地保存学习记录

## 在电脑上试运行

```bash
pip install kivy pyjnius
python main.py
```

在 Windows 上试运行时，Android TTS 不会启用；打包到安卓手机后会调用手机系统英文 TTS。

## 推荐：在线打包 APK

如果电脑 C 盘空间紧，推荐使用 GitHub Actions 在线打包，不占本机硬盘。

看这个文件：

```text
GITHUB_ACTIONS_APK.md
```

## 本机打包 APK

Buildozer 官方主要支持 Linux。推荐用 WSL2 Ubuntu 或 Linux 虚拟机。

```bash
sudo apt update
sudo apt install -y python3-pip git zip unzip openjdk-17-jdk
python3 -m pip install --user buildozer cython
cd android_vocab_app
buildozer android debug
```

成功后 APK 通常在：

```text
bin/四级单词训练-1.0-arm64-v8a_armeabi-v7a-debug.apk
```

把 APK 发到安卓或鸿蒙手机安装即可。第一次打开如果手机提示安装未知来源应用，需要在系统设置里允许。

## 替换或增加词库

把新的词库内容放到：

```text
data/vocab.txt
```

格式保持原来的写法即可：

```text
Unit 1
word | n. | 中文释义 | family
```

如果要内置 Unit 1 到 Unit 6，可以直接把所有 Unit 追加到同一个 `vocab.txt`。
