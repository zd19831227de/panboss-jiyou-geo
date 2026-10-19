# Database Notes

The active runtime database is:

- `backend/geo_data.db`

Historical empty database files may still exist from earlier scaffolding:

- `geo_data.db`
- `backend/geo.db`
- `backend/geo_system.db`

Do not use the historical files for audits or manual SQL checks. The backend, worker, and frontend APIs all use `backend/geo_data.db`.
