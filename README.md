# 远程 Blu-ray ISO / STRM 提取工具

工具流程：

```text
.strm 或 HTTP(S) ISO URL -> HTTP Range -> UDF 2.50 -> BDMV -> ffprobe/ffmpeg
```

不会先下载完整 ISO；目录、播放列表和实际提取都按需读取远程 Range。当前测试原盘约 25.25 GiB。

## 环境

使用 Python 自带的 `venv`，不依赖 Conda：

```powershell
C:\Python314\python.exe -m venv .venv
.\.venv\Scripts\Activate.ps1
python --version
```

## 安装

本目录已经是可构建的 Python 项目，安装后会提供 `remote-bluray` 命令。

在当前目录本地安装：

```powershell
python -m pip install .
remote-bluray --version
```

从 GitHub 安装（把地址替换成实际仓库地址）：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install "git+ssh://git@github.com/haildceu1/remote-bluray.git"
remote-bluray --help
```

构建可分发文件：

```powershell
python -m pip install build
python -m build
```

构建结果会放在 `dist/`，另一台设备可以安装其中的 `.whl` 文件：

```powershell
python -m pip install remote_bluray-0.2.1-py3-none-any.whl
```

依赖和工具：

- Python 包：`requests`
- `C:\ffmpeg\bin\ffprobe.exe`
- `C:\ffmpeg\bin\ffmpeg.exe`
- `C:\Software\VLC\vlc.exe` 可用于普通本地媒体播放，但远程原始 ISO 仍由本工具提供 UDF Range 层。

FFmpeg/ffprobe 不随 Python 包发布，需要在目标设备单独安装并加入 `PATH`。也可以用环境变量指定完整路径：

```powershell
$env:REMOTE_BLURAY_FFMPEG = "C:\tools\ffmpeg\bin\ffmpeg.exe"
$env:REMOTE_BLURAY_FFPROBE = "C:\tools\ffmpeg\bin\ffprobe.exe"
```

## 输入形式

所有命令的位置参数都同时支持：

1. `.strm` 文件路径；工具会读取文件中的第一行 HTTP(S) 地址。
2. `.strm` 中的 HTTP(S) ISO 地址本身。

例如：

```powershell
$isoUrl = (Get-Content -LiteralPath "D:\Cinema\strm\...\movie.strm" -Raw).Trim()
python -X utf8 remote_bluray.py list $isoUrl
```

## 远程读取速度与并发

远程 ISO 默认使用受控并发 Range 读取：`workers=2`、`prefetch=2`、`range-size=8M`。这会在保持请求量相对温和的同时，提前读取后续数据块。

可以根据远程服务器的限速情况调整：

```powershell
python -X utf8 remote_bluray.py --workers 2 --prefetch 2 --range-size 8M list "D:\Cinema\strm\...\movie.strm"

python -X utf8 remote_bluray.py extract-video "D:\Cinema\strm\...\movie.strm" --playlist 00001.mpls -o "D:\output\movie.mkv" --workers 4 --prefetch 4 --range-size 16M
```

建议先使用 `2/2/8M`；确认服务端没有返回 `429`、`403` 或频繁断开后，再尝试 `4/4/16M`。程序对 `429` 和 `5xx` 响应使用指数退避；并发过高仍可能触发远程服务的限速。

## 查看播放列表

```powershell
python -X utf8 remote_bluray.py list "D:\Cinema\strm\...\movie.strm"
```

输出会列出：

- 有效 `.mpls` 播放列表数量；`.mpls.backup` 不计入数量。
- 每个列表的名称、时长、对应文件总大小和总码率。

格式示例：

```text
Name:                   00002.MPLS
Length:                 1:33:16.674 (h:m:s.ms)
Size:                   70,207,316,544 bytes
Total Bitrate:          100.36 Mbps
```

例如测试原盘识别到 12 个有效播放列表，主片是 `00001.mpls`，而不是简单按文件名猜测。

## 按播放列表探测和提取

播放列表可以写完整文件名，也可以写数字编号：`00001.mpls` 和 `1` 等价。

探测指定播放列表：

```powershell
python -X utf8 remote_bluray.py probe "D:\Cinema\strm\...\movie.strm" --playlist 00001.mpls
```

完整复制播放列表时间线到 Matroska（视频、音频和字幕等关联流都保留）：

```powershell
python -X utf8 remote_bluray.py extract-video "D:\Cinema\strm\...\movie.strm" --playlist 00001.mpls -o "D:\output\movie.mkv"
```

按播放列表选择音轨并无损复制到 MKA：

```powershell
python -X utf8 remote_bluray.py extract-audio "D:\Cinema\strm\...\movie.strm" --playlist 00001.mpls --map 0:a:0 -o "D:\output\movie-audio.mka"
```

`--map 0:a:0` 是第一条音频，`0:a:1` 是第二条，以此类推。多片段播放列表会按 MPLS 顺序拼接，并使用每个播放项的入点/出点；多角度播放项当前取主角度。

一次提取多个播放列表：

```powershell
python -X utf8 remote_bluray.py extract-video "D:\Cinema\strm\...\movie.strm" --playlist 00001.mpls 00004.mpls 00011.mpls -o "D:\output\movie.mkv"

