# Paper Brain

[English](README.md) | [简体中文](README.zh-CN.md)

![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?style=flat-square&logo=python&logoColor=white)
![Local First](https://img.shields.io/badge/local--first-research%20workspace-A8D9FF?style=flat-square)
![No Keys Required](https://img.shields.io/badge/offline%20mode-no%20API%20key-9FD9BD?style=flat-square)
![License](https://img.shields.io/badge/license-MIT-lightgrey?style=flat-square)

Paper Brain is a local-first daily paper reader and research graph dashboard. It
helps you discover papers and research projects, triage them against a research
profile, keep a deep-reading queue, and inspect the field as an interactive
knowledge graph.

<p align="center">
  <img src="docs/assets/dashboard-preview.svg" alt="Paper Brain dashboard preview" width="920">
</p>

## Why Paper Brain

Research feeds are noisy. Paper Brain is designed for a slower, higher-quality
workflow:

- find about 10 relevant papers or projects per run;
- prioritize top venues, strong labs, and code-backed work;
- separate auto-imported metadata from verified deep reads;
- keep figures, notes, graph links, favorites, and exports in one local place;
- let an AI coding assistant help run the daily workflow without giving it your
  private credentials.

The clean repository ships with example seed nodes only. It does not include
private papers, PDFs, figure crops, SQLite caches, API keys, or personal notes.

## Features

| Area | What it does |
| --- | --- |
| Discovery | arXiv, Semantic Scholar, GitHub Search, SerpAPI-powered Google Scholar, and local PDFs. |
| Triage | Scores items with your research profile, boost terms, negative terms, and daily limits. |
| Deep Reading | Tracks evidence, verified figures, limitations, mechanisms, and open questions. |
| Graph | Shows papers, topics, domains, code repos, local PDFs, favorites, hidden nodes, and today-added items. |
| Export | Exports selected papers or all today-added papers as Markdown summaries. |
| Bilingual UI | Dashboard and README are available in English and Chinese. |

## Quick Start

```bash
git clone https://github.com/KDafu/paper-brain.git
cd paper-brain
bash scripts/setup.sh
```

Start the local dashboard:

```bash
source .venv/bin/activate
scripts/paper_brain/serve_paper_brain.sh 8765
```

Open:

```text
http://127.0.0.1:8765/doc/paper_brain/index.html
```

## Manual Install

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python scripts/paper_brain/paper_brain.py --offline
scripts/paper_brain/serve_paper_brain.sh 8765
```

The offline command builds the dashboard from the example seed nodes in
`config/paper_watch.json`.

## Daily Workflow

Offline rebuild:

```bash
python scripts/paper_brain/paper_brain.py --offline
```

Online discovery after enabling sources in `config/paper_watch.json`:

```bash
python scripts/paper_brain/paper_brain.py
```

Generated files:

- `doc/daily_paper_digest/YYYY-MM-DD.md`
- `doc/paper_brain/graph.json`
- `doc/paper_brain/graph-data.js`
- `doc/paper_brain/deep_reading_queue.md`
- `doc/paper_brain/quality_audit.md`
- `doc/paper_brain/deep_reads/`
- `data/paper_brain/papers.sqlite` local cache, ignored by git

## Working With An AI Assistant

After installing Paper Brain locally, you can ask an AI coding assistant such as
Codex to operate the workflow inside the repository. The assistant does not need
your GitHub password. API keys, if used, should stay in your shell environment or
local secret manager.

Useful prompts:

```text
Read config/paper_watch.json, run today's Paper Brain workflow, and summarize the
top papers. Pick 3-5 items for deep reading and update the graph.
```

```text
Focus today's discovery on reinforcement learning for mobile manipulation:
re-parking, base-arm coordination, grasp recovery, and failure recovery.
Generate the daily digest, deep-reading queue, and graph update.
```

```text
Deep-read the highest-priority paper from doc/paper_brain/deep_reading_queue.md.
Use full text evidence, verify figures when available, write the note under
doc/paper_brain/deep_reads/, and update doc/paper_brain/deep_read_index.json.
```

Recommended AI workflow:

1. Ask the assistant to inspect `config/paper_watch.json` and your current
   research goal.
2. Let it run `python scripts/paper_brain/paper_brain.py`.
3. Review `doc/daily_paper_digest/YYYY-MM-DD.md` and the dashboard.
4. Ask it to deep-read only 3-5 high-value items.
5. Commit the generated notes and graph files you want to keep.

Quality rule: auto-imported summaries are drafts. Treat a paper as a real
deep-read result only after full text, evidence items, figures, limitations, and
project relevance have been checked.

## Configure The Research Profile

Edit:

```text
config/paper_watch.json
```

Start with:

- `project_name`
- `project.label`
- `profile.core_topics`
- `profile.must_include_any`
- `profile.boost_terms`
- `profile.negative_terms`
- `sources.*.enabled`
- `graph.seed_papers`

The default config keeps online sources disabled so the repository works after
clone. Turn on sources when you are ready.

## Online Sources

Enable arXiv:

```json
"arxiv": {
  "enabled": true
}
```

Enable GitHub repository discovery:

```json
"github": {
  "enabled": true
}
```

For higher GitHub rate limits:

```bash
export GITHUB_TOKEN="..."
```

Enable Semantic Scholar:

```json
"semantic_scholar": {
  "enabled": true
}
```

Optional:

```bash
export SEMANTIC_SCHOLAR_API_KEY="..."
```

Enable Google Scholar through SerpAPI:

```json
"google_scholar": {
  "enabled": true
}
```

Then:

```bash
export SERPAPI_API_KEY="..."
```

Direct Google Scholar scraping is not implemented.

## LLM Summaries

Set:

```json
"llm": {
  "enabled": true,
  "provider": "openai",
  "model": "gpt-4.1-mini",
  "summary_top_k": 5
}
```

Then set your key outside the repository:

```bash
export OPENAI_API_KEY="..."
```

Keep API keys in your shell, `.env`, or system secret manager. Do not put keys
inside `config/paper_watch.json`.

## Dashboard

Open `doc/paper_brain/index.html` through the local server.

Useful controls:

- language button: switch Chinese/English;
- search: find papers, topics, methods, repos;
- Today Added: focus today's discovered items;
- Favorites: filter to saved papers/projects;
- Local Graph: inspect the neighborhood around the selected node;
- Layer: cycle between automatic hierarchy, domains, topics, and details;
- Actions: hide selected graph node and restore hidden nodes;
- Favorites panel: assign categories, move favorites between categories, and export reading summaries;
- Export Selected: export papers manually marked with "Add to export";
- Export Today Added: export all today-added paper/repo nodes directly;
- Clear Export Basket: remove all selected-for-export items.

Browser-only state such as favorites, categories, hidden nodes, export selection,
language, and title is stored in your browser's `localStorage`.

## Deep Reading

Paper Brain separates auto-imported metadata from true deep reading.

A node should only be treated as `deep_read` after you have:

- inspected the full text or project documentation;
- written a deep-reading note in `doc/paper_brain/deep_reads/`;
- recorded at least four evidence items;
- verified important figures or recorded why figures are unavailable;
- rewritten summary, innovations, mechanisms, relevance, limitations, and questions from evidence.

Structured deep-read metadata lives in:

```text
doc/paper_brain/deep_read_index.json
```

The dashboard reads this file when available.

## Local PDFs

Put PDFs under:

```text
papers/
```

Then enable:

```json
"local_pdf": {
  "enabled": true,
  "paths": ["papers"],
  "extract_text": true
}
```

If `pdftotext` is installed, text will be cached under `data/paper_brain/text/`.

Figure preview extraction is disabled by default in the clean config. Enable it
under `graph.figure_preview` when you are ready to store generated figure crops.

## Roadmap

- Better provider plugins for arXiv, Semantic Scholar, GitHub, and SerpAPI.
- Richer deep-read editor and validation UI.
- Safer persistent graph-edit export/import flow.
- Optional vector search over full text.
- Better figure extraction and manual figure verification.
- Multi-profile support for different research projects.

## Maintainers

Release hygiene, update, and publishing notes live in
[`docs/MAINTAINERS.md`](docs/MAINTAINERS.md).
