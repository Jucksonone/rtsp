#!/usr/bin/env python3
"""独立的海康相机录像脚本 —— 不依赖 mouse_not_realtime_pipeline 这个项目。

只做一件事：打开一台海康相机，连续取帧，用 ffmpeg 编码写成一个视频文件，
运行到指定时长或 Ctrl+C 为止。没有数据库、没有多进程框架、没有配置文件体系。

本版本针对实际部署主机做了几处健壮性修正（详见各处注释）：
  1. 显式设置 MVCAM_COMMON_RUNENV 等 SDK 运行环境变量——4.7.x 版 MVS SDK 的
     Python 绑定靠这个变量拼 .so 路径，不只靠 LD_LIBRARY_PATH；非登录 shell
     （systemd/cron/非交互 ssh）不会自动加载 /etc/profile，缺了这个变量会在
     import 阶段直接报 TypeError。
  2. GigE 相机自动探测并设置"最优网络包大小"，减少丢包/花屏。
  3. ROI 的 OffsetX/OffsetY 按相机步进值对齐，避免设置失败或被静默取整。
  4. 编码器改成按优先级链路探测+冒烟测试，而不是假设某个编码器一定可用。
  5. 采帧和编码写出解耦成两个线程，通过有界队列缓冲，避免 ffmpeg 写入偶发
     卡顿（磁盘慢、CPU 争抢）反过来拖慢/阻塞相机取帧。

用法示例
--------
    python record_clip.py --output clip.mp4 --duration 30
    python record_clip.py --output clip.mkv --duration 60 \
        --width 1024 --height 1024 --offset-x 100 --offset-y 0 \
        --exposure-us 6500 --decimation 2 --fps 30

依赖
----
- 海康 MVS SDK（Python 绑定 MvCameraControl_class.py 能被 import 到）
- 系统装好 ffmpeg，在 PATH 里能找到（或用 --ffmpeg-bin 指定路径）
- pip install numpy
"""
from __future__ import annotations

import argparse
import csv
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from ctypes import POINTER, byref, cast, memset, sizeof, c_ubyte, addressof
from pathlib import Path
from typing import Any

import numpy as np

# ---------------------------------------------------------------- 按你的实际安装路径改这里
# Linux 典型路径；Windows 一般是
#   C:\Program Files (x86)\MVS\Development\Samples\Python\MvImport
MVS_IMPORT_CANDIDATES = [
    Path("/opt/MVS/Samples/64/Python/MvImport"),
    Path(r"C:\Program Files (x86)\MVS\Development\Samples\Python\MvImport"),
]


def _import_mvs_sdk():
    """定位 MvImport 目录并 import SDK，顺带把 SDK 运行所需的环境变量配好。

    4.7.x 版 MVS SDK 的 MvCameraControl_class.py 用
    `os.getenv('MVCAM_COMMON_RUNENV') + "/64/libMvCameraControl.so"`
    拼 .so 路径——这个变量不设，import 阶段就会报
    `TypeError: unsupported operand type(s) for +: 'NoneType' and 'str'`。
    正常登录 shell 一般会从 /etc/profile 里继承到这几个变量，但脚本以后如果
    被 systemd/cron/非交互 ssh 这类不走登录 shell 的方式拉起，就会缺失——
    所以这里不依赖外部环境，自己根据找到的 SDK 路径推算并兜底设置好，
    已经正确设置的情况下不覆盖。
    """
    for candidate in MVS_IMPORT_CANDIDATES:
        if (candidate / "MvCameraControl_class.py").exists():
            mv_import = candidate
            break
    else:
        raise RuntimeError(
            "找不到海康 MVS SDK 的 Python 绑定（MvCameraControl_class.py）。"
            "请确认已安装 MVS SDK，或编辑本文件顶部 MVS_IMPORT_CANDIDATES 加上你的实际安装路径。"
        )
    if str(mv_import) not in sys.path:
        sys.path.append(str(mv_import))

    # MvImport 目录形如 <MVS_ROOT>/Samples/64/Python/MvImport，往上推 4 层拿到 SDK 根目录。
    mvs_root = mv_import.parents[3]
    if sys.platform.startswith("linux"):
        os.environ.setdefault("MVCAM_SDK_PATH", str(mvs_root))
        os.environ.setdefault("MVCAM_COMMON_RUNENV", str(mvs_root / "lib"))
        os.environ.setdefault("MVCAM_GENICAM_CLPROTOCOL", str(mvs_root / "lib" / "CLProtocol"))
        lib64 = str(mvs_root / "lib" / "64")
        if lib64 not in os.environ.get("LD_LIBRARY_PATH", ""):
            os.environ["LD_LIBRARY_PATH"] = lib64 + ":" + os.environ.get("LD_LIBRARY_PATH", "")

    import MvCameraControl_class as mvs  # type: ignore
    return mvs


