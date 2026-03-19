#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from string import Template
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPT_PATH = REPO_ROOT / ".github" / "prompts" / "daily_research_prompt.md"
ALLOWED_ROOT_MARKDOWN = {
    "README.md",
    "Industries.md",
    "Insetting.md",
    "Offsetting.md",
}
NEWS_QUERIES = [
    "industrial decarbonization",
    "\"green steel\" industrial decarbonization",
    "\"low-carbon cement\" industrial decarbonization",
    "\"industrial heat\" electrification decarbonization",
    "\"carbon capture\" industrial decarbonization",
]


@dataclass(frozen=True)
class CandidateItem:
    title: str
    url: str
    source: str
    published: str


def env_int(name: str, default: int) -> int:
    value = os.getenv(name, "").strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise SystemExit(f"{name} must be an integer, got {value!r}") from exc


def is_allowed_repo_path(path: str) -> bool:
    if not path or path.startswith("/") or path.startswith("."):
        return False
    normalized = Path(path)
    if ".." in normalized.parts:
        return False
    if normalized.suffix != ".md":
        return False
    if len(normalized.parts) == 1:
        return normalized.name in ALLOWED_ROOT_MARKDOWN
    return len(normalized.parts) == 2 and normalized.parts[0] == "Articles"


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:48] or "update"


def read_repo_context() -> str:
    context_parts = []
    for relative_path in [
        "README.md",
        "Industries.md",
        "Insetting.md",
        "Offsetting.md",
    ]:
        file_path = REPO_ROOT / relative_path
        if not file_path.exists():
            continue
        context_parts.append(f"## FILE: {relative_path}\n{file_path.read_text(encoding='utf-8')}")
    return "\n\n".join(context_parts)


def parse_pub_date(value: str) -> datetime | None:
    for fmt in (
        "%a, %d %b %Y %H:%M:%S %Z",
        "%a, %d %b %Y %H:%M:%S %z",
        "%Y-%m-%dT%H:%M:%SZ",
    ):
        try:
            parsed = datetime.strptime(value, fmt)
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=UTC)
            return parsed.astimezone(UTC)
        except ValueError:
            continue
    return None


def fetch_url(url: str) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "industrial-decarbonization-daily-research/1.0",
            "Accept": "application/rss+xml, application/xml, text/xml, */*",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8", errors="replace")


