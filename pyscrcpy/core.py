import random
import socket
import struct
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional, Tuple, Union

import numpy as np
import numpy.typing as npt
from adbutils import AdbConnection, AdbDevice, AdbError, Network, adb
from av.codec import CodecContext  # type: ignore
from av.error import InvalidDataError  # type: ignore
import cv2 as cv
import cv2
from loguru import logger

from .const import EVENT_DISCONNECT, EVENT_FRAME, EVENT_INIT, LOCK_SCREEN_ORIENTATION_UNLOCKED, EVENT_ONCHANGE
from .control import ControlSender

Frame = npt.NDArray[np.int8]

# Bundled scrcpy-server version. MUST match scrcpy-server.jar exactly or the server refuses to
# start ("server version does not match"). v1.20 crashed on Android >= 14 (clipboard API change,
# fixed upstream in 2.3.1); 3.3.1 supports current Android. Bumping the jar requires bumping this.
VERSION = "3.3.1"
HERE = Path(__file__).resolve().parent
JAR = HERE / "scrcpy-server.jar"

# scrcpy 3.x video stream header: 4-byte codec id, u32 width, u32 height (big-endian).
_CODEC_HEADER = ">4sII"
# Map scrcpy codec id -> PyAV decoder name.
_CODEC_TO_AV = {"h264": "h264", "h265": "hevc"}


