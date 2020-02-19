#!/usr/bin/env python3

import argparse
import glob
import mimetypes
import os
from typing import Tuple

import tqdm
import skimage.draw
import numpy as np
import imageio
import imageio.plugins.ffmpeg
import cv2

from deface import __version__
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
        replacewith, ellipse, enumerate_dets
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
        )


def cam_read_iter(reader):
    while True:
        yield reader.get_next_data()

def video_detect(
        ipath: str,
        opath: str,
        centerface: str,
        threshold: float,
        show: bool,
        cam: bool,
        nested: bool,
        replacewith: str,
        mask_scale: float,
        ellipse: bool,
        enumerate_dets: bool,
):
    try:
        reader: imageio.plugins.ffmpeg.FfmpegFormat.Reader = imageio.get_reader(ipath)
        meta = reader.get_meta_data()
        _ = meta['size']
    except:
        if cam:
            print(f'Could not find video device {ipath}. Please set a valid input.')
        else:
            print(f'Could not open file {ipath} as a video file with imageio. Skipping file...')
        return

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
            # codec='libx265'
        )

    for frame in read_iter:
        # Perform network inference, get bb dets but discard landmark predictions
        dets, _ = centerface(frame, threshold=threshold)

        anonymize_frame(
            dets, frame, mask_scale=mask_scale,
            replacewith=replacewith, ellipse=ellipse, enumerate_dets=enumerate_dets
        )

        if opath is not None:
            writer.append_data(frame)

        if show:
            cv2.imshow('Anonymized', frame[:, :, ::-1])  # RGB -> RGB
            # Press Q on keyboard to stop
            if cv2.waitKey(1) & 0xFF == ord('q'):
                break
        bar.update()
    reader.close()
    if opath is not None:
        writer.close()
    bar.close()


def image_detect(
        ipath: str,
        opath: str,
        centerface: str,
        threshold: float,
        replacewith: str,
        mask_scale: float,
        ellipse: bool,
        enumerate_dets: bool,
):
    frame = imageio.imread(ipath)

    # Perform network inference, get bb dets but discard landmark predictions
    dets, _ = centerface(frame, threshold=threshold)

    anonymize_frame(
        dets, frame, mask_scale=mask_scale,
        replacewith=replacewith, ellipse=ellipse, enumerate_dets=enumerate_dets
    )

    imageio.imsave(opath, frame)
    # print(f'Output saved to {opath}')


def main():
    parser = argparse.ArgumentParser(description='Video anonymization by face detection', add_help=False)
    parser.add_argument(
        'input', default='<video0>', nargs='?',
        help='Video/image/directory path or camera device name (default: <video0>, which is the first camera device).')
    parser.add_argument(
        '--output', '-o', default=None, metavar='O',
        help='Output file name (defaults to input path + postfix "_anonymized").')
    parser.add_argument(
        '--thresh', '-t', default=0.2, type=float, metavar='T',
        help='Detection threshold (tune this to trade off between false positive and false negative rate).')
    parser.add_argument(
        '--scale', '-s', default=None, metavar='WxH',
        help='Downscale images for network inference to this size (format: WxH, example: --scale=640x360).')
    parser.add_argument(
        '--disable-gui', '-q', default=False, action='store_true',
        help='Disable preview GUI. Only applies if the input is a single video file (else it\'s already off by default).')
    parser.add_argument(
        '--enable-enum', '-e', default=False, action='store_true',
        help='Draw detection numbers and scores into the output.')
    parser.add_argument(
        '--enable-boxes', default=False, action='store_true',
        help='Use boxes instead of ellipse masks.')
    parser.add_argument(
        '--mask-scale', default=1.3, type=float, metavar='M',
        help='Scale factor for face masks, to make sure that masks cover the complete face (default: 1.3).)')
    parser.add_argument(
        '--replacewith', default='blur', choices=['solid', 'blur', 'none'],
        help='Anonymization filter mode for face regions.')
    parser.add_argument(
        '--backend', default='auto', choices=['auto', 'onnxrt', 'opencv'],
        help='Backend for ONNX model execution.')
    parser.add_argument(
        '--ext', default='*',
        help='Filter by file extension (no filter (*) by default). Only applies if the input argument is a directory.')
    parser.add_argument(
        '--version', action='version', version=__version__,
        help='Print version number and exit.')
    parser.add_argument('--help', '-h', action='help', help='Show this help message and exit.')

    args = parser.parse_args()

    ipath = args.input
    opath = args.output
    replacewith = args.replacewith
    show = not args.disable_gui
    enumerate_dets = args.enable_enum
    threshold = args.thresh
    ellipse = not args.enable_boxes
    mask_scale = args.mask_scale
    backend = args.backend
    in_shape = args.scale
    extfilter = args.ext
    if in_shape is not None:
        w, h = in_shape.split('x')
        in_shape = int(w), int(h)

    cam = ipath.startswith('<video')

    if opath is None and not cam:
        root, ext = os.path.splitext(ipath)
        opath = f'{root}_anonymized{ext}'

    # TODO: scalar downscaling setting (-> in_shape), preserving aspect ratio
    centerface = CenterFace(in_shape=in_shape, backend=backend)

    if os.path.isfile(ipath) or cam:
        mime = mimetypes.guess_type(ipath)[0]
        if cam or mime.startswith('video'):
            video_detect(
                ipath=ipath,
                opath=opath,
                centerface=centerface,
                threshold=threshold,
                show=show,
                cam=cam,
                replacewith=replacewith,
                mask_scale=mask_scale,
                ellipse=ellipse,
                enumerate_dets=enumerate_dets,
                nested=False,
            )
        elif mime.startswith('image'):
            image_detect(
                ipath=ipath,
                opath=opath,
                centerface=centerface,
                threshold=threshold,
                replacewith=replacewith,
                mask_scale=mask_scale,
                ellipse=ellipse,
                enumerate_dets=enumerate_dets,
            )
        else:
            raise ValueError(f'File {ipath} has an unknown MIME type {mime}.')
    elif os.path.isdir(ipath):
        paths = glob.glob(f'{ipath}/**/*.{extfilter}', recursive=True)
        pbar = tqdm.tqdm(paths, position=0)
        for p in pbar:
            root, ext = os.path.splitext(p)
            opath = f'{root}_anonymized{ext}'
            mime = mimetypes.guess_type(p)[0]
            if mime.startswith('video'):
                pbar.set_description(f'Current video: {p}')
                video_detect(
                    ipath=p,
                    opath=opath,
                    centerface=centerface,
                    threshold=threshold,
                    cam=cam,
                    replacewith=replacewith,
                    mask_scale=mask_scale,
                    ellipse=ellipse,
                    enumerate_dets=enumerate_dets,
                    show=False,
                    nested=True,
                )
            elif mime.startswith('image'):
                image_detect(
                    ipath=p,
                    opath=opath,
                    centerface=centerface,
                    threshold=threshold,
                    replacewith=replacewith,
                    mask_scale=mask_scale,
                    ellipse=ellipse,
                    enumerate_dets=enumerate_dets,
                )
            else:
                print(f'File {p} has an unknown MIME type {mime}. Skipping...')
    else:
        raise FileNotFoundError(f'{ipath} not found.')


if __name__ == '__main__':
    main()
