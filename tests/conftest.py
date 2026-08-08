"""conftest.py — what every test that opens a store has to agree on.

A store is opened for one inverter and stamps every row with its serial, so
each test has to name one. Naming it here rather than in twenty files is not
only tidiness: a test that quietly picked a different serial from the fixture
it reads back through would find no rows and look like a bug in the store.

The value is invented and shaped like a real EG4 serial. It is deliberately not
a serial from the reference installation — this repository is public, and the
pre-push scan has already caught a real dongle serial in two test files.
"""

from __future__ import annotations

TEST_DEVICE = "CE00000000"
