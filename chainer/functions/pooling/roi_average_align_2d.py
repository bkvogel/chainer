# Modified work:
# -----------------------------------------------------------------------------
# Copyright (c) 2018 Preferred Infrastructure, Inc.
# Copyright (c) 2018 Preferred Networks, Inc.
# -----------------------------------------------------------------------------

# Original work:
# -----------------------------------------------------------------------------
# Copyright (c) 2015 by Contributors
# \file roi_pooling.cu
# \brief roi pooling operator
# \author Ross Girshick, Kye-Hyeon Kim, Jian Guo
# \changed to roi_align by Elaine Bao
# \file roi_align.cu
# \roi align operator described in Mask RCNN
# -----------------------------------------------------------------------------

import collections

import numpy
import six

from chainer.backends import cuda
from chainer import function
from chainer.utils import type_check


def _pair(x):
    if isinstance(x, collections.Iterable):
        return x
    return x, x


def _get_bilinear_interp_params(y, x, height, width):
    if y < -1 or y > height or x < -1 or x > width:
        # out of range, so it is empty
        return (None,) * 8

    if y <= 0:
        y = 0
    if x <= 0:
        x = 0

    y_low = int(y)
    x_low = int(x)

    if y_low >= height - 1:
        y_high = y_low = height - 1
        y = float(y_low)
    else:
        y_high = y_low + 1

    if x_low >= width - 1:
        x_high = x_low = width - 1
        x = float(x_low)
    else:
        x_high = x_low + 1

    ly = y - y_low
    lx = x - x_low
    hy = 1. - ly
    hx = 1. - lx

    w1 = hy * hx
    w2 = hy * lx
    w3 = ly * hx
    w4 = ly * lx

    return y_low, x_low, y_high, x_high, w1, w2, w3, w4


_GET_BILINEAR_INTERP_KERNEL = '''
__device__
bool get_bilinear_interp_params(
    T x, T y, const int height, const int width,
    int &y_low, int &x_low, int &y_high, int &x_high,
    T &w1, T &w2, T &w3, T &w4) {
    // deal with cases that inverse elements are
    // out of feature map boundary
    if (y < -1. || y > height || x < -1. || x > width) {
        // empty
        return false;
    }

    if (y <= 0) {
        y = 0;
    }
    if (x <= 0) {
        x = 0;
    }

    y_low = (int)y;
    x_low = (int)x;

    if (y_low >= height - 1) {
        y_high = y_low = height - 1;
        y = (T)y_low;
    } else {
        y_high = y_low + 1;
    }

    if (x_low >= width - 1) {
        x_high = x_low = width - 1;
        x = (T)x_low;
    } else {
        x_high = x_low + 1;
    }

    T ly = y - y_low;
    T lx = x - x_low;
    T hy = 1. - ly;
    T hx = 1. - lx;

    w1 = hy * hx;
    w2 = hy * lx;
    w3 = ly * hx;
    w4 = ly * lx;

    return true;
}
'''


