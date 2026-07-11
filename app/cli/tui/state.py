from dataclasses import dataclass


@dataclass
class TuiState:
    view: str = "dashboard"
    selected: int = 0
