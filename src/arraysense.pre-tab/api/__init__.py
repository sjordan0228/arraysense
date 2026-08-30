"""arraysense/api/__init__.py — the HTTP surface: the application factory and its routes.

Nothing here talks to the inverter. Readings are served out of the store and the
one control the API offers, releasing the dongle, is handed to the collector to
carry out — so a request arriving mid-outage answers from history rather than
blocking on a socket that is not going to open.
"""