class ROIAverageAlign2D(function.Function):

    """ROI average align over a set of 2d planes."""

    def __init__(self, outsize, spatial_scale, sampling_ratio=None):
        outh, outw = _pair(outsize)
        if not (isinstance(outh, int) and outh > 0):
            raise TypeError(
                'outsize[0] must be positive integer: {}, {}'
                .format(type(outh), outh))
        if not (isinstance(outw, int) and outw > 0):
            raise TypeError(
                'outsize[1] must be positive integer: {}, {}'
                .format(type(outw), outw))
        if isinstance(spatial_scale, int):
            spatial_scale = float(spatial_scale)
        elif not (isinstance(spatial_scale, float) and spatial_scale > 0):
            raise TypeError(
                'spatial_scale must be a positive float number: {}, {}'
                .format(type(spatial_scale), spatial_scale)
            )
        sampling_ratio = _pair(sampling_ratio)
        if not all((isinstance(s, int) and s >= 1) or s is None
                   for s in sampling_ratio):
            raise TypeError(
                'sampling_ratio must be integer >= 1 or a pair of it: {}'
                .format(sampling_ratio)
            )

        self.outh, self.outw = outh, outw
        self.spatial_scale = spatial_scale
        self.sampling_ratio = sampling_ratio

    def check_type_forward(self, in_types):
        type_check.expect(in_types.size() == 3)

        x_type, roi_type, roi_index_type = in_types
        type_check.expect(
            x_type.dtype == numpy.float32,
            x_type.ndim == 4,
            roi_type.dtype == numpy.float32,
            roi_type.ndim == 2,
            roi_type.shape[1] == 4,
            roi_index_type.dtype == numpy.int32,
            roi_index_type.ndim == 1,
            roi_type.shape[0] == roi_index_type.shape[0],
        )

    def forward_cpu(self, inputs):
        self.retain_inputs((1, 2))
        self._bottom_data_shape = inputs[0].shape

        bottom_data, bottom_rois, bottom_roi_indices = inputs
        channels, height, width = bottom_data.shape[1:]
        n_rois = bottom_rois.shape[0]
        top_data = numpy.empty((n_rois, channels, self.outh,
                                self.outw), dtype=bottom_data.dtype)

        pooled_width, pooled_height = self.outw, self.outh
        spatial_scale = self.spatial_scale

        for i in six.moves.range(top_data.size):
            pw = i % pooled_width
            ph = int(i / pooled_width) % pooled_height
            c = int(i / pooled_width / pooled_height) % channels
            n = int(i / pooled_width / pooled_height / channels)

            roi_batch_ind = int(bottom_roi_indices[n])
            roi_start_h = bottom_rois[n, 0] * spatial_scale
            roi_start_w = bottom_rois[n, 1] * spatial_scale
            roi_end_h = bottom_rois[n, 2] * spatial_scale
            roi_end_w = bottom_rois[n, 3] * spatial_scale

            roi_height = max(roi_end_h - roi_start_h, 1.)
            roi_width = max(roi_end_w - roi_start_w, 1.)
            bin_size_h = roi_height / pooled_height
            bin_size_w = roi_width / pooled_width

            if self.sampling_ratio[0] is None:
                roi_bin_grid_h = numpy.ceil(roi_height / pooled_height)
            else:
                roi_bin_grid_h = self.sampling_ratio[0]
            if self.sampling_ratio[1] is None:
                roi_bin_grid_w = numpy.ceil(roi_width / pooled_width)
            else:
                roi_bin_grid_w = self.sampling_ratio[1]

            count = roi_bin_grid_h * roi_bin_grid_w

            output_val = 0.
            iy = 0
            while iy < roi_bin_grid_h:
                y = roi_start_h + ph * bin_size_h + \
                    (iy + .5) * bin_size_h / roi_bin_grid_h
                ix = 0
                while ix < roi_bin_grid_w:
                    x = roi_start_w + pw * bin_size_w + \
                        (ix + .5) * bin_size_w / roi_bin_grid_w

                    # bilinear interpolation {{

                    y_low, x_low, y_high, x_high, w1, w2, w3, w4 = \
                        _get_bilinear_interp_params(y, x, height, width)
                    if y_low is None:
                        continue

                    v1 = bottom_data[roi_batch_ind, c, y_low, x_low]
                    v2 = bottom_data[roi_batch_ind, c, y_low, x_high]
                    v3 = bottom_data[roi_batch_ind, c, y_high, x_low]
                    v4 = bottom_data[roi_batch_ind, c, y_high, x_high]

                    output_val += w1 * v1 + w2 * v2 + w3 * v3 + w4 * v4

                    # }}

                    ix += 1
                iy += 1

            output_val /= count
            top_data[n, c, ph, pw] = output_val

        return top_data,

    def forward_gpu(self, inputs):
        self.retain_inputs((1, 2))
        self._bottom_data_shape = inputs[0].shape

        bottom_data, bottom_rois, bottom_roi_indices = inputs
        channels, height, width = bottom_data.shape[1:]
        n_rois = bottom_rois.shape[0]
        top_data = cuda.cupy.empty((n_rois, channels, self.outh,
                                    self.outw), dtype=bottom_data.dtype)
        if self.sampling_ratio[0] is None:
            sampling_ratio_h = 0
        else:
            sampling_ratio_h = self.sampling_ratio[0]
        if self.sampling_ratio[1] is None:
            sampling_ratio_w = 0
        else:
            sampling_ratio_w = self.sampling_ratio[1]
        cuda.elementwise(
            '''
            raw T bottom_data, T spatial_scale, int32 channels,
            int32 height, int32 width, int32 pooled_height, int32 pooled_width,
            int32 sampling_ratio_h, int32 sampling_ratio_w,
            raw T bottom_rois, raw int32 bottom_roi_indices
            ''',
            'T top_data',
            '''
            int pw = i % pooled_width;
            int ph = (i / pooled_width) % pooled_height;
            int c = (i / pooled_width / pooled_height) % channels;
            int n = i / pooled_width / pooled_height / channels;

            int roi_batch_ind = bottom_roi_indices[n];

            T roi_start_h = bottom_rois[n * 4 + 0] * spatial_scale;
            T roi_start_w = bottom_rois[n * 4 + 1] * spatial_scale;
            T roi_end_h = bottom_rois[n * 4 + 2] * spatial_scale;
            T roi_end_w = bottom_rois[n * 4 + 3] * spatial_scale;

            // Force malformed ROIs to be 1x1
            T roi_height = max(roi_end_h - roi_start_h, (T)1.);
            T roi_width = max(roi_end_w - roi_start_w, (T)1.);
            T bin_size_h = static_cast<T>(roi_height)
                            / static_cast<T>(pooled_height);
            T bin_size_w = static_cast<T>(roi_width)
                            / static_cast<T>(pooled_width);

            int bottom_data_offset =
                (roi_batch_ind * channels + c) * height * width;

            // We use roi_bin_grid to sample the grid and mimic integral
            int roi_bin_grid_h = (sampling_ratio_h > 0)
                ? sampling_ratio_h
                : ceil(roi_height / pooled_height);  // e.g. = 2
            int roi_bin_grid_w = (sampling_ratio_w > 0)
                ? sampling_ratio_w
                : ceil(roi_width / pooled_width);

            // We do average (integral) pooling inside a bin
            T count = roi_bin_grid_h * roi_bin_grid_w;  // e.g. = 4

            T output_val = 0.;
            for (int iy = 0; iy < roi_bin_grid_h; iy++)  // e.g. iy = 0, 1
            {
                T y = roi_start_h + ph * bin_size_h +
                    static_cast<T>(iy + .5f) * bin_size_h /
                        static_cast<T>(roi_bin_grid_h);  // e.g. 0.5, 1.5
                for (int ix = 0; ix < roi_bin_grid_w; ix++) {
                    T x = roi_start_w + pw * bin_size_w +
                        static_cast<T>(ix + .5f) * bin_size_w /
                            static_cast<T>(roi_bin_grid_w);

                    // bilinear_interpolation {{
                    int y_low, x_low, y_high, x_high;
                    T w1, w2, w3, w4;
                    bool ret = get_bilinear_interp_params(
                        x, y, height, width,
                        y_low, x_low, y_high, x_high,
                        w1, w2, w3, w4
                    );
                    if (!ret) {
                        continue;
                    }

                    T v1 = bottom_data[bottom_data_offset +
                                       y_low * width + x_low];
                    T v2 = bottom_data[bottom_data_offset +
                                       y_low * width + x_high];
                    T v3 = bottom_data[bottom_data_offset +
                                       y_high * width + x_low];
                    T v4 = bottom_data[bottom_data_offset +
                                       y_high * width + x_high];

                    output_val += (w1 * v1 + w2 * v2 + w3 * v3 + w4 * v4);

                    // }}
                }
            }
            output_val /= count;

            top_data = output_val;
            ''',
            'roi_average_align_2d_fwd',
            preamble=_GET_BILINEAR_INTERP_KERNEL,
        )(bottom_data, self.spatial_scale, channels, height, width,
          self.outh, self.outw, sampling_ratio_h, sampling_ratio_w,
          bottom_rois, bottom_roi_indices, top_data)

        return top_data,

    def backward_cpu(self, inputs, gy):
        bottom_rois, bottom_roi_indices = inputs[1:]
        channels, height, width = self._bottom_data_shape[1:]
        bottom_diff = numpy.zeros(self._bottom_data_shape, gy[0].dtype)

        spatial_scale = self.spatial_scale
        pooled_height = self.outh
        pooled_width = self.outw
        top_diff = gy[0]

        for i in six.moves.range(top_diff.size):
            pw = i % pooled_width
            ph = int(i / pooled_width) % pooled_height
            c = int(i / pooled_width / pooled_height) % channels
            n = int(i / pooled_width / pooled_height / channels)

            roi_batch_ind = int(bottom_roi_indices[n])
            roi_start_h = bottom_rois[n, 0] * spatial_scale
            roi_start_w = bottom_rois[n, 1] * spatial_scale
            roi_end_h = bottom_rois[n, 2] * spatial_scale
            roi_end_w = bottom_rois[n, 3] * spatial_scale

            roi_height = max(roi_end_h - roi_start_h, 1.)
            roi_width = max(roi_end_w - roi_start_w, 1.)
            bin_size_h = roi_height / pooled_height
            bin_size_w = roi_width / pooled_width

            top_diff_this_bin = top_diff[n, c, ph, pw]

            if self.sampling_ratio[0] is None:
                roi_bin_grid_h = numpy.ceil(roi_height / pooled_height)
            else:
                roi_bin_grid_h = self.sampling_ratio[0]
            if self.sampling_ratio[1] is None:
                roi_bin_grid_w = numpy.ceil(roi_width / pooled_width)
            else:
                roi_bin_grid_w = self.sampling_ratio[1]

            count = roi_bin_grid_h * roi_bin_grid_w

            iy = 0
            while iy < roi_bin_grid_h:
                y = roi_start_h + ph * bin_size_h + \
                    (iy + .5) * bin_size_h / roi_bin_grid_h
                ix = 0
                while ix < roi_bin_grid_w:
                    x = roi_start_w + pw * bin_size_w + \
                        (ix + .5) * bin_size_w / roi_bin_grid_w

                    # bilinear_interpolation_gradient {{

                    y_low, x_low, y_high, x_high, w1, w2, w3, w4 = \
                        _get_bilinear_interp_params(y, x, height, width)
                    if y_low is None:
                        continue

                    g1 = top_diff_this_bin * w1 / count
                    g2 = top_diff_this_bin * w2 / count
                    g3 = top_diff_this_bin * w3 / count
                    g4 = top_diff_this_bin * w4 / count

                    if (x_low >= 0 and x_high >= 0 and
                            y_low >= 0 and y_high >= 0):
                        bottom_diff[roi_batch_ind, c, y_low, x_low] += g1
                        bottom_diff[roi_batch_ind, c, y_low, x_high] += g2
                        bottom_diff[roi_batch_ind, c, y_high, x_low] += g3
                        bottom_diff[roi_batch_ind, c, y_high, x_high] += g4

                    # }}

                    ix += 1
                iy += 1

        return bottom_diff, None, None

    def backward_gpu(self, inputs, gy):
        bottom_rois, bottom_roi_indices = inputs[1:]
        channels, height, width = self._bottom_data_shape[1:]
        bottom_diff = cuda.cupy.zeros(self._bottom_data_shape, gy[0].dtype)

        if self.sampling_ratio[0] is None:
            sampling_ratio_h = 0
        else:
            sampling_ratio_h = self.sampling_ratio[0]
        if self.sampling_ratio[1] is None:
            sampling_ratio_w = 0
        else:
            sampling_ratio_w = self.sampling_ratio[1]
        cuda.elementwise(
            '''
            raw T top_diff,
            int32 num_rois, T spatial_scale,
            int32 channels, int32 height, int32 width,
            int32 pooled_height, int32 pooled_width,
            int32 sampling_ratio_h, int32 sampling_ratio_w,
            raw T bottom_rois, raw int32 bottom_roi_indices
            ''',
            'raw T bottom_diff',
            '''
            // (n, c, h, w) coords in bottom data
            int pw = i % pooled_width;
            int ph = (i / pooled_width) % pooled_height;
            int c = (i / pooled_width / pooled_height) % channels;
            int n = i / pooled_width / pooled_height / channels;

            // Do not using rounding; this implementation detail is critical
            int roi_batch_ind = bottom_roi_indices[n];
            T roi_start_h = bottom_rois[n * 4 + 0] * spatial_scale;
            T roi_start_w = bottom_rois[n * 4 + 1] * spatial_scale;
            T roi_end_h = bottom_rois[n * 4 + 2] * spatial_scale;
            T roi_end_w = bottom_rois[n * 4 + 3] * spatial_scale;

            // Force malformed ROIs to be 1x1
            T roi_width = max(roi_end_w - roi_start_w, (T)1.);
            T roi_height = max(roi_end_h - roi_start_h, (T)1.);
            T bin_size_h = static_cast<T>(roi_height) /
                static_cast<T>(pooled_height);
            T bin_size_w = static_cast<T>(roi_width) /
                static_cast<T>(pooled_width);

            int bottom_diff_offset =
                (roi_batch_ind * channels + c) * height * width;

            int top_offset = (n * channels + c) * pooled_height * pooled_width;
            T top_diff_this_bin =
                top_diff[top_offset + ph * pooled_width + pw];

            // We use roi_bin_grid to sample the grid and mimic integral
            int roi_bin_grid_h = (sampling_ratio_h > 0)
                ? sampling_ratio_h
                : ceil(roi_height / pooled_height); // e.g. = 2
            int roi_bin_grid_w = (sampling_ratio_w > 0)
                ? sampling_ratio_w
                : ceil(roi_width / pooled_width);

            // We do average (integral) pooling inside a bin
            T count = roi_bin_grid_h * roi_bin_grid_w;  // e.g. = 4

            for (int iy = 0; iy < roi_bin_grid_h; iy++) {
                T y = roi_start_h + ph * bin_size_h +
                    static_cast<T>(iy + .5f) * bin_size_h /
                        static_cast<T>(roi_bin_grid_h);  // e.g. 0.5, 1.5
                for (int ix = 0; ix < roi_bin_grid_w; ix++) {
                    T x = roi_start_w + pw * bin_size_w +
                        static_cast<T>(ix + .5f) * bin_size_w /
                            static_cast<T>(roi_bin_grid_w);

                    // bilinear_interpolation_gradient {{
                    int y_low, x_low, y_high, x_high;
                    T w1, w2, w3, w4;
                    bool ret = get_bilinear_interp_params(
                        x, y, height, width,
                        y_low, x_low, y_high, x_high,
                        w1, w2, w3, w4
                    );
                    if (!ret) {
                        continue;
                    }

                    T g1 = top_diff_this_bin * w1 / count;
                    T g2 = top_diff_this_bin * w2 / count;
                    T g3 = top_diff_this_bin * w3 / count;
                    T g4 = top_diff_this_bin * w4 / count;

                    if (x_low >= 0 && x_high >= 0 &&
                            y_low >= 0 && y_high >= 0) {
                        atomicAdd(&bottom_diff[bottom_diff_offset +
                                               y_low * width + x_low], g1);
                        atomicAdd(&bottom_diff[bottom_diff_offset +
                                               y_low * width + x_high], g2);
                        atomicAdd(&bottom_diff[bottom_diff_offset +
                                               y_high * width + x_low], g3);
                        atomicAdd(&bottom_diff[bottom_diff_offset +
                                               y_high * width + x_high], g4);
                    }

                    // }}
                }
            }
            ''',
            'roi_average_align_2d_bwd',
            preamble=_GET_BILINEAR_INTERP_KERNEL,
        )(gy[0], bottom_rois.shape[0],
          self.spatial_scale, channels, height, width, self.outh, self.outw,
          sampling_ratio_h, sampling_ratio_w, bottom_rois, bottom_roi_indices,
          bottom_diff, size=gy[0].size)

        return bottom_diff, None, None


