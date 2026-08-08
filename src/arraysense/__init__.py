"""arraysense/__init__.py — Solar ArraySense: monitoring for EG4 and LuxPower inverters.

Holds the version and nothing else. Importing the package therefore pulls in no
store, no collector and no FastAPI, so the CLI, the API's status endpoint and a
smoke test can all read ``__version__`` without opening a database or a socket.
"""

__version__ = "0.5.8"
