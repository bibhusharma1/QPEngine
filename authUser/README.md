# Retired authentication prototype

The earlier standalone authentication prototype has been archived in `docs/archive/authUser-prototype/`. This folder is retained only for its nested Git history. It is not part of the active application or Docker image. MasterDeOrca uses the root FastAPI app in `src/masterdeorca/app.py`, the SQLite admin store in `src/masterdeorca/database.py`, and the single root `.env` file.

The prototype's local database was not present during migration. Its old code used plaintext demo passwords and is not used to authenticate the current API.