def roi_average_align_2d(
        x, rois, roi_indices, outsize, spatial_scale, sampling_ratio=None
):
    """Spatial Region of Interest (ROI) average align function.

    This function acts similarly to :class:`~functions.ROIPooling2D`, but
    it computes average of input spatial patch with bilinear interpolation
    for each channel with the region of interest.

    Args:
        x (~chainer.Variable): Input variable. The shape is expected to be
            4 dimentional: ``(n: batch, c: channel, h, height, w: width)``.
        rois (~chainer.Variable): Input roi variable. The shape is expected to
            be ``(n: data size, 4)``, and each datum is set as below:
            ``(y_min, x_min, y_max, x_max)``.
        roi_indices (~chainer.Variable): Input roi variable. The shape is
            expected to be ``(n: data size, )``.
        outsize ((int, int) or int): Expected output size after pooled
            (height, width). ``outsize=o`` and ``outsize=(o, o)``
            are equivalent.
        spatial_scale (float): Scale of the roi is resized.
        sampling_ratio ((int, int) or int): Sampling step for the alignment.
            It must be an integer over :math:`1` or :obj:`None`, and the value
            is automatically decided when :obj:`None` is passed.  Use of
            different ratio in height and width axis is also supported by
            passing tuple of int as ``(sampling_ratio_h, sampling_ratio_w)``.
            ``sampling_ratio=s`` and ``sampling_ratio=(s, s)`` are equivalent.

    Returns:
        ~chainer.Variable: Output variable.

    See the original paper proposing ROIAlign:
    `Mask R-CNN <https://arxiv.org/abs/1703.06870>`_.

    """
    return ROIAverageAlign2D(outsize, spatial_scale, sampling_ratio)(
        x, rois, roi_indices)
