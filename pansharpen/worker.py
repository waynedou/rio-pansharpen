#!/usr/bin/env python
from __future__ import division

import click
import numpy as np
import rasterio
import riomucho
from pansharpen.methods import Brovey
from rasterio.transform import guard_transform

from . utils import (
    pad_window, upsample, simple_mask, calc_windows, check_crs)


def pansharpen_worker(open_files, pan_window, _, g_args):
    """
    Reading input files and performing pansharpening on each window
    """
    pan = open_files[0].read(1, window=pan_window).astype(np.float32)
    pan_dtype = open_files[0].meta['dtype']

    # Get the rgb window that covers the pan window
    padding = 2
    pan_bounds = open_files[0].window_bounds(pan_window)
    rgb_base_window = open_files[1].window(*pan_bounds)
    rgb_window = pad_window(rgb_base_window, padding)

    # Determine affines for those windows
    pan_affine = open_files[0].window_transform(pan_window)
    rgb_affine = open_files[1].window_transform(rgb_window)

    rgb = riomucho.utils.array_stack(
        [src.read(window=rgb_window, boundless=True).astype(np.float32)
         for src in open_files[1:]])

    # Create a mask of pixels where any channel is 0 (nodata):
    color_mask = np.minimum(
        rgb[0],
        np.minimum(rgb[1], rgb[2])) * 2 ** 16

    # Apply the mask:
    rgb = np.array([
        np.minimum(band, color_mask) for band in rgb])

    if g_args["verb"]:
        click.echo('pan shape: %s, rgb shape %s' % (pan.shape, rgb[0].shape))

    rgb = upsample(rgb, pan.shape, rgb_affine, g_args["r_crs"],
                   pan_affine, g_args['dst_crs'])

    # Main Pansharpening Processing
    pan_sharpened, _ = Brovey(rgb, pan, g_args["weight"], pan_dtype)

    # convert to 8bit value range in place
    scale = float(np.iinfo(np.uint16).max) / float(np.iinfo(np.uint8).max)

    pan_sharpened = np.concatenate(
        [(pan_sharpened / scale).astype(np.uint8),
         simple_mask(
             pan_sharpened.astype(np.uint8),
             (0, 0, 0)).reshape(
                 1, pan_sharpened.shape[1], pan_sharpened.shape[2])])

    return pan_sharpened


def pansharpen(src_paths, dst_path, weight, verbosity, jobs, customwindow):
    """
    Main entry point called by the command line utility

    Pansharpening a landsat scene --
    Opening files, reading input meta data and writing the result
    of pansharpening into each window respentively.
    """
    with rasterio.open(src_paths[0]) as pan_src:
        windows = calc_windows(pan_src, customwindow)
        profile = pan_src.profile

        if profile['count'] > 1:
            raise RuntimeError(
                "Pan band must be 1 band - is {}".format(profile['count']))

        profile.update(
            transform=guard_transform(pan_src.transform),
            compress='DEFLATE',
            blockxsize=512,
            blockysize=512,
            dtype=np.uint8,
            tiled=True,
            count=4,
            photometric='rgb')

    with rasterio.open(src_paths[1]) as r_src:
        r_meta = r_src.meta

    if profile['width'] <= r_meta['width'] or \
       profile['height'] <= r_meta['height']:
        raise RuntimeError(
            "Pan band must be larger than RGB bands")

    check_crs([r_meta, profile])

    g_args = {
        "verb": verbosity,
        "weight": weight,
        "dst_aff": guard_transform(profile['transform']),
        "dst_crs": profile['crs'],
        "r_aff": guard_transform(r_meta['transform']),
        "r_crs": r_meta['crs']}

    with riomucho.RioMucho(src_paths, dst_path, pansharpen_worker,
                           windows=windows, global_args=g_args,
                           options=profile, mode='manual_read') as rm:
        rm.run(jobs)
