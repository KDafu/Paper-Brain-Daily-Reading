#!/usr/bin/env python3
"""Daily paper digest and lightweight research graph builder.

The script intentionally starts with a low-dependency design: Python stdlib plus
requests when available. It can fetch arXiv metadata, score papers against a
research profile, maintain a SQLite cache, write a Markdown digest, and export a
static graph for doc/paper_brain/index.html.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import textwrap
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

try:
    import requests  # type: ignore
except Exception:  # pragma: no cover - requests is optional.
    requests = None


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = REPO_ROOT / "config" / "paper_watch.json"
DEFAULT_DB = REPO_ROOT / "data" / "paper_brain" / "papers.sqlite"
ARXIV_API = "http://export.arxiv.org/api/query"
ATOM_NS = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}


@dataclass
class Paper:
    paper_id: str
    source: str
    title: str
    abstract: str
    authors: list[str]
    published: str
    updated: str
    url: str
    pdf_url: str
    categories: list[str]
    topics: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    score: float = 0.0
    priority: str = "P2"
    reasons: list[str] = field(default_factory=list)
    summary: str = ""


def repo_path(path: str | Path) -> Path:
    p = Path(path)
    return p if p.is_absolute() else REPO_ROOT / p


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def ensure_dirs(config: dict[str, Any], db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    repo_path(config["digest"]["output_dir"]).mkdir(parents=True, exist_ok=True)
    repo_path(config["graph"]["output_dir"]).mkdir(parents=True, exist_ok=True)
    repo_path(config["graph"].get("pdf_cache_dir", "data/paper_brain/pdfs")).mkdir(parents=True, exist_ok=True)
    repo_path(config["graph"].get("text_cache_dir", "data/paper_brain/text")).mkdir(parents=True, exist_ok=True)
    repo_path(config["graph"].get("figure_cache_dir", "doc/paper_brain/figures")).mkdir(parents=True, exist_ok=True)
    quality_cfg = config.get("quality", {})
    repo_path(quality_cfg.get("deep_read_dir", "doc/paper_brain/deep_reads")).mkdir(parents=True, exist_ok=True)


def connect_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS papers (
            paper_id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            title TEXT NOT NULL,
            abstract TEXT NOT NULL,
            authors_json TEXT NOT NULL,
            published TEXT,
            updated TEXT,
            url TEXT,
            pdf_url TEXT,
            categories_json TEXT NOT NULL,
            topics_json TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            score REAL NOT NULL DEFAULT 0,
            priority TEXT NOT NULL DEFAULT 'P2',
            reasons_json TEXT NOT NULL,
            summary TEXT NOT NULL DEFAULT '',
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL
        )
        """
    )
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(papers)").fetchall()}
    if "metadata_json" not in existing_cols:
        conn.execute("ALTER TABLE papers ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}'")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS edges (
            src TEXT NOT NULL,
            dst TEXT NOT NULL,
            relation TEXT NOT NULL,
            weight REAL NOT NULL DEFAULT 1,
            evidence TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (src, dst, relation)
        )
        """
    )
    conn.commit()
    return conn


def normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(text or "")).strip()


def slugify(text: str, max_len: int = 90) -> str:
    text = normalize_whitespace(text).lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text[:max_len].strip("-") or "paper"


def arxiv_id_from_url(url: str) -> str:
    tail = url.rstrip("/").split("/")[-1]
    return tail.replace("v1", "").replace("v2", "").replace("v3", "").replace("v4", "")


def http_get(url: str, params: dict[str, Any] | None = None, timeout: int = 35) -> str:
    if params:
        query = urllib.parse.urlencode(params, doseq=True)
        url = f"{url}?{query}"
    headers = {"User-Agent": "paper-brain/0.1 (daily research digest)"}
    if requests is not None:
        try:
            response = requests.get(url, headers=headers, timeout=timeout)
            response.raise_for_status()
            return response.text
        except Exception:
            # Some proxy stacks fail in requests but work through urllib. Fall through.
            pass

    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read().decode("utf-8")


def build_arxiv_query(config: dict[str, Any], term: str) -> str:
    categories = config["sources"]["arxiv"].get("categories", [])
    cat_query = " OR ".join(f"cat:{cat}" for cat in categories)
    return f"all:{term} AND ({cat_query})" if cat_query else f"all:{term}"


def parse_arxiv_feed(xml_text: str) -> list[Paper]:
    root = ET.fromstring(xml_text)
    papers: list[Paper] = []
    for entry in root.findall("atom:entry", ATOM_NS):
        id_url = normalize_whitespace(entry.findtext("atom:id", default="", namespaces=ATOM_NS))
        title = normalize_whitespace(entry.findtext("atom:title", default="", namespaces=ATOM_NS))
        abstract = normalize_whitespace(entry.findtext("atom:summary", default="", namespaces=ATOM_NS))
        published = normalize_whitespace(entry.findtext("atom:published", default="", namespaces=ATOM_NS))
        updated = normalize_whitespace(entry.findtext("atom:updated", default="", namespaces=ATOM_NS))
        authors = [
            normalize_whitespace(author.findtext("atom:name", default="", namespaces=ATOM_NS))
            for author in entry.findall("atom:author", ATOM_NS)
        ]
        categories = [
            cat.attrib.get("term", "")
            for cat in entry.findall("atom:category", ATOM_NS)
            if cat.attrib.get("term")
        ]
        pdf_url = ""
        for link in entry.findall("atom:link", ATOM_NS):
            if link.attrib.get("title") == "pdf" or link.attrib.get("type") == "application/pdf":
                pdf_url = link.attrib.get("href", "")
                break
        if not pdf_url and id_url:
            pdf_url = id_url.replace("/abs/", "/pdf/")
        if title:
            papers.append(
                Paper(
                    paper_id=f"arxiv:{arxiv_id_from_url(id_url)}",
                    source="arxiv",
                    title=title,
                    abstract=abstract,
                    authors=[a for a in authors if a],
                    published=published,
                    updated=updated,
                    url=id_url,
                    pdf_url=pdf_url,
                    categories=categories,
                )
            )
    return papers


def fetch_arxiv(config: dict[str, Any], offline: bool = False) -> list[Paper]:
    arxiv_cfg = config["sources"]["arxiv"]
    if offline or not arxiv_cfg.get("enabled", True):
        return []

    papers_by_id: dict[str, Paper] = {}
    for idx, term in enumerate(arxiv_cfg.get("query_terms", [])):
        query = build_arxiv_query(config, term)
        params = {
            "search_query": query,
            "start": 0,
            "max_results": int(arxiv_cfg.get("max_results_per_query", 10)),
            "sortBy": "submittedDate",
            "sortOrder": "descending",
        }
        try:
            xml_text = http_get(arxiv_cfg.get("api_url", ARXIV_API), params=params)
            for paper in parse_arxiv_feed(xml_text):
                papers_by_id[paper.paper_id] = paper
        except (urllib.error.URLError, TimeoutError, ET.ParseError, RuntimeError, Exception) as exc:
            print(f"[warn] arXiv fetch failed for term {term!r}: {exc}", file=sys.stderr)
        if idx < len(arxiv_cfg.get("query_terms", [])) - 1:
            time.sleep(float(arxiv_cfg.get("request_delay_seconds", 3)))
    return list(papers_by_id.values())


def fetch_semantic_scholar(config: dict[str, Any], offline: bool = False) -> list[Paper]:
    ss_cfg = config.get("sources", {}).get("semantic_scholar", {})
    if offline or not ss_cfg.get("enabled", False):
        return []

    params = {
        "query": ss_cfg.get("query", ""),
        "limit": int(ss_cfg.get("limit", 20)),
        "fields": ss_cfg.get(
            "fields",
            "title,abstract,authors,year,url,externalIds,publicationDate,openAccessPdf,fieldsOfStudy,citationCount,referenceCount,influentialCitationCount",
        ),
    }
    headers = {"User-Agent": "paper-brain/0.1"}
    api_key = os.environ.get("SEMANTIC_SCHOLAR_API_KEY")
    if api_key:
        headers["x-api-key"] = api_key

    url = ss_cfg.get("api_url", "https://api.semanticscholar.org/graph/v1/paper/search")
    try:
        if requests is not None:
            response = requests.get(url, params=params, headers=headers, timeout=35)
            response.raise_for_status()
            payload = response.json()
        else:
            query = urllib.parse.urlencode(params)
            req = urllib.request.Request(f"{url}?{query}", headers=headers)
            with urllib.request.urlopen(req, timeout=35) as response:
                payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        print(f"[warn] Semantic Scholar fetch failed: {exc}", file=sys.stderr)
        return []

    papers: list[Paper] = []
    for item in payload.get("data", []):
        title = normalize_whitespace(item.get("title", ""))
        if not title:
            continue
        paper_id = item.get("paperId") or slugify(title)
        external_ids = item.get("externalIds") or {}
        arxiv_id = external_ids.get("ArXiv")
        url_value = item.get("url") or (f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else "")
        pdf_url = ""
        if isinstance(item.get("openAccessPdf"), dict):
            pdf_url = item["openAccessPdf"].get("url") or ""
        authors = [normalize_whitespace(author.get("name", "")) for author in item.get("authors", [])]
        categories = item.get("fieldsOfStudy") or []
        papers.append(
            Paper(
                paper_id=f"semantic-scholar:{paper_id}",
                source="semantic_scholar",
                title=title,
                abstract=normalize_whitespace(item.get("abstract", "")),
                authors=[a for a in authors if a],
                published=str(item.get("publicationDate") or item.get("year") or ""),
                updated=str(item.get("publicationDate") or item.get("year") or ""),
                url=url_value,
                pdf_url=pdf_url,
                categories=categories,
                metadata={
                    "citation_count": item.get("citationCount"),
                    "reference_count": item.get("referenceCount"),
                    "influential_citation_count": item.get("influentialCitationCount"),
                    "external_ids": external_ids,
                },
            )
        )
        time.sleep(float(ss_cfg.get("request_delay_seconds", 0)))
    return papers


def fetch_github(config: dict[str, Any], offline: bool = False) -> list[Paper]:
    gh_cfg = config.get("sources", {}).get("github", {})
    if offline or not gh_cfg.get("enabled", False):
        return []

    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "paper-brain/0.1",
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    papers_by_id: dict[str, Paper] = {}
    for idx, term in enumerate(gh_cfg.get("query_terms", [])):
        params = {
            "q": term,
            "sort": gh_cfg.get("sort", "updated"),
            "order": gh_cfg.get("order", "desc"),
            "per_page": int(gh_cfg.get("max_results_per_query", 6)),
        }
        try:
            if requests is not None:
                response = requests.get(gh_cfg.get("api_url", "https://api.github.com/search/repositories"), params=params, headers=headers, timeout=35)
                response.raise_for_status()
                payload = response.json()
            else:
                query = urllib.parse.urlencode(params)
                req = urllib.request.Request(f"{gh_cfg.get('api_url', 'https://api.github.com/search/repositories')}?{query}", headers=headers)
                with urllib.request.urlopen(req, timeout=35) as response:
                    payload = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            print(f"[warn] GitHub fetch failed for term {term!r}: {exc}", file=sys.stderr)
            continue

        for item in payload.get("items", []):
            full_name = item.get("full_name") or item.get("name") or ""
            if not full_name:
                continue
            description = normalize_whitespace(item.get("description") or "")
            topics = item.get("topics") or []
            title = full_name
            abstract = description or "GitHub repository discovered by the Paper Brain."
            papers_by_id[f"github:{full_name.lower()}"] = Paper(
                paper_id=f"github:{full_name.lower()}",
                source="github",
                title=title,
                abstract=abstract,
                authors=[(item.get("owner") or {}).get("login", "")],
                published=item.get("created_at", ""),
                updated=item.get("updated_at", ""),
                url=item.get("html_url", ""),
                pdf_url="",
                categories=["github_repo", *topics],
                topics=split_keywords(" ".join(topics)),
                metadata={
                    "stars": item.get("stargazers_count"),
                    "forks": item.get("forks_count"),
                    "language": item.get("language"),
                    "github_topics": topics,
                    "is_code_repo": True,
                },
            )
        if idx < len(gh_cfg.get("query_terms", [])) - 1:
            time.sleep(float(gh_cfg.get("request_delay_seconds", 1)))
    return list(papers_by_id.values())


def fetch_google_scholar_serpapi(config: dict[str, Any], offline: bool = False) -> list[Paper]:
    scholar_cfg = config.get("sources", {}).get("google_scholar", {})
    if offline or not scholar_cfg.get("enabled", False):
        return []
    if scholar_cfg.get("provider") != "serpapi":
        print("[warn] Google Scholar source only supports provider='serpapi'.", file=sys.stderr)
        return []
    api_key = os.environ.get("SERPAPI_API_KEY")
    if not api_key:
        print("[warn] Google Scholar via SerpAPI skipped; SERPAPI_API_KEY is not set.", file=sys.stderr)
        return []

    papers_by_id: dict[str, Paper] = {}
    for idx, term in enumerate(scholar_cfg.get("query_terms", [])):
        params = {
            "engine": "google_scholar",
            "q": term,
            "num": int(scholar_cfg.get("num", 10)),
            "api_key": api_key,
        }
        if scholar_cfg.get("as_ylo"):
            params["as_ylo"] = scholar_cfg.get("as_ylo")
        try:
            if requests is not None:
                response = requests.get(scholar_cfg.get("api_url", "https://serpapi.com/search"), params=params, timeout=45)
                response.raise_for_status()
                payload = response.json()
            else:
                query = urllib.parse.urlencode(params)
                req = urllib.request.Request(f"{scholar_cfg.get('api_url', 'https://serpapi.com/search')}?{query}")
                with urllib.request.urlopen(req, timeout=45) as response:
                    payload = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            print(f"[warn] Google Scholar fetch failed for term {term!r}: {exc}", file=sys.stderr)
            continue

        for item in payload.get("organic_results", []):
            title = normalize_whitespace(item.get("title") or "")
            if not title:
                continue
            publication = item.get("publication_info") or {}
            authors = []
            for author in publication.get("authors") or []:
                if author.get("name"):
                    authors.append(author["name"])
            cited_by = item.get("inline_links", {}).get("cited_by", {}) if isinstance(item.get("inline_links"), dict) else {}
            paper = Paper(
                paper_id=f"google-scholar:{slugify(title)}",
                source="google_scholar",
                title=title,
                abstract=normalize_whitespace(item.get("snippet") or ""),
                authors=authors,
                published=str(item.get("year") or ""),
                updated=str(item.get("year") or ""),
                url=item.get("link", ""),
                pdf_url=(item.get("resources") or [{}])[0].get("link", "") if item.get("resources") else "",
                categories=["google_scholar"],
                metadata={
                    "citation_count": cited_by.get("total"),
                    "cited_by_link": cited_by.get("link"),
                    "publication_summary": publication.get("summary"),
                },
            )
            papers_by_id[paper.paper_id] = paper
        if idx < len(scholar_cfg.get("query_terms", [])) - 1:
            time.sleep(float(scholar_cfg.get("request_delay_seconds", 1)))
    return list(papers_by_id.values())


def title_from_pdf_filename(path: Path) -> str:
    known = {
        "2305": "NavGPT: Explicit Reasoning in Vision-and-Language Navigation with Large Language Models",
        "2310": "NoMaD: Goal Masked Diffusion Policies for Navigation and Exploration",
        "2312": "VLFM: Vision-Language Frontier Maps for Zero-Shot Semantic Navigation",
        "2412": "Uni-NaVid: A Video-based Vision-Language-Action Model for Unifying Embodied Navigation Tasks",
        "2502": "MapNav: Annotated Semantic Maps for Vision-and-Language Navigation",
        "GPT4Scene": "GPT4Scene: Understand 3D Scenes from Videos with Vision-Language Models",
    }
    return known.get(path.stem, path.stem.replace("_", " ").replace("-", " ").strip())


def relative_or_absolute(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def cached_figure_previews_exist(previews: Any) -> bool:
    if not isinstance(previews, list) or not previews:
        return False
    for preview in previews:
        if not isinstance(preview, dict):
            return False
        src = preview.get("src")
        if not src:
            return False
        path = repo_path(str(src))
        if not path.exists() or path.stat().st_size <= 0:
            return False
    return True


def preview_error(message: str, *, stage: str = "unknown") -> dict[str, str]:
    return {
        "stage": stage,
        "message": normalize_whitespace(message)[:500],
        "at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }


def recent_figure_preview_failure(metadata: dict[str, Any], config: dict[str, Any]) -> bool:
    preview_cfg = config.get("graph", {}).get("figure_preview", {})
    extractor_version = str(preview_cfg.get("extractor_version", "v2"))
    if metadata.get("figure_preview_extractor_version") and metadata.get("figure_preview_extractor_version") != extractor_version:
        return False
    cooldown_days = int(preview_cfg.get("failure_cooldown_days", 14))
    if cooldown_days <= 0:
        return False
    failed_at = metadata.get("figure_preview_failed_at")
    if not failed_at:
        return False
    try:
        failed_dt = dt.datetime.fromisoformat(str(failed_at))
    except ValueError:
        return False
    if failed_dt.tzinfo is None:
        failed_dt = failed_dt.replace(tzinfo=dt.timezone.utc)
    return dt.datetime.now(dt.timezone.utc) - failed_dt < dt.timedelta(days=cooldown_days)


def extract_pdf_text(path: Path, text_cache_dir: Path, max_chars: int) -> tuple[str, Path | None]:
    text_cache_dir.mkdir(parents=True, exist_ok=True)
    out_path = text_cache_dir / f"{slugify(path.stem)}.txt"
    if out_path.exists() and out_path.stat().st_size > 0:
        text = out_path.read_text(encoding="utf-8", errors="ignore")
        return normalize_whitespace(text[:max_chars]), out_path

    pdftotext = shutil.which("pdftotext")
    if not pdftotext:
        return "", None
    try:
        subprocess.run(
            [pdftotext, "-layout", "-enc", "UTF-8", str(path), str(out_path)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=45,
        )
    except Exception as exc:
        print(f"[warn] PDF text extraction failed for {path}: {exc}", file=sys.stderr)
        return "", None
    text = out_path.read_text(encoding="utf-8", errors="ignore")
    return normalize_whitespace(text[:max_chars]), out_path


def ensure_pdf_text_cache(paper: Paper, config: dict[str, Any]) -> None:
    if paper.metadata.get("has_fulltext") and paper.metadata.get("text_cache_path"):
        return
    pdf_path: Path | None = None
    local_pdf = paper.metadata.get("local_pdf_path")
    if local_pdf:
        pdf_path = repo_path(local_pdf)
    else:
        cached_pdf = repo_path(config["graph"].get("pdf_cache_dir", "data/paper_brain/pdfs")) / f"{slugify(paper.paper_id)}.pdf"
        if cached_pdf.exists() and cached_pdf.stat().st_size > 2048:
            pdf_path = cached_pdf
        elif paper.pdf_url:
            pdf_path = download_pdf_preview_source(paper, config)
    if not pdf_path or not pdf_path.exists():
        return
    text_cache_dir = repo_path(config["graph"].get("text_cache_dir", "data/paper_brain/text"))
    max_chars = int(config.get("sources", {}).get("local_pdf", {}).get("max_text_chars", 5000))
    extracted, text_path = extract_pdf_text(pdf_path, text_cache_dir, max_chars)
    if extracted and text_path:
        paper.abstract = paper.abstract or extracted[:1200]
        paper.metadata["text_cache_path"] = relative_or_absolute(text_path)
        paper.metadata["has_fulltext"] = True


def download_pdf_preview_source(paper: Paper, config: dict[str, Any]) -> Path | None:
    pdf_url = paper.pdf_url or ""
    if not pdf_url.startswith(("http://", "https://")):
        return None
    preview_cfg = config.get("graph", {}).get("figure_preview", {})
    max_bytes = int(preview_cfg.get("max_pdf_download_mb", 60)) * 1024 * 1024
    max_seconds = float(preview_cfg.get("download_timeout_seconds", 45))
    cache_dir = repo_path(config["graph"].get("pdf_cache_dir", "data/paper_brain/pdfs"))
    cache_dir.mkdir(parents=True, exist_ok=True)
    out_path = cache_dir / f"{slugify(paper.paper_id)}.pdf"
    if out_path.exists() and out_path.stat().st_size > 2048:
        return out_path
    tmp_path = out_path.with_suffix(".pdf.part")
    try:
        headers = {"User-Agent": "paper-brain/0.1"}
        started = time.monotonic()
        if requests is not None:
            with requests.get(pdf_url, headers=headers, timeout=(10, 20), stream=True) as response:
                response.raise_for_status()
                written = 0
                with tmp_path.open("wb") as f:
                    for chunk in response.iter_content(chunk_size=1024 * 256):
                        if not chunk:
                            continue
                        written += len(chunk)
                        if written > max_bytes:
                            raise RuntimeError(f"PDF exceeds download limit ({max_bytes // (1024 * 1024)} MB)")
                        if time.monotonic() - started > max_seconds:
                            raise TimeoutError(f"PDF download exceeded {max_seconds:.0f}s")
                        f.write(chunk)
        else:
            req = urllib.request.Request(pdf_url, headers=headers)
            with urllib.request.urlopen(req, timeout=20) as response, tmp_path.open("wb") as f:
                written = 0
                while True:
                    chunk = response.read(1024 * 256)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > max_bytes:
                        raise RuntimeError(f"PDF exceeds download limit ({max_bytes // (1024 * 1024)} MB)")
                    if time.monotonic() - started > max_seconds:
                        raise TimeoutError(f"PDF download exceeded {max_seconds:.0f}s")
                    f.write(chunk)
        tmp_path.replace(out_path)
    except Exception as exc:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        print(f"[warn] PDF preview download failed for {paper.title!r}: {exc}", file=sys.stderr)
        return None
    return out_path if out_path.exists() and out_path.stat().st_size > 2048 else None


def render_pdf_previews(pdf_path: Path, paper: Paper, config: dict[str, Any]) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    preview_cfg = config.get("graph", {}).get("figure_preview", {})
    if not preview_cfg.get("enabled", True):
        return [], []
    pdfimages = shutil.which("pdfimages")
    pdftoppm = shutil.which("pdftoppm")
    if not pdfimages or not pdf_path.exists():
        return [], [preview_error("pdfimages is unavailable or PDF path does not exist", stage="setup")]
    figure_dir = repo_path(config["graph"].get("figure_cache_dir", "doc/paper_brain/figures"))
    figure_dir.mkdir(parents=True, exist_ok=True)
    max_figures = int(preview_cfg.get("max_figures_per_paper", 3))
    max_candidate_pages = int(preview_cfg.get("max_candidate_pages_per_paper", max(4, max_figures * 2)))
    extraction_timeout = int(preview_cfg.get("extraction_timeout_seconds", 12))
    min_width = int(preview_cfg.get("min_extracted_width", 420))
    min_height = int(preview_cfg.get("min_extracted_height", 260))
    min_area = int(preview_cfg.get("min_extracted_area", 180000))
    allow_page_fallback = bool(preview_cfg.get("page_snapshot_fallback", True))
    page_fallback_dpi = int(preview_cfg.get("page_snapshot_dpi", 120))
    previews: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []
    try:
        listed = subprocess.run(
            [pdfimages, "-list", str(pdf_path)],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=45,
        ).stdout.splitlines()
    except Exception as exc:
        print(f"[warn] PDF image listing failed for {pdf_path}: {exc}", file=sys.stderr)
        errors.append(preview_error(str(exc), stage="list"))
        return [], errors

    candidates: list[dict[str, int | str]] = []
    for line in listed:
        parts = line.split()
        if len(parts) < 5 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        try:
            page = int(parts[0])
            num = int(parts[1])
            image_type = parts[2].lower()
            width = int(parts[3])
            height = int(parts[4])
        except ValueError:
            continue
        if image_type != "image":
            continue
        area = width * height
        aspect = width / max(1, height)
        if width < min_width or height < min_height or area < min_area:
            continue
        if aspect > 8 or aspect < 0.15:
            continue
        candidates.append({"page": page, "num": num, "width": width, "height": height, "area": area})
    if not candidates:
        errors.append(preview_error("No embedded image met minimum size/aspect constraints", stage="candidate_filter"))
        if allow_page_fallback and pdftoppm:
            slug = slugify(paper.paper_id)
            fallback_pages = list(range(1, max(1, max_candidate_pages) + 1))
            for page in fallback_pages:
                if len(previews) >= max_figures:
                    break
                out_prefix = figure_dir / f"{slug}-page-fallback-p{page:03d}"
                try:
                    subprocess.run(
                        [pdftoppm, "-f", str(page), "-l", str(page), "-r", str(page_fallback_dpi), "-png", str(pdf_path), str(out_prefix)],
                        check=True,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=extraction_timeout,
                    )
                except Exception as exc:
                    errors.append(preview_error(f"page {page}: {exc}", stage="page_snapshot_fallback"))
                    continue
                rendered = sorted(figure_dir.glob(f"{out_prefix.name}-*.png"))
                source_path = max(rendered, key=lambda path: path.stat().st_size, default=None)
                if not source_path or not source_path.exists() or source_path.stat().st_size <= 0:
                    errors.append(preview_error(f"page {page}: pdftoppm produced no image", stage="page_snapshot_fallback"))
                    continue
                final_path = figure_dir / f"{slug}-fig-{len(previews) + 1}.png"
                try:
                    shutil.copyfile(source_path, final_path)
                except OSError as exc:
                    errors.append(preview_error(str(exc), stage="page_snapshot_copy"))
                    continue
                previews.append(
                    {
                        "src": relative_or_absolute(final_path),
                        "caption": f"PDF page snapshot fallback · page {page} · needs manual figure verification",
                        "page": str(page),
                        "kind": "page_snapshot_fallback",
                        "needs_verification": "true",
                    }
                )
            for tmp_file in figure_dir.glob(f"{slug}-page-fallback-p*"):
                if re.search(r"-\d+\.png$", tmp_file.name):
                    try:
                        tmp_file.unlink()
                    except OSError:
                        pass
        return previews, errors

    candidates.sort(key=lambda item: (int(item["area"]), -int(item["page"])), reverse=True)
    slug = slugify(paper.paper_id)
    for pattern in (f"{slug}-fig-*", f"{slug}-extract-p*"):
        for old_file in figure_dir.glob(pattern):
            try:
                old_file.unlink()
            except OSError:
                pass

    used_sources: set[Path] = set()
    attempted_pages: set[int] = set()
    extracted_by_page: dict[int, list[Path]] = {}
    tmp_prefixes: set[str] = set()
    try:
        for item in candidates:
            if len(previews) >= max_figures:
                break
            page = int(item["page"])
            num = int(item["num"])
            if page not in extracted_by_page:
                if page not in attempted_pages and len(attempted_pages) >= max_candidate_pages:
                    continue
                page_prefix = figure_dir / f"{slug}-extract-p{page:03d}"
                tmp_prefixes.add(page_prefix.name)
                for old_file in figure_dir.glob(f"{page_prefix.name}-*"):
                    try:
                        old_file.unlink()
                    except OSError:
                        pass
                attempted_pages.add(page)
                try:
                    subprocess.run(
                        [pdfimages, "-all", "-p", "-f", str(page), "-l", str(page), str(pdf_path), str(page_prefix)],
                        check=True,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=extraction_timeout,
                    )
                except Exception as exc:
                    print(f"[warn] PDF image extraction failed for {pdf_path} page {page}: {exc}", file=sys.stderr)
                    errors.append(preview_error(f"page {page}: {exc}", stage="embedded_extract"))
                    extracted_by_page[page] = []
                    continue
                extracted_by_page[page] = sorted(
                    path
                    for path in figure_dir.glob(f"{page_prefix.name}-{page:03d}-*")
                    if path.is_file() and path.suffix.lower() in {".png", ".jpg", ".jpeg", ".jp2", ".ppm", ".pbm", ".pgm"}
                )

            extracted = extracted_by_page.get(page, [])
            exact = [path for path in extracted if re.search(rf"-{num:03d}\.[^.]+$", path.name)]
            remaining = [path for path in (exact or extracted) if path.exists() and path not in used_sources]
            source_path = max(remaining, key=lambda path: path.stat().st_size, default=None)
            if not source_path or not source_path.exists():
                continue
            if source_path.stat().st_size <= 0:
                continue
            suffix = source_path.suffix.lower() if source_path.suffix.lower() in {".png", ".jpg", ".jpeg", ".jp2", ".ppm", ".pbm", ".pgm"} else ".png"
            final_path = figure_dir / f"{slug}-fig-{len(previews) + 1}{suffix}"
            try:
                shutil.copyfile(source_path, final_path)
            except OSError:
                continue
            used_sources.add(source_path)
            previews.append(
                {
                    "src": relative_or_absolute(final_path),
                    "caption": f"PDF embedded figure · page {page} · {int(item['width'])}x{int(item['height'])}",
                    "page": str(page),
                    "kind": "embedded_figure",
                }
            )
        if not previews and allow_page_fallback and pdftoppm:
            fallback_pages = []
            for item in candidates:
                page = int(item["page"])
                if page not in fallback_pages:
                    fallback_pages.append(page)
                if len(fallback_pages) >= max_candidate_pages:
                    break
            for page in fallback_pages:
                if len(previews) >= max_figures:
                    break
                out_prefix = figure_dir / f"{slug}-page-fallback-p{page:03d}"
                try:
                    subprocess.run(
                        [pdftoppm, "-f", str(page), "-l", str(page), "-r", str(page_fallback_dpi), "-png", str(pdf_path), str(out_prefix)],
                        check=True,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=extraction_timeout,
                    )
                except Exception as exc:
                    errors.append(preview_error(f"page {page}: {exc}", stage="page_snapshot_fallback"))
                    continue
                rendered = sorted(figure_dir.glob(f"{out_prefix.name}-*.png"))
                source_path = max(rendered, key=lambda path: path.stat().st_size, default=None)
                if not source_path or not source_path.exists() or source_path.stat().st_size <= 0:
                    errors.append(preview_error(f"page {page}: pdftoppm produced no image", stage="page_snapshot_fallback"))
                    continue
                final_path = figure_dir / f"{slug}-fig-{len(previews) + 1}.png"
                try:
                    shutil.copyfile(source_path, final_path)
                except OSError as exc:
                    errors.append(preview_error(str(exc), stage="page_snapshot_copy"))
                    continue
                used_sources.add(source_path)
                previews.append(
                    {
                        "src": relative_or_absolute(final_path),
                        "caption": f"PDF page snapshot fallback · page {page} · needs manual figure verification",
                        "page": str(page),
                        "kind": "page_snapshot_fallback",
                        "needs_verification": "true",
                    }
                )
    finally:
        for prefix_name in tmp_prefixes:
            for tmp_file in figure_dir.glob(f"{prefix_name}-*"):
                try:
                    tmp_file.unlink()
                except OSError:
                    pass
        for tmp_file in figure_dir.glob(f"{slug}-page-fallback-p*"):
            if re.search(r"-\d+\.png$", tmp_file.name):
                try:
                    tmp_file.unlink()
                except OSError:
                    pass
    if not previews and not errors:
        errors.append(preview_error("No preview image could be produced", stage="unknown"))
    return previews, errors


def attach_figure_previews(papers: list[Paper], config: dict[str, Any]) -> None:
    preview_cfg = config.get("graph", {}).get("figure_preview", {})
    if not preview_cfg.get("enabled", True):
        return
    max_papers = int(preview_cfg.get("max_papers_per_run", 10))
    max_external = int(preview_cfg.get("max_external_downloads_per_run", 4))
    external_downloads = 0
    candidates = sorted(
        [paper for paper in papers if paper.source != "github" and paper.source != "seed"],
        key=lambda p: (p.metadata.get("has_fulltext", False), p.score),
        reverse=True,
    )
    extraction_attempts = 0
    for paper in candidates:
        ensure_pdf_text_cache(paper, config)
        if cached_figure_previews_exist(paper.metadata.get("figure_previews")):
            continue
        if recent_figure_preview_failure(paper.metadata, config):
            continue
        if extraction_attempts >= max_papers:
            break
        pdf_path: Path | None = None
        local_pdf = paper.metadata.get("local_pdf_path")
        if local_pdf:
            pdf_path = repo_path(local_pdf)
        elif preview_cfg.get("download_external_pdfs", False) and external_downloads < max_external:
            pdf_path = download_pdf_preview_source(paper, config)
            if pdf_path:
                external_downloads += 1
        if not pdf_path:
            continue
        extraction_attempts += 1
        previews, preview_errors = render_pdf_previews(pdf_path, paper, config)
        if previews:
            paper.metadata["figure_previews"] = previews
            paper.metadata.pop("figure_preview_failed_at", None)
            paper.metadata["figure_preview_extractor_version"] = str(preview_cfg.get("extractor_version", "v2"))
            if preview_errors:
                paper.metadata["figure_preview_errors"] = preview_errors[-5:]
            else:
                paper.metadata.pop("figure_preview_errors", None)
        else:
            paper.metadata["figure_preview_failed_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
            paper.metadata["figure_preview_extractor_version"] = str(preview_cfg.get("extractor_version", "v2"))
            paper.metadata["figure_preview_errors"] = preview_errors[-5:] if preview_errors else [preview_error("No figure preview produced", stage="unknown")]


def local_pdf_papers(config: dict[str, Any]) -> list[Paper]:
    pdf_cfg = config.get("sources", {}).get("local_pdf", {})
    if not pdf_cfg.get("enabled", False):
        return []

    text_cache_dir = repo_path(config["graph"].get("text_cache_dir", "data/paper_brain/text"))
    max_chars = int(pdf_cfg.get("max_text_chars", 5000))
    papers: list[Paper] = []
    for raw_path in pdf_cfg.get("paths", []):
        path = repo_path(raw_path)
        files = [path] if path.is_file() else sorted(path.glob("*.pdf")) if path.exists() else []
        for pdf_path in files:
            title = title_from_pdf_filename(pdf_path)
            extracted = ""
            text_path = None
            if pdf_cfg.get("extract_text", True):
                extracted, text_path = extract_pdf_text(pdf_path, text_cache_dir, max_chars)
            rel_pdf = relative_or_absolute(pdf_path)
            papers.append(
                Paper(
                    paper_id=f"local-pdf:{slugify(rel_pdf)}",
                    source="local_pdf",
                    title=title,
                    abstract=extracted or "Local PDF imported into the Paper Brain.",
                    authors=[],
                    published="",
                    updated="",
                    url=rel_pdf,
                    pdf_url=rel_pdf,
                    categories=["local_pdf"],
                    metadata={
                        "local_pdf_path": rel_pdf,
                        "text_cache_path": relative_or_absolute(text_path) if text_path else "",
                        "has_fulltext": bool(extracted),
                    },
                )
            )
    return papers


def quality_config(config: dict[str, Any]) -> dict[str, Any]:
    return config.get("quality", {})


def deep_read_slug(paper: Paper) -> str:
    source = slugify(paper.source, 24)
    pid = slugify(paper.paper_id.replace(":", "-"), 48)
    title = slugify(paper.title, 64)
    return f"{source}-{pid}-{title}".strip("-")


def load_deep_read_index(config: dict[str, Any]) -> dict[str, Any]:
    path = repo_path(quality_config(config).get("deep_read_index", "doc/paper_brain/deep_read_index.json"))
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception as exc:
        print(f"[warn] Could not read deep-read index {path}: {exc}", file=sys.stderr)
        return {}


def deep_read_entry_for(paper: Paper, config: dict[str, Any], index: dict[str, Any] | None = None) -> dict[str, Any]:
    idx = index if index is not None else load_deep_read_index(config)
    entries = idx.get("papers", idx)
    if not isinstance(entries, dict):
        entries = {}
    entry = entries.get(paper.paper_id) or entries.get(slugify(paper.title)) or {}
    return entry if isinstance(entry, dict) else {}


def deep_read_file_path(paper: Paper, config: dict[str, Any], entry: dict[str, Any] | None = None) -> Path:
    quality_cfg = quality_config(config)
    deep_dir = repo_path(quality_cfg.get("deep_read_dir", "doc/paper_brain/deep_reads"))
    if entry and entry.get("path"):
        return repo_path(entry["path"])
    return deep_dir / f"{deep_read_slug(paper)}.md"


def deep_read_file_exists(paper: Paper, config: dict[str, Any], entry: dict[str, Any] | None = None) -> bool:
    return deep_read_file_path(paper, config, entry).exists()


def deep_read_file_is_draft(paper: Paper, config: dict[str, Any], entry: dict[str, Any] | None = None) -> bool:
    path = deep_read_file_path(paper, config, entry)
    if not path.exists():
        return False
    try:
        head = path.read_text(encoding="utf-8", errors="ignore")[:1200].lower()
    except Exception:
        return True
    return "status: draft template" in head or "do not mark as `deep_read`" in head


def quality_assessment(paper: Paper, config: dict[str, Any], index: dict[str, Any] | None = None) -> dict[str, Any]:
    quality_cfg = quality_config(config)
    entry = deep_read_entry_for(paper, config, index)
    has_deep_file = deep_read_file_exists(paper, config, entry)
    is_draft_file = deep_read_file_is_draft(paper, config, entry)
    has_fulltext = bool(
        paper.metadata.get("has_fulltext")
        or paper.metadata.get("text_cache_path")
        or entry.get("has_fulltext")
        or entry.get("text_cache_path")
    )
    figures = entry.get("figure_previews") or paper.metadata.get("figure_previews")
    figure_count = len(figures) if isinstance(figures, list) else 0
    has_unverified_fallback = any(isinstance(fig, dict) and fig.get("kind") == "page_snapshot_fallback" for fig in figures or [])
    verified_figures = bool(entry.get("figure_verified"))
    evidence_items = entry.get("evidence_items", [])
    if isinstance(evidence_items, int):
        evidence_count = evidence_items
    elif isinstance(evidence_items, list):
        evidence_count = len(evidence_items)
    else:
        evidence_count = 0
    min_evidence = int(quality_cfg.get("min_evidence_items", 4))
    require_fulltext = bool(quality_cfg.get("require_fulltext_for_deep_read", True))
    require_figures = bool(quality_cfg.get("require_verified_figures_for_deep_read", True))

    gaps: list[str] = []
    if not has_deep_file:
        gaps.append("missing_deep_read_file")
    elif is_draft_file:
        gaps.append("draft_deep_read_template")
    if require_fulltext and not has_fulltext:
        gaps.append("missing_fulltext")
    if require_figures and not verified_figures:
        gaps.append("unverified_page_snapshot" if has_unverified_fallback else "missing_verified_figure")
    if evidence_count < min_evidence:
        gaps.append(f"insufficient_evidence:{evidence_count}/{min_evidence}")

    if has_deep_file and not is_draft_file and not gaps:
        status = "deep_read"
    elif has_deep_file and not is_draft_file and evidence_count > 0:
        status = "quick_checked"
    elif paper.source == "seed":
        status = "needs_deep_read"
    else:
        status = quality_cfg.get("default_existing_status", "needs_deep_read")

    score = 0
    score += 35 if has_deep_file and not is_draft_file else 0
    score += 20 if has_fulltext else 0
    score += 20 if verified_figures else 0
    score += min(25, int((evidence_count / max(1, min_evidence)) * 25))
    if status == "deep_read":
        score = max(score, 90)
    elif status == "quick_checked":
        score = min(score, 79)
    else:
        score = min(score, 49)

    return {
        "status": status,
        "score": score,
        "gaps": gaps,
        "deep_read_path": relative_or_absolute(deep_read_file_path(paper, config, entry)),
        "has_deep_read_file": has_deep_file,
        "is_draft_file": is_draft_file,
        "has_fulltext": has_fulltext,
        "figure_count": figure_count,
        "has_unverified_fallback": has_unverified_fallback,
        "figure_verified": verified_figures,
        "evidence_count": evidence_count,
        "min_evidence": min_evidence,
        "strict_mode": bool(quality_cfg.get("strict_mode", True)),
        "deep_read_entry": entry,
    }


def quality_status_label(status: str) -> str:
    return {
        "auto_imported": "自动导入",
        "needs_deep_read": "待精读",
        "quick_checked": "快读校验",
        "deep_read": "已精读",
    }.get(status, status or "待精读")


def seed_papers(config: dict[str, Any]) -> list[Paper]:
    papers: list[Paper] = []
    for seed in config["graph"].get("seed_papers", []):
        title = seed["title"]
        topics = seed.get("topics", [])
        abstract = "Seed paper or project node for this research map."
        if seed.get("is_project"):
            abstract = seed.get("abstract", "This is the project anchor for your research graph.")
        papers.append(
            Paper(
                paper_id=f"seed:{slugify(title)}",
                source="seed",
                title=title,
                abstract=abstract,
                authors=[],
                published=str(seed.get("year", "")),
                updated=str(seed.get("year", "")),
                url=seed.get("url", ""),
                pdf_url="",
                categories=[],
                topics=topics,
            )
        )
    return papers


def split_markdown_row(line: str) -> list[str]:
    line = line.strip()
    if not (line.startswith("|") and line.endswith("|")):
        return []
    cells = [normalize_whitespace(cell) for cell in line.strip("|").split("|")]
    return cells


def extract_first_url(text: str) -> str:
    match = re.search(r"https?://[^\s)]+", text)
    return match.group(0) if match else ""


def split_keywords(text: str) -> list[str]:
    text = re.sub(r"`([^`]+)`", r"\1", text)
    parts = re.split(r"[,，/、;；]+", text)
    return [normalize_whitespace(part) for part in parts if normalize_whitespace(part)]


def local_matrix_papers(config: dict[str, Any]) -> list[Paper]:
    matrix_cfg = config.get("sources", {}).get("local_reading_matrix", {})
    if not matrix_cfg.get("enabled", True):
        return []
    path = repo_path(matrix_cfg.get("path", "doc/reading_matrix.md"))
    if not path.exists():
        return []

    papers: list[Paper] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        cells = split_markdown_row(line)
        if len(cells) < 5 or not re.fullmatch(r"\d+", cells[0]):
            continue
        title = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", cells[1]).strip()
        if not title or title.lower() in {"工作", "paper"}:
            continue
        year = cells[2]
        keywords = split_keywords(cells[3])
        url = extract_first_url(cells[4])
        relevance = cells[5] if len(cells) > 5 else ""
        abstract = relevance or "Imported from the local VLN/VLA/OVMM SOTA reading matrix."
        papers.append(
            Paper(
                paper_id=f"matrix:{slugify(title)}",
                source="matrix",
                title=title,
                abstract=abstract,
                authors=[],
                published=year,
                updated=year,
                url=url,
                pdf_url="",
                categories=[],
                topics=keywords,
            )
        )
    return papers


TOPIC_PATTERNS: list[tuple[str, list[str]]] = [
    ("VLN", ["vision-and-language navigation", "vision language navigation", "vln", "navigation instruction"]),
    ("VLA", ["vision-language-action", "vision language action", "vla", "robotics transformer", "action model"]),
    ("OVMM", ["open-vocabulary mobile manipulation", "open vocabulary mobile manipulation", "ovmm"]),
    ("Mobile Manipulation", ["mobile manipulation", "mobile manipulator"]),
    ("Active Perception", ["active perception", "next-best-view", "next best view", "active vision"]),
    ("Semantic Mapping", ["semantic map", "semantic mapping", "language-grounded map", "annotated semantic"]),
    ("Scene Graph", ["scene graph", "3d scene graph", "object graph"]),
    ("Object Memory", ["object memory", "object-centric memory", "memory map", "object state"]),
    ("Frontier Exploration", ["frontier", "exploration", "explore"]),
    ("Affordance", ["affordance", "grasp point", "keypoint", "value map"]),
    ("Legged Robot Navigation", ["legged robot", "quadruped", "go2", "locomotion"]),
    ("Manipulation", ["manipulation", "grasp", "gripper", "pick-and-place", "pick and place"]),
    ("Robot Foundation Model", ["foundation model", "generalist robot", "robot policy", "embodied foundation"]),
    ("Embodied AI", ["embodied", "agent", "sim-to-real", "sim2real"]),
    ("Task-Semantic Object State", ["task state", "object state", "manipulation-ready", "state modeling"]),
    ("Wrist Reacquisition", ["wrist", "reacquire", "re-acquire", "hand camera", "eye-in-hand"]),
]

TOPIC_DOMAINS: dict[str, dict[str, str]] = {
    "Embodied Intelligence": {
        "description_zh": "具身智能关注语言、视觉、地图、记忆和动作如何在真实或仿真机器人任务中闭环。",
        "description_en": "Embodied intelligence connects language, perception, mapping, memory, and action in robot tasks.",
    },
    "Navigation": {
        "description_zh": "导航方向关注远场目标搜索、路径/前沿选择、拓扑记忆和语言指令执行。",
        "description_en": "Navigation covers long-range target search, frontier or waypoint choice, topology, and instruction following.",
    },
    "Manipulation": {
        "description_zh": "操作方向关注抓取、可达性、技能边界、动作生成和 manipulation-ready 状态确认。",
        "description_en": "Manipulation covers grasping, reachability, skill boundaries, action generation, and manipulation-ready checks.",
    },
    "Scene Memory": {
        "description_zh": "场景记忆方向关注语义地图、对象记忆、3D 场景图和长期环境状态更新。",
        "description_en": "Scene memory covers semantic maps, object memory, 3D scene graphs, and long-horizon environment state.",
    },
    "Active Perception": {
        "description_zh": "主动感知方向关注下一最佳视角、重观察、可见性刷新和任务信息增益。",
        "description_en": "Active perception covers next-best-view, re-observation, visibility freshness, and task information gain.",
    },
    "Robot Foundation Models": {
        "description_zh": "机器人基础模型方向关注 VLA、通用策略、模型服务边界和结构化动作输出。",
        "description_en": "Robot foundation models cover VLA, generalist policies, model-serving boundaries, and structured action outputs.",
    },
    "System & Benchmark": {
        "description_zh": "系统与基准方向关注平台集成、评测任务、sim2real、数据集和开源复现。",
        "description_en": "Systems and benchmarks cover platform integration, evaluation, sim2real, datasets, and reproducibility.",
    },
}

TOPIC_PARENT: dict[str, str] = {
    "VLN": "Navigation",
    "ObjectNav": "Navigation",
    "Frontier Exploration": "Navigation",
    "Waypoint Policy": "Navigation",
    "Topological Map": "Navigation",
    "Legged Robot Navigation": "Navigation",
    "OVMM": "Manipulation",
    "Mobile Manipulation": "Manipulation",
    "Manipulation": "Manipulation",
    "Affordance": "Manipulation",
    "Wrist Reacquisition": "Manipulation",
    "Semantic Mapping": "Scene Memory",
    "Scene Graph": "Scene Memory",
    "Object Memory": "Scene Memory",
    "Task-Semantic Object State": "Scene Memory",
    "Active Perception": "Active Perception",
    "VLA": "Robot Foundation Models",
    "Robot Foundation Model": "Robot Foundation Models",
    "Policy Server": "Robot Foundation Models",
    "Embodied AI": "Embodied Intelligence",
    "Benchmark": "System & Benchmark",
}

TOPIC_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "VLN": {
        "zh": "Vision-and-Language Navigation：机器人根据自然语言指令和视觉观测进行长程导航，常涉及路径记忆、进度监控和拓扑图。",
        "en": "Vision-and-Language Navigation asks an agent to follow natural-language instructions using visual observations, often with memory and topology.",
    },
    "VLA": {
        "zh": "Vision-Language-Action：把视觉和语言输入映射到动作、技能或结构化控制接口，是当前机器人基础模型的核心方向。",
        "en": "Vision-Language-Action maps visual and language inputs into actions, skills, or structured control interfaces.",
    },
    "OVMM": {
        "zh": "Open-Vocabulary Mobile Manipulation：开放词汇找物、导航、确认和操作的组合任务，常用于连接语义感知、移动和操作。",
        "en": "Open-vocabulary mobile manipulation combines open-set object search, navigation, confirmation, and manipulation.",
    },
    "Active Perception": {
        "zh": "主动感知研究如何选择下一视角或观察动作，以提升目标可见性、状态置信度和任务成功率。",
        "en": "Active perception chooses viewpoints or observation actions to improve visibility, state confidence, and task success.",
    },
    "Semantic Mapping": {
        "zh": "语义地图把空间位置和对象/语言语义绑定，是远场找物到近场确认之间的关键状态层。",
        "en": "Semantic mapping binds spatial locations to object and language semantics, bridging long-range search and local confirmation.",
    },
    "Scene Graph": {
        "zh": "场景图用对象、关系和属性组织 3D 环境，适合表达长期任务状态和对象记忆。",
        "en": "Scene graphs organize objects, relations, and attributes in 3D scenes, supporting long-horizon state and object memory.",
    },
    "Object Memory": {
        "zh": "对象记忆维护目标身份、位置、新鲜度和失败历史，可帮助 selected target lock 和重观察策略。",
        "en": "Object memory tracks identity, location, freshness, and failures for target locking and re-observation.",
    },
    "Mobile Manipulation": {
        "zh": "移动操作连接底盘导航和机械臂执行，核心难点是停车位姿、可达性、视角和安全门控耦合。",
        "en": "Mobile manipulation couples base navigation and arm execution, with parking pose, reachability, viewpoint, and safety gates.",
    },
    "Robot Foundation Model": {
        "zh": "机器人基础模型关注跨任务泛化的策略/世界模型/动作模型，工程上要特别注意服务边界和输出约束。",
        "en": "Robot foundation models target cross-task generalization, with special attention to service boundaries and output constraints.",
    },
    "Legged Robot Navigation": {
        "zh": "四足导航关注地形、稳定性、机体姿态和长程目标搜索，与轮式移动操作有明显平台差异。",
        "en": "Legged robot navigation adds terrain, stability, body posture, and platform constraints beyond wheeled navigation.",
    },
}

TOP_VENUE_PATTERNS: list[tuple[str, str]] = [
    ("CVPR", "CVPR"),
    ("ICCV", "ICCV"),
    ("ECCV", "ECCV"),
    ("NeurIPS", "NeurIPS"),
    ("ICLR", "ICLR"),
    ("ICML", "ICML"),
    ("CoRL", "CoRL"),
    ("RSS", "RSS"),
    ("ICRA", "ICRA"),
    ("IROS", "IROS"),
    ("RA-L", "RA-L"),
    ("Science Robotics", "Science Robotics"),
]

TOP_LAB_PATTERNS: list[tuple[str, str, str]] = [
    ("Stanford", "Stanford", "university"),
    ("UC Berkeley", "UC Berkeley", "university"),
    ("Berkeley", "UC Berkeley", "university"),
    ("MIT", "MIT", "university"),
    ("CMU", "Carnegie Mellon University", "university"),
    ("Carnegie Mellon", "Carnegie Mellon University", "university"),
    ("Princeton", "Princeton", "university"),
    ("Georgia Tech", "Georgia Tech", "university"),
    ("ETH Zurich", "ETH Zurich", "university"),
    ("Tsinghua", "Tsinghua University", "university"),
    ("Peking University", "Peking University", "university"),
    ("Shanghai Jiao Tong", "Shanghai Jiao Tong University", "university"),
    ("Google DeepMind", "Google DeepMind", "industry lab"),
    ("DeepMind", "Google DeepMind", "industry lab"),
    ("Google Research", "Google Research", "industry lab"),
    ("NVIDIA", "NVIDIA", "industry lab"),
    ("Meta AI", "Meta AI", "industry lab"),
    ("FAIR", "Meta FAIR", "industry lab"),
    ("Microsoft Research", "Microsoft Research", "industry lab"),
    ("Toyota Research Institute", "Toyota Research Institute", "industry lab"),
    ("Boston Dynamics", "Boston Dynamics", "industry lab"),
]


def find_topics(title: str, abstract: str, seed_topics: Iterable[str] | None = None) -> list[str]:
    text = f"{title} {abstract[:1200]}".lower()
    topics = list(dict.fromkeys(seed_topics or []))
    for topic, patterns in TOPIC_PATTERNS:
        if any(pattern.lower() in text for pattern in patterns) and topic not in topics:
            topics.append(topic)
    return topics


def topic_parent(topic: str) -> str:
    return TOPIC_PARENT.get(topic, "Embodied Intelligence")


def topic_description(topic: str) -> dict[str, str]:
    if topic in TOPIC_DESCRIPTIONS:
        return TOPIC_DESCRIPTIONS[topic]
    parent = topic_parent(topic)
    parent_desc = TOPIC_DOMAINS.get(parent, TOPIC_DOMAINS["Embodied Intelligence"])
    return {
        "zh": f"{topic} 属于 {parent} 方向。{parent_desc['description_zh']}",
        "en": f"{topic} belongs to {parent}. {parent_desc['description_en']}",
    }


def infer_prestige_tags(paper: Paper) -> dict[str, Any]:
    text = " ".join(
        [
            paper.title,
            paper.abstract[:2400],
            " ".join(paper.authors),
            str(paper.metadata.get("publication_summary", "")),
            " ".join(paper.categories),
            " ".join(str(topic) for topic in paper.metadata.get("github_topics", []) or []),
            str(paper.url),
        ]
    )
    venue_tags: list[str] = []
    lab_tags: list[dict[str, str]] = []
    for pattern, label in TOP_VENUE_PATTERNS:
        if re.search(rf"(?<![A-Za-z0-9]){re.escape(pattern)}(?:\s*\d{{4}})?(?![A-Za-z0-9])", text, flags=re.IGNORECASE):
            venue_tags.append(label)
    for pattern, label, kind in TOP_LAB_PATTERNS:
        if re.search(rf"(?<![A-Za-z0-9]){re.escape(pattern)}(?![A-Za-z0-9])", text, flags=re.IGNORECASE):
            lab_tags.append({"label": label, "kind": kind})
    if paper.source == "github":
        owner = (paper.authors or [""])[0].lower()
        owner_map = {
            "facebookresearch": ("Meta FAIR", "industry lab"),
            "google-deepmind": ("Google DeepMind", "industry lab"),
            "google-research": ("Google Research", "industry lab"),
            "nvidia": ("NVIDIA", "industry lab"),
            "stanford": ("Stanford", "university"),
            "mit": ("MIT", "university"),
            "cmu": ("Carnegie Mellon University", "university"),
        }
        if owner in owner_map:
            label, kind = owner_map[owner]
            lab_tags.append({"label": label, "kind": kind})
    unique_labs: list[dict[str, str]] = []
    seen_labs: set[str] = set()
    for lab in lab_tags:
        if lab["label"] not in seen_labs:
            unique_labs.append(lab)
            seen_labs.add(lab["label"])
    unique_venues = list(dict.fromkeys(venue_tags))
    return {
        "venue_tags": unique_venues[:3],
        "lab_tags": unique_labs[:3],
        "is_top_venue": bool(unique_venues),
        "is_top_lab": bool(unique_labs),
    }


def score_paper(paper: Paper, config: dict[str, Any]) -> Paper:
    profile = config["profile"]
    text = f"{paper.title} {paper.abstract} {' '.join(paper.categories)}".lower()
    score = 0.0
    reasons: list[str] = []

    for term in profile.get("core_topics", []):
        if term.lower() in text:
            score += 2.0
            reasons.append(f"core:{term}")

    for term in profile.get("must_include_any", []):
        if term.lower() in text:
            score += 1.0

    for term, weight in profile.get("boost_terms", {}).items():
        if term.lower() in text:
            score += float(weight)
            reasons.append(f"+{weight}:{term}")

    for term, weight in profile.get("negative_terms", {}).items():
        if term.lower() in text:
            score += float(weight)
            reasons.append(f"{weight}:{term}")

    topics = find_topics(paper.title, paper.abstract, paper.topics)
    score += min(len(topics), 6) * 1.5
    if paper.source == "seed":
        score += 15
        reasons.append("seed/project graph anchor")
    if paper.metadata.get("has_fulltext"):
        score += 2
        reasons.append("local fulltext available")
    if isinstance(paper.metadata.get("citation_count"), int):
        score += min(5, math.log1p(paper.metadata["citation_count"]))
        reasons.append(f"citations:{paper.metadata['citation_count']}")
    prestige = infer_prestige_tags(paper)
    if prestige["is_top_venue"]:
        score += 2.5
        reasons.append("top venue signal")
    if prestige["is_top_lab"]:
        score += 2.0
        reasons.append("top lab signal")

    paper.topics = topics
    paper.score = round(score, 2)
    paper.priority = "P0" if score >= 18 else "P1" if score >= 10 else "P2"
    paper.reasons = reasons[:8]
    paper.metadata.update(prestige)
    paper.summary = heuristic_summary(paper)
    return paper


def heuristic_summary(paper: Paper) -> str:
    text = paper.abstract or "No abstract available."
    sentences = re.split(r"(?<=[.!?])\s+", text)
    compact = " ".join(sentences[:2]).strip()
    if not compact:
        compact = text[:360]
    compact = compact[:520].rstrip()
    if len(compact) < len(text):
        compact += "..."
    return compact


def analysis_for_paper(paper: Paper, config: dict[str, Any] | None = None, quality: dict[str, Any] | None = None) -> dict[str, Any]:
    topics = set(paper.topics)
    title = paper.title
    summary = paper.summary or heuristic_summary(paper)
    quality = quality or (quality_assessment(paper, config) if config else {})
    deep_entry = quality.get("deep_read_entry") if isinstance(quality, dict) else {}
    if not isinstance(deep_entry, dict):
        deep_entry = {}

    innovations: list[str] = []
    mechanisms: list[str] = []
    limitations: list[str] = []
    relevance: list[str] = []

    if paper.source == "github":
        language = paper.metadata.get("language")
        stars = paper.metadata.get("stars")
        repo_note = "开源代码仓库"
        if language:
            repo_note += f" · {language}"
        if stars is not None:
            repo_note += f" · {stars} stars"
        innovations.append("提供可复现实验代码、系统模块或数据处理流程，适合追踪论文工作流落地方式。")
        relevance.append("可作为研究系统实现参考，重点看依赖、接口边界、模型模块和部署假设。")
        mechanisms.append("优先抽取 README 中的输入输出、运行脚本、模型权重、ROS/仿真接口和评测流程。")
        limitations.append("GitHub 仓库不等同于论文贡献，需要回链到论文、project page 或技术报告后再做学术判断。")
        if not summary or summary == "GitHub repository discovered by the Paper Brain.":
            summary = repo_note

    if "VLN" in topics:
        innovations.append("把语言指令、视觉观测和导航记忆结合，用于长程目标搜索或路径决策。")
        relevance.append("可对齐到远场语义导航、目标搜索和语言条件决策。")
        mechanisms.append("关注 waypoint/frontier/topological map 等中层导航输出，而不是直接底层控制。")
        limitations.append("标准 VLN 通常不处理腕部相机确认、机械臂工作空间和抓取授权。")
    if "VLA" in topics:
        innovations.append("将视觉-语言输入映射到动作、候选技能或中层控制接口。")
        relevance.append("适合作为近场 affordance、候选点或 typed skill proposer。")
        mechanisms.append("将模型输出限制为 action chunk、waypoint、候选点或结构化 schema 后再执行。")
        limitations.append("不能直接等同于真实机器人全链路安全控制。")
    if "Active Perception" in topics or "Frontier Exploration" in topics:
        innovations.append("主动选择观察位置或视角，以提升目标可见性和任务信息增益。")
        relevance.append("可对应重观察、下一最佳视角、目标重确认和失败恢复。")
        mechanisms.append("可转化为 viewpoint value、failure penalty、visibility freshness 等评分项。")
        limitations.append("多数工作没有显式建模真实平台的可达性、稳定性和安全约束耦合。")
    if "Semantic Mapping" in topics or "Object Memory" in topics or "Scene Graph" in topics:
        innovations.append("用结构化地图、对象记忆或场景图替代原始历史帧堆叠。")
        relevance.append("支撑 task-semantic object state 和 selected target lock。")
        mechanisms.append("把 map-level evidence、object identity、freshness 和失败记忆写入状态层。")
        limitations.append("地图级语义证据不能单独授权近场抓取，需要 wrist/depth/workspace 门控。")
    if "Mobile Manipulation" in topics or "OVMM" in topics or "Manipulation" in topics:
        innovations.append("把导航、找物和操作连接成开放词汇移动操作任务。")
        relevance.append("与开放词汇找物、靠近、确认和抓取链路高度相关。")
        mechanisms.append("可借鉴模块化 skill boundary、semantic target grounding 和 manipulation readiness。")
        limitations.append("现有系统常依赖特定平台或室内基准，迁移前需要复核平台假设。")

    if paper.source == "local_pdf":
        relevance.append("本地已有 PDF 全文，可优先做函数级/方法级精读。")
    if not innovations:
        innovations.append("该节点来自阅读矩阵或外部元数据，需要进一步精读确认真实贡献。")
    if not relevance:
        relevance.append("需要判断它是否影响你的研究问题、系统接口或实验设计。")
    if not mechanisms:
        mechanisms.append("优先抽取其输入/输出 schema、状态表示、候选生成和执行门控。")
    if not limitations:
        limitations.append("迁移到真实系统前，需要检查坐标系、实时性、安全门控和平台假设。")

    questions = [
        "它如何表示任务状态、对象记忆或语义地图？",
        "模型/策略输出是否被限制为 typed skill、waypoint、候选点或 value map？",
        "它能否帮助你的项目形成更清晰的状态表示、模型接口或评估协议？",
        "它的失败处理或 uncertainty 机制能否写回主动感知策略？",
    ]

    def localized_value(key: str, lang: str) -> Any:
        i18n = deep_entry.get("i18n")
        if isinstance(i18n, dict):
            lang_payload = i18n.get(lang)
            if isinstance(lang_payload, dict) and key in lang_payload:
                return lang_payload.get(key)
        lang_key = f"{key}_{lang}"
        if lang_key in deep_entry:
            return deep_entry.get(lang_key)
        if lang == "en":
            return deep_entry.get(key)
        return None

    def localized_string(key: str, lang: str) -> str:
        value = localized_value(key, lang)
        if value is None and key == "one_line":
            value = localized_value("summary", lang)
        if isinstance(value, str):
            return normalize_whitespace(value)
        return ""

    def localized_list(key: str, lang: str, fallback: list[str], limit: int) -> list[str]:
        value = localized_value(key, lang)
        if isinstance(value, str) and value.strip():
            return [normalize_whitespace(value)]
        if isinstance(value, list):
            cleaned = [normalize_whitespace(str(item)) for item in value if normalize_whitespace(str(item))]
            if cleaned:
                return cleaned[:limit]
        if lang != "en":
            en_value = localized_list(key, "en", fallback, limit)
            if en_value:
                return en_value
        return list(dict.fromkeys(fallback))[:limit]

    entry_summary = localized_string("summary", "en") or localized_string("one_line", "en")
    if entry_summary:
        summary = entry_summary

    def entry_list(key: str, fallback: list[str], limit: int) -> list[str]:
        value = deep_entry.get(key)
        if isinstance(value, str) and value.strip():
            return [normalize_whitespace(value)]
        if isinstance(value, list):
            cleaned = [normalize_whitespace(str(item)) for item in value if normalize_whitespace(str(item))]
            if cleaned:
                return cleaned[:limit]
        return list(dict.fromkeys(fallback))[:limit]

    analysis = {
        "one_line": summary,
        "innovations": entry_list("innovations", innovations, 4),
        "relevance": entry_list("relevance", relevance, 4),
        "mechanisms": entry_list("mechanisms", mechanisms, 4),
        "limitations": entry_list("limitations", limitations, 3),
        "questions": entry_list("questions", questions, 4),
        "topics": paper.topics,
        "reason_tags": paper.reasons,
        "evidence_items": entry_list("evidence_items", [], 8),
        "verified_figures": entry_list("verified_figures", [], 6),
        "figure_notes": entry_list("figure_notes", [], 6),
        "source_note": f"{paper.source} node for {title}",
        "quality_status": quality.get("status", "needs_deep_read"),
        "quality_score": quality.get("score", 0),
        "quality_gaps": quality.get("gaps", []),
        "quality_warning": "" if quality.get("status") == "deep_read" else "该总结仍处于自动导入/待精读状态，需用全文证据复核后才能作为精读结论。",
    }
    analysis["i18n"] = {
        "en": {
            "one_line": localized_string("summary", "en") or localized_string("one_line", "en") or analysis["one_line"],
            "innovations": localized_list("innovations", "en", analysis["innovations"], 4),
            "relevance": localized_list("relevance", "en", analysis["relevance"], 4),
            "mechanisms": localized_list("mechanisms", "en", analysis["mechanisms"], 4),
            "limitations": localized_list("limitations", "en", analysis["limitations"], 3),
            "questions": localized_list("questions", "en", analysis["questions"], 4),
            "evidence_items": localized_list("evidence_items", "en", analysis["evidence_items"], 8),
            "verified_figures": localized_list("verified_figures", "en", analysis["verified_figures"], 6),
            "figure_notes": localized_list("figure_notes", "en", analysis["figure_notes"], 6),
        },
        "zh": {
            "one_line": localized_string("summary", "zh") or localized_string("one_line", "zh") or analysis["one_line"],
            "innovations": localized_list("innovations", "zh", analysis["innovations"], 4),
            "relevance": localized_list("relevance", "zh", analysis["relevance"], 4),
            "mechanisms": localized_list("mechanisms", "zh", analysis["mechanisms"], 4),
            "limitations": localized_list("limitations", "zh", analysis["limitations"], 3),
            "questions": localized_list("questions", "zh", analysis["questions"], 4),
            "evidence_items": localized_list("evidence_items", "zh", analysis["evidence_items"], 8),
            "verified_figures": localized_list("verified_figures", "zh", analysis["verified_figures"], 6),
            "figure_notes": localized_list("figure_notes", "zh", analysis["figure_notes"], 6),
        },
    }
    return analysis


def upsert_papers(conn: sqlite3.Connection, papers: list[Paper]) -> None:
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    for paper in papers:
        conn.execute(
            """
            INSERT INTO papers (
                paper_id, source, title, abstract, authors_json, published, updated,
                url, pdf_url, categories_json, topics_json, metadata_json, score, priority,
                reasons_json, summary, first_seen, last_seen
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(paper_id) DO UPDATE SET
                source=excluded.source,
                title=excluded.title,
                abstract=excluded.abstract,
                authors_json=excluded.authors_json,
                published=excluded.published,
                updated=excluded.updated,
                url=excluded.url,
                pdf_url=excluded.pdf_url,
                categories_json=excluded.categories_json,
                topics_json=excluded.topics_json,
                metadata_json=excluded.metadata_json,
                score=excluded.score,
                priority=excluded.priority,
                reasons_json=excluded.reasons_json,
                summary=excluded.summary,
                last_seen=excluded.last_seen
            """,
            (
                paper.paper_id,
                paper.source,
                paper.title,
                paper.abstract,
                json.dumps(paper.authors, ensure_ascii=False),
                paper.published,
                paper.updated,
                paper.url,
                paper.pdf_url,
                json.dumps(paper.categories, ensure_ascii=False),
                json.dumps(paper.topics, ensure_ascii=False),
                json.dumps(paper.metadata, ensure_ascii=False),
                paper.score,
                paper.priority,
                json.dumps(paper.reasons, ensure_ascii=False),
                paper.summary,
                now,
                now,
            ),
        )
    conn.commit()


def preserve_cached_figure_previews(conn: sqlite3.Connection, papers: list[Paper]) -> None:
    paper_ids = [paper.paper_id for paper in papers]
    if not paper_ids:
        return
    cached: dict[str, dict[str, Any]] = {}
    for start in range(0, len(paper_ids), 200):
        chunk = paper_ids[start : start + 200]
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"SELECT paper_id, metadata_json FROM papers WHERE paper_id IN ({placeholders})",
            chunk,
        ).fetchall()
        for paper_id, metadata_json in rows:
            try:
                metadata = json.loads(metadata_json or "{}")
            except json.JSONDecodeError:
                continue
            cached_item: dict[str, Any] = {}
            previews = metadata.get("figure_previews")
            if cached_figure_previews_exist(previews):
                cached_item["figure_previews"] = previews
            if metadata.get("figure_preview_failed_at"):
                cached_item["figure_preview_failed_at"] = metadata.get("figure_preview_failed_at")
            if metadata.get("figure_preview_errors"):
                cached_item["figure_preview_errors"] = metadata.get("figure_preview_errors")
            if metadata.get("figure_preview_extractor_version"):
                cached_item["figure_preview_extractor_version"] = metadata.get("figure_preview_extractor_version")
            if metadata.get("text_cache_path"):
                cached_item["text_cache_path"] = metadata.get("text_cache_path")
                cached_item["has_fulltext"] = metadata.get("has_fulltext", True)
            if cached_item:
                cached[paper_id] = cached_item
    for paper in papers:
        cached_value = cached.get(paper.paper_id)
        if not cached_value:
            continue
        if cached_figure_previews_exist(cached_value.get("figure_previews")) and not cached_figure_previews_exist(paper.metadata.get("figure_previews")):
            paper.metadata["figure_previews"] = cached_value.get("figure_previews")
        for key in ("figure_preview_failed_at", "figure_preview_errors", "figure_preview_extractor_version", "text_cache_path", "has_fulltext"):
            if cached_value.get(key) and not paper.metadata.get(key):
                paper.metadata[key] = cached_value.get(key)


def load_recent_papers(conn: sqlite3.Connection, limit: int = 240) -> list[Paper]:
    rows = conn.execute(
        """
        SELECT paper_id, source, title, abstract, authors_json, published, updated,
               url, pdf_url, categories_json, topics_json, metadata_json, score, priority,
               reasons_json, summary
        FROM papers
        ORDER BY score DESC, COALESCE(updated, published) DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    papers: list[Paper] = []
    for row in rows:
        paper = Paper(
            paper_id=row[0],
            source=row[1],
            title=row[2],
            abstract=row[3],
            authors=json.loads(row[4] or "[]"),
            published=row[5] or "",
            updated=row[6] or "",
            url=row[7] or "",
            pdf_url=row[8] or "",
            categories=json.loads(row[9] or "[]"),
            topics=json.loads(row[10] or "[]"),
            metadata=json.loads(row[11] or "{}"),
            score=float(row[12] or 0),
            priority=row[13] or "P2",
            reasons=json.loads(row[14] or "[]"),
            summary=row[15] or "",
        )
        papers.append(paper)
    return papers


def format_authors(authors: list[str], max_authors: int = 4) -> str:
    if not authors:
        return ""
    if len(authors) <= max_authors:
        return ", ".join(authors)
    return ", ".join(authors[:max_authors]) + " et al."


def write_digest(papers: list[Paper], config: dict[str, Any], date: dt.date) -> Path:
    digest_cfg = config["digest"]
    output_dir = repo_path(digest_cfg["output_dir"])
    top_n = int(digest_cfg.get("top_n", 10))
    selected = select_daily_items(papers, config)[:top_n]
    quality_index = load_deep_read_index(config)

    path = output_dir / f"{date.isoformat()}.md"
    lines: list[str] = [
        f"# Daily Paper Digest - {date.isoformat()}",
        "",
        f"Profile: {config.get('project_name', 'Paper Brain')}",
        "",
        "## 今日高相关论文 / 代码仓库",
        "",
    ]
    if not selected:
        lines += [
            "No new high-scoring fetched papers were found. The graph still includes seed papers and cached papers.",
            "",
        ]
    for idx, paper in enumerate(selected, start=1):
        authors = format_authors(paper.authors)
        topics = ", ".join(paper.topics) if paper.topics else "未自动识别"
        reasons = "; ".join(paper.reasons) if paper.reasons else "profile match"
        venue_tags = ", ".join(paper.metadata.get("venue_tags", []) or [])
        lab_tags = ", ".join(lab.get("label", "") for lab in paper.metadata.get("lab_tags", []) or [])
        quality = quality_assessment(paper, config, quality_index)
        lines += [
            f"### {idx}. {paper.title}",
            "",
            f"- Priority: `{paper.priority}` | Score: `{paper.score}`",
            f"- Quality: `{quality['status']}` ({quality_status_label(quality['status'])}) | Quality score: `{quality['score']}`",
            f"- Quality gaps: {', '.join(quality['gaps']) if quality['gaps'] else 'N/A'}",
            f"- Deep-read note: `{quality['deep_read_path']}`",
            f"- Authors: {authors or 'N/A'}",
            f"- Published: {paper.published[:10] if paper.published else 'N/A'}",
            f"- Topics: {topics}",
            f"- Links: [abs]({paper.url})" + (f" / [pdf]({paper.pdf_url})" if paper.pdf_url else ""),
            f"- Source: `{paper.source}`",
            f"- Tags: {', '.join([tag for tag in [venue_tags, lab_tags] if tag]) or 'N/A'}",
            f"- Why it matters: {reasons}",
            "",
            paper.summary,
            "",
            "**Reading questions**",
            "",
            "- Does this work change the research map, system interface, or evaluation plan?",
            "- Are model outputs constrained as typed skills, waypoints, candidates, value maps, or another safe interface?",
            "- Can its state, memory, uncertainty, or failure-handling mechanism transfer to your project?",
            "",
        ]

    lines += [
        "## 下一步建议",
        "",
        "- Add P0 items to your own reading matrix or literature tracker.",
        "- Deep-read papers that connect multiple important topics in the graph.",
        "- 打开 `doc/paper_brain/index.html` 查看图谱位置和领域交叉点。",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def write_deep_reading_queue(papers: list[Paper], config: dict[str, Any], date: dt.date) -> Path:
    digest_cfg = config["digest"]
    path = repo_path(digest_cfg.get("deep_reading_queue", "doc/paper_brain/deep_reading_queue.md"))
    selection_cfg = config.get("daily_selection", {})
    target_max = int(selection_cfg.get("deep_read_max_items", digest_cfg.get("deep_read_top_k", 5)))
    candidate_pool = select_daily_items(papers, config)[: int(selection_cfg.get("max_new_items", target_max))]
    quality_index = load_deep_read_index(config)
    assessed = [(paper, quality_assessment(paper, config, quality_index)) for paper in candidate_pool]
    completed = [(paper, quality) for paper, quality in assessed if quality["status"] == "deep_read"][:target_max]
    pending = [(paper, quality) for paper, quality in assessed if quality["status"] != "deep_read"][: max(0, target_max - len(completed))]
    selected = completed + pending
    lines = [
        f"# Deep Reading Queue - {date.isoformat()}",
        "",
        "目的：从每日高相关论文/项目中挑选 3-5 个最重要的今日新增项做精读。未满足质量闸门前，不允许标记为已精读。",
        "",
        "标准：每个精读项都需要全文/项目证据、双语笔记、双语索引、关键图人工核验，以及与你的研究 profile 的具体关系。",
        "",
        "排序：已完成的今日重点精读优先列出；若不足目标数量，再用最高相关候选补齐。",
        "",
    ]
    for idx, (paper, quality) in enumerate(selected, start=1):
        metadata = paper.metadata or {}
        write_deep_read_template(paper, config, quality)
        lines += [
            f"## {idx}. {paper.title}",
            "",
            f"- Priority: `{paper.priority}` | Score: `{paper.score}` | Source: `{paper.source}`",
            f"- Quality: `{quality['status']}` ({quality_status_label(quality['status'])}) | Quality score: `{quality['score']}`",
            f"- Quality gaps: {', '.join(quality['gaps']) if quality['gaps'] else 'N/A'}",
            f"- Deep-read note: `{quality['deep_read_path']}`",
            f"- Topics: {', '.join(paper.topics) if paper.topics else 'N/A'}",
            f"- URL: {paper.url or 'N/A'}",
            f"- PDF: {paper.pdf_url or metadata.get('local_pdf_path') or 'N/A'}",
        ]
        if metadata.get("citation_count") is not None:
            lines.append(f"- Citations: {metadata.get('citation_count')} | References: {metadata.get('reference_count')}")
        if metadata.get("text_cache_path"):
            lines.append(f"- Text cache: {metadata.get('text_cache_path')}")
        lines += [
            "",
            "### 精读问题",
            "",
            "- 这篇论文的状态表示、地图记忆或感知动作选择机制是什么？",
            "- 它如何限制大模型/策略输出，是否输出 typed skill、waypoint、候选点或 value map？",
            "- 对你的研究系统、状态表示、接口边界或评估协议有什么直接启发？",
            "- 哪些假设不能迁移到你的平台或任务？",
            "",
            "### 待摘录证据",
            "",
            "- 方法结构：",
            "- 输入/输出 schema：",
            "- 实验指标：",
            "- 可复现代码入口：",
            "- 关键图确认：",
            "",
        ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def write_deep_read_template(paper: Paper, config: dict[str, Any], quality: dict[str, Any]) -> Path:
    path = repo_path(quality["deep_read_path"])
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    topics = ", ".join(paper.topics) if paper.topics else "N/A"
    lines = [
        f"# {paper.title}",
        "",
        "> Status: draft template. Do not mark as `deep_read` until evidence, figures, and conclusions are filled.",
        "",
        "## Metadata",
        "",
        f"- Paper ID: `{paper.paper_id}`",
        f"- Source: `{paper.source}`",
        f"- Priority / Score: `{paper.priority}` / `{paper.score}`",
        f"- Authors: {format_authors(paper.authors) or 'N/A'}",
        f"- Published: {paper.published[:10] if paper.published else 'N/A'}",
        f"- URL: {paper.url or 'N/A'}",
        f"- PDF: {paper.pdf_url or paper.metadata.get('local_pdf_path') or 'N/A'}",
        f"- Topics: {topics}",
        "",
        "## Verdict",
        "",
        "- One-sentence contribution:",
        "- Why it matters to this research profile:",
        "- Reusable mechanism:",
        "- Do-not-copy caveat:",
        "- Priority after reading:",
        "",
        "## Evidence Notes",
        "",
        "- [ ] Abstract/problem evidence:",
        "- [ ] Method mechanism evidence:",
        "- [ ] Experiment/result evidence:",
        "- [ ] Limitation/failure evidence:",
        "",
        "## Key Figures",
        "",
        "- [ ] Figure 1:",
        "- [ ] Figure 2:",
        "- [ ] Figure 3:",
        "",
        "## Method Extraction",
        "",
        "- Inputs:",
        "- Outputs:",
        "- State representation:",
        "- Planner/policy interface:",
        "- Failure or uncertainty handling:",
        "",
        "## Graph Updates",
        "",
        "- Nodes to add/update:",
        "- Edges to add/update:",
        "- Relations to current project:",
        "",
        "## Quality Checklist",
        "",
        "- [ ] Full text inspected",
        "- [ ] At least 4 evidence items recorded",
        "- [ ] Key figures verified or explicitly marked unavailable",
        "- [ ] Summary rewritten from evidence, not only abstract",
        "- [ ] Relevance to the research profile checked",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def write_quality_audit(papers: list[Paper], config: dict[str, Any], date: dt.date) -> Path:
    quality_cfg = quality_config(config)
    path = repo_path(quality_cfg.get("quality_audit", "doc/paper_brain/quality_audit.md"))
    index = load_deep_read_index(config)
    assessed = [(paper, quality_assessment(paper, config, index)) for paper in sorted(papers, key=lambda p: p.score, reverse=True)]
    for paper, quality in assessed:
        if can_generate_deep_read_template(paper):
            write_deep_read_template(paper, config, quality)

    counts: dict[str, int] = {}
    for _, quality in assessed:
        counts[quality["status"]] = counts.get(quality["status"], 0) + 1

    lines = [
        f"# Paper Brain Quality Audit - {date.isoformat()}",
        "",
        "This audit is intentionally strict: a node is not considered deep-read unless full-text evidence, figure review, and a deep-read note exist.",
        "",
        "## Status Counts",
        "",
        f"- 已精读 deep_read: {counts.get('deep_read', 0)}",
        f"- 快读校验 quick_checked: {counts.get('quick_checked', 0)}",
        f"- 待精读 needs_deep_read: {counts.get('needs_deep_read', 0)}",
        f"- 自动导入 auto_imported: {counts.get('auto_imported', 0)}",
        "",
        "## Deep-Read Debt",
        "",
    ]
    for paper, quality in assessed:
        if quality["status"] == "deep_read":
            continue
        lines += [
            f"### {paper.title}",
            "",
            f"- Status: `{quality['status']}` ({quality_status_label(quality['status'])}) | Quality: `{quality['score']}`",
            f"- Priority: `{paper.priority}` | Score: `{paper.score}` | Source: `{paper.source}`",
            f"- Deep-read note: `{quality['deep_read_path']}`",
            f"- Gaps: {', '.join(quality['gaps']) if quality['gaps'] else 'N/A'}",
            f"- PDF/Text: {paper.pdf_url or paper.metadata.get('local_pdf_path') or 'N/A'} / {paper.metadata.get('text_cache_path') or 'N/A'}",
            "",
        ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def can_generate_deep_read_template(paper: Paper) -> bool:
    return paper.source in {"arxiv", "semantic_scholar", "google_scholar", "local_pdf", "seed"} or bool(paper.pdf_url)


def select_daily_items(papers: list[Paper], config: dict[str, Any]) -> list[Paper]:
    selection_cfg = config.get("daily_selection", {})
    max_items = int(selection_cfg.get("max_new_items", 10))
    max_papers = int(selection_cfg.get("max_papers", 8))
    max_repos = int(selection_cfg.get("max_code_repos", 3))
    min_score = float(selection_cfg.get("min_score", 0))
    selected: list[Paper] = []
    paper_count = 0
    repo_count = 0
    for paper in sorted(papers, key=lambda p: p.score, reverse=True):
        if paper.source == "seed" or paper.score < min_score:
            continue
        if paper.source == "github":
            if repo_count >= max_repos:
                continue
            repo_count += 1
        else:
            if paper_count >= max_papers:
                continue
            paper_count += 1
        selected.append(paper)
        if len(selected) >= max_items:
            break
    return selected


def node_id(prefix: str, name: str) -> str:
    return f"{prefix}:{slugify(name, 70)}"


def build_graph(papers: list[Paper], config: dict[str, Any], daily_items: list[Paper] | None = None) -> dict[str, Any]:
    max_papers = int(config["graph"].get("max_papers", 240))
    papers = papers[:max_papers]
    daily_ids = {paper.paper_id for paper in (daily_items or [])}
    quality_index = load_deep_read_index(config)
    project_cfg = config.get("project", {})
    project_id = project_cfg.get("id", "project:paper-brain-research-focus")
    project_label = project_cfg.get("label", config.get("project_name", "Paper Brain Research Focus"))
    project_url = project_cfg.get("url", "README.md")
    nodes: dict[str, dict[str, Any]] = {}
    edges: dict[tuple[str, str, str], dict[str, Any]] = {}

    def add_node(nid: str, label: str, kind: str, **attrs: Any) -> None:
        current = nodes.get(nid, {})
        merged = {"id": nid, "label": label, "kind": kind}
        merged.update(current)
        merged.update({k: v for k, v in attrs.items() if v not in (None, "", [])})
        nodes[nid] = merged

    def add_edge(src: str, dst: str, relation: str, weight: float = 1.0, evidence: str = "") -> None:
        if src == dst:
            return
        key = (src, dst, relation)
        if key in edges:
            edges[key]["weight"] = max(edges[key]["weight"], weight)
            return
        edges[key] = {
            "source": src,
            "target": dst,
            "relation": relation,
            "weight": round(weight, 2),
            "evidence": evidence,
        }

    add_node(
        project_id,
        project_label,
        "project",
        score=25,
        priority="P0",
        url=project_url,
    )
    for domain, info in TOPIC_DOMAINS.items():
        d_node = node_id("domain", domain)
        add_node(
            d_node,
            domain,
            "domain",
            score=14,
            description_zh=info["description_zh"],
            description_en=info["description_en"],
        )
        add_edge(project_id, d_node, "uses", 1.3, "research domain")

    for paper in papers:
        paper.metadata.update(infer_prestige_tags(paper))
        quality = quality_assessment(paper, config, quality_index)
        deep_entry = quality.get("deep_read_entry", {}) if isinstance(quality.get("deep_read_entry"), dict) else {}
        venue_tags = deep_entry.get("venue_tags") or paper.metadata.get("venue_tags")
        lab_tags = deep_entry.get("lab_tags") or paper.metadata.get("lab_tags")
        is_code_repo = paper.source == "github" or bool(paper.metadata.get("is_code_repo"))
        p_node = node_id("repo" if is_code_repo else "paper", paper.title)
        node_kind = "code_repo" if is_code_repo else ("paper" if paper.source != "seed" else "seed")
        add_node(
            p_node,
            paper.title,
            node_kind,
            paper_id=paper.paper_id,
            source=paper.source,
            score=paper.score,
            priority=paper.priority,
            url=deep_entry.get("url") or paper.url,
            pdf_url=deep_entry.get("pdf_url") or paper.pdf_url,
            year=(paper.published or paper.updated or "")[:4],
            abstract=paper.summary or paper.abstract[:520],
            raw_abstract=paper.abstract[:1600],
            analysis=analysis_for_paper(paper, config, quality),
            deep_read_summary=quality.get("deep_read_entry", {}).get("summary") if isinstance(quality.get("deep_read_entry"), dict) else None,
            verified_figures=quality.get("deep_read_entry", {}).get("verified_figures") if isinstance(quality.get("deep_read_entry"), dict) else None,
            quality_status=quality.get("status"),
            quality_score=quality.get("score"),
            quality_gaps=quality.get("gaps"),
            quality_label=quality_status_label(quality.get("status", "")),
            deep_read_path=quality.get("deep_read_path"),
            deep_read_paths=deep_entry.get("paths") or deep_entry.get("path_i18n"),
            has_deep_read_file=quality.get("has_deep_read_file"),
            figure_verified=quality.get("figure_verified"),
            evidence_count=quality.get("evidence_count"),
            min_evidence=quality.get("min_evidence"),
            authors=format_authors(paper.authors, 6),
            citation_count=paper.metadata.get("citation_count"),
            reference_count=paper.metadata.get("reference_count"),
            influential_citation_count=paper.metadata.get("influential_citation_count"),
            local_pdf_path=paper.metadata.get("local_pdf_path"),
            text_cache_path=deep_entry.get("text_cache_path") or paper.metadata.get("text_cache_path"),
            has_fulltext=deep_entry.get("has_fulltext") or paper.metadata.get("has_fulltext"),
            stars=paper.metadata.get("stars"),
            forks=paper.metadata.get("forks"),
            language=paper.metadata.get("language"),
            github_topics=paper.metadata.get("github_topics"),
            is_code_repo=is_code_repo,
            figure_previews=quality.get("deep_read_entry", {}).get("figure_previews") if isinstance(quality.get("deep_read_entry"), dict) and quality.get("deep_read_entry", {}).get("figure_previews") else paper.metadata.get("figure_previews"),
            figure_preview_errors=paper.metadata.get("figure_preview_errors"),
            figure_preview_failed_at=paper.metadata.get("figure_preview_failed_at"),
            venue_tags=venue_tags,
            lab_tags=lab_tags,
            is_top_venue=bool(venue_tags) or paper.metadata.get("is_top_venue"),
            is_top_lab=bool(lab_tags) or paper.metadata.get("is_top_lab"),
            is_today_new=paper.paper_id in daily_ids,
        )
        for topic in paper.topics:
            t_node = node_id("topic", topic)
            parent = topic_parent(topic)
            d_node = node_id("domain", parent)
            desc = topic_description(topic)
            add_node(
                t_node,
                topic,
                "topic",
                score=10,
                parent_domain=parent,
                parent_domain_id=d_node,
                description_zh=desc["zh"],
                description_en=desc["en"],
            )
            add_edge(d_node, t_node, "contains", 1.4, parent)
            add_edge(p_node, t_node, "has_topic", 1.0 + min(paper.score, 20) / 20, paper.title)
            add_edge(p_node, d_node, "has_domain", 0.7 + min(paper.score, 20) / 40, paper.title)

            project_topic_names = {
                "OVMM",
                "Mobile Manipulation",
                "Active Perception",
                "Task-Semantic Object State",
                "Wrist Reacquisition",
                "Semantic Mapping",
                "VLA",
                "VLN",
                "Legged Robot Navigation",
                "Object Memory",
                "Affordance",
            }
            if topic in project_topic_names:
                add_edge(project_id, t_node, "uses", 2.2, "project scope")
                if paper.source != "seed":
                    add_edge(project_id, p_node, "related_work", 1.5, topic)

        if "Active Perception" in paper.topics and ("VLA" in paper.topics or "OVMM" in paper.topics):
            add_edge(p_node, project_id, "inspiration", 2.4, "active perception + embodied robot")
        if "Semantic Mapping" in paper.topics and "Object Memory" in paper.topics:
            add_edge(p_node, project_id, "memory_reference", 1.9, "map/object memory")

    # Topic co-occurrence edges.
    topic_counts: dict[tuple[str, str], int] = {}
    for paper in papers:
        unique_topics = sorted(set(paper.topics))
        for i, src in enumerate(unique_topics):
            for dst in unique_topics[i + 1 :]:
                key = (node_id("topic", src), node_id("topic", dst))
                topic_counts[key] = topic_counts.get(key, 0) + 1
    for (src, dst), count in topic_counts.items():
        add_edge(src, dst, "co_occurs", 0.6 + math.log1p(count), f"{count} papers")

    graph = {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "project": config.get("project_name", "Paper Brain"),
        "project_node_id": project_id,
        "daily_item_ids": sorted(daily_ids),
        "nodes": list(nodes.values()),
        "edges": list(edges.values()),
    }
    return graph


def load_graph_overrides(config: dict[str, Any]) -> dict[str, Any]:
    path = repo_path(config["graph"].get("overrides_path", "doc/paper_brain/graph-overrides.json"))
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[warn] Could not read graph overrides from {path}: {exc}", file=sys.stderr)
        return {}


def apply_graph_overrides(graph: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    overrides = load_graph_overrides(config)
    hidden = set(overrides.get("hidden_node_ids", []))
    added_nodes = overrides.get("added_nodes", [])
    added_edges = overrides.get("added_edges", [])

    nodes_by_id = {node["id"]: node for node in graph.get("nodes", []) if node.get("id") not in hidden}
    for node in added_nodes:
        if node.get("id") and node.get("label"):
            nodes_by_id[node["id"]] = node

    valid_ids = set(nodes_by_id)
    edges = [
        edge
        for edge in graph.get("edges", [])
        if edge.get("source") in valid_ids and edge.get("target") in valid_ids
    ]
    for edge in added_edges:
        if edge.get("source") in valid_ids and edge.get("target") in valid_ids:
            edges.append(edge)

    graph["nodes"] = list(nodes_by_id.values())
    graph["edges"] = edges
    graph["overrides"] = {
        "hidden_count": len(hidden),
        "added_node_count": len(added_nodes),
        "added_edge_count": len(added_edges),
        "path": config["graph"].get("overrides_path", "doc/paper_brain/graph-overrides.json"),
    }
    return graph


def export_graph(graph: dict[str, Any], config: dict[str, Any]) -> tuple[Path, Path]:
    output_dir = repo_path(config["graph"]["output_dir"])
    graph_json = output_dir / "graph.json"
    graph_js = output_dir / "graph-data.js"
    graph_json.write_text(json.dumps(graph, ensure_ascii=False, indent=2), encoding="utf-8")
    graph_js.write_text(
        "window.PAPER_BRAIN_GRAPH = "
        + json.dumps(graph, ensure_ascii=False, separators=(",", ":"))
        + ";\n",
        encoding="utf-8",
    )
    return graph_json, graph_js


def run(config_path: Path, db_path: Path, offline: bool = False, date: dt.date | None = None) -> dict[str, Path | int]:
    config = load_config(config_path)
    ensure_dirs(config, db_path)
    conn = connect_db(db_path)
    fetched = fetch_arxiv(config, offline=offline)
    semantic_scholar = fetch_semantic_scholar(config, offline=offline)
    github = fetch_github(config, offline=offline)
    google_scholar = fetch_google_scholar_serpapi(config, offline=offline)
    local_pdfs = local_pdf_papers(config)
    all_new = seed_papers(config) + local_matrix_papers(config) + local_pdfs + fetched + semantic_scholar + github + google_scholar
    scored = [score_paper(paper, config) for paper in all_new]
    preserve_cached_figure_previews(conn, scored)
    upsert_papers(conn, scored)
    graph_papers = load_recent_papers(conn, int(config["graph"].get("max_papers", 240)))
    attach_figure_previews(graph_papers, config)
    upsert_papers(conn, graph_papers)
    daily_items = select_daily_items(graph_papers, config)
    digest_path = write_digest(graph_papers, config, date or dt.date.today())
    deep_reading_queue = write_deep_reading_queue(graph_papers, config, date or dt.date.today())
    quality_audit = write_quality_audit(graph_papers, config, date or dt.date.today())
    graph = build_graph(graph_papers, config, daily_items)
    graph = apply_graph_overrides(graph, config)
    graph_json, graph_js = export_graph(graph, config)
    conn.close()
    return {
        "fetched": len(fetched),
        "semantic_scholar": len(semantic_scholar),
        "github": len(github),
        "google_scholar": len(google_scholar),
        "local_pdfs": len(local_pdfs),
        "scored": len(scored),
        "digest": digest_path,
        "deep_reading_queue": deep_reading_queue,
        "quality_audit": quality_audit,
        "graph_json": graph_json,
        "graph_js": graph_js,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="Path to paper_watch.json")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="Path to SQLite database")
    parser.add_argument("--offline", action="store_true", help="Skip network fetch and rebuild from seed/cache")
    parser.add_argument("--date", default="", help="Digest date in YYYY-MM-DD, defaults to today")
    args = parser.parse_args(argv)

    try:
        date = dt.date.fromisoformat(args.date) if args.date else dt.date.today()
        result = run(repo_path(args.config), repo_path(args.db), offline=args.offline, date=date)
    except Exception as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1

    print(
        textwrap.dedent(
            f"""
            Paper Brain updated.
              fetched: {result['fetched']}
              semantic_scholar: {result['semantic_scholar']}
              github: {result['github']}
              google_scholar: {result['google_scholar']}
              local_pdfs: {result['local_pdfs']}
              scored:  {result['scored']}
              digest:  {result['digest']}
              deep:    {result['deep_reading_queue']}
              quality: {result['quality_audit']}
              graph:   {result['graph_json']}
              web data:{result['graph_js']}
            """
        ).strip()
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
