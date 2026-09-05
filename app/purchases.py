"""The receipt department: what was bought over the counter, by job.

Zack: *"this one will have more folders than all. Because sometimes we spend
money on jobs we don't get so this will turn out to be a loss."*

Both halves of that are the design.

**More folders than all.** Every other department only has a folder for a job
that reached it - a job with a quote, a job with a subcontract, a job with a
check. Counter spend touches everything: the job that never got past an
estimate, the callback that took an afternoon, the one that turned out to be
somebody else's roof. So this department has a folder for every job anybody
ever bought anything for, which is nearly all of them.

**And some of it is a loss.** A job number is assigned when we start chasing
the work. The fuel, the ladder rental and the sample shingles get spent whether
or not the board picks us. On a job we won that spend is cost; on one we lost
it is money gone with nothing on the other side of it, and no invoice, no
quote and no contract in this system can tell the two apart - only the outcome
of the job can. So it is totalled separately and named for what it is.

Pure Python over rows already loaded. Decimal throughout.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Iterable, Optional

from app.models import JOB_LOST, Job, Purchase

ZERO = Decimal("0")


@dataclass
class Folder:
    """One job's counter spend."""

    job: Job
    purchases: list[Purchase] = field(default_factory=list)

    @property
    def total(self) -> Decimal:
        return sum((p.total or ZERO for p in self.purchases), ZERO)

    @property
    def count(self) -> int:
        return len(self.purchases)

    @property
    def latest(self) -> Optional[date]:
        dates = [p.purchased_on for p in self.purchases if p.purchased_on]
        return max(dates) if dates else None

    @property
    def is_loss(self) -> bool:
        """Spent on work we did not get."""
        return self.job.outcome == JOB_LOST

    @property
    def merchants(self) -> list[str]:
        seen: list[str] = []
        for purchase in self.purchases:
            name = (purchase.merchant or "").strip()
            if name and name not in seen:
                seen.append(name)
        return seen


@dataclass
class Summary:
    folders: list[Folder] = field(default_factory=list)

    @property
    def total(self) -> Decimal:
        return sum((f.total for f in self.folders), ZERO)

    @property
    def count(self) -> int:
        return sum(f.count for f in self.folders)

    @property
    def lost(self) -> list[Folder]:
        return [f for f in self.folders if f.is_loss]

    @property
    def lost_total(self) -> Decimal:
        """Money spent chasing work we did not get.

        Reported on its own because it is the only spend in this system with
        nothing on the other side of it. It is not an error and not waste -
        it is the cost of bidding, and a business that does not know what that
        costs is guessing at the price of everything else.
        """
        return sum((f.total for f in self.lost), ZERO)

    @property
    def working_total(self) -> Decimal:
        return self.total - self.lost_total


def build(jobs: Iterable[Job]) -> Summary:
    """Folders, biggest spend first, with the losses kept identifiable.

    Ordered by amount rather than by job number: with a folder for nearly
    every job, the number is not what anybody is scanning for.
    """
    folders = [
        Folder(job=job, purchases=sorted(
            job.purchases,
            key=lambda p: (p.purchased_on or date.min, p.id or 0),
            reverse=True,
        ))
        for job in jobs
        if job.purchases
    ]
    folders.sort(key=lambda f: (-f.total, f.job.job_number))
    return Summary(folders=folders)