def fetch_google_news_items(query: str, days_back: int) -> list[CandidateItem]:
    encoded_query = urllib.parse.quote_plus(f"{query} when:{days_back}d")
    url = (
        "https://news.google.com/rss/search"
        f"?q={encoded_query}&hl=en-US&gl=US&ceid=US:en"
    )
    xml_text = fetch_url(url)
    root = ET.fromstring(xml_text)
    cutoff = datetime.now(tz=UTC) - timedelta(days=days_back)
    items: list[CandidateItem] = []

    for item in root.findall("./channel/item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        source = (item.findtext("source") or "").strip()
        published = (item.findtext("pubDate") or "").strip()
        if not title or not link:
            continue
        parsed_date = parse_pub_date(published)
        if parsed_date and parsed_date < cutoff:
            continue
        items.append(
            CandidateItem(
                title=title,
                url=link,
                source=source or "Unknown source",
                published=published or "Unknown publication date",
            )
        )

    return items


def dedupe_candidates(items: list[CandidateItem]) -> list[CandidateItem]:
    seen: set[tuple[str, str]] = set()
    deduped: list[CandidateItem] = []
    for item in items:
        key = (item.title.casefold(), item.url)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def render_prompt(items: list[CandidateItem]) -> str:
    template = Template(PROMPT_PATH.read_text(encoding="utf-8"))
    repo_context = read_repo_context()
    candidates = "\n".join(
        f"- {item.title} | {item.source} | {item.published} | {item.url}"
        for item in items
    )
    return template.safe_substitute(
        TODAY=datetime.now(tz=UTC).date().isoformat(),
        REPO_CONTEXT=repo_context,
        CANDIDATE_ITEMS=candidates,
    )


def extract_json_object(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.DOTALL).strip()
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("Model response must be a JSON object")
    return data


def call_github_models(prompt: str) -> dict[str, Any]:
    fixture_path = os.getenv("MODEL_RESPONSE_PATH", "").strip()
    if fixture_path:
        return extract_json_object(Path(fixture_path).read_text(encoding="utf-8"))

    token = os.getenv("GITHUB_TOKEN", "").strip()
    if not token:
        raise SystemExit("GITHUB_TOKEN is required to call GitHub Models")

    payload = {
        "model": os.getenv("RESEARCH_MODEL", "openai/gpt-4.1-mini"),
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a careful maintainer assistant. "
                    "Return only valid JSON that follows the requested schema."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
        "top_p": 0.9,
        "response_format": {"type": "json_object"},
    }
    request = urllib.request.Request(
        "https://models.inference.ai.azure.com/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        response_body = json.loads(response.read().decode("utf-8"))

    choices = response_body.get("choices") or []
    if not choices:
        raise ValueError("GitHub Models response did not include any choices")
    content = choices[0].get("message", {}).get("content", "")
    return extract_json_object(content)


def normalize_response(data: dict[str, Any]) -> dict[str, Any]:
    decision = data.get("decision", "")
    if decision not in {"skip", "propose_pr"}:
        raise ValueError("decision must be 'skip' or 'propose_pr'")

    summary = str(data.get("summary", "")).strip()
    pr_title = str(data.get("pr_title", "")).strip()
    pr_body = str(data.get("pr_body", "")).strip()
    raw_changes = data.get("changes") or []
    raw_sources = data.get("sources") or []

    if not isinstance(raw_changes, list) or not isinstance(raw_sources, list):
        raise ValueError("changes and sources must be arrays")

    changes: list[dict[str, str]] = []
    for entry in raw_changes:
        if not isinstance(entry, dict):
            raise ValueError("each change must be an object")
        path = str(entry.get("path", "")).strip()
        content = str(entry.get("content", ""))
        if not is_allowed_repo_path(path):
            raise ValueError(f"disallowed change path: {path!r}")
        if len(content) > 150_000:
            raise ValueError(f"content too large for {path!r}")
        changes.append({"path": path, "content": content})

    sources: list[dict[str, str]] = []
    for entry in raw_sources:
        if not isinstance(entry, dict):
            raise ValueError("each source must be an object")
        sources.append(
            {
                "title": str(entry.get("title", "")).strip(),
                "url": str(entry.get("url", "")).strip(),
                "why_it_matters": str(entry.get("why_it_matters", "")).strip(),
            }
        )

    if decision == "skip":
        return {
            "decision": "skip",
            "summary": summary or "No strong, non-duplicative update was identified.",
            "pr_title": "",
            "pr_body": "",
            "changes": [],
            "sources": [],
        }

    if not pr_title:
        raise ValueError("pr_title is required when decision is propose_pr")
    if not pr_body:
        raise ValueError("pr_body is required when decision is propose_pr")
    if not changes:
        raise ValueError("at least one change is required when decision is propose_pr")

    return {
        "decision": "propose_pr",
        "summary": summary or pr_title,
        "pr_title": pr_title,
        "pr_body": pr_body,
        "changes": changes,
        "sources": sources,
    }


def apply_changes(changes: list[dict[str, str]]) -> list[str]:
    written_files: list[str] = []
    for change in changes:
        relative_path = change["path"]
        target = REPO_ROOT / relative_path
        if not target.resolve().is_relative_to(REPO_ROOT):
            raise ValueError(f"path escapes repository root: {relative_path}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(change["content"], encoding="utf-8")
        written_files.append(relative_path)
    return written_files


def write_github_output(name: str, value: str) -> None:
    output_path = os.getenv("GITHUB_OUTPUT", "").strip()
    if not output_path:
        return
    delimiter = f"EOF_{name.upper()}"
    with open(output_path, "a", encoding="utf-8") as handle:
        handle.write(f"{name}<<{delimiter}\n{value}\n{delimiter}\n")


def main() -> int:
    days_back = env_int("DAYS_BACK", 7)
    max_items = env_int("MAX_CANDIDATES", 20)

    gathered_items: list[CandidateItem] = []
    for query in NEWS_QUERIES:
        try:
            gathered_items.extend(fetch_google_news_items(query, days_back))
        except Exception as exc:
            print(f"Warning: failed to fetch candidate items for {query!r}: {exc}", file=sys.stderr)

    candidates = dedupe_candidates(gathered_items)[:max_items]
    if not candidates:
        print("No candidate items were gathered; skipping.")
        write_github_output("decision", "skip")
        write_github_output("summary", "No candidate items were gathered.")
        return 0

    prompt = render_prompt(candidates)
    model_response = call_github_models(prompt)
    result = normalize_response(model_response)

    write_github_output("decision", result["decision"])
    write_github_output("summary", result["summary"])

    if result["decision"] == "skip":
        print(result["summary"])
        return 0

    written_files = apply_changes(result["changes"])
    branch_suffix = slugify(result["pr_title"])
    write_github_output("pr_title", result["pr_title"])
    write_github_output("pr_body", result["pr_body"])
    write_github_output("branch_suffix", branch_suffix)
    write_github_output("changed_files", "\n".join(written_files))
    print(f"Prepared {len(written_files)} file(s): {', '.join(written_files)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
