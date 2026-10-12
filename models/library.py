"""A card library: a pool of lent cards, its policy, and who it is for.

The unit people contribute to. Somebody who deposits into Cube Night is
lending to Cube Night's members wherever they play, and to nobody else -- the
partition is the library, not the server. A library serves any number of
servers; a server draws on exactly one.

All of them live in ONE MTGO account, so the separation is bookkeeping rather
than physics: the account's vault holds every library's cards commingled, and
what keeps them apart is that a borrow may only draw against the ledger rows
attributed to its own library. See card_library_inventory for that arithmetic.
It is the reason a library cannot simply be told "lend whatever is in the
vault".

`kind` is the whole policy distinction:

  * communal -- the members' own cards, lent to each other. Deposits list and
    price themselves, because making somebody hand-price each cube puts an
    operator in the middle of a thing meant to need none.
  * rental -- somebody else's cards, stocked to replace a paid rental service.
    Cubes are listed by whoever runs the library, borrowing is invite-only
    until opened, and a deposit is expected.

The price lives here rather than per cube. One number per library is what
"policy at the library level" means: a server bound to a library is bound to
its terms, and there is no per-cube figure for anyone to argue about. A
library holding cubes of wildly different value is a thing to warn about at
deposit time, not to prevent -- somebody lending their own Power is entitled
to.

Two numbers, though, where a library wants them: the price asked of a borrower
it knows and the price asked of one it does not. That is the same policy set at
the same level, and the cube still has no say in it.

Set from a shell tool, never from Discord. A server admin can write
`configs/<guild>.json` through the bot's own commands, so anything they could
reach is a number they could lower.
"""
from datetime import datetime

from sqlalchemy import Column, DateTime, Integer, String, text

from database.models_base import Base

COMMUNAL = "communal"
RENTAL = "rental"
KINDS = (COMMUNAL, RENTAL)


class Library(Base):
    __tablename__ = 'library'

    # A short slug chosen by whoever creates it ("cubenight", "lotuslounge").
    # Readable on purpose: it is the scope custody rows are booked under and
    # the thing a shell tool is pointed at, and an opaque integer would make
    # both unreadable at exactly the moments they are being checked by hand.
    id = Column(String(64), primary_key=True)
    name = Column(String(128), nullable=False)
    kind = Column(String(16), nullable=False, default=RENTAL,
                  server_default=text("'rental'"))

    # What a borrower puts up, refunded when the deck comes back.
    collateral_tix = Column(Integer, nullable=False, default=0,
                            server_default=text('0'))

    # The same, for a borrower this library does not yet know: somebody who has
    # not drafted here enough to be a known quantity
    # (services/library_trust_service).
    #
    # NULL means the library is not tiered and charges everyone the figure
    # above, which is every library until somebody sets this -- so the column
    # arriving changed nothing for anybody.
    #
    # A second number rather than a multiplier because both are policy somebody
    # chose and both should be readable off the row. It does not weaken the "one
    # number per library" rule above: that rule is about there being no per-CUBE
    # price for anyone to argue over, and this is not a per-cube anything. What
    # it adds is an axis the rule never spoke to -- who is borrowing -- and a
    # borrower cannot move themselves along it except by drafting here.
    collateral_tix_untrusted = Column(Integer, nullable=True)

    created_at = Column(DateTime, default=datetime.now)
    created_by = Column(String(64), nullable=True)
