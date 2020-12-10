# osmo_gsm_tester: global logging
#
# Copyright (C) 2016-2017 by sysmocom - s.f.m.c. GmbH
#
# Author: Neels Hofmeyr <neels@hofmeyr.de>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import os
import sys
import time
import traceback
import atexit
import re
from datetime import datetime # we need this for strftime as the one from time doesn't carry microsecond info
from inspect import getframeinfo, stack

from .util import is_dict

L_ERR = 30
L_LOG = 20
L_DBG = 10
L_TRACEBACK = 'TRACEBACK'

LEVEL_STRS = {
            'err': L_ERR,
            'log': L_LOG,
            'dbg': L_DBG,
        }

C_NET = 'net'
C_RUN = 'run'
C_TST = 'tst'
C_CNF = 'cnf'
C_BUS = 'bus'
C_DEFAULT = '---'

FILE_LOG = 'log'
FILE_LOG_BRIEF = 'log_brief'

LOG_CTX_VAR = '_log_ctx_'

def dbg(*messages, _origin=None, _category=None, _src=None, **named_items):
    '''Log on debug level. See also log()'''
    _log(messages, named_items, origin=_origin, category=_category, level=L_DBG, src=_src)

def log(*messages, _origin=None, _category=None, _level=L_LOG, _src=None, **named_items):
    '''Log a message. The origin, an Origin class instance, is normally
    determined by stack magic, only pass _origin to override. The category is
    taken from the origin. _src is normally an integer indicating how many
    levels up the stack sits the interesting source file to log about, can also
    be a string. The log message is composed of all *messages and
    **named_items, for example:
      log('frobnicate:', thing, key=current_key, prop=erty)
    '''
    _log(messages, named_items, origin=_origin, category=_category, level=_level, src=_src)

def err(*messages, _origin=None, _category=None, _src=None, **named_items):
    '''Log on error level. See also log()'''
    _log(messages, named_items, origin=_origin, category=_category, level=L_ERR, src=_src)

def _log(messages=[], named_items={}, origin=None, category=None, level=L_LOG, src=None):
    if origin is None:
        origin = Origin.find_on_stack()
    if category is None and isinstance(origin, Origin):
        category = origin._log_category
    if src is None:
        # two levels up
        src = 2
    if isinstance(src, int):
        src = get_src_from_caller(src + 1)
    for target in LogTarget.all_targets:
        target.log(origin, category, level, src, messages, named_items)


LONG_DATEFMT = '%Y-%m-%d_%H:%M:%S.%f'
DATEFMT = '%H:%M:%S.%f'

# may be overridden by regression tests
get_process_id = lambda: '%d-%d' % (os.getpid(), time.time())

class Error(Exception):
    def __init__(self, *messages, origin=None, **named_items):
        msg = ''
        if origin is None:
            origin = Origin.find_on_stack(f=sys._getframe(1))
        if origin:
            msg += origin.name() + ': '
        msg += compose_message(messages, named_items)
        if origin and origin._parent is not None:
            deeper_origins = origin.ancestry_str()
            msg += ' [%s]' % deeper_origins
        super().__init__(msg)

