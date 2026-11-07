# Assigned Subjects and Individual Rights – Smart QPG Backend

Repository: `bibhusharma1/QPEngine`  
Local directory: `~/Documents/BibhuWorks/MasterDeOrca`

This document describes the backend implementation of teacher subject assignments and the four per-subject individual permissions ("rights"): View Papers, Upload & Process, Manage Bank, and Generate Papers. It is based solely on the actual backend code.

---

## 1. System Overview

### 1.1 Teacher subject assignments

- A **teacher** is a row in `users` with `role = 'teacher'`.
- A teacher can be assigned multiple subjects.
- Assignments are stored in two mechanisms:
  - Legacy table: `teacher_subjects` (`teacher_id`, `subject_name`).
  - Canonical tables: `user_subject_assignments` + `user_subject_permissions`.
- The canonical path is preferred; the legacy table is used as a fallback when no canonical assignment rows exist.
- Subject access is resolved at request time from the database (no cached token claims).

### 1.2 Individual permissions

- Each assigned subject can carry four independent boolean flags:
  1. `can_view_papers`
  2. `can_upload_process`
  3. `can_manage_question_bank`
  4. `can_generate_papers`
- Flags are stored in `user_subject_permissions`.
- Defaults in the schema are `1` (true) for all four, but rows may be absent; absence means **no access** under the current policy.
- Only `generate_papers` is actually enforced in route guards today; the other three columns exist but are not checked in any route.

### 1.3 Relationship between subjects, teachers, and permissions

- A subject is a registered row in `subjects` (`subject_id`, `subject_name`).
- Assignment is per user + subject (`UNIQUE(user_id, subject_name)`).
- Permission is per assignment (`UNIQUE(assignment_id)` and `UNIQUE(user_id, subject_name)`).
- Deleting a user cascades to assignments and permissions.
- Deleting an assignment cascades to its permission row.
- Removing a subject from a teacher's set removes the assignment and, through cascade/sync, its permission row.

---

## 2. Database Structure

### 2.1 `teacher_subjects` (legacy)

```sql
CREATE TABLE IF NOT EXISTS teacher_subjects (
    teacher_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    subject_name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(teacher_id, subject_name)
);
CREATE INDEX idx_teacher_subjects_subject ON teacher_subjects(subject_name);
```

### 2.2 `user_subject_assignments` (canonical)

```sql
CREATE TABLE IF NOT EXISTS user_subject_assignments (
    assignment_id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    subject_name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(user_id, subject_name)
);
CREATE INDEX idx_user_subject_assignments_user ON user_subject_assignments(user_id);
CREATE INDEX idx_user_subject_assignments_subject ON user_subject_assignments(subject_name);
```

### 2.3 `user_subject_permissions`

```sql
CREATE TABLE IF NOT EXISTS user_subject_permissions (
    permission_id TEXT PRIMARY KEY,
    assignment_id TEXT NOT NULL REFERENCES user_subject_assignments(assignment_id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    subject_name TEXT NOT NULL,
    can_view_papers INTEGER NOT NULL DEFAULT 1,
    can_upload_process INTEGER NOT NULL DEFAULT 1,
    can_manage_question_bank INTEGER NOT NULL DEFAULT 1,
    can_generate_papers INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(assignment_id),
    UNIQUE(user_id, subject_name)
);
CREATE INDEX idx_user_subject_permissions_user ON user_subject_permissions(user_id);
CREATE INDEX idx_user_subject_permissions_subject ON user_subject_permissions(subject_name);
```

### 2.4 Legacy migration note

`_migrate_teacher_subjects_to_user_assignments` backfills legacy `teacher_subjects` into `user_subject_assignments`, but explicitly does **not** create `user_subject_permissions` rows. The comment in code states: "Do not auto-grant permissions; leave permission records absent so the existing policy (no record => no access) applies until an admin sets them."

---

## 3. API Endpoints

### Legacy teacher subject endpoints

