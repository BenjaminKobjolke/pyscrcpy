import cv2 as cv
from pyscrcpy import Client


def on_frame(client, frame):
    # View-only fork: control is disabled. Just display the frame.
    cv.imshow('Video', frame)
    cv.waitKey(1)


if __name__ == '__main__':
    client = Client(max_fps=15, max_size=900)
    client.on_frame(on_frame)
    client.start()
