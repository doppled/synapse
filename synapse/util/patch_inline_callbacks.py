# -*- coding: utf-8 -*-
# Copyright 2018 New Vector Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import print_function

import functools
import sys

from twisted.internet import defer
from twisted.internet.defer import Deferred
from twisted.python.failure import Failure


def do_patch():
    """
    Patch defer.inlineCallbacks so that it checks the state of the logcontext on exit
    """

    from synapse.logging.context import LoggingContext

    orig_inline_callbacks = defer.inlineCallbacks
    if hasattr(orig_inline_callbacks, "patched_by_synapse"):
        return

    def new_inline_callbacks(f):
        @functools.wraps(f)
        def wrapped(*args, **kwargs):
            start_context = LoggingContext.current_context()
            changes = []
            orig = orig_inline_callbacks(_check_yield_points(f, changes, start_context))

            try:
                res = orig(*args, **kwargs)
            except Exception:
                if LoggingContext.current_context() != start_context:
                    for err in changes:
                        print(err, file=sys.stderr)

                    err = "%s changed context from %s to %s on exception" % (
                        f,
                        start_context,
                        LoggingContext.current_context(),
                    )
                    print(err, file=sys.stderr)
                    raise Exception(err)
                raise

            if not isinstance(res, Deferred) or res.called:
                if LoggingContext.current_context() != start_context:
                    for err in changes:
                        print(err, file=sys.stderr)

                    err = "Completed %s changed context from %s to %s" % (
                        f,
                        start_context,
                        LoggingContext.current_context(),
                    )
                    # print the error to stderr because otherwise all we
                    # see in travis-ci is the 500 error
                    print(err, file=sys.stderr)
                    raise Exception(err)
                return res

            if LoggingContext.current_context() != LoggingContext.sentinel:
                err = (
                    "%s returned incomplete deferred in non-sentinel context "
                    "%s (start was %s)"
                ) % (f, LoggingContext.current_context(), start_context)
                print(err, file=sys.stderr)
                raise Exception(err)

            def check_ctx(r):
                if LoggingContext.current_context() != start_context:
                    for err in changes:
                        print(err, file=sys.stderr)
                    err = "%s completion of %s changed context from %s to %s" % (
                        "Failure" if isinstance(r, Failure) else "Success",
                        f,
                        start_context,
                        LoggingContext.current_context(),
                    )
                    print(err, file=sys.stderr)
                    raise Exception(err)
                return r

            res.addBoth(check_ctx)
            return res

        return wrapped

    defer.inlineCallbacks = new_inline_callbacks
    new_inline_callbacks.patched_by_synapse = True


def _check_yield_points(f, changes, start_context):
    """Wraps a generator that is about to be passed to defer.inlineCallbacks
    checking that after every yield the log contexts are correct.
    """

    from synapse.logging.context import LoggingContext

    @functools.wraps(f)
    def check_yield_points_inner(*args, **kwargs):
        expected_context = start_context

        gen = f(*args, **kwargs)

        last_yield_line_no = 1
        result = None
        while True:
            try:
                isFailure = isinstance(result, Failure)
                if isFailure:
                    d = result.throwExceptionIntoGenerator(gen)
                else:
                    d = gen.send(result)
            except (StopIteration, defer._DefGen_Return) as e:
                if LoggingContext.current_context() != expected_context:
                    # This happens when the context is lost sometime *after* the
                    # final yield and returning. E.g. we forgot to yield on a
                    # function that returns a deferred.
                    err = (
                        "Function %r returned and changed context from %s to %s,"
                        " in %s between %d and end of func"
                        % (
                            f.__qualname__,
                            start_context,
                            LoggingContext.current_context(),
                            f.__code__.co_filename,
                            last_yield_line_no,
                        )
                    )
                    changes.append(err)
                    # raise Exception(err)
                return getattr(e, "value", None)

            frame = gen.gi_frame

            if isinstance(d, defer.Deferred):
                # This happens if we yield on a deferred that doesn't follow
                # the log context rules without wrappin in a `make_deferred_yieldable`
                if LoggingContext.current_context() != LoggingContext.Sentinel:
                    err = (
                        "%s yielded with context %s rather than Sentinel,"
                        " yielded on line %d in %s"
                        % (
                            frame.f_code.co_name,
                            start_context,
                            LoggingContext.current_context(),
                            frame.f_lineno,
                            frame.f_code.co_filename,
                        )
                    )
                    changes.append(err)

            try:
                result = yield d
            except Exception as e:
                result = Failure(e)

            if LoggingContext.current_context() != expected_context:
                # This happens because the context is lost sometime *after* the
                # previous yield and *after* the current yield. E.g. the
                # deferred we waited on didn't follow the rules, or we forgot to
                # yield on a function between the two yield points.
                err = (
                    "%s changed context from %s to %s, happened between lines %d and %d in %s"
                    % (
                        frame.f_code.co_name,
                        start_context,
                        LoggingContext.current_context(),
                        last_yield_line_no,
                        frame.f_lineno,
                        frame.f_code.co_filename,
                    )
                )
                changes.append(err)
                # raise Exception(err)

                expected_context = LoggingContext.current_context()

            last_yield_line_no = frame.f_lineno

    return check_yield_points_inner