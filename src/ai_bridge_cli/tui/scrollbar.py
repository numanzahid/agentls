"""Thin scrollbar in the style of lazygit: a half-block thumb beside the border, no track."""
from __future__ import annotations

from rich.segment import Segment, Segments
from rich.style import Style
from rich.color import Color
from textual.scrollbar import ScrollBarRender


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
