# Last Session — 2026-02-22

## Changes Made

### CLAUDE.md (NEW)
- **CLAUDE.md** — NEW FILE. Project documentation including:
  - Architecture overview
  - Key components table
  - Database tables (14)
  - REST API endpoints for Atlas
  - Atlas integration details (spoke key, tools)
  - Cross-spoke rules with approved exceptions
  - Development notes
  - Running instructions
  - Ecosystem overview
  - Master Schema Reference

### Atlas Integration
- Atlas now has full Facebook-Monitor spoke support:
  - 5 tool schemas and handlers
  - Query classifier keywords
  - Unified search integration
  - RAG chunking and retrieval
  - System prompt mentions this spoke

## What to Test
1. Start the web UI: `python web_ui.py`
2. Verify `/api/health` returns OK
3. Test `/api/posts/search?q=test` returns JSON results
4. Test `/api/people?search=test` returns JSON results
5. Test from Atlas: chat "search Facebook Monitor posts about city council"
