"""The loop and dashboard turn off QuickEdit in their console window (25 Nov 2026).

The autostart task now opens them minimized, which Windows does in a classic console window,
where a click or a text selection pauses the program until the selection is cleared."""

from __future__ import annotations

from tradebot.cli import ENABLE_EXTENDED_FLAGS, ENABLE_QUICK_EDIT_MODE, no_quick_edit

ECHO_AND_LINE_INPUT = 0x0004 | 0x0002


class FakeKernel:
    def __init__(self, mode: int | None, handle: int = 7) -> None:
        self.mode, self.handle, self.set_to = mode, handle, None

    def GetStdHandle(self, which):
        return self.handle

    def GetConsoleMode(self, handle, ref):
        if self.mode is None:
            return 0  # not a console: output redirected, a service, a test runner
        ref._obj.value = self.mode
        return 1

    def SetConsoleMode(self, handle, mode):
        self.set_to = mode
        return 1


def test_quick_edit_is_turned_off_and_the_rest_of_the_mode_kept():
    kernel = FakeKernel(ECHO_AND_LINE_INPUT | ENABLE_QUICK_EDIT_MODE | ENABLE_EXTENDED_FLAGS)
    assert no_quick_edit(kernel)
    assert kernel.set_to == ECHO_AND_LINE_INPUT | ENABLE_EXTENDED_FLAGS


def test_nothing_changes_when_quick_edit_is_already_off():
    kernel = FakeKernel(ECHO_AND_LINE_INPUT | ENABLE_EXTENDED_FLAGS)
    assert not no_quick_edit(kernel) and kernel.set_to is None


def test_without_a_console_nothing_happens():
    kernel = FakeKernel(None)
    assert not no_quick_edit(kernel) and kernel.set_to is None
    assert not no_quick_edit(FakeKernel(ENABLE_QUICK_EDIT_MODE, handle=0))
