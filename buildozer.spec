[app]
title = 四级单词训练
package.name = vocabtrainer
package.domain = org.codex.local

source.dir = .
source.include_exts = py,txt,json,png,kv,ttf

version = 1.0

requirements = python3==3.11.9,hostpython3==3.11.9,kivy,pyjnius

orientation = portrait
fullscreen = 0

android.api = 35
android.minapi = 24
android.ndk = 28c

android.archs = arm64-v8a, armeabi-v7a

android.allow_backup = True

p4a.branch = develop


[buildozer]
log_level = 2
warn_on_root = 1
