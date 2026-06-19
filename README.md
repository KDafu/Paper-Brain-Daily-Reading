# Paper Brain

Paper Brain is a local daily paper reader and research-graph dashboard.

It can:

- discover recent papers and GitHub projects from configurable sources;
- score them against your research profile;
- generate a daily digest and deep-reading queue;
- render a local interactive knowledge graph;
- switch the dashboard between Chinese and English;
- favorite papers, organize favorites into manual categories, hide/restore graph nodes;
- export selected papers or all "today added" papers as a Markdown summary.

This repository is intentionally clean: it ships with example seed nodes only.
It does not include private papers, PDFs, figure crops, SQLite cache files, API
keys, or personal reading notes.

## Quick Start

```bash
git clone https://github.com/YOUR_NAME/paper-brain.git
cd paper-brain
bash scripts/setup.sh
```

Then run:

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

## Daily Use

Offline rebuild:

```bash
python scripts/paper_brain/paper_brain.py --offline
```

Online discovery after enabling sources:

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

## Configure Your Research Profile

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

The default config keeps online sources disabled so the repository works
immediately after clone. Turn on sources when you are ready.

## Optional Online Sources

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

## Optional OpenAI Summaries

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

Never commit API keys, `.env` files, SQLite cache files, PDFs, or generated
figure crops.

## Dashboard Workflow

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
language, and title is stored in `localStorage`.

## Deep-Read Quality Gate

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

## Repository Hygiene

This clean version ignores:

- `.env` and `.env.*`
- `.venv/`
- `data/paper_brain/`
- `papers/`
- generated figures under `doc/paper_brain/figures/`
- dashboard export Markdown files

Before pushing:

```bash
git status --short
```

Review every file that will be committed.

## Upload To GitHub

Create a new empty GitHub repository, then:

```bash
git init
git add .
git commit -m "Initial clean Paper Brain release"
git branch -M main
git remote add origin git@github.com:YOUR_NAME/paper-brain.git
git push -u origin main
```

HTTPS alternative:

```bash
git remote add origin https://github.com/YOUR_NAME/paper-brain.git
git push -u origin main
```

GitHub no longer accepts account passwords for git push. Use SSH keys,
`gh auth login`, or a Personal Access Token.

## Roadmap

- Better provider plugins for arXiv, Semantic Scholar, GitHub, and SerpAPI.
- Richer deep-read editor and validation UI.
- Safer persistent graph-edit export/import flow.
- Optional vector search over full text.
- Better figure extraction and manual figure verification.
- Multi-profile support for different research projects.