class LogTarget:
    all_targets = []

    do_log_time = None
    do_log_category = None
    do_log_level = None
    do_log_origin = None
    do_log_all_origins_on_levels = None
    do_log_traceback = None
    do_log_src = None
    origin_width = None
    origin_fmt = None
    all_levels = None

    # redirected by logging test
    get_time_str = lambda self: datetime.now().strftime(self.log_time_fmt)

    # sink that gets each complete logging line
    log_write_func = None

    category_levels = None

    def __init__(self, log_write_func=None):
        if log_write_func is None:
            log_write_func = sys.__stdout__.write
        self.log_write_func = log_write_func
        self.category_levels = {}
        self.style()
        LogTarget.all_targets.append(self)

    def remove(self):
        LogTarget.all_targets.remove(self)

    def style(self, time=True, time_fmt=DATEFMT, category=True, level=True, origin=True, origin_width=32, src=True, trace=False, all_origins_on_levels=(L_ERR, L_LOG, L_DBG, L_TRACEBACK)):
        '''
        set all logging format aspects, to defaults if not passed:
        time: log timestamps;
        time_fmt: format of timestamps;
        category: print the logging category (three letters);
        level: print the logging level, unless it is L_LOG;
        origin: print which object(s) the message originated from;
        origin_width: fill up the origin string with whitespace to this witdh;
        src: log the source file and line number the log comes from;
        trace: on exceptions, log the full stack trace;
        all_origins_on_levels: pass a tuple of logging levels that should have a full trace of origins
        '''
        self.log_time_fmt = time_fmt
        self.do_log_time = bool(time)
        if not self.log_time_fmt:
            self.do_log_time = False
        self.do_log_category = bool(category)
        self.do_log_level = bool(level)
        self.do_log_origin = bool(origin)
        self.origin_width = int(origin_width)
        self.origin_fmt = '{:>%ds}' % self.origin_width
        self.do_log_src = src
        self.do_log_traceback = trace
        self.do_log_all_origins_on_levels = tuple(all_origins_on_levels or [])
        return self

    def style_change(self, time=None, time_fmt=None, category=None, level=None, origin=None, origin_width=None, src=None, trace=None, all_origins_on_levels=None):
        'modify only the given aspects of the logging format'
        self.style(
            time=(time if time is not None else self.do_log_time),
            time_fmt=(time_fmt if time_fmt is not None else self.log_time_fmt),
            category=(category if category is not None else self.do_log_category),
            level=(level if level is not None else self.do_log_level),
            origin=(origin if origin is not None else self.do_log_origin),
            origin_width=(origin_width if origin_width is not None else self.origin_width),
            src=(src if src is not None else self.do_log_src),
            trace=(trace if trace is not None else self.do_log_traceback),
            all_origins_on_levels=(all_origins_on_levels if all_origins_on_levels is not None else self.do_log_all_origins_on_levels),
            )
        return self

    def set_level(self, category, level):
        'set global logging log.L_* level for a given log.C_* category'
        self.category_levels[category] = level
        return self

    def set_all_levels(self, level):
        self.all_levels = level
        return self

    def is_enabled(self, category, level):
        if level == L_TRACEBACK:
            return self.do_log_traceback
        if self.all_levels is not None:
            is_level = self.all_levels
        else:
            is_level = self.category_levels.get(category)
        if is_level is None:
            is_level = L_LOG
        if level < is_level:
            return False
        return True

    def log(self, origin, category, level, src, messages, named_items):
        if category and len(category) != 3:
            self.log_write_func('WARNING: INVALID LOGGING CATEGORY %r\n' % category)
            self.log_write_func('origin=%r category=%r level=%r\n' % (origin, category, level));

        if not category:
            category = C_DEFAULT
        if not self.is_enabled(category, level):
            return

        log_pre = []
        if self.do_log_time:
            log_pre.append(self.get_time_str())

        if self.do_log_category:
            log_pre.append(category)

        deeper_origins = ''
        if self.do_log_origin:
            if origin is None:
                name = '-'
            elif isinstance(origin, Origin):
                name = origin.src()
                # only log ancestry when there is more than one
                if origin._parent is not None:
                    deeper_origins = origin.ancestry_str()
            elif isinstance(origin, str):
                name = origin or None
            if not name:
                name = str(origin.__class__.__name__)
            log_pre.append(self.origin_fmt.format(name))

        if self.do_log_level and level != L_LOG:
            loglevel = '%s: ' % (level_str(level) or ('loglevel=' + str(level)))
        else:
            loglevel = ''

        log_line = [compose_message(messages, named_items)]

        if deeper_origins and (level in self.do_log_all_origins_on_levels):
            log_line.append(' [%s]' % deeper_origins)

        if self.do_log_src and src:
            log_line.append(' [%s]' % str(src))

        log_str = '%s%s%s%s' % (' '.join(log_pre),
                              ': ' if log_pre else '',
                              loglevel,
                              ' '.join(log_line))

        if not log_str.endswith('\n'):
            log_str = log_str + '\n'
        self.log_write_func(log_str)

    def large_separator(self, *msgs, sublevel=1, space_above=True):
        sublevel = max(1, min(3, sublevel))
        msg = ' '.join(msgs)
        sep = '-' * int(23 * (5 - sublevel))
        if not msg:
            msg = sep
        lines = [sep, msg, sep, '']
        if space_above:
            lines.insert(0, '')
        self.log_write_func('\n'.join(lines))

    def get_mark(self):
        # implemented in FileLogTarget
        return 0

    def get_output(self, since_mark=0):
        # implemented in FileLogTarget
        return ''


