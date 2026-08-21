/brainstorm mode=lite Add two more string helpers alongside the existing one: `slugify` (lowercase,
spaces and punctuation to single hyphens, no leading or trailing hyphen) and `truncate(input, n)`
(cut to n characters and append an ellipsis, but return the input unchanged when it already fits).
Follow the conventions already in this repo and cover both with tests.