# ---------------------------------------------------------------- 相机封装
class Camera:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.mvs = _import_mvs_sdk()
        self.cam = None
        self.width = 0
        self.height = 0
        self.is_gige = False

    def open(self) -> None:
        mvs = self.mvs
        cam = mvs.MvCamera()
        device_list = mvs.MV_CC_DEVICE_INFO_LIST()
        ret = mvs.MvCamera.MV_CC_EnumDevices(mvs.MV_GIGE_DEVICE | mvs.MV_USB_DEVICE, device_list)
        if ret != 0 or device_list.nDeviceNum == 0:
            raise RuntimeError(f"没有发现海康相机设备，ret=0x{ret:x}（检查相机供电/网线或USB连接）")
        device = cast(device_list.pDeviceInfo[0], POINTER(mvs.MV_CC_DEVICE_INFO)).contents
        self.is_gige = device.nTLayerType == mvs.MV_GIGE_DEVICE
        print(f"发现设备类型：{'GigE' if self.is_gige else 'USB/其它'}")
        ret = cam.MV_CC_CreateHandle(device)
        if ret != 0:
            raise RuntimeError(f"创建相机句柄失败：0x{ret:x}")
        ret = cam.MV_CC_OpenDevice(mvs.MV_ACCESS_Exclusive, 0)
        if ret != 0:
            cam.MV_CC_DestroyHandle()
            raise RuntimeError(f"独占打开相机失败：0x{ret:x}（可能已被其它程序占用）")
        self.cam = cam
        if self.is_gige:
            self._optimize_gige_packet_size()
        self._configure()
        ret = cam.MV_CC_StartGrabbing()
        if ret != 0:
            raise RuntimeError(f"启动取流失败：0x{ret:x}")

    def _optimize_gige_packet_size(self) -> None:
        """GigE 相机设最优网络包大小；能显著降低丢包/花屏概率，USB 相机不需要。"""
        try:
            packet_size = self.cam.MV_CC_GetOptimalPacketSize()
            if packet_size > 0:
                ret = self.cam.MV_CC_SetIntValue("GevSCPSPacketSize", int(packet_size))
                if ret == 0:
                    print(f"GigE 最优包大小设置成功：{packet_size}")
                else:
                    print(f"[警告] GigE 最优包大小设置失败，ret=0x{ret:x}（继续用相机默认值）")
            else:
                print(f"[警告] 获取 GigE 最优包大小失败：{packet_size}")
        except Exception as exc:
            print(f"[警告] GigE 包大小优化异常（继续运行）：{exc}")

    def _configure(self) -> None:
        a, cam, mvs = self.args, self.cam, self.mvs
        # 关键：强制像素格式为 Mono8。如果相机当前输出 Bayer/彩色或 Mono10/12
        # 等格式，而我们按 gray(8bit) 喂给 ffmpeg，编码出的视频会偏色/发绿。
        ret = cam.MV_CC_SetEnumValueByString("PixelFormat", a.pixel_format)
        if ret != 0:
            print(f"[警告] 像素格式设置失败，ret=0x{ret:x}，若视频偏色请检查相机实际输出格式")
        cam.MV_CC_SetEnumValue("TriggerMode", 0)  # 0=Off，连续自由采集

        if a.decimation and a.decimation > 1:
            cam.MV_CC_SetEnumValue("DecimationHorizontal", int(a.decimation))
            cam.MV_CC_SetEnumValue("DecimationVertical", int(a.decimation))
            time.sleep(0.2)  # 等待下采样生效，否则紧接着读到的 OffsetX 步进值可能还是旧的

        # OffsetX/OffsetY 必须按相机步进值（nInc）对齐，否则在不少机型上会设置
        # 失败，或者被相机悄悄取整到一个你没想到的值。
        offset_x, offset_y = a.offset_x, a.offset_y
        if offset_x is not None or offset_y is not None:
            inc_x_info, inc_y_info = mvs.MVCC_INTVALUE(), mvs.MVCC_INTVALUE()
            cam.MV_CC_GetIntValue("OffsetX", inc_x_info)
            cam.MV_CC_GetIntValue("OffsetY", inc_y_info)
            inc_x = inc_x_info.nInc or 1
            inc_y = inc_y_info.nInc or 1
            if offset_x is not None:
                aligned = (int(offset_x) // inc_x) * inc_x
                if aligned != offset_x:
                    print(f"[提示] OffsetX {offset_x} 按步进值 {inc_x} 对齐为 {aligned}")
                offset_x = aligned
            if offset_y is not None:
                aligned = (int(offset_y) // inc_y) * inc_y
                if aligned != offset_y:
                    print(f"[提示] OffsetY {offset_y} 按步进值 {inc_y} 对齐为 {aligned}")
                offset_y = aligned

        # 设置顺序固定为 Width -> Height -> OffsetX -> OffsetY（海康相机对这个
        # 顺序比较敏感，反过来容易因为超出当前 OffsetX+Width 的合法范围而失败）。
        if a.width:
            ret = cam.MV_CC_SetIntValue("Width", int(a.width))
            if ret != 0:
                raise RuntimeError(f"设置 ROI 宽度失败：0x{ret:x}")
        if a.height:
            ret = cam.MV_CC_SetIntValue("Height", int(a.height))
            if ret != 0:
                raise RuntimeError(f"设置 ROI 高度失败：0x{ret:x}")
        if offset_x is not None:
            ret = cam.MV_CC_SetIntValue("OffsetX", int(offset_x))
            if ret != 0:
                raise RuntimeError(f"设置 OffsetX 失败：0x{ret:x}")
        if offset_y is not None:
            ret = cam.MV_CC_SetIntValue("OffsetY", int(offset_y))
            if ret != 0:
                raise RuntimeError(f"设置 OffsetY 失败：0x{ret:x}")

        cam.MV_CC_SetEnumValueByString("ExposureAuto", "Continuous" if a.auto_exposure else "Off")
        if not a.auto_exposure and a.exposure_us:
            ret = cam.MV_CC_SetFloatValue("ExposureTime", float(a.exposure_us))
            if ret != 0:
                print(f"[警告] 曝光时间设置失败，ret=0x{ret:x}，将使用相机当前值")

        cam.MV_CC_SetEnumValueByString("GainAuto", "Continuous" if a.auto_gain else "Off")
        if not a.auto_gain and a.gain_db is not None:
            cam.MV_CC_SetFloatValue("Gain", float(a.gain_db))

        # Gamma/Contrast 不是所有机型都支持，失败就跳过、不影响录制主流程。
        if a.gamma is not None:
            try:
                cam.MV_CC_SetBoolValue("GammaEnable", True)
                ret = cam.MV_CC_SetFloatValue("Gamma", float(a.gamma))
                if ret != 0:
                    print(f"[警告] Gamma 设置失败，ret=0x{ret:x}（此机型可能不支持，使用默认值）")
            except Exception as exc:
                print(f"[警告] Gamma 设置异常（跳过）：{exc}")
        if a.contrast is not None:
            try:
                ret = cam.MV_CC_SetIntValue("Contrast", int(a.contrast))
                if ret != 0:
                    print(f"[警告] 对比度设置失败，ret=0x{ret:x}（此机型可能不支持，使用默认值）")
            except Exception as exc:
                print(f"[警告] 对比度设置异常（跳过）：{exc}")

        if a.fps:
            cam.MV_CC_SetBoolValue("AcquisitionFrameRateEnable", True)
            ret = cam.MV_CC_SetFloatValue("AcquisitionFrameRate", float(a.fps))
            if ret != 0:
                print(f"[警告] 帧率设置失败，ret=0x{ret:x}")
            time.sleep(0.2)
            actual = mvs.MVCC_FLOATVALUE()
            if cam.MV_CC_GetFloatValue("ResultingFrameRate", actual) == 0:
                print(f"相机实际输出帧率：{actual.fCurValue:.2f} FPS（目标 {a.fps}）")

        # 回读实际生效的宽高，交给调用方核对/传给 ffmpeg（比如 Width 设置因为
        # 范围限制被相机取整，这里拿到的才是真值）。
        w, h = mvs.MVCC_INTVALUE(), mvs.MVCC_INTVALUE()
        cam.MV_CC_GetIntValue("Width", w)
        cam.MV_CC_GetIntValue("Height", h)
        self.width, self.height = w.nCurValue, h.nCurValue

    def read(self, timeout_ms: int = 1000) -> np.ndarray:
        mvs = self.mvs
        frame_out = mvs.MV_FRAME_OUT()
        memset(byref(frame_out), 0, sizeof(frame_out))
        ret = self.cam.MV_CC_GetImageBuffer(frame_out, timeout_ms)
        if ret != 0 or not frame_out.pBufAddr:
            raise TimeoutError(f"取帧超时/失败：0x{ret:x}")
        try:
            info = frame_out.stFrameInfo
            width, height = int(info.nWidth), int(info.nHeight)
            expected = width * height
            data = (c_ubyte * expected).from_address(addressof(frame_out.pBufAddr.contents))
            return np.frombuffer(data, dtype=np.uint8, count=expected).reshape(height, width).copy()
        finally:
            self.cam.MV_CC_FreeImageBuffer(frame_out)

    def close(self) -> None:
        if self.cam is None:
            return
        try:
            self.cam.MV_CC_StopGrabbing()
            self.cam.MV_CC_CloseDevice()
            self.cam.MV_CC_DestroyHandle()
        finally:
            self.cam = None


# ---------------------------------------------------------------- ffmpeg 编码器探测
# 优先级链路跟主项目 configs/session.yaml 的 video_archive.raw.encoder_auto_order
# 保持一致：优先 h264_nvenc（硬件 H.264），不是 HEVC——H.264 解码兼容性更广，
# 分析管线/各种工具链都认。GPU 不可用再降级到 CPU 软编码。每一档既要在
# -encoders 列表里，又要实际跑一帧冒烟测试——NVENC 经常是"编进 ffmpeg 了但
# 运行时驱动不支持"，只看列表会判断错误。
ENCODER_FALLBACK_CHAIN = ["h264_nvenc", "hevc_nvenc", "libx264", "libx265", "libopenh264"]


def _encoder_listed(ffmpeg_bin: str, encoder: str) -> bool:
    proc = subprocess.run([ffmpeg_bin, "-hide_banner", "-encoders"],
                          capture_output=True, text=True, check=False)
    return proc.returncode == 0 and encoder in proc.stdout


def _encoder_works(ffmpeg_bin: str, encoder: str) -> bool:
    cmd = [ffmpeg_bin, "-hide_banner", "-nostdin", "-loglevel", "error",
           "-f", "lavfi", "-i", "testsrc2=size=64x64:rate=1",
           "-frames:v", "1", "-pix_fmt", "yuv420p", "-c:v", encoder, "-f", "null", "-"]
    return subprocess.run(cmd, capture_output=True, check=False).returncode == 0


def choose_encoder(ffmpeg_bin: str, requested: str | None) -> str:
    if requested and requested != "auto":
        return requested
    for encoder in ENCODER_FALLBACK_CHAIN:
        if _encoder_listed(ffmpeg_bin, encoder) and _encoder_works(ffmpeg_bin, encoder):
            print(f"自动选定编码器：{encoder}")
            return encoder
    raise RuntimeError(f"没有一个可用的编码器（尝试过：{ENCODER_FALLBACK_CHAIN}）")


def start_ffmpeg(ffmpeg_bin: str, encoder: str, width: int, height: int,
                  fps: float, output: str, log_path: Path,
                  quality: int, preset: str) -> subprocess.Popen:
    """quality/preset 默认对齐主项目 raw 视频的参数（near-lossless + 0 B帧）：
    cq/crf=15、preset=slow、-bf 0。raw 视频是分析唯一的输入源，近无损是为了不
    丢检测/跟踪需要的细节，零 B 帧是为了帧号和视频内位置能严格对齐（逐帧精确
    寻址的前提）——不是单纯为了画质，这里按同样的理由默认继承。
    """
    cmd = [
        ffmpeg_bin, "-hide_banner", "-nostdin", "-loglevel", "warning",
        "-f", "rawvideo", "-vcodec", "rawvideo",
        "-s", f"{width}x{height}",
        "-pix_fmt", "gray",              # Mono8 对应 ffmpeg 的 gray（8bit 灰度）
        "-r", str(fps),
        "-i", "-",
        "-c:v", encoder,
        "-bf", "0",                      # 零 B 帧：保证顺序解码，帧号与视频内位置严格对齐
    ]
    if encoder in ("libx264", "libx265"):
        cmd += ["-preset", preset, "-crf", str(quality)]
    elif encoder.endswith("_nvenc"):
        cmd += ["-preset", preset, "-rc", "vbr", "-cq", str(quality), "-gpu", "0"]
    elif encoder == "libopenh264":
        cmd += ["-b:v", "6M"]
    cmd += ["-pix_fmt", "yuv420p", "-y", output]
    log_fh = open(log_path, "wb")
    return subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=log_fh)


# ---------------------------------------------------------------- 采帧线程（解耦读/写）
class LatestFrame:
    """跨线程共享"最新一帧"，只保留最新值，不排队——预览旁路用。

    录制走的是有界队列（宁可丢也不堵），预览比录制要求更低：只要当下这一帧，
    不需要追赶中间帧。用一个加锁的单值槽，set() 永远是 O(1) 覆盖，不会跟
    录制路径抢时间、也不会自己无限增长。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._item: tuple[float, np.ndarray] | None = None

    def set(self, ts: float, frame: np.ndarray) -> None:
        with self._lock:
            self._item = (ts, frame)

    def get(self) -> tuple[float, np.ndarray] | None:
        with self._lock:
            return self._item


class FrameReader(threading.Thread):
    """独立线程持续取帧放进有界队列；队列满时丢帧而不是阻塞取帧循环。

    ffmpeg 写入端偶发卡顿（磁盘慢、CPU 被抢）不应该反过来拖慢相机取帧——
    这正是 mouse_not_realtime_pipeline 主项目里"采集优先"原则在这个独立小
    脚本里的对应做法。队列容量按 --queue-seconds 秒数折算成帧数。
    """

    def __init__(self, camera: Camera, out_queue: "queue.Queue[Any]", stop_event: threading.Event,
                 latest_frame: "LatestFrame | None" = None):
        super().__init__(daemon=True, name="frame-reader")
        self.camera = camera
        self.out_queue = out_queue
        self.stop_event = stop_event
        self.latest_frame = latest_frame
        self.dropped = 0
        self.error: Exception | None = None

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                frame = self.camera.read(timeout_ms=1000)
            except TimeoutError as exc:
                print(f"[警告] {exc}，继续尝试", file=sys.stderr)
                continue
            except Exception as exc:  # 相机级错误：记下来，交给主线程决定是否终止
                self.error = exc
                return
            ts = time.time()
            if self.latest_frame is not None:
                self.latest_frame.set(ts, frame)  # O(1)，不影响下面的录制入队
            try:
                self.out_queue.put_nowait((ts, frame))
            except queue.Full:
                self.dropped += 1
        try:
            self.out_queue.put_nowait(None)  # 结束哨兵
        except queue.Full:
            # 仅预览模式下没有消费者排空队列，退出时队列大概率已经满了；
            # 哨兵塞不进去不影响什么（主循环本来就是靠 stop_event 而不是靠
            # 这个哨兵来判断仅预览模式该不该退出的）。
            pass


# ---------------------------------------------------------------- 预览旁路（可选，RTSP/RTMP/HLS/WebRTC/SRT）
def start_preview_ffmpeg(ffmpeg_bin: str, width: int, height: int, fps: float,
                          rtsp_url: str, log_path: Path) -> subprocess.Popen:
    """推一路低负载的预览流到 MediaMTX（或任意接受 RTSP 推流的服务器）。

    跟录制完全是另一个 ffmpeg 进程、另一路参数：固定用 CPU 的 libx264
    ultrafast，不用 NVENC——这颗 Quadro M620 是不是真的不限并发编码会话不好
    确定，录制已经在用 h264_nvenc 了，预览绝不能跟它抢，这跟主项目
    preview_stream_service 的注释原因一模一样（"避免与主归档争抢 NVENC"）。
    推送到服务器之后，RTSP/RTMP/HLS/WebRTC/SRT 这几种协议都能从服务器那边
    直接拉，不需要为每种协议单独编码。
    """
    cmd = [
        ffmpeg_bin, "-hide_banner", "-nostdin", "-loglevel", "warning",
        "-f", "rawvideo", "-vcodec", "rawvideo",
        "-s", f"{width}x{height}",
        "-pix_fmt", "gray",
        "-r", str(fps),
        "-i", "-",
        "-an",
        "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
        "-crf", "28", "-threads", "1",
        "-pix_fmt", "yuv420p",
        "-f", "rtsp", "-rtsp_transport", "tcp", rtsp_url,
    ]
    log_fh = open(log_path, "wb")
    return subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=log_fh)


class PreviewPusher(threading.Thread):
    """按 preview_fps 从 LatestFrame 抽帧喂给预览 ffmpeg；只抽最新帧，不追赶。

    任何失败（推流进程退出、网络断开）都只是静默停止预览本身，绝不抛到主
    线程、绝不影响录制——这是预览旁路存在的全部意义：可丢、可断、不重要。
    """

    def __init__(self, latest_frame: LatestFrame, ffmpeg_proc: subprocess.Popen,
                 fps: float, stop_event: threading.Event):
        super().__init__(daemon=True, name="preview-pusher")
        self.latest_frame = latest_frame
        self.ffmpeg_proc = ffmpeg_proc
        self.period_s = 1.0 / max(0.1, fps)
        self.stop_event = stop_event
        self._last_ts: float | None = None

    def run(self) -> None:
        next_at = time.monotonic()
        while not self.stop_event.is_set():
            now = time.monotonic()
            if now < next_at:
                time.sleep(min(0.05, next_at - now))
                continue
            next_at += self.period_s
            item = self.latest_frame.get()
            if item is None:
                continue
            ts, frame = item
            if ts == self._last_ts:
                continue  # 还是上一帧，预览旁路不追赶中间帧，跳过
            self._last_ts = ts
            try:
                self.ffmpeg_proc.stdin.write(frame.tobytes())
            except (BrokenPipeError, OSError):
                return  # 推流断了：静默退出，录制完全不受影响


def main() -> None:
    p = argparse.ArgumentParser(description="独立海康相机录像脚本")
    p.add_argument("--output", default=None,
                   help="输出视频文件路径，如 clip.mp4 / clip.mkv；不填则只推流不落盘"
                        "（这种情况下必须给 --rtsp-url，否则脚本什么都不干）")
    p.add_argument("--duration", type=float, default=None, help="录制时长（秒），不填则录到 Ctrl+C")
    p.add_argument("--fps", type=float, default=30.0)
    p.add_argument("--pixel-format", default="Mono8")
    p.add_argument("--width", type=int, default=None, help="不填则用相机当前/默认值")
    p.add_argument("--height", type=int, default=None)
    p.add_argument("--offset-x", type=int, default=None)
    p.add_argument("--offset-y", type=int, default=None)
    p.add_argument("--decimation", type=int, default=1, help="抽点降采样因子，1=不降采样")
    p.add_argument("--exposure-us", type=float, default=6500.0)
    p.add_argument("--auto-exposure", action="store_true", help="用自动曝光（默认用固定曝光）")
    p.add_argument("--auto-gain", dest="auto_gain", action="store_true", default=True)
    p.add_argument("--gain-db", type=float, default=None, help="固定增益(dB)，配合 --no-auto-gain 使用")
    p.add_argument("--no-auto-gain", dest="auto_gain", action="store_false")
    p.add_argument("--gamma", type=float, default=None, help="伽马值，如 0.5；不填则不改动")
    p.add_argument("--contrast", type=int, default=None, help="对比度，如 100；不填则不改动")
    p.add_argument("--encoder", default="auto", help="auto（自动探测）/ libx264 / h264_nvenc / ...")
    p.add_argument("--quality", type=int, default=15,
                   help="NVENC用作cq、libx264/265用作crf，越小越接近无损；默认15对齐主项目raw视频标准")
    p.add_argument("--preset", default="slow", help="编码预设，默认slow（压缩率优先，对齐主项目）")
    p.add_argument("--ffmpeg-bin", default="ffmpeg")
    p.add_argument("--queue-seconds", type=float, default=10.0, help="采帧/编码之间的缓冲秒数")
    p.add_argument("--rtsp-url", default=None,
                   help="可选：实时预览推流地址，如 rtsp://127.0.0.1:8554/live"
                        "（推到 MediaMTX 之类的服务器，RTSP/RTMP/HLS/WebRTC/SRT 都能从服务器拉）")
    p.add_argument("--preview-fps", type=float, default=8.0,
                   help="预览推流帧率，默认8（跟录制帧率解耦，低负载、可丢帧）")
    args = p.parse_args()

    recording_enabled = bool(args.output)
    if not recording_enabled and not args.rtsp_url:
        p.error("必须指定 --output（落盘）或 --rtsp-url（推流）中至少一个，否则脚本无事可做")

    output_path = ffmpeg_log = timestamps_path = None
    if recording_enabled:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        ffmpeg_log = output_path.with_suffix(output_path.suffix + ".ffmpeg.log")
        timestamps_path = output_path.with_suffix(output_path.suffix + ".timestamps.csv")

    stop_event = threading.Event()

    def _on_signal(signum, frame):
        stop_event.set()

    signal.signal(signal.SIGINT, _on_signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _on_signal)

    camera = Camera(args)
    camera.open()
    if recording_enabled:
        print(f"相机已打开，实际分辨率 {camera.width}x{camera.height}，开始录制 -> {args.output}")
    else:
        print(f"相机已打开，实际分辨率 {camera.width}x{camera.height}，仅预览模式（不落盘）")

    ffmpeg: subprocess.Popen | None = None
    if recording_enabled:
        encoder = choose_encoder(args.ffmpeg_bin, args.encoder)
        ffmpeg = start_ffmpeg(args.ffmpeg_bin, encoder, camera.width, camera.height,
                              args.fps, str(output_path), ffmpeg_log,
                              args.quality, args.preset)

    # 预览旁路完全可选，且跟录制路径是两个独立的 ffmpeg 进程/线程：这里起不起、
    # 起成功与否，都不应该影响下面的录制逻辑一个字节。仅预览模式没有 output_path
    # 可借用，日志落在 /tmp 下，按 pid 区分。
    preview_log = (output_path.with_suffix(output_path.suffix + ".preview.log") if recording_enabled
                   else Path(f"/tmp/record_clip_preview_{os.getpid()}.log"))
    latest_frame: LatestFrame | None = None
    preview_ffmpeg: subprocess.Popen | None = None
    preview_pusher: PreviewPusher | None = None
    if args.rtsp_url:
        try:
            latest_frame = LatestFrame()
            preview_ffmpeg = start_preview_ffmpeg(args.ffmpeg_bin, camera.width, camera.height,
                                                  args.preview_fps, args.rtsp_url, preview_log)
            preview_pusher = PreviewPusher(latest_frame, preview_ffmpeg, args.preview_fps, stop_event)
            preview_pusher.start()
            print(f"预览推流已启动 -> {args.rtsp_url}（{args.preview_fps} FPS，仅供预览，不影响录制）")
        except Exception as exc:
            print(f"[警告] 预览推流启动失败，不影响录制，继续：{exc}", file=sys.stderr)
            latest_frame = None

    # 仅预览模式没有消费者去读这个队列，给个小容量即可（反正不会有人取，多大
    # 都一样会被 FrameReader 判满丢弃，没必要留大内存）。
    queue_size = max(1, int(args.fps * args.queue_seconds)) if recording_enabled else 2
    frame_queue: "queue.Queue[Any]" = queue.Queue(maxsize=queue_size)
    reader = FrameReader(camera, frame_queue, stop_event, latest_frame=latest_frame)
    reader.start()

    deadline = time.monotonic() + args.duration if args.duration else None
    frame_count = 0
    timestamps: list[tuple[int, float]] = []
    try:
        if recording_enabled:
            while True:
                if deadline is not None and time.monotonic() >= deadline:
                    stop_event.set()
                try:
                    item = frame_queue.get(timeout=1.0)
                except queue.Empty:
                    if stop_event.is_set():
                        break
                    continue
                if item is None:  # 读帧线程结束哨兵
                    break
                ts, frame = item
                try:
                    ffmpeg.stdin.write(frame.tobytes())
                except BrokenPipeError:
                    print("[错误] ffmpeg 已退出，停止录制", file=sys.stderr)
                    stop_event.set()
                    break
                timestamps.append((frame_count, ts))
                frame_count += 1
                if frame_count % max(1, int(args.fps)) == 0:
                    print(f"已录制 {frame_count} 帧 (~{frame_count / args.fps:.0f}秒)"
                          f"，丢帧 {reader.dropped}", end="\r")
        else:
            # 仅预览模式：没有消费者读 frame_queue（本来也没人要用），只是等
            # 时长到或 Ctrl+C；真正的画面搬运由 PreviewPusher 线程独立完成。
            last_print = 0.0
            while not stop_event.is_set():
                if deadline is not None and time.monotonic() >= deadline:
                    break
                now = time.time()
                if now - last_print > 2:
                    print("仅预览模式运行中...（Ctrl+C 停止）", end="\r")
                    last_print = now
                time.sleep(0.2)
    finally:
        stop_event.set()
        reader.join(timeout=5)
        camera.close()
        rc = 0
        if recording_enabled:
            print(f"\n正在收尾：共采集 {frame_count} 帧，丢帧 {reader.dropped}")
            if ffmpeg.stdin:
                try:
                    ffmpeg.stdin.close()
                except Exception:
                    pass
            rc = ffmpeg.wait(timeout=30)
            with open(timestamps_path, "w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(["frame_id", "unix_timestamp"])
                writer.writerows(timestamps)
            print(f"ffmpeg 退出码={rc}，输出文件：{output_path}")
            print(f"逐帧时间戳：{timestamps_path}")
            print(f"ffmpeg 日志：{ffmpeg_log}")
        else:
            print("\n仅预览模式结束，没有写入任何视频文件。")
        # 预览旁路收尾：可丢弃、超时就直接 kill，绝不拖慢主录制流程已经完成的事实。
        if preview_pusher is not None:
            preview_pusher.join(timeout=2)
        if preview_ffmpeg is not None:
            if preview_ffmpeg.stdin:
                try:
                    preview_ffmpeg.stdin.close()
                except Exception:
                    pass
            try:
                preview_ffmpeg.wait(timeout=3)
            except subprocess.TimeoutExpired:
                preview_ffmpeg.kill()
        if reader.error is not None:
            print(f"[错误] 采帧线程异常退出：{reader.error}", file=sys.stderr)
        if rc != 0:
            sys.exit(1)


if __name__ == "__main__":
    main()