| Method | Route | Function | Auth |
|---|---|---|---|
| GET | `/teachers/{teacher_id}/subjects` | `list_teacher_subjects` | Admin |
| POST | `/teachers/{teacher_id}/subjects` | `assign_teacher_subjects` | AdminWrite |
| DELETE | `/teachers/{teacher_id}/subjects/{subject}` | `remove_teacher_subject` | AdminWrite |

- `POST` body: `TeacherSubjectAssign` = `{"subjects": [...], "subject": "..."}`. Either replace set or add one.
- Response: `{"teacher_id": int, "subjects": [str] | [objects]}`.

### Canonical user subject endpoints

| Method | Route | Function | Auth |
|---|---|---|---|
| GET | `/users/{user_id}/subjects` | `get_user_subject_assignments` | Admin |
| POST | `/users/{user_id}/subjects` | `assign_user_subjects_api` | AdminWrite |
| PUT | `/users/{user_id}/subjects/{subject}/permissions` | `update_user_subject_permissions_api` | AdminWrite |
| DELETE | `/users/{user_id}/subjects/{subject}` | `remove_user_subject_api` | AdminWrite |

- `POST` body: `UserSubjectAssign` = `{"subjects": [...], "permissions": {...}}`.
- `PUT` body: permission dict, e.g. `{"can_view_papers": 1, "can_upload_process": 0, "can_manage_question_bank": 1, "can_generate_papers": 1}`.
- Response: `{"user_id": int, "subjects": [...]}` or `{"user_id": int, "subject": str, "permissions": {...}}`.

### Teacher creation/editing

- `POST /teachers` and `PATCH /teachers/{teacher_id}` accept `subjects` array, which triggers `set_teacher_subjects` (legacy + canonical sync).

---

## 4. Backend Functions

File: `src/masterdeorca/database.py`

- `get_teacher_subjects(email)` — canonical first, legacy fallback.
- `set_teacher_subjects(teacher_id, subject_names)` — replaces legacy rows and syncs canonical assignments.
- `sync_user_subject_assignments(user_id, subject_names)` — creates missing assignments/permissions preserving flags; deletes removed subjects.
- `get_user_subjects(user_id)` / `get_user_subjects_by_email(email)` — return assignment + permission rows.
- `check_user_subject_permission(email, subject_name, permission)` — admin returns `True`; otherwise requires permission row and column.
- `assign_user_subjects(user_id, subjects, permissions)` — creates assignments/permissions.
- `update_user_subject_permissions(user_id, subject, permissions)` — updates flags.
- `remove_user_subject(user_id, subject)` / `remove_teacher_subject(teacher_id, subject)` — delete assignment (cascade).

File: `src/masterdeorca/security.py`

- `accessible_subjects(email)` — admin returns `None` (unrestricted); teacher returns legacy subject set.
- `require_subject_access(email, subject)` — admin bypass; teacher checks canonical assignments if present, else legacy.
- `filter_subjects(email, subjects)` — admin returns all; teacher filters by assignments.
- `subject_allowed(email, subject_id)` — admin unrestricted.

File: `src/masterdeorca/app.py`

- `list_subjects` — uses `filter_subjects`.
- `list_teacher_subjects`, `assign_teacher_subjects`, `remove_teacher_subject`.
- `get_user_subject_assignments`, `assign_user_subjects_api`, `update_user_subject_permissions_api`, `remove_user_subject_api`.

---

## 5. Individual Permission Implementation

### View Papers

- Stored: `user_subject_permissions.can_view_papers`.
- Retrieved: `get_user_subjects` / `get_user_subjects_by_email`.
- Enforced routes: **none** (not checked in any route; only subject access is checked).
- Operations: would gate PYQ viewing/retrieval, but currently no enforcement.
- Background jobs: none.

### Upload & Process

- Stored: `user_subject_permissions.can_upload_process`.
- Retrieved: same as above.
- Enforced routes: **none** (only `require_subject_access` in `start_processing`).
- Operations: would gate PDF upload/processing.
- Background jobs: none beyond subject access at enqueue.

### Manage Bank

