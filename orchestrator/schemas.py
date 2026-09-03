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
    """A brand-new file, in full. Only for files_to_create - an existing
    file being edited must never appear here (see EditBlock)."""
    path: str
    content: str


class EditBlock(BaseModel):
    """A single search/replace edit to an EXISTING file. `search` must
    match the file's current content exactly once; `replace` is what takes
    its place. Multiple EditBlocks may target the same path."""
    path: str
    search: str
    replace: str


class CodeOut(BaseModel):
    new_files: list[FileOut]
    edits: list[EditBlock]
    notes: str