python -X utf8 remote_bluray.py extract-audio "D:\Cinema\strm\...\movie.strm" --playlist 00001.mpls 00004.mpls 00011.mpls --map 0:a:0 -o "D:\output\audio.mka"
```

多列表时 `-o` 会作为输出前缀或目录使用，例如上面的命令会生成 `movie-00001.mkv`、`movie-00004.mkv`、`movie-00011.mkv`。没有音频轨的列表会在 `extract-audio` 中跳过并提示，不会影响其它列表。

## 自动提取模式

`main` 模式按每个播放列表引用的 `.m2ts` 文件总大小选择最大的列表：

```powershell
python -X utf8 remote_bluray.py extract-video "D:\Cinema\strm\...\movie.strm" --mode main -o "D:\output\main.mkv"
```

`feat` 模式先排除上述 main 列表，再提取时长达到阈值的其它列表。阈值支持秒数或 `H:M:S(.ms)`：

```powershell
python -X utf8 remote_bluray.py extract-video "D:\Cinema\strm\...\movie.strm" --mode feat --min-duration 60 -o "D:\output\features"

python -X utf8 remote_bluray.py extract-audio "D:\Cinema\strm\...\movie.strm" --mode feat --min-duration 0:02:00 --map 0:a:0 -o "D:\output\feature-audio"
```

`feat` 可能产生多个文件，`-o` 建议指定目录；每个结果会以播放列表编号命名。视频模式会提取所有关联流，音频模式只提取指定音轨。

此外，`feat` 会自动排除时长超过 main 本身时长的播放列表。main 判定会对重复引用的同一个 `.m2ts` 只计一次，避免把原盘中的循环/混淆播放列表误判成主片。

`feat` 还会跳过周期性重复的菜单/引导播放列表：播放项序列至少重复 3 个周期且重复度达到 95% 时，会被识别为循环列表，不会因为循环后的总时长很长而误提取。

正式提取前可加 `--duration 1` 做 1 秒冒烟测试：

```powershell
python -X utf8 remote_bluray.py extract-video "D:\Cinema\strm\...\movie.strm" --playlist 00000.mpls --duration 1 -o "D:\output\smoke.mkv"
```

## 直接选择单个 M2TS

不使用播放列表时，仍可直接指定 `.m2ts`：

```powershell
python -X utf8 remote_bluray.py extract-audio "D:\Cinema\strm\...\movie.strm" --stream 00000.m2ts --map 0:a:0 -o "D:\output\audio.mka"
```

不指定 `--stream` 时默认选择最大的 M2TS。需要完整主片时建议优先使用 `--playlist`，因为播放列表才包含正确的入点、出点和片段顺序。

## 已验证

- 远程 HTTP `206 Partial Content`、115 CDN 重定向和 UDF 分区识别正常。
- 目标原盘列出 12 个有效 `.mpls` 播放列表，并解析出单片段及多片段列表。
- 播放列表模式的 `ffprobe`、音频无损复制和完整视频容器复制均已通过 1 秒冒烟测试。
- 多列表提取、main/feat 选择逻辑和无音频列表跳过逻辑已加入工具。
- `.strm` 路径和直接 ISO URL 两种输入均已通过提取测试。

## 文件

- `remote_bluray.py`：主工具。
- `udf_probe.py`：底层 UDF 诊断脚本。
- `remote_iso_test.py`：早期 `pycdlib` 兼容性测试，不是主流程。
