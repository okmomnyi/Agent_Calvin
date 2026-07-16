# secrets/

Mounted into the containers at `/app/secrets` (see `docker-compose.yml`). **Gitignored** —
nothing in here is ever committed (§0: secrets never touch the repo).

Put **`token.json`** here on the droplet:

```bash
# on the laptop (it opens a browser):
python manage.py auth              # writes ./token.json
# then:
scp token.json agentos@<droplet>:~/AgentOS/secrets/
```

The containers read it via `GOOGLE_TOKEN_PATH=/app/secrets/token.json`.

Two things worth knowing:

- This is a **directory** mount, not a file mount, on purpose. Bind-mounting `./token.json`
  directly would have Docker silently create a *directory* by that name when the file is
  absent — and since the Gmail client only checks `path.exists()`, the clear
  "run `manage.py auth` first" error would turn into a baffling `IsADirectoryError`.
- It is mounted **read-write**, also on purpose. Gmail access tokens expire hourly and
  `_load_credentials()` writes the refreshed token straight back to this file; a read-only
  mount would crash the first time that happened.

`credentials.json` is **not** needed here — it's only used by the laptop-side OAuth flow that
mints the token in the first place.
