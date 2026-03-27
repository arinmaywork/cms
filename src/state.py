"""
src/state.py
─────────────────────────────────────────────────────────────────────────────
Previously held in-memory queues and a watcher-started flag.

In the new architecture the watcher runs inside launch.py (a separate OS
process from Streamlit), so in-memory queues cannot be shared.  All queue
I/O now goes through src/file_queue.py.

This module is kept as a thin compatibility shim so no other imports break.
"""

# Nothing needed here in the new architecture.
# The UI reads directly from src.file_queue.pop_all().

