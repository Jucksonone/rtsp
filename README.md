# record_clip.py —— 独立海康相机录像/推流脚本

打开一台海康相机，可以录成文件、可以推 RTSP 直播、也可以两者同时做。

## 环境

```
python3 record_clip.py   # 需要能 import numpy 的解释器（如有专门的 conda/venv 环境，用那个环境的 python）
```

依赖：海康 MVS SDK（Linux 典型装在 `/opt/MVS`，Windows 装在 MVS 安装目录下的
`Samples/Python/MvImport`，装到别处要改脚本顶部 `MVS_IMPORT_CANDIDATES`）、
ffmpeg（PATH 里能找到即可）、numpy。

## 常用命令

**只录文件**：
```bash
python3 record_clip.py \
    --output clip.mkv --duration 300 \
    --width 1024 --height 1024 --offset-x 100 --offset-y 0 \
    --decimation 2 --exposure-us 6500 --gamma 0.5
```

**只推流，不落盘**（不传 `--output`）：
```bash
python3 record_clip.py \
    --duration 300 \
    --width 1024 --height 1024 --offset-x 100 --offset-y 0 \
    --decimation 2 --exposure-us 6500 --gamma 0.5 \
    --rtsp-url rtsp://127.0.0.1:8554/live
```

**录文件 + 同时推流**：
```bash
python3 record_clip.py \
    --output clip.mkv --duration 300 \
    --width 1024 --height 1024 --offset-x 100 --offset-y 0 \
    --decimation 2 --exposure-us 6500 --gamma 0.5 \
    --rtsp-url rtsp://127.0.0.1:8554/live --preview-fps 8
```

不传 `--duration` 就一直录/推到按 `Ctrl+C` 为止。

**推流前必须先起一个接受 RTSP 推流的服务器**，推荐 [MediaMTX](https://github.com/bluenviron/mediamtx)
（单文件二进制，下载解压即可用默认配置启动）：
```bash
./mediamtx mediamtx.yml &
```
局域网内看直播（把 `<host>` 换成跑脚本那台机器的局域网 IP）：
- VLC/ffplay：`rtsp://<host>:8554/live`
- 浏览器 WebRTC（延迟最低）：`http://<host>:8889/live`
- 浏览器 HLS（兼容性最好，延迟几秒）：`http://<host>:8888/live/index.m3u8`

## 参数说明

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--output` | 无 | 输出视频路径（`.mkv`/`.mp4`）。不填则只推流不落盘，此时必须给 `--rtsp-url` |
| `--duration` | 不限 | 录制/推流时长（秒）。不填则一直跑到 `Ctrl+C` |
| `--fps` | 30 | 相机目标帧率 |
| `--width` / `--height` | 相机当前值 | ROI 宽高 |
| `--offset-x` / `--offset-y` | 不设 | ROI 起始坐标，会自动按相机步进值对齐 |
| `--decimation` | 1 | 硬件抽点降采样倍数，1=不降采样 |
| `--exposure-us` | 6500 | 固定曝光时间（微秒） |
| `--auto-exposure` | 关闭 | 加这个开关 = 用自动曝光（忽略 `--exposure-us`） |
| `--auto-gain` / `--no-auto-gain` | 自动增益开 | 用 `--no-auto-gain --gain-db <值>` 切成固定增益 |
| `--gamma` | 不改 | 伽马值，如 `0.5` |
| `--contrast` | 不改 | 对比度，如 `100`（此相机型号不支持，会自动跳过并提示） |
| `--encoder` | auto | `auto`=自动探测（优先 h264_nvenc，GPU可用时实测会选中）；也可指定 `libx264`/`hevc_nvenc` 等 |
| `--quality` | 15 | 画质，NVENC 用作 `cq`，libx264/265 用作 `crf`，越小越接近无损（对齐主项目分析用视频标准） |
| `--preset` | slow | 编码预设，压缩率优先 |
| `--rtsp-url` | 无 | 推流地址，如 `rtsp://127.0.0.1:8554/live`。给了才会推流，MediaMTX 要先启动好 |
| `--preview-fps` | 8 | 推流帧率，跟主录制帧率解耦，独立一路低负载编码，不占用录制用的 NVENC |
| `--queue-seconds` | 10 | 采帧线程和录制编码之间的缓冲时长（秒），一般不用改 |
| `--ffmpeg-bin` | ffmpeg | 自定义 ffmpeg 路径 |

## 输出文件

录制模式下，`--output clip.mkv` 会在同目录多产出两个文件：
- `clip.mkv.timestamps.csv` —— 每帧的墙钟时间戳（`frame_id,unix_timestamp`）
- `clip.mkv.ffmpeg.log` —— 录制编码器的 stderr 日志，出问题先看这个

推流时还会多一个 `xxx.preview.log`（仅录制+推流模式）或 `/tmp/record_clip_preview_<pid>.log`（仅预览模式），是推流 ffmpeg 的日志。

## 设计要点（排障用得上）

- 采帧和编码写出是两个独立线程/队列，下游卡顿只会丢帧不会拖慢相机取流。
- 推流是完全独立的第二路 ffmpeg 进程，只抽"当前最新帧"（不追赶），固定用 CPU `libx264`（不占用录制用的 GPU 编码会话），推流炸了只会静默停止预览，不影响录制。
- 终端打印的"丢帧"计数应该始终是 0；如果不是，说明下游（磁盘或网络）跟不上，需要排查而不是忽略。
