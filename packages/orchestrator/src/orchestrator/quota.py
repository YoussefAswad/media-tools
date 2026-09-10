"""Who the household's bandwidth counted for.

Two measurements already exist and neither answers the question on its own. The router's
per-device counters (:mod:`orchestrator.wan_usage`) know *how much* internet crossed the link and
*which device* pulled it, but nothing about why. The downloads ledger (:mod:`orchestrator.ledger`)
knows what a slice of that traffic was and can be told who it was for, but it is a partial record
of one host's torrents — it is not, and must never be treated as, a measurement of the link.

This module joins them:

* **The router is the only source of bytes.** Every figure here starts as a device's WAN total.
  The ledger never adds bandwidth; it only *moves* bandwidth between quotas. So the sum over
  quotas always equals what the router measured, and a bug in the attribution shows up as a
  reconciliation mismatch rather than as a plausible wrong number.
* **A device counts to its owner's quota** — device -> person -> quota, resolved from config at
  read time, so reassigning a device or a person re-attributes history that is already stored.
  A device nobody owns counts to the shared pot, which is the right answer for the TV box and
  guest phones rather than a gap to be filled.
* **A ledger row can override that for its own bytes.** A film pulled down on the server for
  someone else in the house belongs to the shared pot, not to whoever's server it is.

Everything here is pure — no database, no config file, no clock. It takes rows and returns
numbers, in the same spirit as :func:`orchestrator.wan_usage.fold_identities` and
:mod:`orchestrator.library.estimate`, because the rules below are the part most likely to change
and the part most expensive to get wrong.

Three rails, each earned by a measurement on this household's real data:

**1. Reassignment is period-level arithmetic, never day-level.** A ledger row is stamped with the
day the download *completed*, while its bytes crossed the link over however many days it ran.
Nine ``[BO3]`` rows are stamped 8 Sep 2026 and total 40.56 GB, but PC-114's WAN that day was
31.86 GB — subtracting per day would have driven a device negative and invented 8.7 GB somewhere
else. Over 5-8 Sep the same device moved 62.20 GB against 54.94 GB of those downloads and it
reconciles. So carve-outs are summed over the whole period and clamped to what the source device
actually consumed in it.

**2. A clamp reports its shortfall; it never absorbs it.** :attr:`Attribution.dropped` is the
bytes a reassignment asked to move and could not. Silently moving less than asked would make the
totals add up while quietly telling the wrong story about a device — the shape of failure
:mod:`orchestrator.wan_usage` was built to refuse.

**3. A row older than the WAN record moves nothing.** Recording began 29 Aug 2026 while the ledger
reaches back to 23 Aug; eleven rows sit in that gap. There is no measured traffic there to move,
and moving it anyway would carve bytes out of a device's *later* days to satisfy a download that
happened before anything was watching. Same rail as the Bandwidth tab's "tracked as downloads"
tile: a number whose inputs do not cover the same window is not a number.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from orchestrator.config import QuotasConfig
from orchestrator.ledger import DownloadRow
from orchestrator.wan_usage import DeviceUsage

#: usage at or above this share of an allocation is amber; at or above 100% it is red.
WARN_PCT = 85.0


@dataclass(frozen=True)
class Reassignment:
    """One ledger row counting somewhere other than its device's own quota."""

    row_id: int
    name: str
    #: the device whose WAN total these bytes come out of.
    device: str
    from_quota: str
    to_quota: str
    #: bytes actually moved, after the period clamp.
    moved: int
    #: bytes the row asked to move.
    requested: int
    day: str

    @property
    def clamped(self) -> bool:
        return self.moved < self.requested

    @property
    def shortfall(self) -> int:
        return self.requested - self.moved


@dataclass(frozen=True)
class QuotaUsage:
    """One quota's usage over a period."""

    key: str
    label: str
    allocation: int
    shared: bool
    #: what this quota's own devices consumed, before any reassignment.
    raw: int
    moved_in: int
    moved_out: int
    devices: tuple[DeviceUsage, ...] = ()

    @property
    def used(self) -> int:
        return self.raw - self.moved_out + self.moved_in

    @property
    def pct(self) -> float | None:
        """Share of this quota's allocation, or None when it has none.

        None is not zero and the UI must not render it as a full bar or an empty one: a quota
        with no allocation is tracked but unbounded, and the honest thing to show is its share of
        the household instead.
        """
        return (self.used / self.allocation * 100) if self.allocation > 0 else None

    @property
    def tone(self) -> str:
        """``ok`` / ``warn`` / ``over`` / ``""`` — the app's tile and badge vocabulary."""
        pct = self.pct
        if pct is None:
            return ""
        if pct >= 100:
            return "over"
        return "warn" if pct >= WARN_PCT else "ok"