def level_str(level):
    if level == L_TRACEBACK:
        return L_TRACEBACK
    if level <= L_DBG:
        return 'DBG'
    if level <= L_LOG:
        return 'LOG'
    return 'ERR'

def _log_all_targets(origin, category, level, src, messages, named_items=None):
    if origin is None:
        origin = Origin.find_on_stack()
    if isinstance(src, int):
        src = get_src_from_caller(src + 1)
    for target in LogTarget.all_targets:
        target.log(origin, category, level, src, messages, named_items)

def large_separator(*msgs, sublevel=1, space_above=True):
    for target in LogTarget.all_targets:
        target.large_separator(*msgs, sublevel=sublevel, space_above=space_above)

def get_src_from_caller(levels_up=1):
    # Poke into internal to avoid hitting the linecache which will make one or
    # more calls to stat(2).
    frame = sys._getframe(levels_up)
    return '%s:%d' % (os.path.basename(frame.f_code.co_filename), frame.f_lineno)

def get_src_from_exc_info(exc_info=None, levels_up=1):
    if exc_info is None:
        exc_info = sys.exc_info()
    ftb = traceback.extract_tb(exc_info[2])
    f,l,m,c = ftb[-levels_up]
    f = os.path.basename(f)
    return '%s:%s: %s' % (f, l, c)

def get_line_for_src(src_path):
    '''find a given source file on the stack and return the line number for
    that file. (Used to indicate the position in a test script.)'''
    etype, exception, tb = sys.exc_info()
    if tb:
        ftb = traceback.extract_tb(tb)
        for f,l,m,c in ftb:
            if f.endswith(src_path):
                return l

    for frame in stack():
        caller = getframeinfo(frame[0])
        if caller.filename.endswith(src_path):
            return caller.lineno
    return None

def ctx(*name_items, **detail_items):
    '''Store log context in the current frame. This string will appear as
    origin information for exceptions thrown within the calling scope.'''
    if not name_items and not detail_items:
        ctx_obj(None)
    if not detail_items and len(name_items) == 1 and isinstance(name_items[0], Origin):
        ctx_obj(name_items[0])
    else:
        ctx_obj(compose_message(name_items, detail_items))

def ctx_obj(origin_or_str):
    f = sys._getframe(2)
    if origin_or_str is None:
        f.f_locals.pop(LOG_CTX_VAR, None)
        return
    if isinstance(origin_or_str, Origin) and origin_or_str is f.f_locals.get('self'):
        # Avoid adding log ctx in stack frame where Origin it is already "self",
        # it is not needed and will make find_on_stack() to malfunction
        raise Error('Don\'t use log.ctx(self), it\'s not needed!')
    f.f_locals[LOG_CTX_VAR] = origin_or_str

class OriginLoopError(Error):
    pass

