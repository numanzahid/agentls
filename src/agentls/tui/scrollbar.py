"""lazygit-style scrollbars: a ▐ thumb on the pane border (BorderScrollbar) or, inside dialogs,
a thin half-block thumb with no track (ThinScrollBarRender)."""
from __future__ import annotations

import math

from rich.segment import Segment, Segments
from rich.style import Style
from rich.color import Color
from textual.scrollbar import ScrollBarRender
from textual.strip import Strip


class ThinScrollBarRender(ScrollBarRender):
    @classmethod
    def render_bar(cls, size: int = 25, virtual_size: float = 50, window_size: float = 20, position: float = 0,
                   thickness: int = 1, vertical: bool = True, back_color: Color = Color.parse("#555555"),
                   bar_color: Color = Color.parse("#ff00ff")) -> Segments:
        if not vertical:
            return super().render_bar(size, virtual_size, window_size, position, thickness, vertical,
                                      back_color, bar_color)
        blank = " " * thickness
        upper, lower = Style(meta={"@mouse.down": "scroll_up"}), Style(meta={"@mouse.down": "scroll_down"})
        if not (window_size and size and virtual_size and size != virtual_size):
            return Segments([Segment(blank, upper)] * size, new_lines=True)
        thumb = max(1, round(window_size * size / virtual_size))
        start = round((size - thumb) * position / (virtual_size - window_size))
        start = min(max(0, start), size - thumb)
        grab = Style(color=bar_color, meta={"@mouse.down": "grab"})
        segments = ([Segment(blank, upper)] * start + [Segment("▐" * thickness, grab)] * thumb
                    + [Segment(blank, lower)] * (size - start - thumb))
        return Segments(segments, new_lines=True)


def lazygit_thumb(list_size: int, page_size: int, position: int, area: int) -> tuple[int, int]:
    """gocui's calcScrollbar: (start, height) of the thumb within a scroll area of `area` rows.

    The thumb only touches the ends when the view is at the top or bottom of the list.
    """
    height = area if page_size >= list_size else int(page_size / list_size * area)
    max_position = list_size - page_size
    if max_position <= 0:
        return 0, height
    if position >= max_position:
        return area - height, height
    return math.ceil(position / max_position * (area - height - 1)), height


class BorderScrollbar:
    """lazygit-style scrollbar drawn on the widget's own right border: the border's │ becomes ▐ on
    the thumb rows, in the border's color. Use on bordered scrollable widgets with the native
    vertical scrollbar hidden (scrollbar-size-vertical: 0)."""

    def thumb_rows(self) -> range:
        inner = self.size.height  # content rows between the top and bottom border
        page, total = self.scrollable_content_region.height, self.virtual_size.height
        if inner < 2 or page >= total:
            return range(0)
        start, height = lazygit_thumb(total, page, round(self.scroll_y), inner - 1)
        top = self.styles.border_top[0] not in ("", "none", "hidden")
        first = int(top) + start
        return range(first, first + height + 1)  # gocui draws start..end inclusive

    def render_lines(self, crop):
        strips = super().render_lines(crop)
        x = self.outer_size.width - 1
        rows = self.thumb_rows()
        if not rows or not crop.x <= x < crop.right:
            return strips
        at = x - crop.x
        out = []
        for offset, strip in enumerate(strips):
            if crop.y + offset in rows:
                cell = list(strip.crop(at, at + 1))
                style = cell[0].style if cell else None
                strip = Strip.join([strip.crop(0, at), Strip([Segment("▐", style)], 1), strip.crop(at + 1)])
            out.append(strip)
        return out

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        super().watch_scroll_y(old_value, new_value)
        if round(old_value) != round(new_value):
            self.refresh()  # the border thumb moves; repaint the border too
