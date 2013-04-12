# coding=utf-8
"""
Download mode implementation.

"""
from __future__ import division
import os
import re
import sys
import mimetypes
import threading
from time import time
from datetime import timedelta

from .output import RawStream
from .models import HTTPResponse
from .utils import humanize_bytes
from .compat import urlsplit


PARTIAL_CONTENT = 206


CLEAR_LINE = '\r\033[K'
PROGRESS = ('{percentage: 6.2f}% ({downloaded})'
            ' of {total} ({speed}/s) ETA {eta}')
PROGRESS_NO_CONTENT_LENGTH = '{downloaded} ({speed}/s) ETA {eta}'
SUMMARY = 'Done. {downloaded} of {total} in {time:0.5f}s ({speed}/s)\n'
SPINNER = '|/-\\'


class ContentRangeError(ValueError):
    pass


def parse_content_range(content_range, resumed_from):
    """
    Parse and validate Content-Range header.

    <http://www.w3.org/Protocols/rfc2616/rfc2616-sec14.html>

    :param content_range: the value of a Content-Range response header
                          eg. "bytes 21010-47021/47022"
    :param resumed_from: first byte pos. from the Range request header
    :return: total size of the response body when fully downloaded.

    """
    pattern = (
        '^bytes (?P<first_byte_pos>\d+)-(?P<last_byte_pos>\d+)'
        '/(\*|(?P<instance_length>\d+))$'
    )
    match = re.match(pattern, content_range)

    if not match:
        raise ContentRangeError(
            'Invalid Content-Range format %r' % content_range)

    content_range_dict = match.groupdict()
    first_byte_pos = int(content_range_dict['first_byte_pos'])
    last_byte_pos = int(content_range_dict['last_byte_pos'])
    instance_length = (
        int(content_range_dict['instance_length'])
        if content_range_dict['instance_length']
        else None
    )

    # "A byte-content-range-spec with a byte-range-resp-spec whose
    # last- byte-pos value is less than its first-byte-pos value,
    # or whose instance-length value is less than or equal to its
    # last-byte-pos value, is invalid. The recipient of an invalid
    # byte-content-range- spec MUST ignore it and any content
    # transferred along with it."
    if (first_byte_pos >= last_byte_pos
            or (instance_length is not None
                and instance_length <= last_byte_pos)):
        raise ContentRangeError(
            'Invalid Content-Range returned: %r' % content_range)

    if (first_byte_pos != resumed_from
        or (instance_length is not None
            and last_byte_pos + 1 != instance_length)):
        # Not what we asked for.
        raise ContentRangeError(
            'Unexpected Content-Range returned (%r)'
            ' for the requested Range ("bytes=%d-")'
            % (content_range, resumed_from)
        )

    return last_byte_pos + 1


def filename_from_content_disposition(content_disposition):
    """
    Extract and validate filename from a Content-Disposition header.

    :param content_disposition: Content-Disposition value
    :return: the filename if present and valid, otherwise `None`

    """
    # attachment; filename=jkbr-httpie-0.4.1-20-g40bd8f6.tar.gz
    match = re.search('filename=(\S+)', content_disposition)
    if match and match.group(1):
        fn = match.group(1).lstrip('.')
        if re.match('^[a-zA-Z0-9._-]+$', fn):
            return fn


def filename_from_url(url, content_type):
    fn = urlsplit(url).path.rstrip('/')
    fn = os.path.basename(fn) if fn else 'index'
    if '.' not in fn and content_type:
        content_type = content_type.split(';')[0]
        if content_type == 'text/plain':
            # mimetypes returns '.ksh'
            ext = '.txt'
        else:
            ext = mimetypes.guess_extension(content_type)

        if ext:
            fn += ext

    return fn


def get_unique_filename(fn, exists=os.path.exists):
    attempt = 0
    while True:
        suffix = '-' + str(attempt) if attempt > 0 else ''
        if not exists(fn + suffix):
            return fn + suffix
        attempt += 1


