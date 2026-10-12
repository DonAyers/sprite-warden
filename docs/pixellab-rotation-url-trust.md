# Offline PixelLab rotation URL resolution

Exact manifest URL registrations take precedence. Otherwise only literal HTTPS
rotation URLs on `backblaze.pixellab.ai` resolve through a registered character
folder inside the project. Signing queries do not change the selected view.
No download or provider request is performed.

The exact facing filename wins. If it is absent, explicit facing aliases are
considered, but multiple distinct resolved files are rejected conservatively,
even if their bytes happen to match. A gated alias cannot substitute for an
ambiguous or exact ungated/rejected view. Current gate, rejection and required
vision evidence checks still apply to the resolved file.

## Registration trust boundary

The account path segment is syntactically validated but is not bound to the
local registration. Resolution currently trusts the project owner's
`pixellab_characters` character-ID-to-folder registration. This is not evidence
that character IDs are globally unique across provider accounts, nor proof of
remote ownership. Use exact URL registration when account-specific binding is
needed; do not use this offline fallback as account authentication. A future
registered account binding can tighten this without making network requests.
