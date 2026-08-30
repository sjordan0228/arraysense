"""arraysense/store/__init__.py — the persistence layer: schema, writes, rollups, tiers.

Data moves one way, collector to store to api, and nothing in this package may
import from ``arraysense.collector``. That one-way rule is what lets the store be
exercised, and one day swapped for another backend, with no inverter in reach.
"""