class Download(object):

    def __init__(self, output_file=None,
                 resume=False, progress_file=sys.stderr):
        """
        :param resume: Should the download resume if partial download
                       already exists.
        :type resume: bool

        :param output_file: The file to store response body in. If not
                            provided, it will be guessed from the response.
        :type output_file: file

        :param progress_file: Where to report download progress.
        :type progress_file: file

        """
        self._output_file = output_file
        self._resume = resume
        self._resumed_from = 0

        self._progress = Progress()
        self._progress_reporter = ProgressReporter(
            progress=self._progress,
            output=progress_file
        )

    def pre_request(self, request_headers):
        """Called just before the HTTP request is sent.

        Might alter `request_headers`.

        :type request_headers: dict

        """
        # Disable content encoding so that we can resume, etc.
        request_headers['Accept-Encoding'] = None
        if self._resume:
            bytes_have = os.path.getsize(self._output_file.name)
            if bytes_have:
                # Set ``Range`` header to resume the download
                # TODO: Use "If-Range: mtime" to make sure it's fresh?
                request_headers['Range'] = 'bytes=%d-' % bytes_have
                self._resumed_from = bytes_have

    def start(self, response):
        """
        Initiate and return a stream for `response` body  with progress
        callback attached. Can be called only once.

        :param response: Initiated response object with headers already fetched
        :type response: requests.models.Response

        :return: RawStream, output_file

        """
        assert not self._progress.time_started

        try:
            total_size = int(response.headers['Content-Length'])
        except (KeyError, ValueError):
            total_size = None

        if self._output_file:
            if self._resume and response.status_code == PARTIAL_CONTENT:
                content_range = response.headers.get('Content-Range')
                if content_range:
                    total_size = parse_content_range(
                        content_range, self._resumed_from)

            else:
                self._resumed_from = 0
                try:
                    self._output_file.seek(0)
                    self._output_file.truncate()
                except IOError:
                    pass  # stdout
        else:
            # TODO: Should the filename be taken from response.history[0].url?
            # Output file not specified. Pick a name that doesn't exist yet.
            fn = None
            if 'Content-Disposition' in response.headers:
                fn = filename_from_content_disposition(
                    response.headers['Content-Disposition'])
            if not fn:
                fn = filename_from_url(
                    url=response.url,
                    content_type=response.headers.get('Content-Type'),
                )
            self._output_file = open(get_unique_filename(fn), mode='a+b')

        self._progress.started(
            resumed_from=self._resumed_from,
            total_size=total_size
        )

        stream = RawStream(
            msg=HTTPResponse(response),
            with_headers=False,
            with_body=True,
            on_body_chunk_downloaded=self._on_progress,
            # TODO: Find the optimal chunk size.
            # The smaller it is the slower it gets, but gives better feedback.
            chunk_size=1024 * 8
        )

        self._progress_reporter.output.write(
            'Saving to "%s"\n' % self._output_file.name)
        self._progress_reporter.report()

        return stream, self._output_file

    def finish(self):
        assert not self._output_file.closed
        self._output_file.close()
        self._progress.finished()

    @property
    def interrupted(self):
        return (
            self._output_file.closed
            and self._progress.total_size
            and self._progress.total_size != self._progress.downloaded
        )

    def _on_progress(self, chunk):
        """
        A download progress callback.

        :param chunk: A chunk of response body data that has just
                      been downloaded and written to the output.
        :type chunk: bytes

        """
        self._progress.chunk_downloaded(len(chunk))


class Progress(object):

    def __init__(self):
        self.downloaded = 0
        self.total_size = None
        self.resumed_from = 0
        self.total_size_humanized = '?'
        self.time_started = None
        self.time_finished = None

    def started(self, resumed_from=0, total_size=None):
        assert self.time_started is None
        if total_size is not None:
            self.total_size_humanized = humanize_bytes(total_size)
            self.total_size = total_size
        self.downloaded = self.resumed_from = resumed_from
        self.time_started = time()

    def chunk_downloaded(self, size):
        assert self.time_finished is None
        self.downloaded += size

    @property
    def has_finished(self):
        return self.time_finished is not None

    def finished(self):
        assert self.time_started is not None
        assert self.time_finished is None
        self.time_finished = time()


class ProgressReporter(object):

    def __init__(self, progress, output, tick=.1, update_interval=1):
        """

        :type progress: Progress
        :type output: file
        """
        self.progress = progress
        self.output = output
        self._prev_bytes = 0
        self._prev_time = time()
        self._spinner_pos = 0
        self._tick = tick
        self._update_interval = update_interval
        self._status_line = ''
        super(ProgressReporter, self).__init__()

    def report(self):
        if self.progress.has_finished:
            self.sum_up()
        else:
            self.report_speed()
            threading.Timer(self._tick, self.report).start()

    def report_speed(self):

        now = time()

        if now - self._prev_time >= self._update_interval:

            downloaded = self.progress.downloaded

            if self.progress.total_size:
                template = PROGRESS
                percentage = (
                    downloaded / self.progress.total_size * 100)
            else:
                template = PROGRESS_NO_CONTENT_LENGTH
                percentage = None

            try:
                # TODO: Use a longer interval for the speed/eta calculation?
                speed = ((downloaded - self._prev_bytes)
                         / (now - self._prev_time))
                eta = int((self.progress.total_size - downloaded) / speed)
                eta = str(timedelta(seconds=eta))
            except ZeroDivisionError:
                speed = 0
                eta = '?'

            self._status_line = template.format(
                percentage=percentage,
                downloaded=humanize_bytes(downloaded),
                total=self.progress.total_size_humanized,
                speed=humanize_bytes(speed),
                eta=eta,
            )

            self._prev_time = now
            self._prev_bytes = downloaded

        self.output.write(
            CLEAR_LINE
            + SPINNER[self._spinner_pos]
            + ' '
            + self._status_line
        )
        self.output.flush()

        self._spinner_pos = (
            self._spinner_pos + 1
            if self._spinner_pos + 1 != len(SPINNER)
            else 0
        )

    def sum_up(self):
        actually_downloaded = (
            self.progress.downloaded - self.progress.resumed_from)
        time_taken = self.progress.time_finished - self.progress.time_started

        self.output.write(CLEAR_LINE)
        self.output.write(SUMMARY.format(
            downloaded=humanize_bytes(actually_downloaded),
            total=humanize_bytes(self.progress.total_size),
            speed=humanize_bytes(actually_downloaded / time_taken),
            time=time_taken,
        ))
        self.output.flush()