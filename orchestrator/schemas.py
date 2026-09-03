"""The contract between us and the models. pydantic rejects any reply
that doesn't match these shapes."""
from pydantic import BaseModel


class Plan(BaseModel):
    summary: str
    files_to_change: list[str]
    files_to_create: list[str]
    test_file: str
    acceptance: list[str]


class FileOut(BaseModel):
    """A brand-new file, in full. Only for files_to_create."""
    path: str
    content: str


class EditBlock(BaseModel):
    """A single search/replace edit to an EXISTING file."""
    path: str
    search: str
    replace: str


class CodeOut(BaseModel):
    new_files: list[FileOut]
    edits: list[EditBlock]
    notes: str


class ReviewResult(BaseModel):
    """Reviewer agent verdict on the code as it currently stands on disk,
    BEFORE the (comparatively expensive) Godot validation cycle runs."""
    approve: bool
    issues: list[str]