class Origin:
    '''
    Base class for all classes that want to appear in the log.
    It is a simple named marker to find in the stack frames.
    This depends on the object instance named 'self' in each member class.

    In addition, it provides a logging category and a globally unique ID for
    each instance.

    Each child class *must* call super().__init__(category, name), to allow
    noting its parent origins.
    '''

    _global_id = None

    _name = None
    _origin_id = None
    _log_category = None
    _parent = None

    @staticmethod
    def find_on_stack(except_obj=None, f=None):
        if f is None:
            f = sys._getframe(2)
        log_ctx_obj = None
        origin = None
        while f is not None:
            l = f.f_locals

            # if there is a log_ctx in the scope, add it, pointing to the next
            # actual Origin class in the stack
            log_ctx = l.get(LOG_CTX_VAR)
            if log_ctx:
                if isinstance(log_ctx, Origin):
                    new_log_ctx_obj = log_ctx
                else:
                    new_log_ctx_obj = Origin(None, log_ctx, find_parent=False)
                if log_ctx_obj is None:
                    log_ctx_obj = new_log_ctx_obj
                else:
                    log_ctx_obj.highest_ancestor()._set_parent(new_log_ctx_obj)

            obj = l.get('self')
            if obj and isinstance(obj, Origin) and (except_obj is not obj):
                origin = obj
                break
            f = f.f_back

        if (origin is not None) and (log_ctx_obj is not None):
            log_ctx_highest_ancestor = log_ctx_obj.highest_ancestor()
            # If Both end up in same ancestor it means they are connected to the
            # same tree, so no need to connect them, we'll use log_ctx_obj
            # specific path in that case.
            if log_ctx_highest_ancestor != origin.highest_ancestor():
                log_ctx_highest_ancestor._set_parent(origin)
            p = log_ctx_obj
            while p:
                p._set_log_category(origin._log_category)
                p = p._parent
        if log_ctx_obj is not None:
            return log_ctx_obj
        # may return None
        return origin

    @staticmethod
    def find_in_exc_info(exc_info):
        tb = exc_info[2]
        # get last tb ... I hope that's right
        while tb.tb_next:
            tb = tb.tb_next
        return Origin.find_on_stack(f=tb.tb_frame)

    def __init__(self, category, *name_items, find_parent=True, **detail_items):
        self._set_log_category(category)
        self.set_name(*name_items, **detail_items)
        if find_parent:
            self._set_parent(Origin.find_on_stack(except_obj=self))

    def _set_parent(self, parent):
        # make sure to avoid loops
        p = parent
        while p:
            if p is self:
                raise OriginLoopError('Origin parent loop')
            p = p._parent
        self._parent = parent

    def set_name(self, *name_items, **detail_items):
        '''Change the origin's name for log output; rather use the constructor.
        This function can be used to change the name in case naming info
        becomes available only after class creation (like a pid)'''
        if name_items:
            name = '-'.join([str(i) for i in name_items])
        elif not detail_items:
            name = self.__class__.__name__
        else:
            name = ''
        if detail_items:
            details = '(%s)' % (', '.join([("%s=%r" % (k,v))
                                           for k,v in sorted(detail_items.items())]))
        else:
            details = ''
        self._name = name + details

    def name(self):
        return self._name or self.__class__.__name__

    def src(self):
        '''subclasses may override this to provide more detailed source
        information with the name, for a backtrace. For example, a line number
        in a test script.'''
        return self.name()

    __str__ = name
    __repr__ = name

    def origin_id(self):
        if not self._origin_id:
            if not Origin._global_id:
                Origin._global_id = get_process_id()
            self._origin_id = '%s-%s' % (self.name(), Origin._global_id)
        return self._origin_id

    def _set_log_category(self, category):
        self._log_category = category

    def ancestry(self):
        origins = []
        n = 10
        origin = self
        while origin:
            origins.insert(0, origin)
            origin = origin._parent
            n -= 1
            if n < 0:
                break
        return origins

    def ancestry_str(self):
        return '↪'.join([o.src() for o in self.ancestry()])

    def highest_ancestor(self):
        if self._parent:
            return self._parent.highest_ancestor()
        return self

    def log(self, *messages, _src=3, **named_items):
        '''same as log.log() but passes this object to skip looking up an origin'''
        log(*messages, _origin=self, _src=_src, **named_items)

    def dbg(self, *messages, _src=3, **named_items):
        '''same as log.dbg() but passes this object to skip looking up an origin'''
        dbg(*messages, _origin=self, _src=_src, **named_items)

    def err(self, *messages, _src=3, **named_items):
        '''same as log.err() but passes this object to skip looking up an origin'''
        err(*messages, _origin=self, _src=_src, **named_items)