@dataclass(frozen=True)
class Attribution:
    """The whole answer for one period, plus everything that did not fit cleanly into it."""

    quotas: tuple[QuotaUsage, ...] = ()
    reassignments: tuple[Reassignment, ...] = ()
    #: devices with no owner in config — counting to the default quota, and worth assigning.
    unassigned: tuple[DeviceUsage, ...] = ()
    #: what the router measured across every device in the window.
    household: int = 0
    #: bytes a reassignment asked to move and could not (see rail 2 in the module docstring).
    dropped: int = 0
    #: ledger rows skipped because they predate the WAN record (rail 3).
    before_record: int = 0
    #: ledger rows pointing at a quota that no longer exists in config.
    unknown_targets: tuple[str, ...] = field(default_factory=tuple)

    @property
    def attributed(self) -> int:
        return sum(q.used for q in self.quotas)

    @property
    def reconciles(self) -> bool:
        """The invariant: moving bytes between quotas never changes their sum.

        Rendered on the tab rather than merely asserted in a test, because the one failure this
        design cannot tolerate is a total that looks reasonable and is wrong.
        """
        return self.attributed == self.household

    def get(self, key: str) -> QuotaUsage | None:
        return next((q for q in self.quotas if q.key == key), None)


def attribute(
    devices: Sequence[DeviceUsage],
    rows: Iterable[DownloadRow],
    cfg: QuotasConfig,
    *,
    wan_first_day: str | None = None,
) -> Attribution:
    """Attribute a period's WAN usage to quotas.

    ``devices`` are folded identities (:func:`orchestrator.wan_usage.fold_identities`), not raw
    MACs — ownership is keyed to what a device is *called*, so a phone that rotated its address
    mid-period stays one owned device. ``rows`` are the ledger rows completing in the same
    window. ``wan_first_day`` is :meth:`orchestrator.wan_usage.WanUsage.first_day`; rows older
    than it are counted in :attr:`Attribution.before_record` and move nothing.
    """
    buckets = cfg.resolved_quotas()
    raw: dict[str, int] = dict.fromkeys(buckets, 0)
    members: dict[str, list[DeviceUsage]] = {key: [] for key in buckets}
    by_device: dict[str, int] = {}
    unassigned: list[DeviceUsage] = []
    household = 0

    for dev in devices:
        key = cfg.quota_of_device(dev.host)
        if key not in buckets:
            # Only reachable if default_quota was somehow not resolved; keep the bytes rather
            # than dropping them, since the household total must stay whole.
            key = cfg.default_quota
            buckets.setdefault(key, buckets.get(key) or _fallback_bucket())
            raw.setdefault(key, 0)
            members.setdefault(key, [])
        raw[key] += dev.total
        members[key].append(dev)
        by_device[dev.host] = by_device.get(dev.host, 0) + dev.total
        household += dev.total
        if dev.host not in cfg.devices:
            unassigned.append(dev)

    moved_in: dict[str, int] = dict.fromkeys(buckets, 0)
    moved_out: dict[str, int] = dict.fromkeys(buckets, 0)
    reassignments: list[Reassignment] = []
    unknown: list[str] = []
    dropped = 0
    before_record = 0
    #: how much each source device has already given away this period — the clamp's running total.
    given: dict[str, int] = {}

    # Oldest first, so which row gets clamped when a device is over-committed is deterministic
    # and explicable ("the last one that day did not fit"), not an artefact of row ordering.
    for row in sorted(rows, key=lambda r: (r.day, r.id)):
        source = (row.device or cfg.server_device or "").strip()
        if not source:
            continue  # nothing says whose bytes these were; the device totals already hold them
        from_quota = cfg.quota_of_device(source)
        target = (row.quota or "").strip()
        if not target or target == from_quota:
            continue  # counts where it already counts
        if target not in buckets:
            unknown.append(target)
            continue  # a deleted quota is not a reason to invent one
        if wan_first_day and row.day < wan_first_day:
            before_record += row.total_bytes
            continue

        requested = row.total_bytes
        headroom = max(0, by_device.get(source, 0) - given.get(source, 0))
        moved = min(requested, headroom)
        given[source] = given.get(source, 0) + moved
        dropped += requested - moved
        if moved:
            moved_out[from_quota] = moved_out.get(from_quota, 0) + moved
            moved_in[target] = moved_in.get(target, 0) + moved
        reassignments.append(
            Reassignment(
                row_id=row.id,
                name=row.name,
                device=source,
                from_quota=from_quota,
                to_quota=target,
                moved=moved,
                requested=requested,
                day=row.day,
            )
        )

    usages = [
        QuotaUsage(
            key=key,
            label=cfg.label_of(key),
            allocation=bucket.allocation_bytes,
            shared=bucket.shared,
            raw=raw.get(key, 0),
            moved_in=moved_in.get(key, 0),
            moved_out=moved_out.get(key, 0),
            devices=tuple(sorted(members.get(key, []), key=lambda d: d.total, reverse=True)),
        )
        for key, bucket in buckets.items()
    ]
    # Heaviest first, matching the By-device table; an empty quota still shows, because "Logain
    # used nothing this period" is an answer and a missing row is not.
    usages.sort(key=lambda q: (q.used, q.raw), reverse=True)

    return Attribution(
        quotas=tuple(usages),
        reassignments=tuple(reassignments),
        unassigned=tuple(unassigned),
        household=household,
        dropped=dropped,
        before_record=before_record,
        unknown_targets=tuple(dict.fromkeys(unknown)),
    )


def _fallback_bucket():
    from orchestrator.config import QuotaBucket

    return QuotaBucket(shared=True)
