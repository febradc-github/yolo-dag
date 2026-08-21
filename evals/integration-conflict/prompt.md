/brainstorm mode=lite Add two independent runtime settings to this project: a `retryLimit`
setting (default 3) and a `logLevel` setting (default "info"). Both belong in the existing
central config module. Each should be covered by the existing `npm test` check.

Run the full pipeline through integration.
