"""Read-only research tools: web search, source snapshots and paged PDF evidence."""
import base64
from copy import deepcopy
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import urllib.parse
import urllib.request

from .agent import DeepSeekClient
from .tools import write_json
from .prompts.catalog import DOCUMENT_VISION

TEXT_EXTENSIONS = {".txt", ".md", ".rst", ".html", ".htm", ".json", ".yaml", ".yml",
                   ".csv", ".tsv", ".xdc", ".v", ".sv", ".vh", ".svh", ".vhd", ".vhdl"}


class PageText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts, self.links, self.hidden = [], [], 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        if tag == "a":
            self.links.extend(value for name, value in attrs if name == "href")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        if tag in {"p", "div", "br", "tr", "h1", "h2", "li"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data + " ")


class Documents:
    def __init__(self, task, config):
        self.task, self.config = Path(task), config
        self.root = self.task / "sources"
        self.root.mkdir(exist_ok=True)
        index = self.root / "index.json"
        self.records = json.loads(index.read_text(encoding="utf-8")) if index.exists() else {}

    def save(self):
        write_json(self.root / "index.json", self.records)

    def inventory(self):
        return [{k: record[k] for k in ("id", "title", "origin", "sha256", "pages")}
                for record in self.records.values()]

    def add(self, source):
        parsed = urllib.parse.urlsplit(str(source))
        remote = parsed.scheme in {"http", "https"}
        limit = 25 * 1024 * 1024
        if remote:
            if not parsed.hostname or parsed.username or parsed.password:
                raise ValueError("Source URL must be HTTP(S), without embedded credentials")
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({})) if self.config["llm"].get("proxy_mode") == "direct" else urllib.request.build_opener()
            request = urllib.request.Request(str(source), headers={"User-Agent": "FPGAgent/1.0"})
            with opener.open(request, timeout=30) as response:
                data = response.read(limit + 1)
                source = response.geturl()
            title = Path(urllib.parse.unquote(urllib.parse.urlsplit(source).path)).name or "web-page"
        else:
            path = Path(source).resolve(strict=True)
            if path.suffix.lower() not in TEXT_EXTENSIONS | {".pdf"}:
                raise ValueError("Local sources must be PDF or document text files")
            if path.stat().st_size > limit:
                raise ValueError("Source exceeds 25 MiB")
            data, title, source = path.read_bytes(), path.name, str(path)
        if len(data) > limit:
            raise ValueError("Source exceeds 25 MiB")
        sha = hashlib.sha256(data).hexdigest()
        doc_id = "doc-" + sha[:16]
        if doc_id in self.records:
            return self.records[doc_id]
        pdf = data.startswith(b"%PDF-")
        snapshot = self.root / (doc_id + (".pdf" if pdf else ".txt"))
        links = []
        if pdf:
            import pymupdf
            with pymupdf.open(stream=data, filetype="pdf") as doc:
                if doc.needs_pass or not 1 <= len(doc) <= 2000:
                    raise ValueError("PDF is encrypted or has an unsupported page count")
                pages = [page.get_text(sort=True) for page in doc]
            snapshot.write_bytes(data)
        else:
            text = data.decode("utf-8-sig", errors="replace")
            if (remote or Path(title).suffix.lower() in {".html", ".htm"}) and re.search(r"<html|<!doctype", text, re.I):
                parser = PageText()
                parser.feed(text)
                text = "".join(parser.parts)
                links = [urllib.parse.urljoin(source, link) for link in parser.links][:80]
            pages = [text]
            snapshot.write_bytes(data)
        record = {"id": doc_id, "title": title, "origin": source, "sha256": sha, "pages": len(pages),
                  "pdf": pdf, "snapshot": snapshot.name, "links": links, "observed": {}, "vision": {}}
        write_json(self.root / (doc_id + "-pages.json"), {"pages": pages})
        self.records[doc_id] = record
        self.save()
        return record

    def pages(self, doc_id):
        if doc_id not in self.records:
            raise ValueError("Unknown source_id; use list_sources")
        return json.loads((self.root / (doc_id + "-pages.json")).read_text(encoding="utf-8"))["pages"]

    def search(self, query):
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 128:
            raise ValueError("Search query must contain 1..128 characters")
        client = DeepSeekClient(self.config)
        base = self.config["llm"]["base_url"].rstrip("/")
        endpoint = base + ("/tools/search" if base.endswith("/v1") else "/v1/tools/search")
        request = urllib.request.Request(endpoint, data=json.dumps({"query": query, "count": 5, "need_content": False}).encode(),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + client.key})
        with client._open_request(request) as response:
            data = json.load(response)
        return [{"title": r.get("title"), "url": r.get("url"), "summary": (r.get("summary") or r.get("snippet") or "")[:2000]}
                for r in data.get("results", [])[:5]]

    def find(self, query, source_id=None):
        terms = query.lower().split()
        if not terms:
            raise ValueError("A document search needs keywords")
        hits = []
        for doc_id in ([source_id] if source_id else self.records):
            for page, text in enumerate(self.pages(doc_id), 1):
                score = sum(text.lower().count(term) for term in terms)
                if score:
                    first = min(text.lower().find(term) for term in terms if term in text.lower())
                    hits.append({"source_id": doc_id, "page": page, "score": score, "excerpt": text[max(0, first-100):first+600]})
        return sorted(hits, key=lambda h: h["score"], reverse=True)[:12]

    def read(self, source_id, page, offset=0):
        pages = self.pages(source_id)
        if type(page) is not int or not 1 <= page <= len(pages) or type(offset) is not int or offset < 0:
            raise ValueError("page is 1-based; offset must be a nonnegative integer")
        text = pages[page-1][offset:offset+10000]
        self.records[source_id]["observed"].setdefault(str(page), []).append(text)
        self.save()
        return {"source_id": source_id, "page": page, "text": text,
                "next_offset": offset+len(text) if offset+len(text) < len(pages[page-1]) else None,
                "note": "Empty text may be a scanned page; inspect_page can read the rendered image"}

    def inspect(self, source_id, page, question):
        import pymupdf
        self.read(source_id, page)
        record = self.records[source_id]
        if not record["pdf"]:
            raise ValueError("inspect_page requires PDF")
        with pymupdf.open(self.root / record["snapshot"]) as doc:
            p = doc[page-1]
            scale = min(2, 1800 / max(p.rect.width, p.rect.height))
            image = p.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False).tobytes("png")
        (self.root / f"{source_id}-page-{page}.png").write_bytes(image)
        config = deepcopy(self.config)
        config["llm"]["model"] = config.get("research", {}).get("vision_model", "deepseek-flash")
        response = DeepSeekClient(config).complete([
            {"role": "system", "content": DOCUMENT_VISION},
            {"role": "user", "content": [{"type": "text", "text": question},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(image).decode()}}]}],
            self.task / "llm_calls.jsonl")
        record["vision"][str(page)] = response
        self.save()
        return {"source_id": source_id, "page": page, "visual_extraction": response}

    def evidence_text(self, citation):
        record = self.records.get(citation.get("source_id"))
        page = str(citation.get("page"))
        if not record or page not in record["observed"]:
            raise ValueError("Evidence must reference an actually read source page")
        text = "\n".join(record["observed"][page])
        if page in record["vision"]:
            text += "\n" + json.dumps(record["vision"][page], ensure_ascii=False)
        quote = citation.get("quote", "")
        normalize = lambda value: "".join(value.split()).casefold()
        if not isinstance(quote, str) or not quote.strip() or normalize(quote) not in normalize(text):
            raise ValueError("Evidence quote was not found in the observed text/visual extraction")
        return record
