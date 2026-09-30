# 在线自动打包 APK

这个方法不占你电脑 C 盘空间。你只需要把整个 `android_vocab_app` 文件夹上传到 GitHub，GitHub 会在云端 Linux 机器上打包 APK。

## 第一次使用

1. 打开 GitHub，新建一个仓库。
2. 把 `android_vocab_app` 文件夹里的所有文件上传到仓库根目录。
3. 上传后确认仓库里能看到这些文件：
   - `main.py`
   - `buildozer.spec`
   - `data/vocab.txt`
   - `.github/workflows/build-apk.yml`

## 开始打包

1. 打开 GitHub 仓库页面。
2. 点上方 `Actions`。
3. 左侧选择 `Build Android APK`。
4. 点 `Run workflow`。
5. 等它跑完，通常需要 20 到 60 分钟。

## 下载 APK

打包成功后：

1. 点进最新的一次运行记录。
2. 页面下方找到 `Artifacts`。
3. 下载 `vocab-trainer-apk`。
4. 解压后里面就是 `.apk` 文件。

把 APK 发到安卓/鸿蒙手机安装即可。

## 如果失败

打开失败的运行记录，把最后红色报错复制给我，我再帮你改。
