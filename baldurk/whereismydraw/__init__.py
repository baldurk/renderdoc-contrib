###############################################################################
# The MIT License (MIT)
#
# Copyright (c) 2021 Baldur Karlsson
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.
###############################################################################

import qrenderdoc as qrd

from . import window

extiface_version = ''


def open_window_callback(ctx: qrd.CaptureContext, data):
    win = window.get_window(ctx, extiface_version)

    ctx.RaiseDockWindow(win)


def register(version: str, ctx: qrd.CaptureContext):
    global extiface_version
    extiface_version = version

    print("Registering 'Where is my draw?' extension for RenderDoc version {}".format(version))

    ctx.Extensions().RegisterWindowMenu(qrd.WindowMenu.Window, ["Where is my Draw?"], open_window_callback)


def unregister():
    print("Unregistering 'Where is my draw?' extension")
