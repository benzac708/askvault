from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Passage:
    """One retrieved chunk of a source document.

    `doc` + `section` together are the citation an answer must reference, which
    is why a passage is a section and not a whole document.
    """

    passage_id: str
    doc: str
    section: str
    text: str
    score: float
    snippet: str
