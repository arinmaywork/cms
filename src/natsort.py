"""
src/natsort.py — natural ("human") sort key.
'1.1.2' < '1.1.10' < '2.1.1', unlike plain lexicographic sorting where
'1.1.10' lands before '1.1.2'. Used for video/image ordering everywhere.
"""

import re


def natkey(s) -> list:
    return [int(t) if t.isdigit() else t.lower()
            for t in re.split(r"(\d+)", str(s))]
