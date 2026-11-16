"""Feature gates for saved job formats. Public submissions default to V1."""
import re

RELEASE = "1.3.0"


def version(record):
    value = str(record.get("drafting_version", "0.11")).lower().removeprefix("v")
    if value in ("1", "1.0", "1.0.0", "1.0.1", "1.0.2"):
        return (1, 0)
    if value == "1.1.0":
        return (1, 1)
    if value == "1.2.0":
        return (1, 2)
    if value == RELEASE:
        return (1, 3)
    if re.fullmatch(r"0\.(?:[1-9]|1[0-5])", value):
        return (0, int(value.split(".")[1]))
    if value == "":  # Early standalone ledgers did not declare a format.
        return (0, 11)
    raise ValueError(f"Unsupported drafting_version {value!r}")
