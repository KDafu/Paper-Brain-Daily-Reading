# Maintainers

This document is for people updating and publishing Paper Brain.

## Update Checklist

Before committing:

```bash
python -m py_compile scripts/paper_brain/paper_brain.py
python scripts/paper_brain/paper_brain.py --offline
sed -n '/<script>/,/<\/script>/p' doc/paper_brain/index.html > /tmp/paper_brain_inline.with_tags.js
sed '1d;$d' /tmp/paper_brain_inline.with_tags.js > /tmp/paper_brain_inline.js
node --check /tmp/paper_brain_inline.js
python -m json.tool config/paper_watch.json >/tmp/paper_brain_config.json
python -m json.tool doc/paper_brain/graph.json >/tmp/paper_brain_graph.json
```

Then inspect the repository state:

```bash
git status --short
```

## Repository Hygiene

Do not commit:

- API keys or `.env` files;
- local virtual environments;
- `data/paper_brain/`;
- private PDFs under `papers/`;
- SQLite cache files;
- generated private figure crops;
- personal reading notes not intended for release.

The clean repository intentionally ignores these by default.

## Publishing Updates

```bash
git add README.md docs/MAINTAINERS.md config scripts doc requirements.txt .gitignore
git commit -m "Update Paper Brain"
git push
```

Use a more specific commit message when useful, for example:

```bash
git commit -m "Improve favorites and export workflow"
```

## First-Time GitHub Setup

Create an empty GitHub repository, then:

```bash
git remote add origin git@github.com:YOUR_NAME/paper-brain.git
git push -u origin main
```

HTTPS alternative:

```bash
git remote add origin https://github.com/YOUR_NAME/paper-brain.git
git push -u origin main
```

GitHub does not accept account passwords for git push. Use SSH keys,
`gh auth login`, or a Personal Access Token.
