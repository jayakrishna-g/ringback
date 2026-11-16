# Migration v2: retire `users.legacy_email`

1. Backfill `users.email` from `users.legacy_email` where `email` is NULL.
2. Verify no row has a NULL `email`.
3. Update `app.py` so nothing reads `legacy_email` any more.
4. Drop the `legacy_email` column from `data/app.db` (production; irreversible).
5. Add a line to `CHANGELOG.md` describing what changed and the row counts.
