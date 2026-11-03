# Backend API Integration Test Report (Live: https://qpengine-6f07.onrender.com)

## Summary
- OpenAPI paths: 70
- Health: 200 OK
- Auth/CORS/validation working as expected
- Protected endpoints require auth (401)
- Assignment routes present for both teachers and users

## Key findings
- `/teachers/{teacher_id}/subjects` supports GET/POST/DELETE (no PUT/PATCH in OpenAPI)
- `/users/{user_id}/subjects` supports GET/POST
- `/users/{user_id}/subjects/{subject}/permissions` supports PUT
- Frontend calling PUT /teachers/{id}/subjects would not match backend routes - needs adjustment or backend support

## Status
Backend is functional for core flows. Frontend/backend method mismatch exists for teacher subject updates.