def trace(exc_info=None, origin=None):
    if exc_info is None:
        exc_info = sys.exc_info()
    if origin is None:
        origin = Origin.find_in_exc_info(exc_info)
    _log(messages=traceback.format_exception(*exc_info),
         origin=origin, level=L_TRACEBACK)

def log_exn():
    exc_info = sys.exc_info()
    origin = Origin.find_in_exc_info(exc_info)

    etype, exception, tb = exc_info
    if hasattr(exception, 'msg'):
        msg = exception.msg
    else:
        msg = str(exception)

    trace(exc_info, origin=origin)
    _log(messages=('%s:' % str(etype.__name__), msg),
         origin=origin, level=L_ERR, src=get_src_from_exc_info(exc_info))


def set_all_levels(level):
    for target in LogTarget.all_targets:
        target.set_all_levels(level)

def set_level(category, level):
    for target in LogTarget.all_targets:
        target.set_level(category, level)

def style(**kwargs):
    for target in LogTarget.all_targets:
        target.style(**kwargs)

def style_change(**kwargs):
    for target in LogTarget.all_targets:
        target.style_change(**kwargs)

class TestsTarget(LogTarget):
    'LogTarget producing deterministic results for regression tests'
    def __init__(self, log_write_func=None):
        super().__init__(log_write_func)
        self.style(time=False, src=False, origin_width=0)

class FileLogTarget(LogTarget):
    'LogTarget to log to a file system path'
    log_file = None

    def __init__(self, log_path):
        atexit.register(self.at_exit)
        self.path = log_path
        self.log_file = open(log_path, 'a')
        super().__init__(self.write_to_log_and_flush)

    def remove(self):
        super().remove()
        self.log_file.close()
        self.log_file = None

    def write_to_log_and_flush(self, msg):
        self.log_file.write(msg)
        self.log_file.flush()

    def at_exit(self):
        if self.log_file is not None:
            self.log_file.flush()
            self.log_file.close()

    def log_file_path(self):
        return self.path

    def get_mark(self):
        if self.path is None:
            return 0
        # return current file length
        with open(self.path, 'r') as logfile:
            return logfile.seek(0, 2)

    def get_output(self, since_mark=0):
        if self.path is None:
            return ''
        with open(self.path, 'r') as logfile:
            if since_mark:
                logfile.seek(since_mark)
            return logfile.read()

def run_logging_exceptions(func, *func_args, return_on_failure=None, **func_kwargs):
    try:
        return func(*func_args, **func_kwargs)
    except:
        log_exn()
        return return_on_failure

def _compose_named_items(item):
    'make sure dicts are output sorted, for test expectations'
    if is_dict(item):
        return '{%s}' % (', '.join(
               ['%s=%s' % (k, _compose_named_items(v))
                for k,v in sorted(item.items())]))
    return repr(item)

def compose_message(messages, named_items):
    msgs = [str(m) for m in messages]

    if named_items:
        # unfortunately needs to be sorted to get deterministic results
        msgs.append(_compose_named_items(named_items))

    return ' '.join(msgs)

# vim: expandtab tabstop=4 shiftwidth=4