class Client:
    def __init__(
            self,
            device: Optional[Union[AdbDevice, str]] = None,
            max_size: int = 0,
            bitrate: int = 8000000,
            max_fps: int = 0,
            block_frame: bool = True,
            stay_awake: bool = True,
            lock_screen_orientation: int = LOCK_SCREEN_ORIENTATION_UNLOCKED,
            skip_same_frame=False
    ):
        """
        Create a scrcpy client. The client won't be started until you call .start()

        Args:
            device: Android device to connect to, or its serial string. If None, connect to the
                first available adb device.
            max_size: Max dimension (longest side) of the video stream. 0 = no limit.
            bitrate: video bit rate.
            max_fps: Max FPS. 0 = no limit (Android 10+). Must be > 0 if you rely on
                min_frame_interval.
            block_frame: if True, on_frame callbacks fire only on non-empty frames.
            stay_awake: keep the device awake while connected.
            lock_screen_orientation: kept for API compatibility (scrcpy 3.x manages orientation
                differently); the value is not forwarded to the server.

        Note: this fork is **view-only** -- control is disabled (control=false) so only the video
        socket is opened. ``self.control`` exists for API compatibility but is not wired to a
        socket under the 3.x protocol.
        """
        self.max_size = max_size
        self.bitrate = bitrate
        self.max_fps = max_fps
        self.block_frame = block_frame
        self.stay_awake = stay_awake
        self.lock_screen_orientation = lock_screen_orientation
        self.skip_same_frame = skip_same_frame
        self.min_frame_interval = 1 / max_fps if max_fps else 0

        if device is None:
            try:
                device = adb.device_list()[0]
            except IndexError:
                raise Exception("Cannot connect to phone")
        elif isinstance(device, str):
            device = adb.device(serial=device)

        self.device = device
        self.listeners = dict(frame=[], init=[], disconnect=[], onchange=[])

        # User accessible
        self.last_frame: Optional[np.ndarray] = None
        self.resolution: Optional[Tuple[int, int]] = None
        self.device_name: Optional[str] = None
        self.codec_name: str = "h264"
        self.control = ControlSender(self)

        # Need to destroy
        self.alive = False
        self.scid: str = ""
        self.__server_stream: Optional[AdbConnection] = None
        self.__video_socket: Optional[socket.socket] = None
        self.control_socket: Optional[socket.socket] = None
        self.control_socket_lock = threading.Lock()

    @staticmethod
    def _random_scid() -> str:
        """31-bit random id, hex; lets several scrcpy instances coexist on one device."""
        return format(random.randint(0, 0x7FFFFFFF), "08x")

    def __deploy_server(self) -> None:
        """Push scrcpy-server.jar and launch it with scrcpy 3.x ``key=value`` arguments.

        Video-only (audio/control disabled). ``send_frame_meta=false`` -> a raw H264/H265 byte
        stream (no per-frame headers), matching __stream_loop.
        """
        self.scid = self._random_scid()
        jar_remote = f"/data/local/tmp/scrcpy-server-{self.scid}.jar"

        args = {
            "log_level": "info",
            "tunnel_forward": "true",
            "send_frame_meta": "false",
            "audio": "false",
            "control": "false",
            "video": "true",
            "video_codec": "h264",
            "stay_awake": "true" if self.stay_awake else "false",
            "scid": self.scid,
            "video_bit_rate": str(self.bitrate),
        }
        if self.max_size and self.max_size > 0:
            args["max_size"] = str(self.max_size)
        if self.max_fps and self.max_fps > 0:
            args["max_fps"] = str(self.max_fps)

        cmd = [
            f"CLASSPATH={jar_remote}",
            "app_process",
            "/",
            "com.genymobile.scrcpy.Server",
            VERSION,
        ] + [f"{k}={v}" for k, v in args.items()]

        self.device.sync.push(str(JAR), jar_remote)
        logger.debug(f"push scrcpy-server {VERSION} -> {jar_remote}")
        logger.debug("scrcpy server cmd: " + " ".join(cmd))
        self.__server_stream = self.device.shell(cmd, stream=True)
        threading.Thread(target=self.__server_log_loop, daemon=True).start()

    def __server_log_loop(self) -> None:
        """Drain and log the server's stdout/stderr so crashes are visible (best-effort)."""
        stream = self.__server_stream
        if stream is None:
            return
        buf = ""
        while self.alive or buf:
            try:
                chunk = stream.read_string(256)
            except Exception:
                break
            if not chunk:
                break
            buf += chunk
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                if line.strip():
                    logger.debug(f"[scrcpy-server] {line.strip()}")

    def __init_server_connection(self) -> None:
        """Open the video socket (scrcpy 3.x handshake) and read the codec metadata header."""
        sock_name = f"scrcpy_{self.scid}"
        for _ in range(100):  # ~5s: server may need a moment to start listening
            try:
                self.__video_socket = self.device.create_connection(
                    Network.LOCAL_ABSTRACT, sock_name
                )
                break
            except AdbError:
                time.sleep(0.05)
        else:
            raise ConnectionError("Failed to connect scrcpy-server after 5 seconds")

        dummy_byte = self.__video_socket.recv(1)
        if not len(dummy_byte) or dummy_byte != b"\x00":
            raise ConnectionError("Did not receive Dummy Byte!")

        self.device_name = self.__recv_exactly(64).decode("utf-8").rstrip("\x00")
        if not len(self.device_name):
            raise ConnectionError("Did not receive Device Name!")

        header = self.__recv_exactly(struct.calcsize(_CODEC_HEADER))
        codec_raw, width, height = struct.unpack(_CODEC_HEADER, header)
        self.codec_name = codec_raw.replace(b"\x00", b"").decode("utf-8")
        self.resolution = (width, height)
        logger.info(f"video codec={self.codec_name} resolution={width}x{height}")

        self.__video_socket.setblocking(False)

    def __recv_exactly(self, n: int) -> bytes:
        """Receive exactly n bytes from the (blocking) video socket."""
        data = b""
        while len(data) < n:
            chunk = self.__video_socket.recv(n - len(data))
            if chunk == b"":
                raise ConnectionError("Socket closed during handshake")
            data += chunk
        return data

    def start(self, threaded: bool = False) -> None:
        """Start the client-server connection (register on_frame/on_init callbacks first)."""
        assert self.alive is False

        self.__deploy_server()
        self.alive = True
        self.__init_server_connection()
        for func in self.listeners[EVENT_INIT]:
            func(self)

        if threaded:
            threading.Thread(target=self.__stream_loop).start()
        else:
            self.__stream_loop()

    def stop(self) -> None:
        """Close all sockets / streams. Safe to call repeatedly."""
        self.alive = False
        try:
            self.__server_stream.close()
        except Exception:
            pass
        try:
            self.control_socket.close()
        except Exception:
            pass
        try:
            self.__video_socket.close()
        except Exception:
            pass

    def __del__(self):
        self.stop()

    def __calculate_diff(self, img1, img2):
        if img1 is None:
            return 1
        gray1 = cv.cvtColor(img1, cv.COLOR_BGR2GRAY)
        gray2 = cv.cvtColor(img2, cv.COLOR_BGR2GRAY)
        diff = cv2.absdiff(gray1, gray2)
        threshold = 30
        _, thresholded_diff = cv2.threshold(diff, threshold, 255, cv2.THRESH_BINARY)
        total_diff_pixels = np.sum(thresholded_diff / 255)
        total_pixels = gray1.size
        return total_diff_pixels / total_pixels

    def __stream_loop(self) -> None:
        """Receive the raw video stream and decode it into BGR frames via PyAV."""
        codec = CodecContext.create(_CODEC_TO_AV.get(self.codec_name, "h264"), "r")
        while self.alive:
            try:
                raw = self.__video_socket.recv(0x10000)
                if raw == b"":
                    raise ConnectionError("Video stream is disconnected")
                for packet in codec.parse(raw):
                    for frame in codec.decode(packet):
                        frame = frame.to_ndarray(format="bgr24")

                        if len(self.listeners[EVENT_ONCHANGE]) == 0 and not self.skip_same_frame:
                            self.last_frame = frame
                        elif self.__calculate_diff(self.last_frame, frame) > 0.1:
                            logger.debug("different frame detected")
                            self.last_frame = frame
                            for func in self.listeners[EVENT_ONCHANGE]:
                                func(self, frame)
                        else:
                            continue

                        self.resolution = (frame.shape[1], frame.shape[0])
                        for func in self.listeners[EVENT_FRAME]:
                            func(self, frame)
            except (BlockingIOError, InvalidDataError):  # no data ready / undecodable yet
                time.sleep(0.01)
                if not self.block_frame:
                    for func in self.listeners[EVENT_FRAME]:
                        func(self, None)
            except (ConnectionError, OSError) as e:  # socket closed
                if self.alive:
                    self.stop()
                    raise e

    def on_init(self, func: Callable[[Any], None]) -> None:
        """Add a callback run after the server starts."""
        self.listeners[EVENT_INIT].append(func)

    def on_frame(self, func: Callable[[Any, Frame], None]):
        """Add a callback run on every valid frame."""
        self.listeners[EVENT_FRAME].append(func)

    def on_change(self, func: Callable[[Any, Frame], None]):
        self.listeners[EVENT_ONCHANGE].append(func)
