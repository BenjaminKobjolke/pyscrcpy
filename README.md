# Python Scrcpy Client

> ## What this fork achieves
>
> Upstream `pyscrcpy` bundles **scrcpy-server v1.20** (2021), which **crashes on Android ≥ 14**
> during server startup (`Device.<init>` → `IClipboard.addPrimaryClipChangedListener`
> `NoSuchMethodException`). The server dies right after the handshake, so the video socket closes
> and you get `ConnectionError: Video stream is disconnected` / black frames — modern phones
> simply don't stream.
>
> **This fork fixes that:** it bundles the official **scrcpy-server v3.3.1** and ports the client
> to the **scrcpy 3.x protocol** (random `scid`, `key=value` server args, `>4sII` codec-metadata
> header, raw stream with `send_frame_meta=false`). Verified streaming on Android 16. The public
> `Client` API is unchanged, so existing code keeps working.
>
> **View-only:** audio and control are disabled (`audio=false control=false`) — only the video
> socket is opened. `client.control` exists for API compatibility but is not wired under the 3.x
> protocol. To bump the server, replace `pyscrcpy/scrcpy-server.jar` and the matching `VERSION`
> in `pyscrcpy/core.py` (they must match exactly).

# Introduction
![scrcpy-badge](https://img.shields.io/badge/scrcpy-v3.3.1-violet)

A Python Library for scrcpy  
pyscrcpy is an innovative Python library designed to simplify and streamline the integration of scrcpy into your Python projects. Scrcpy, a versatile screen mirroring tool for Android devices, gains a new level of accessibility through the seamless capabilities provided by pyscrcpy.

# Key Features

1. Easy Integration: With pyscrcpy, incorporating scrcpy functionality into your Python scripts becomes a straightforward process. The library abstracts away the complexities, allowing you to focus on leveraging scrcpy's powerful features without the need for intricate setup.
2. Enhanced Control: pyscrcpy empowers developers to exert precise control over Android devices from within their Python applications. Whether it's automating UI interactions, conducting tests, or creating custom applications, pyscrcpy provides a convenient interface for managing scrcpy commands.
3. Customization Options: Tailor scrcpy behavior to suit your project's requirements using the customizable options provided by pyscrcpy. Fine-tune parameters such as display size, bit rate, and more, all while maintaining the simplicity of Python scripting.

# Demo & Tutorial
```python
import cv2 as cv
from pyscrcpy import Client # import scrcpy client


def on_frame(client, frame):
    # View-only fork: control is disabled. Just use the frame.
    cv.imshow('Video', frame)
    cv.waitKey(1)


def demo1():
    client = Client(max_fps=1, max_size=900)
    client.on_frame(on_frame)
    client.start()

def demo2():
    client = Client(max_fps=20)
    client.start(threaded=True)  # create a new thread for scrcpy
    while 1:
        if client.last_frame is None:
            continue
        on_frame(client, client.last_frame)
```

## Reference & Appreciation
- Fork: [S1M0N38/scrcpy (don't support python3.11)](https://github.com/S1M0N38/scrcpy)
- Fork: [py-scrcpy-client](https://github.com/leng-yue/py-scrcpy-client)
- Core: [scrcpy](https://github.com/Genymobile/scrcpy)
- Idea: [py-android-viewer (many bugs)](https://github.com/razumeiko/py-android-viewer)
- CI: [index.py](https://github.com/index-py/index.py)
