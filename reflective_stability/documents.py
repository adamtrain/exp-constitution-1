"""Fetch the source documents once, convert them to plain Markdown, and freeze them with hashes.

  documents/claude_constitution.md          https://www.anthropic.com/constitution
  documents/system_prompts/<model-id>.md    https://platform.claude.com/docs/en/release-notes/system-prompts/<model-id>

The system prompt files serve two roles: the document edited in the claude_system_prompt cells,
and the "in character" persona prepended when editing the constitution.
"""

import hashlib
import json
import re
import urllib.request
from datetime import date
from html.parser import HTMLParser

from .config import DOCUMENTS_DIR, MODELS

CONSTITUTION_URL = "https://www.anthropic.com/constitution"
SYSTEM_PROMPT_URL = "https://platform.claude.com/docs/en/release-notes/system-prompts/{model_id}"
MANIFEST = DOCUMENTS_DIR / "manifest.json"


def load_document(document_id: str, model_id: str) -> str:
    if document_id == "claude_constitution":
        return (DOCUMENTS_DIR / "claude_constitution.md").read_text()
    return load_persona(model_id)


def load_persona(model_id: str) -> str:
    return (DOCUMENTS_DIR / "system_prompts" / f"{model_id}.md").read_text()


def verify() -> None:
    """Refuse to run on documents that differ from the frozen (preregistered) versions."""
    if not MANIFEST.exists():
        raise SystemExit("No frozen documents. Run `uv run experiment fetch-documents` first.")
    for entry in json.loads(MANIFEST.read_text()).values():
        text = (DOCUMENTS_DIR / entry["path"]).read_text()
        if hashlib.sha256(text.encode()).hexdigest() != entry["sha256"]:
            raise SystemExit(f"{entry['path']} differs from the hash in documents/manifest.json.")


def fetch_all() -> None:
    if MANIFEST.exists():
        raise SystemExit("Documents are already frozen; delete documents/ to re-fetch (a deviation once data exists).")
    today = date.today()
    manifest = {}

    constitution = constitution_markdown(_get(CONSTITUTION_URL))
    manifest["claude_constitution"] = _save("claude_constitution.md", constitution, CONSTITUTION_URL, today)

    for model_id in MODELS.values():
        url = SYSTEM_PROMPT_URL.format(model_id=model_id)
        version, prompt = latest_system_prompt(_get(url + ".md"))
        # claude.ai fills in the date at serving time; we freeze it to the fetch date.
        prompt = prompt.replace("{{currentDateTime}}", today.strftime("%A, %B %-d, %Y"))
        manifest[model_id] = _save(f"system_prompts/{model_id}.md", prompt, url, today, version)

    MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n")


def _get(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "reflective-stability-replication"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read().decode()


def _save(relative_path: str, text: str, url: str, fetched: date, version: str | None = None) -> dict:
    path = DOCUMENTS_DIR / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return {
        "path": relative_path,
        "source": url,
        "version": version,
        "fetched": fetched.isoformat(),
        "sha256": hashlib.sha256(text.encode()).hexdigest(),
        "characters": len(text),
    }


def latest_system_prompt(page_markdown: str) -> tuple[str, str]:
    """Return (date heading, prompt text) for the newest entry on a docs system-prompt page.

    The docs split a prompt into several ```text blocks wherever the prompt itself contains a
    code fence; joining the blocks with a newline restores it.
    """
    sections = re.split(r"^## ", page_markdown, flags=re.MULTILINE)[1:]
    if not sections:
        raise ValueError("No dated system prompt found on the page.")
    heading, body = sections[0].split("\n", 1)
    blocks = re.findall(r"^```text[^\n]*\n(.*?)^```$", body, flags=re.MULTILINE | re.DOTALL)
    return heading.strip(), "\n".join(block.rstrip("\n") for block in blocks) + "\n"


def constitution_markdown(page_html: str) -> str:
    """Convert the constitution's article body (from the Overview heading on) to Markdown."""
    start = page_html.index('id="overview"')
    start = page_html.rindex("<h2", 0, start)
    end = page_html.index("</article>", start)
    converter = _ArticleToMarkdown()
    converter.feed(page_html[start:end])
    return converter.markdown()


class _ArticleToMarkdown(HTMLParser):
    BLOCKS = {"h2": "## ", "h3": "### ", "h4": "#### ", "p": "", "blockquote": "> "}
    SKIP = {"button", "svg", "nav", "aside", "script", "style"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks: list[str] = []
        self.current: list[str] | None = None
        self.lists: list[list] = []  # stack of [tag, item counter]
        self.skipping = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skipping += 1
        elif tag in ("ul", "ol"):
            self.lists.append([tag, 0])
        elif tag == "li":
            self._flush()  # text of an enclosing item that precedes a nested list
            self.lists[-1][1] += 1
            kind, n = self.lists[-1]
            indent = "  " * (len(self.lists) - 1)
            self.current = [indent + ("- " if kind == "ul" else f"{n}. ")]
        elif tag in self.BLOCKS and self.current is None:
            self.current = [self.BLOCKS[tag]]
        elif tag in ("strong", "b"):
            self._text("**")
        elif tag in ("em", "i"):
            self._text("*")
        elif tag == "br":
            self._text("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skipping -= 1
        elif tag in ("ul", "ol"):
            self.lists.pop()
        elif tag == "li" or (tag in self.BLOCKS and not (tag == "p" and self.lists)):
            self._flush()
        elif tag in ("strong", "b"):
            self._text("**")
        elif tag in ("em", "i"):
            self._text("*")

    def handle_data(self, data):
        if not self.skipping:
            self._text(re.sub(r"\s+", " ", data))

    def _text(self, text):
        if self.current is not None:
            self.current.append(text)

    def _flush(self):
        if self.current is not None:
            text = "".join(self.current).rstrip()
            if text.strip(" -#>*0123456789."):
                self.blocks.append(text)
        self.current = None

    def markdown(self) -> str:
        self._flush()
        return "\n\n".join(self.blocks) + "\n"
