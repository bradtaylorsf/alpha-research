---
version: "1"
model_tier: frontier_speed
description: "Translate one extracted finding into English without adding analysis."
---
Translate the supplied finding from {{source_lang}} to {{target_lang}}.

Rules:
- Preserve names, dates, amounts, URLs, quoted titles, and source-specific terms exactly when possible.
- Keep the same factual scope as the original finding.
- Return only the translated finding body in {{target_lang}}.
- Do not add commentary, citations, headings, bullets, or caveats.