- Stored: `user_subject_permissions.can_manage_question_bank`.
- Retrieved: same as above.
- Enforced routes: **none** (question bank create/update/delete use `require_write_user` + `require_subject_access`).
- Operations: would gate question bank viewing/modification.
- Background jobs: none.

### Generate Papers

- Stored: `user_subject_permissions.can_generate_papers`.
- Retrieved: same as above.
- Enforced routes: all `/api/question-papers/*` generation/draft/export routes call `check_user_subject_permission(user, subject, "generate_papers")`.
- Operations: allows/blocks question paper generation and draft operations.
- Background jobs: none (synchronous request checks).

---

## 6. Subject Assignment Lifecycle

1. Admin assigns subject(s) via legacy or canonical endpoint.
2. Legacy path: `set_teacher_subjects` deletes/reinserts `teacher_subjects`, then `sync_user_subject_assignments` creates canonical assignment rows and preserves/creates permission rows.
3. Canonical path: `assign_user_subjects` creates assignment + permission rows.
4. Persistence: rows stored in `user_subject_assignments` and `user_subject_permissions`.
5. Retrieval: `get_user_subjects` returns assignment + permission rows; `get_teacher_subjects` falls back to legacy.
6. Authorization: `require_subject_access` and `check_user_subject_permission` evaluate at request time.
7. Removal: assignment deleted; permission row cascades; legacy row deleted.

---

## 7. Administrator vs Teacher

- Role identified by `users.role == 'admin'`.
- Administrator bypasses subject assignment restrictions (`accessible_subjects` returns `None`; `require_subject_access` returns early).
- Administrator bypasses all permission checks (`check_user_subject_permission` returns `True`).
- Administrator Edit/Read-only mode (`require_write_user`) blocks mutations in readonly mode but allows reads.
- Teachers remain restricted to assigned subjects and their permission flags.

---

## 8. Current Authorization Architecture

- Permission checks are **distributed** (called inline in routes), not centralized.
- Only `generate_papers` is enforced; the other three columns are effectively informational.
- Assignment and permission records are coupled by FK cascade; permission rows may be absent for legacy-backfilled teachers.
- Inconsistency: `get_teacher_subjects` fallback can return subjects with `None` permissions, while `check_user_subject_permission` requires a row.
- Security risk: `view_papers`, `upload_process`, `manage_question_bank` are not enforced, so any authenticated user with subject access can perform them.

---

## 9. Future Simplification Considerations (not implemented)

Proposed future behavior:
- Assigning a subject automatically grants all four capabilities.
- Removing a subject removes access to all four capabilities.
- Individual permission checks remain in backend code.
- Existing structures/records preserved where possible.
- Administrator unaffected.

To achieve this in the future:
- Modify assignment creation (`assign_user_subjects`, `set_teacher_subjects`, teacher create/update) to create permission rows with all four flags = `1`.
- Keep `check_user_subject_permission` unchanged.
- Ensure removal deletes assignment + permission rows (already cascades).
- Optionally backfill existing assignments lacking permission rows.
- Do not change admin bypass or access mode.

---

## 10. Implementation Reference

- Database tables: `teacher_subjects`, `user_subject_assignments`, `user_subject_permissions` in `src/masterdeorca/migrations.py`.
- Database functions: `src/masterdeorca/database.py` (`get_teacher_subjects`, `set_teacher_subjects`, `sync_user_subject_assignments`, `get_user_subjects`, `get_user_subjects_by_email`, `check_user_subject_permission`, `assign_user_subjects`, `update_user_subject_permissions`, `remove_user_subject`, `remove_teacher_subject`).
- Security helpers: `src/masterdeorca/security.py` (`accessible_subjects`, `require_subject_access`, `filter_subjects`, `subject_allowed`).
- API routes: `src/masterdeorca/app.py` (`/teachers/{teacher_id}/subjects*`, `/users/{user_id}/subjects*`, `/users/{user_id}/subjects/{subject}/permissions`).
- Permission enforcement example: `check_user_subject_permission(user, subject, "generate_papers")` in question-paper routes.