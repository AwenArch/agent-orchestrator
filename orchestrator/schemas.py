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
    path: str
    content: str


class CodeOut(BaseModel):
    files: list[FileOut]
    notes: str
