"""`dugup mcp serve`: DugUp's read-only search as tools for an assistant, over MCP on standard input and output.

The rules (off by default, read-only, allowed folders only, passages not whole files) live in assistant.py;
this file only presents them through the MCP library (`pip install mcp`; the Windows installer includes it).
"""

from __future__ import annotations

from typing import Callable, List, Optional

from .assistant import Refused, Tools

INSTRUCTIONS = (
    "DugUp searches the files on this person's computer that they chose to index: by the words in them (including "
    "words read from pictures and scans), by what a passage means, or pictures by what they show. It is read-only. "
    "Results carry the path, the file's kind, size and date, and the passage that matched. Use search first; use "
    "passages for more of one file around some words; use status to see what is indexed and how far the "
    "sketches for searching by meaning and by looks have got. Only folders the person allowed are visible.")


def build(tools: Tools):
    """The MCP server over these tools (not started)."""
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
    from mcp.types import ToolAnnotations

    from . import __version__

    server = MCPServer("DugUp", instructions=INSTRUCTIONS, version=__version__)
    read_only = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True,
                                open_world_hint=False)

    def answer(call: Callable[[], dict]) -> dict:
        try:
            return call()
        except Refused as exc:
            raise ToolError(str(exc)) from None

    @server.tool(annotations=read_only)
    def search(query: str, how: str = "words", folder: Optional[str] = None, kinds: Optional[List[str]] = None,
               modified_after: Optional[str] = None, modified_before: Optional[str] = None, limit: int = 10) -> dict:
        """Search the person's indexed files.

        query: what to look for. With how="words", words (stems match: "educate" finds "education"; put a phrase
            in double quotes to find it as typed). With "meaning" or "both", a question or description in plain
            language. With "looks", a description of a picture ("a dog on a snowy porch").
        how: "words" (the words themselves, in text, file names and text read from pictures), "meaning" (what
            passages are about), "both" (words and meaning together; good for names plus a topic), or "looks"
            (pictures by what they show).
        folder: only files in this folder or below it (a full path).
        kinds: only these kinds of file: "pdf", "image", "text", "office", "other".
        modified_after, modified_before: dates written YYYY-MM-DD.
        limit: how many results (at most 50).
        """
        return answer(lambda: tools.search(query, how, folder, kinds or (), modified_after, modified_before, limit))

    @server.tool(annotations=read_only)
    def passages(path: str, words: str, limit: int = 5) -> dict:
        """More passages from one indexed file: the places where these words are found, a few hundred characters
        each, in reading order (at most 10). Never the whole file.

        path: the file's full path, as a search result gave it.
        words: the words to find in it.
        """
        return answer(lambda: tools.passages(path, words, limit))

    @server.tool(annotations=read_only)
    def status() -> dict:
        """What is indexed (the folders visible to assistants, with file counts), whether an update is running,
        and how far the sketches for searching by meaning and pictures by looks have got."""
        return answer(tools.status)

    return server


def serve(service, load_settings) -> None:
    """Serve until the assistant closes the connection."""
    tools = Tools(service, load_settings)
    try:
        build(tools).run("stdio")
    finally:
        tools.close()
