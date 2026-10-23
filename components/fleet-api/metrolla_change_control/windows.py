"""Release windows and freezes.

A window is an RRULE (RFC 5545) giving wall-clock start times in the configured time zone,
plus a duration. Rules are anchored at 2024-01-01 00:00 local so INTERVAL phases are stable;
BYHOUR/BYMINUTE pick the time of day (unset minutes and seconds are 0).

A freeze beats a window: any part of a window that overlaps a freeze is closed. With no
windows configured, every moment outside a freeze counts as open.
"""
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

from dateutil.rrule import rrulestr

from .config import ChangeControlConfig, Freeze

Interval = Tuple[datetime, datetime]

ANCHOR = (2024, 1, 1)
HORIZON = timedelta(days=400)
_FAR = timedelta(days=36500)


def _subtract(interval: Interval, freezes) -> List[Interval]:
    out = [interval]
    for f in freezes:
        nxt = []
        for s, e in out:
            if f.end <= s or f.start >= e:
                nxt.append((s, e))
                continue
            if s < f.start:
                nxt.append((s, f.start))
            if f.end < e:
                nxt.append((f.end, e))
        out = nxt
    return out


class Schedule:
    def __init__(self, cfg: ChangeControlConfig):
        self.cfg = cfg
        self.tz = cfg.tz
        anchor = datetime(*ANCHOR, tzinfo=self.tz)
        self._rules = [(rrulestr(w.rrule, dtstart=anchor, cache=True), timedelta(minutes=w.duration_min), w.name)
                       for w in cfg.windows]

    def frozen(self, now: datetime) -> Optional[Freeze]:
        for f in self.cfg.freezes:
            if f.covers(now):
                return f
        return None

    def _occurrences(self, start: datetime, end: datetime):
        """(start, end, name) of every window occurrence overlapping [start, end)."""
        out = []
        for rule, dur, name in self._rules:
            t = rule.after((start - dur).astimezone(self.tz), inc=False)
            while t is not None and t < end:
                if t + dur > start:
                    out.append((t, t + dur, name))
                t = rule.after(t, inc=False)
        return sorted(out)

    def _open_intervals(self, start: datetime, end: datetime) -> List[Tuple[datetime, datetime, str]]:
        if not self._rules:
            raw = [(start, end, 'always')]
        else:
            raw = self._occurrences(start, end)
        out = []
        for s, e, name in raw:
            for fs, fe in _subtract((s, e), self.cfg.freezes):
                if fe > start:
                    out.append((max(fs, start), fe, name))
        return sorted(out)

    def window_at(self, now: datetime) -> Optional[Interval]:
        """The open window containing `now` (start, end), or None."""
        for s, e, _ in self._open_intervals(now, now + timedelta(seconds=1)):
            if s <= now < e:
                return self._extend(s, e)
        return None

    def next_open(self, after: datetime) -> Optional[Interval]:
        """The first open window that ends after `after` (it may already be open)."""
        for s, e, _ in self._open_intervals(after, after + HORIZON):
            if e > after:
                return self._extend(max(s, after) if s <= after else s, e)
        return None

    def _extend(self, s: datetime, e: datetime) -> Interval:
        if not self._rules:
            # No windows: open until the next freeze starts.
            nxt = [f.start for f in self.cfg.freezes if f.start >= e - timedelta(seconds=1) and f.start > s]
            return s, min(nxt) if nxt else s + _FAR
        return s, e

    def describe(self, now: datetime) -> dict:
        nxt = self.next_open(now)
        frozen = self.frozen(now)
        return {'now': now.astimezone(self.tz).isoformat(), 'open_now': self.window_at(now) is not None,
                'frozen': {'reason': frozen.reason, 'until': frozen.end.isoformat()} if frozen else None,
                'next_window': {'start': nxt[0].astimezone(self.tz).isoformat(),
                                'end': nxt[1].astimezone(self.tz).isoformat()} if nxt else None}
