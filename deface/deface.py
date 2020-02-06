#!/usr/bin/env python3

import argparse
import os
from typing import Tuple

import tqdm
import skimage.draw
import numpy as np
import imageio
import imageio.plugins.ffmpeg
import cv2

from deface.centerface import CenterFace


# TODO: Optionally preserve audio track?


def scale_bb(x1, y1, x2, y2, mask_scale=1.0):
    s = mask_scale - 1.0
    h, w = y2 - y1, x2 - x1
    y1 -= h * s
    y2 += h * s
    x1 -= w * s
    x2 += w * s
    return np.round([x1, y1, x2, y2]).astype(int)


def draw_det(
        frame, score, det_idx, x1, y1, x2, y2,
        replacewith: str = 'blur',
        ellipse: bool = True,
        enumerate_dets: bool = False,
        ovcolor: Tuple[int] = (0, 0, 0)
):
    if replacewith == 'solid':
        cv2.rectangle(frame, (x1, y1), (x2, y2), ovcolor, -1)
    elif replacewith == 'blur':
        bf = 2  # blur factor (number of pixels in each dimension that the face will be reduced to)
        blurred_box =  cv2.blur(
            frame[y1:y2, x1:x2],
            (abs(x2 - x1) // bf, abs(y2 - y1) // bf)
        )
        if ellipse:
            roibox = frame[y1:y2, x1:x2]
            # Get y and x coordinate lists of the "bounding ellipse"
            ey, ex = skimage.draw.ellipse((y2 - y1) // 2, (x2 - x1) // 2, (y2 - y1) // 2, (x2 - x1) // 2)
            roibox[ey, ex] = blurred_box[ey, ex]
            frame[y1:y2, x1:x2] = roibox
        else:
            frame[y1:y2, x1:x2] = blurred_box
    elif replacewith == 'none':
        pass
    if enumerate_dets:
        cv2.putText(
            frame, f'{det_idx + 1}: {score:.2f}', (x1 +0, y1 - 20),
            cv2.FONT_HERSHEY_SIMPLEX, 1.0, (128, 255, 128)
        )


def anonymize_frame(
        dets, frame, mask_scale,
        replacewith, ellipse, enumerate_dets, ovcolor
):
    for i, det in enumerate(dets):
        boxes, score = det[:4], det[4]
        x1, y1, x2, y2 = boxes.astype(int)
        x1, y1, x2, y2 = scale_bb(x1, y1, x2, y2, mask_scale)
        # Clip bb coordinates to valid frame region
        y1, y2 = max(0, y1), min(frame.shape[0] - 1, y2)
        x1, x2 = max(0, x1), min(frame.shape[1] - 1, x2)

        draw_det(
            frame, score, i, x1, y1, x2, y2,
            replacewith=replacewith,
            ellipse=ellipse,
            enumerate_dets=enumerate_dets,
            ovcolor=ovcolor
        )


def cam_read_iter(reader):
    while True:
        yield reader.get_next_data()

def video_detect(
        ipath,
        opath,
        centerface,
        threshold,
        show,
        cam,
        nested,
        replacewith,
        mask_scale,
        ellipse,
        enumerate_dets,
        ovcolor
):
    reader: imageio.plugins.ffmpeg.FfmpegFormat.Reader = imageio.get_reader(ipath)
    meta = reader.get_meta_data()
    frame_width, frame_height = meta['size']
    if cam:
        nframes = None
        read_iter = cam_read_iter(reader)
    else:
        read_iter = reader.iter_data()
        nframes = reader.count_frames()
    if nested:
        bar = tqdm.tqdm(dynamic_ncols=True, total=nframes, position=1, leave=False)
    else:
        bar = tqdm.tqdm(dynamic_ncols=True, total=nframes)

    if opath is not None:
        writer: imageio.plugins.ffmpeg.FfmpegFormat.Writer = imageio.get_writer(
            opath, format='FFMPEG', mode='I', fps=meta['fps'],
            codec='libx264'
            # codec='hevc_nvenc'
            # codec='h264_nvenc'
        )

    for frame in read_iter:
        # Perform network inference, get bb dets but discard landmark predictions
        dets, _ = centerface(frame, threshold=threshold)

        anonymize_frame(
            dets, frame, mask_scale=mask_scale,
            replacewith=replacewith, ellipse=ellipse, enumerate_dets=enumerate_dets, ovcolor=ovcolor
        )

        if opath is not None:
            writer.append_data(frame)

        if show:
            cv2.imshow('out', frame[:, :, ::-1])  # RGB -> RGB
            # Press Q on keyboard to stop recording
            if cv2.waitKey(1) & 0xFF == ord('q'):
                break
        bar.update()
    reader.close()
    if opath is not None:
        writer.close()
    bar.close()


def main():
    parser = argparse.ArgumentParser(description='Video anonymization by face detection')
    parser.add_argument('-i', default='<video0>', help='Input file name or camera index')
    parser.add_argument('-o', default=None, help='Output file name (defaults to input path + postfix "_anonymized")')
    parser.add_argument('-r', default='blur', choices=['solid', 'blur', 'none'], help='Anonymization filter mode for face regions')
    parser.add_argument('-d', default=None, help='Downsample images for network inference to this size')
    # parser.add_argument('-c', default='red', help='Color hue of the overlays (boxes, texts)')
    parser.add_argument('-q', default=False, action='store_true', help='Disable GUI')
    parser.add_argument('-n', default=None, help='Path to CenterFace ONNX model file')
    parser.add_argument('-e', default=False, action='store_true', help='Enable detection enumeration')
    parser.add_argument('-t', default=0.2, type=float, help='Detection threshold')
    parser.add_argument('-m', default=False, action='store_true', help='Use boxes instead of ellipse masks')
    parser.add_argument('-s', default=1.3, type=float, help='Scale factor for face masks (use high values to be on the safe side)')
    parser.add_argument('-b', default='auto', choices=['auto', 'onnxrt', 'opencv'], help='Backend for ONNX model execution')
    parser.add_argument('--nested', default=False, action='store_true', help='Run in nested progress mode (for batch processes)')

    args = parser.parse_args()

    ipath = args.i
    opath = args.o
    replacewith = args.r
    show = not args.q
    onnxpath = args.n
    enumerate_dets = args.e
    threshold = args.t
    ellipse = not args.m
    mask_scale = args.s
    backend = args.b
    # ovcolor = colors.get(args.c, (0, 0, 0))
    in_shape = args.d
    nested = args.nested
    if in_shape is not None:
        w, h = in_shape.split('x')
        in_shape = int(w), int(h)


    # cam = isinstance(ipath, int)
    cam = ipath.startswith('<video')

    if opath is None and not cam:
        root, ext = os.path.splitext(ipath)
        opath = f'{root}_anonymized{ext}'

    ovcolor = (0, 0, 0)

    centerface = CenterFace(onnxpath, in_shape=in_shape, backend=backend)

    video_detect(
        ipath=ipath,
        opath=opath,
        centerface=centerface,
        threshold=threshold,
        show=show,
        cam=cam,
        nested=nested,
        replacewith=replacewith,
        mask_scale=mask_scale,
        ellipse=ellipse,
        enumerate_dets=enumerate_dets,
        ovcolor=ovcolor
    )


if __name__ == '__main__':
    main()
