"""One way to read PR numbers for every command."""
import re
from typing import Callable, List, Optional

from .staging import StagingError


def parse_numbers(tokens: List[str], all_open: Optional[Callable[[], List[int]]] = None) -> List[int]:
    """['13', '14,15', '20-22', '#30', 'all'] -> [13, 14, 15, 20, 21, 22, 30, ...] (no duplicates)."""
    numbers: List[int] = []
    for token in ",".join(tokens).split(","):
        token = token.strip().lstrip("#")
        if not token:
            continue
        if token == "all":
            if all_open is None:
                raise StagingError("'all' is not accepted here; list the PR numbers.")
            numbers.extend(all_open())
        elif re.fullmatch(r"\d+", token):
            numbers.append(int(token))
        else:
            m = re.fullmatch(r"(\d+)-(\d+)", token)
            if not m or int(m.group(1)) > int(m.group(2)) or int(m.group(2)) - int(m.group(1)) > 200:
                raise StagingError("not a valid PR number or range: " + token)
            numbers.extend(range(int(m.group(1)), int(m.group(2)) + 1))
    return list(dict.fromkeys(numbers))
