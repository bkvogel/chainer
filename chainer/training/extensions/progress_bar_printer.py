from __future__ import print_function
from __future__ import division
import datetime
import os
import sys
import time

from chainer.training.extensions import util


class ProgressBarPrinter(object):

    """Training utility to print a progress bar and recent training status.

    This is a callable object that prints an updated progress bar on each
    call. It must be supplied with the total training length (that is, the
    desired number of training iterations or epochs) when it is created. It
    also must be supplied with the current iteration and epoch values when it
    is called.

    Note: In an actualy training loop, users will typically find it more
    convinient to use either the Trainer-based or timer-based progress bar
    implementations since the releive the user of the responsiblity of
    determining when to print the progress bar. Those implementations
    actually use a timer or counter to then automatically call this
    implementation at the appropriate time.

    Args:
        training_length (tuple): Length of whole training. It consists of an
            integer and either ``'epoch'`` or ``'iteration'``.
        bar_length (int): Length of the progress bar in characters.
        out: Stream to print the bar. Standard output is used by default.

    """

    def __init__(self, training_length, bar_length=50,
                 out=sys.stdout):
        self._training_length = training_length
        self._status_template = None
        self._bar_length = bar_length
        self._out = out
        self._recent_timing = []

    def __call__(self, iteration, epoch):
        """Redraw progress bar.

        Args:
            iteration (int): Current iteration.
            epoch (float): Float-valued epoch.
        """
        training_length = self._training_length
        length, unit = training_length
        out = self._out
        recent_timing = self._recent_timing
        now = time.time()

        recent_timing.append((iteration, epoch, now))

        if os.name == 'nt':
            util.erase_console(0, 0)
        else:
            out.write('\033[J')

        if unit == 'iteration':
            rate = iteration / length
        else:
            rate = epoch / length

        bar_length = self._bar_length
        marks = '#' * int(rate * bar_length)
        out.write('     total [{}{}] {:6.2%}\n'.format(
            marks, '.' * (bar_length - len(marks)), rate))

        epoch_rate = epoch - int(epoch)
        marks = '#' * int(epoch_rate * bar_length)
        out.write('this epoch [{}{}] {:6.2%}\n'.format(
            marks, '.' * (bar_length - len(marks)), epoch_rate))

        status = '{} iter, {} epoch / {}\n'.format(iteration, int(epoch),
                                                   training_length[0])
        out.write(status)

        old_t, old_e, old_sec = recent_timing[0]
        span = now - old_sec
        if span != 0:
            speed_t = (iteration - old_t) / span
            speed_e = (epoch - old_e) / span
        else:
            speed_t = float('inf')
            speed_e = float('inf')

        if unit == 'iteration':
            estimated_time = (length - iteration) / speed_t
        else:
            estimated_time = (length - epoch) / speed_e
        out.write('{:10.5g} iters/sec. Estimated time to finish: {}.\n'.format(speed_t, datetime.timedelta(seconds=estimated_time)))  # NOQA

        # move the cursor to the head of the progress bar
        if os.name == 'nt':
            util.set_console_cursor_position(0, -4)
        else:
            out.write('\033[4A')
        out.flush()

        if len(recent_timing) > 100:
            del recent_timing[0]

    def finalize(self):
        # delete the progress bar
        out = self._out
        if os.name == 'nt':
            util.erase_console(0, 0)
        else:
            out.write('\033[J')
        out.flush()
