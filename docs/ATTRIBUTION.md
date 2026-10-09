# Attribution

Third-party licenses this repository honors. Kept separate from `LICENSE` (Apache-2.0, this
code) because attribution survives renames and refactors.

## 9router

- Project: `9router-app` v0.5.95
- Copyright (c) 2024-2026 decolua and contributors
- License: MIT (verified from the project's own `LICENSE` file, 2026-10-09)
- Relationship to this repository: `engrix-router` reimplements observed behavior of this
  Node/JavaScript gateway in Python. **No source code was copied** -- the languages differ and
  every citation of a 9router file in this tree (`path.js:line`) is an analysis pointer that
  explains a deviation, not a transplanted snippet. The porting map and the defect list that
  motivated each deviation live in [ARCHITECTURE.md](ARCHITECTURE.md) and `docs/adr/`.
- Why the notice ships anyway: MIT requires the copyright + permission notice in copies and
  substantial portions; this file includes it so the obligation is met even for behavior-level
  derivation, and so the compatibility story (MIT -> Apache-2.0, one-way) is on the record.

The MIT text, verbatim:

```
MIT License

Copyright (c) 2024-2026 decolua and contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Inter and JetBrains Mono

The dashboard loads these two webfonts from Google Fonts (the same way the owner's other
products do). Both are OFL-licensed; the repository ships no font files, only the stylesheet
link, so no redistribution occurs.

## Brand assets

The logo, lockup and mascot SVGs under `src/engrix_router/web/static/assets/` are the owner's
own brand pack (Engrix), not third-party work.
