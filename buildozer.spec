[app]
title = 四级单词训练
package.name = vocabtrainer
package.domain = org.codex.local
source.dir = .
source.include_exts = py,txt,json,png,kv
version = 1.0
requirements = python3==3.11.9,kivy,pyjnius
orientation = portrait
fullscreen = 0
android.permissions =
android.api = 35
android.minapi = 24
android.ndk = 25b
android.archs = arm64-v8a, armeabi-v7a
android.gradle_dependencies =
android.allow_backup = True
p4a.branch = develop

[buildozer]
log_level = 2
warn_on_root = 1
