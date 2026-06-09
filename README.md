# Career Page Agent (single-file edition)

## Quick start
```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
streamlit run main.py
```

## What it includes
- Streamlit interface to manage career pages, role keywords and email settings
- Greenhouse + Lever support via public JSON endpoints when detected
- Generic HTML parsing fallback
- Daily HTML email digest with hyperlinks
- SQLite storage in `agent.db`

## Daily automation
This package includes a GitHub Actions workflow template. You can also keep the app local and run scans from the UI.
