# Contributing to Academic Advisor AI

Thank you for your interest in contributing to this project! Here's how you can help.

## Getting Started

1. **Fork the repository** and clone it locally
2. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   pip install pytest pytest-cov  # for testing
   ```
3. **Create a feature branch**:
   ```bash
   git checkout -b feature/your-feature-name
   ```

## Code Style

- **Python**: Follow [PEP 8](https://www.python.org/dev/peps/pep-0008/)
- **Docstrings**: Include docstrings for all functions (Google style)
- **Comments**: Explain the "why", not the "what"
- **Type hints**: Use type hints where possible (Python 3.8+)

### Example Function
```python
def find_students(sheets: dict, query: str) -> list:
    """
    Search for students across Excel sheets by ID, English name, or Arabic name.
    
    Supports case-insensitive substring matching and normalizes Arabic text
    to handle diacritics and character variations.
    
    Args:
        sheets: Dictionary of sheet_name -> DataFrame
        query: Student ID, name (English or Arabic), or comma-separated queries
        
    Returns:
        List of (sheet_name, DataFrame) tuples sorted by match score
    """
    # Implementation...
```

## Testing

Before submitting a PR:

1. **Test locally**:
   ```bash
   python advisor_gui.py
   ```
2. **Test edge cases**: Empty sheets, special characters, long names, multiple key rotation, etc.
3. **Test with multiple Excel formats**: `.xls` and `.xlsx`
4. **Test with multiple API keys**: Ensure key rotation works correctly
5. **Run unit tests** (once added):
   ```bash
   pytest tests/ -v
   ```

## Areas for Contribution

### High Priority (Phase 1)
- [ ] Unit tests for `advisor_core.py` (test coverage: prerequisites, Arabic search, JSON validation)
- [ ] Error message improvements and user-friendly tooltips
- [ ] `config.yaml` support (externalize constants)
- [ ] Response schema enforcement (strict JSON typing)

### Medium Priority (Phase 2)
- [ ] Async batch processing with `asyncio.Semaphore` (3–5 concurrent)
- [ ] Web UI prototype (FastAPI + lightweight frontend)
- [ ] SQLite history backend (track recommendation sessions)
- [ ] RAG implementation with ChromaDB

### Low Priority (Phase 3+)
- [ ] Dark/light mode toggle
- [ ] Keyboard shortcuts
- [ ] Localization (translations)
- [ ] Fine-tuning infrastructure

## Reporting Bugs

Use GitHub Issues with the following template:

```
**Describe the bug**
A clear description of what the bug is.

**To reproduce**
Steps to reproduce the behavior:
1. Upload catalog → ...
2. Search for → ...
3. Click → ...
4. Error appears

**Expected behavior**
What should have happened.

**Screenshots**
If applicable, attach screenshots.

**Environment**
- OS: [e.g., macOS, Windows, Linux]
- Python version: [e.g., 3.9]
- Installed packages: (output of `pip list`)

**Additional context**
Any other context about the problem.
```

## Submitting Pull Requests

1. **Update the README** if your changes affect user-facing features
2. **Update ARCHITECTURE.md** if you change system design
3. **Write clear commit messages**:
   - `feat: add async batch processing`
   - `fix: correct Arabic normalization for hamza characters`
   - `docs: expand troubleshooting section`
4. **Link related issues**: `Fixes #123` in the PR description
5. **Request review** from maintainers

### PR Checklist
- [ ] Code follows PEP 8
- [ ] All functions have docstrings
- [ ] Manual testing completed
- [ ] Unit tests written (if applicable)
- [ ] README/ARCHITECTURE updated (if needed)
- [ ] No breaking changes (or clearly documented)
- [ ] Commit messages are clear and atomic

## Git Workflow

```bash
# Create feature branch
git checkout -b feature/your-feature

# Make changes
git add .
git commit -m "feat: your feature description"

# Push and create PR
git push origin feature/your-feature
```

## Performance Considerations

When optimizing the app, keep these in mind:

- **Single recommendation**: Currently ~45–60s (3 Gemini calls)
- **Batch of 100**: Currently ~76–90 minutes (sequential)
- **Target for Phase 2**: Async batch of 100 in ~15–20 minutes (5 concurrent)

If you improve performance, update the timing table in `README.md`.

## Documentation

- **README.md**: User-facing guide, features, quick start
- **ARCHITECTURE.md**: Technical design, data structures, system diagram
- **This file (CONTRIBUTING.md)**: Development workflow
- **Code comments**: Explain tricky logic and design decisions

## Questions or Need Help?

Feel free to open a Discussion or reach out in an Issue. We're here to help!

---

**Code of Conduct**: Be respectful, inclusive, and professional. All contributors are expected to adhere to basic standards of courtesy and respect.
