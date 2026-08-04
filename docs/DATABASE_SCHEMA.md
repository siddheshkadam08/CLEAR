---
title: "CLEAR — Database Schema Reference"
subtitle: "All 36 tables, columns, keys, indexes and triggers"
author: "IRIS RegTech — Engineering"
date: "2026-07-31"
---

# How to read this document

This is the complete reference for the CLEAR database: every table, every column,
every constraint, every index. It is the companion to
[`BACKEND_GUIDE.md`](BACKEND_GUIDE.md) — that document explains *how the system
works*; this one documents *what is stored*.

> **To turn this into a Word document**, run:
>
> ```
> pandoc docs/DATABASE_SCHEMA.md -o CLEAR-Database-Schema.docx --toc --toc-depth=3
> ```
>
> Diagrams are plain text inside code blocks so they survive the conversion.
> Consider landscape orientation — several column tables are wide.

## Conventions used throughout

| Notation | Meaning |
| --- | --- |
| **PK** | Primary key |
| **FK →** | Foreign key, with the referenced table and its `ON DELETE` behaviour |
| **UQ** | Unique constraint |
| `NOT NULL` | Column is required |
| *(mixin)* | Column comes from a shared mixin — see Chapter 2 |
| `→ CASCADE` | Deleting the parent deletes this row |
| `→ SET NULL` | Deleting the parent blanks this column but keeps the row |
| `→ RESTRICT` | The parent cannot be deleted while this row exists |

## Type legend

| Type | What it is | Why we use it here |
| --- | --- | --- |
| `uuid` | 128-bit identifier | All primary keys. Generated in Python so an id exists before the row is written, letting a whole object graph be built in one transaction. |
| `citext` | Case-insensitive text | Email addresses. Uniqueness works without every query remembering to `lower()` both sides. |
| `varchar(n)` | Bounded text | Names, codes, paths. The bound is documentation as much as constraint. |
| `text` | Unbounded text | Clause bodies, descriptions, notes. |
| `jsonb` | Binary JSON | Flexible structures that still need indexing. **Never** the `json` type — `jsonb` supports GIN indexes and containment queries. |
| `timestamptz` | Timestamp with time zone | Every time value. Never a naive timestamp. |
| `date` | Calendar date | Contract dates — an effective date has no time zone. |
| `numeric(p,s)` | Exact decimal | Confidence scores and money. **Not** floats: these values are shown to users and compared against thresholds. |
| `int` / `bigint` | Integer | Counts, scores, sizes. `bigint` for file sizes. |
| `bool` | True/false | Flags. |
| `halfvec(2048)` | pgvector half-precision vector | The embedding column. See section 6.2. |
| `tsvector` | Postgres full-text vector | Keyword search over chunk text. Trigger-maintained. |
| *enum types* | Native Postgres enums | Closed sets — see Chapter 10. |

\newpage

# Chapter 1 — The map

## 1.1 Table index

36 tables, in nine functional groups.

| # | Table | Group | What it holds |
| --- | --- | --- | --- |
| 1 | `users` | Identity | People who can sign in |
| 2 | `roles` | Identity | The four named permission sets |
| 3 | `refresh_tokens` | Identity | Active sessions |
| 4 | `projects` | Identity | **The security boundary** |
| 5 | `project_members` | Identity | Who is in which project, with which role |
| 6 | `project_activities` | Identity | Human-readable activity feed |
| 7 | `contracts` | Documents | One row per uploaded document |
| 8 | `contract_versions` | Documents | Every uploaded revision of a document |
| 9 | `contract_metadata` | Documents | Flat, indexed projection used by filters and dashboards |
| 10 | `document_profiles` | Documents | Document Intelligence Profiles (DIPs) |
| 11 | `processing_jobs` | Pipeline | One processing run per contract; the state machine |
| 12 | `job_stage_runs` | Pipeline | One row per stage attempt — **the checkpoints** |
| 13 | `document_artifacts` | Pipeline | Pointers to stage outputs in object storage |
| 14 | `chunks` | Retrieval | Meaning-preserving passages + keyword index |
| 15 | `embeddings` | Retrieval | The three-level vector store |
| 16 | `clauses` | Knowledge | Extracted contract terms |
| 17 | `entities` | Knowledge | Parties, organisations, people |
| 18 | `obligations` | Knowledge | Who must do what, by when |
| 19 | `risks` | Knowledge | Risk findings with severity |
| 20 | `key_dates` | Knowledge | The contract timeline |
| 21 | `knowledge_relationships` | Knowledge | Cross-references and dependencies |
| 22 | `contract_summaries` | Knowledge | Generated summaries, one per format |
| 23 | `graph_nodes` | Graph | Knowledge-graph nodes |
| 24 | `graph_edges` | Graph | Knowledge-graph edges |
| 25 | `clause_master_categories` | Governance | The admin-editable clause taxonomy |
| 26 | `clause_master_rules` | Governance | Versioned extraction rules per category |
| 27 | `ai_settings` | Governance | Runtime AI configuration override |
| 28 | `alerts` | Operations | Actionable alerts |
| 29 | `alert_rules` | Operations | Configurable alert thresholds |
| 30 | `export_jobs` | Operations | Export requests and their files |
| 31 | `chat_sessions` | Copilot | Conversations |
| 32 | `chat_messages` | Copilot | Turns, with citations and provenance |
| 33 | `audit_log` | Audit | Immutable record of every mutating request |
| 34 | `contract_history` | Audit | Field-level change trail for contracts |
| 35 | `clause_history` | Audit | Change trail + human review decisions |
| 36 | `retrieval_audit` | Audit | What was searched/answered, and how |

## 1.2 The relationship map

```
  ┌───────┐                          ┌─────────┐
  │ roles │◄────────┐                │  users  │
  └───────┘         │                └────┬────┘
                    │                     │
              ┌─────┴──────────┐          │
              │ project_members├──────────┘
              └─────┬──────────┘
                    │
              ┌─────▼────────┐          ┌────────────────────┐
              │   projects   │──────────► project_activities │
              │  (BOUNDARY)  │          └────────────────────┘
              └─────┬────────┘
                    │
              ┌─────▼─────────────────────────────────────────────┐
              │                  contracts                        │
              └──┬────────┬─────────┬──────────┬──────────┬───────┘
                 │        │         │          │          │
     ┌───────────▼──┐ ┌───▼──────┐ ┌▼─────────┐│ ┌────────▼─────────┐
     │contract_     │ │contract_ │ │processing││ │ document_        │
     │versions      │ │metadata  │ │_jobs     ││ │ artifacts        │
     └──────────────┘ └──────────┘ └────┬─────┘│ └──────────────────┘
                                        │      │
                              ┌─────────▼────┐ │
                              │job_stage_runs│ │
                              └──────────────┘ │
                                               │
        ┌──────────────────────────────────────┘
        │
        ├──► chunks ─────────┬──────────────┐
        │       ▲            │              │
        │       │ chunk_id   │              │
        ├──► clauses ────────┤              │
        │       ▲            │              │
        │       │ clause_id  │              │
        ├──► obligations ────┤        ┌─────▼──────┐
        ├──► risks ──────────┤        │ embeddings │
        ├──► key_dates ──────┤        │  L1 / L2 / │
        ├──► entities ───────┘        │  L3        │
        ├──► knowledge_relationships  └────────────┘
        └──► contract_summaries          (ref_id points at a
                                          contract, clause or
   graph_nodes ──► graph_edges            chunk — polymorphic)

  Governance / operations (project- or platform-scoped):
     clause_master_categories ──► clause_master_rules
     ai_settings
     alert_rules ──► alerts ──► contracts
     export_jobs
     chat_sessions ──► chat_messages
     audit_log · contract_history · clause_history · retrieval_audit
```

## 1.3 The one rule that governs the whole schema

**Every table holding contract-derived data carries `project_id`, and every read
filters on it.**

This is not merely a convention enforced in application code. A database trigger
(`cip_assert_project_scope`, Chapter 9) rejects any insert or update where a row's
`project_id` disagrees with its contract's `project_id`. A bug that mis-scoped a
write would otherwise leak one project's clauses into another project's search
results — so it is enforced where it cannot be bypassed.

Fourteen tables carry that trigger: `chunks`, `clauses`, `entities`, `obligations`,
`risks`, `key_dates`, `knowledge_relationships`, `contract_summaries`, `embeddings`,
`document_artifacts`, `job_stage_runs`, `processing_jobs`, `contract_versions`,
`contract_metadata`.

\newpage

# Chapter 2 — Shared building blocks (mixins)

Rather than re-declaring the same columns on twenty tables (and occasionally
forgetting one), cross-cutting columns live in mixins. Each table below states which
mixins it uses; expand them here.

## 2.1 `UUIDPrimaryKeyMixin`

| Column | Type | Null | Default | Description |
| --- | --- | --- | --- | --- |
| `id` | `uuid` | NOT NULL | `uuid_generate_v4()` | **PK.** Generated in Python (`uuid4`) so the id exists before `flush()`. |

## 2.2 `TimestampMixin`

| Column | Type | Null | Default | Description |
| --- | --- | --- | --- | --- |
| `created_at` | `timestamptz` | NOT NULL | `now()` | Indexed. |
| `updated_at` | `timestamptz` | NOT NULL | `now()` | Maintained by **both** the ORM and a database trigger, so a bulk `UPDATE` issued outside the ORM still refreshes it. |

## 2.3 `SoftDeleteMixin`

| Column | Type | Null | Default | Description |
| --- | --- | --- | --- | --- |
| `deleted_at` | `timestamptz` | NULL | `NULL` | `deleted_at IS NULL` is the "live row" predicate. Nothing legally traceable is ever hard-deleted. |

## 2.4 `EvidenceMixin` — provenance for anything AI-derived

Applied to `chunks`, `clauses`, `entities`, `obligations`, `risks`, `key_dates`.

| Column | Type | Null | Default | Description |
| --- | --- | --- | --- | --- |
| `page_start` | `int` | NULL | — | First page this item appears on. |
| `page_end` | `int` | NULL | — | Last page. A clause spanning a page break keeps one row with a range. |
| `bounding_boxes` | `jsonb` | NOT NULL | `'[]'` | `[{page_number, x, y, width, height}]` — exactly what the PDF viewer needs to draw a highlight. |
| `evidence` | `jsonb` | NOT NULL | `'{}'` | Supporting detail: source quote, matched pattern, extraction path. |

## 2.5 `ExtractionProvenanceMixin` — which AI produced this

Applied to `clauses`, `entities`, `obligations`, `risks`, `key_dates`,
`knowledge_relationships`, `contract_summaries`.

| Column | Type | Null | Default | Description |
| --- | --- | --- | --- | --- |
| `confidence` | `numeric(5,4)` | NULL | — | 0..1 model confidence. Indexed. Exact decimal, not float — it is shown to users and compared against thresholds. |
| `validation_score` | `numeric(5,4)` | NULL | — | Result of post-extraction validation. |
| `review_status` | `varchar(32)` | NOT NULL | `'not_required'` | `not_required` · `pending` · `approved` · `rejected` · `corrected` |
| `profile_version` | `varchar(32)` | NULL | — | DIP version in effect. |
| `prompt_version` | `varchar(32)` | NULL | — | Prompt template version. |
| `model_version` | `varchar(128)` | NULL | — | The exact model. |
| `artifact_version` | `varchar(32)` | NULL | — | Source artifact version. |

> **Why per row rather than per job:** a contract can be partially reprocessed,
> leaving clauses from two different prompt versions side by side. Each must remain
> individually reproducible.

\newpage

# Chapter 3 — Identity and access

## 3.1 `users`

People who can sign in — local password accounts and Microsoft SSO identities.

*Mixins: UUID PK · Timestamps · Soft delete*

| Column | Type | Null | Default | Description |
| --- | --- | --- | --- | --- |
| `id` | `uuid` | NOT NULL | `uuid_generate_v4()` | **PK** |
| `email` | `citext` | NOT NULL | — | **UQ**, indexed. Case-insensitive by column type. |
| `full_name` | `varchar(255)` | NOT NULL | — | Display name. |
| `password_hash` | `varchar(255)` | NULL | — | Argon2 (or bcrypt) digest. **NULL for SSO-only accounts** — they have no local credential to steal. |
| `is_active` | `bool` | NOT NULL | `true` | Deactivating takes effect on the very next request. |
| `is_system_admin` | `bool` | NOT NULL | `false` | The only role that crosses project boundaries. |
| `must_change_password` | `bool` | NOT NULL | `false` | Blocks all normal API use until changed. |
| `auth_provider` | `auth_provider` | NOT NULL | `'local'` | `local` · `microsoft` |
| `external_subject` | `varchar(255)` | NULL | — | Immutable subject claim from the identity provider. The SSO join key — an email can be reassigned, an object id cannot. |
| `job_title` | `varchar(150)` | NULL | — | |
| `department` | `varchar(150)` | NULL | — | |
| `avatar_url` | `varchar(1024)` | NULL | — | |
| `locale` | `varchar(16)` | NOT NULL | `'en'` | |
| `timezone` | `varchar(64)` | NOT NULL | `'UTC'` | |
| `preferences` | `jsonb` | NOT NULL | `'{}'` | UI preferences only — theme, default project, table density. **Never security data.** |
| `last_login_at` | `timestamptz` | NULL | — | |
| `failed_login_count` | `int` | NOT NULL | `0` | Reset on success. |
| `locked_until` | `timestamptz` | NULL | — | Temporary lockout after repeated failures. |
| `created_at` | `timestamptz` | NOT NULL | `now()` | *(mixin)* |
| `updated_at` | `timestamptz` | NOT NULL | `now()` | *(mixin)* |
| `deleted_at` | `timestamptz` | NULL | — | *(mixin)* |

**Constraints**

- `uq_users_provider_subject` — UQ (`auth_provider`, `external_subject`)

**Indexes**

| Index | Columns | Type | Purpose |
| --- | --- | --- | --- |
| `ix_users_active` | `is_active` | partial `WHERE deleted_at IS NULL` | Live user listing. |
| `ix_users_full_name_trgm` | `full_name` | GIN trigram | Fuzzy name search in the member picker. |

---

## 3.2 `roles`

The four named permission sets.

*Mixins: UUID PK · Timestamps*

| Column | Type | Null | Default | Description |
| --- | --- | --- | --- | --- |
| `id` | `uuid` | NOT NULL | `uuid_generate_v4()` | **PK** |
| `name` | `role_name` | NOT NULL | — | **UQ**, indexed. `system_admin` · `project_manager` · `reviewer` · `viewer` |
| `display_name` | `varchar(100)` | NOT NULL | — | UI label ("Contract Manager"). |
| `description` | `varchar(500)` | NULL | — | |
| `permissions` | `jsonb` | NOT NULL | `'[]'` | List of `Permission` values. Editable by an admin **without a migration**. |
| `is_system` | `bool` | NOT NULL | `true` | Seeded roles cannot be deleted or renamed through the API. |
| `rank` | `int` | NOT NULL | `0` | Higher rank wins if a user somehow holds two roles on one project. |
| `created_at`, `updated_at` | `timestamptz` | NOT NULL | `now()` | *(mixin)* |

> **Note:** the System Admin's `permissions` list is complete, but the actual bypass
> is granted by `users.is_system_admin`. An accidental edit here therefore cannot
> lock every administrator out of the platform.

---

## 3.3 `refresh_tokens`

Active sessions. Only the digest is stored.

*Mixins: UUID PK · Timestamps*

| Column | Type | Null | Default | Description |
| --- | --- | --- | --- | --- |
| `id` | `uuid` | NOT NULL | `uuid_generate_v4()` | **PK** |
| `user_id` | `uuid` | NOT NULL | — | **FK →** `users.id` `→ CASCADE`, indexed |
| `token_hash` | `varchar(64)` | NOT NULL | — | **UQ**, indexed. SHA-256 of the token — the raw token is never stored. |
| `expires_at` | `timestamptz` | NOT NULL | — | |
| `revoked_at` | `timestamptz` | NULL | — | Set on rotation or logout. |
| `replaced_by_id` | `uuid` | NULL | — | **FK →** `refresh_tokens.id` `→ SET NULL`. The rotation chain. |
| `user_agent` | `varchar(512)` | NULL | — | Shown on the "active sessions" screen. |
| `ip_address` | `varchar(64)` | NULL | — | |
| `created_at`, `updated_at` | `timestamptz` | NOT NULL | `now()` | *(mixin)* |

**Indexes**

| Index | Columns | Type | Purpose |
| --- | --- | --- | --- |
| `ix_refresh_tokens_user_active` | `user_id`, `expires_at` | partial `WHERE revoked_at IS NULL` | Session list and validity check. |

> **Why the rotation chain matters:** rotation revokes the old row and records its
> successor. Replaying a stolen token therefore hits a *revoked* row, and the chain
> identifies exactly which session was compromised.

---

## 3.4 `projects`

**The security boundary.** Contracts live inside a project; access is granted per
project.

*Mixins: UUID PK · Timestamps · Soft delete*

| Column | Type | Null | Default | Description |
| --- | --- | --- | --- | --- |
| `id` | `uuid` | NOT NULL | `uuid_generate_v4()` | **PK** |
| `name` | `varchar(255)` | NOT NULL | — | |
| `slug` | `varchar(140)` | NOT NULL | — | **UQ**, indexed. URL-safe identifier. |
| `description` | `text` | NULL | — | |
| `status` | `project_status` | NOT NULL | `'active'` | `active` · `archived` · `on_hold` |
| `organization_id` | `uuid` | NOT NULL | — | Single-organisation deployment: a label for reporting and future partitioning, **never** an authorisation check. |
| `client_name` | `varchar(255)` | NULL | — | |
| `department` | `varchar(150)` | NULL | — | |
| `business_unit` | `varchar(150)` | NULL | — | |
| `default_language` | `varchar(16)` | NOT NULL | `'en'` | |
| `settings` | `jsonb` | NOT NULL | `'{}'` | Per-project overrides: default profile, review thresholds, alert windows, processing priority, retention. Read by the Workflow Engine when planning a job. |
| `created_by` | `uuid` | NULL | — | **FK →** `users.id` `→ SET NULL` |
| `contract_count` | `int` | NOT NULL | `0` | Denormalised counter. |
| `ready_contract_count` | `int` | NOT NULL | `0` | Denormalised counter. |
| `last_activity_at` | `timestamptz` | NULL | — | |
| `created_at`, `updated_at`, `deleted_at` | `timestamptz` | | | *(mixins)* |

**Indexes**

| Index | Columns | Type | Purpose |
| --- | --- | --- | --- |
| `ix_projects_status_active` | `status` | partial `WHERE deleted_at IS NULL` | Project list. |
| `ix_projects_name_trgm` | `name` | GIN trigram | Fuzzy project search. |

> **Why the denormalised counters:** dashboards read these instead of counting
> millions of rows on every page load. The scheduler reconciles them, so drift is
> self-healing rather than permanent.

---

## 3.5 `project_members`

The `(user, project, role)` authorisation triple. One row per user per project.

*Mixins: UUID PK · Timestamps*

| Column | Type | Null | Default | Description |
| --- | --- | --- | --- | --- |
| `id` | `uuid` | NOT NULL | `uuid_generate_v4()` | **PK** |
| `project_id` | `uuid` | NOT NULL | — | **FK →** `projects.id` `→ CASCADE`, indexed |
| `user_id` | `uuid` | NOT NULL | — | **FK →** `users.id` `→ CASCADE`, indexed |
| `role_id` | `uuid` | NOT NULL | — | **FK →** `roles.id` `→ RESTRICT` — a role in use cannot be deleted. |
| `added_by` | `uuid` | NULL | — | **FK →** `users.id` `→ SET NULL` |
| `permission_overrides` | `jsonb` | NOT NULL | `'[]'` | **Narrows** a role for one member. Intersected with the role's permissions, so it can never widen access. |
| `is_favourite` | `bool` | NOT NULL | `false` | Starred in the project switcher. |
| `last_accessed_at` | `timestamptz` | NULL | — | |
| `created_at`, `updated_at` | `timestamptz` | NOT NULL | `now()` | *(mixin)* |

**Constraints**

- `uq_project_members_project_id_user_id` — UQ (`project_id`, `user_id`)

**Indexes**

| Index | Columns | Purpose |
| --- | --- | --- |
| `ix_project_members_user_project` | `user_id`, `project_id` | "Which projects can this user see?" — resolved on every request. |

---

## 3.6 `project_activities`

Human-readable activity feed.

*Mixins: UUID PK (own `created_at`)*

| Column | Type | Null | Default | Description |
| --- | --- | --- | --- | --- |
| `id` | `uuid` | NOT NULL | `uuid_generate_v4()` | **PK** |
| `project_id` | `uuid` | NOT NULL | — | **FK →** `projects.id` `→ CASCADE`, indexed |
| `user_id` | `uuid` | NULL | — | **FK →** `users.id` `→ SET NULL` |
| `activity_type` | `varchar(64)` | NOT NULL | — | e.g. `contracts_uploaded` |
| `summary` | `varchar(500)` | NOT NULL | — | The sentence shown in the feed. |
| `entity_type` | `varchar(64)` | NULL | — | |
| `entity_id` | `uuid` | NULL | — | Polymorphic — not a FK. |
| `payload` | `jsonb` | NOT NULL | `'{}'` | Counts and detail for a rich card. |
| `created_at` | `timestamptz` | NOT NULL | `now()` | Indexed. |

**Indexes**

| Index | Columns | Purpose |
| --- | --- | --- |
| `ix_project_activities_project_created` | `project_id`, `created_at` | The feed query is always "this project, newest first". |

> **Not the same as `audit_log`.** The audit log is a complete, immutable compliance
> record. This is a small, presentation-shaped feed that can be pruned without
> losing the audit trail.

\newpage

# Chapter 4 — Documents

## 4.1 `contracts`

One uploaded document.

*Mixins: UUID PK · Timestamps · Soft delete*

| Column | Type | Null | Default | Description |
| --- | --- | --- | --- | --- |
| `id` | `uuid` | NOT NULL | `uuid_generate_v4()` | **PK** |
| `project_id` | `uuid` | NOT NULL | — | **FK →** `projects.id` `→ CASCADE`, indexed |
| `uploaded_by` | `uuid` | NULL | — | **FK →** `users.id` `→ SET NULL` |
| `original_file_name` | `varchar(512)` | NOT NULL | — | As uploaded. |
| `storage_path` | `varchar(1024)` | NOT NULL | — | Object-storage key of the current version. |
| `file_type` | `file_type` | NOT NULL | — | `pdf` · `docx` |
| `file_size` | `bigint` | NOT NULL | — | Bytes. `CHECK > 0`. |
| `sha256_hash` | `varchar(64)` | NOT NULL | — | Indexed. The duplicate-detection key. |
| `mime_type` | `varchar(128)` | NULL | — | |
| `page_count` | `int` | NULL | — | Filled by validation/parsing. |
| `title` | `varchar(512)` | NULL | — | Extracted title; falls back to the filename in the UI. |
| `contract_number` | `varchar(128)` | NULL | — | Indexed. |
| `agreement_type` | `varchar(64)` | NULL | — | Indexed. **Extensible** (VARCHAR, not an enum) — classification may emit a type the seeded taxonomy lacks. |
| `agreement_subtype` | `varchar(64)` | NULL | — | |
| `status` | `contract_status` | NOT NULL | `'uploaded'` | Indexed. `uploaded` · `processing` · `ready` · `failed` · `needs_review` · `archived` |
| `current_version` | `int` | NOT NULL | `1` | Points at the active `contract_versions` row. |
| `profile_id` | `uuid` | NULL | — | **FK →** `document_profiles.id` `→ SET NULL`. The DIP that processed it. |
| `profile_version` | `varchar(32)` | NULL | — | **Pinned** — a later profile revision never rewrites how this contract was interpreted. |
| `classification_confidence` | `numeric(5,4)` | NULL | — | |
| `language` | `varchar(16)` | NULL | — | |
| `needs_review` | `bool` | NOT NULL | `false` | True when any extracted item awaits a human. |
| `processed_at` | `timestamptz` | NULL | — | Set when indexing completes. |
| `tags` | `jsonb` | NOT NULL | `'[]'` | User annotations — not AI-derived. |
| `notes` | `text` | NULL | — | |
| `created_at`, `updated_at`, `deleted_at` | `timestamptz` | | | *(mixins)* |

**Constraints**

- `uq_contracts_project_id_sha256_hash` — UQ (`project_id`, `sha256_hash`)
  → **duplicate detection is per project.** The same document in two projects is fine.
- `ck_contracts_file_size_positive` — `CHECK (file_size > 0)`

**Indexes**

| Index | Columns | Type | Purpose |
| --- | --- | --- | --- |
| `ix_contracts_project_status_created` | `project_id`, `status`, `created_at` | btree | The repository listing. |
| `ix_contracts_project_live` | `project_id`, `created_at` | partial `WHERE deleted_at IS NULL` | Live listing. |
| `ix_contracts_project_agreement_type` | `project_id`, `agreement_type` | btree | Type filter. |
| `ix_contracts_title_trgm` | `title` | GIN trigram | Fuzzy title search. |
| `ix_contracts_filename_trgm` | `original_file_name` | GIN trigram | Fuzzy filename search. |
| `ix_contracts_needs_review` | `project_id` | partial `WHERE needs_review = true AND deleted_at IS NULL` | The review queue. |
| `ix_contracts_number_lower` | `project_id`, `lower(contract_number)` | expression, partial | Case-insensitive lookup — users paste contract numbers in any case. |

---

## 4.2 `contract_versions`

An immutable snapshot of a contract's source file.

*Mixins: UUID PK (own `created_at`)*

| Column | Type | Null | Default | Description |
| --- | --- | --- | --- | --- |
| `id` | `uuid` | NOT NULL | `uuid_generate_v4()` | **PK** |
| `contract_id` | `uuid` | NOT NULL | — | **FK →** `contracts.id` `→ CASCADE`, indexed |
| `project_id` | `uuid` | NOT NULL | — | **FK →** `projects.id` `→ CASCADE`, indexed |
| `version` | `int` | NOT NULL | — | 1, 2, 3… |
| `storage_path` | `varchar(1024)` | NOT NULL | — | This version's own object key. |
| `sha256_hash` | `varchar(64)` | NOT NULL | — | |
| `file_size` | `bigint` | NOT NULL | — | |
| `original_file_name` | `varchar(512)` | NOT NULL | — | |
| `uploaded_by` | `uuid` | NULL | — | **FK →** `users.id` `→ SET NULL` |
| `change_note` | `varchar(500)` | NULL | — | "Initial upload." / "Replacement upload." |
| `created_at` | `timestamptz` | NOT NULL | `now()` | |

**Constraints**

- `uq_contract_versions_contract_id_version` — UQ (`contract_id`, `version`)

> **Old bytes are never overwritten.** Earlier extractions reference them, and
> overwriting would break the provenance chain.

---

## 4.3 `contract_metadata`

The flat, heavily-indexed projection that powers filters, dashboards, alerts and the
metadata pre-filter in front of vector search. **One row per contract** — the PK is
`contract_id`, not a separate id.

*Mixins: Timestamps*

| Column | Type | Null | Default | Description |
| --- | --- | --- | --- | --- |
| `contract_id` | `uuid` | NOT NULL | — | **PK**, **FK →** `contracts.id` `→ CASCADE` |
| `project_id` | `uuid` | NOT NULL | — | **FK →** `projects.id` `→ CASCADE`, indexed |
| **Dates** | | | | |
| `effective_date` | `date` | NULL | — | |
| `execution_date` | `date` | NULL | — | |
| `expiration_date` | `date` | NULL | — | Drives the "expiring soon" KPI. |
| `renewal_date` | `date` | NULL | — | |
| `notice_deadline` | `date` | NULL | — | Drives the auto-renewal alert. |
| `term_months` | `int` | NULL | — | |
| **Legal** | | | | |
| `governing_law` | `varchar(150)` | NULL | — | |
| `jurisdiction` | `varchar(150)` | NULL | — | |
| `country` | `varchar(100)` | NULL | — | |
| `language` | `varchar(16)` | NULL | — | |
| **Commercial** | | | | |
| `currency` | `varchar(8)` | NULL | — | |
| `contract_value` | `numeric(20,2)` | NULL | — | |
| `payment_terms_days` | `int` | NULL | — | |
| `payment_terms` | `varchar(255)` | NULL | — | |
| **Parties** | | | | |
| `party_a` | `varchar(255)` | NULL | — | Shown as "Party A". |
| `party_b` | `varchar(255)` | NULL | — | |
| `vendor` | `varchar(255)` | NULL | — | |
| `customer` | `varchar(255)` | NULL | — | |
| **Classification / ownership** | | | | |
| `category` | `varchar(100)` | NULL | — | |
| `department` | `varchar(150)` | NULL | — | |
| `business_unit` | `varchar(150)` | NULL | — | |
| `owner` | `varchar(255)` | NULL | — | |
| `status` | `varchar(64)` | NULL | — | Business status (distinct from `contracts.status`). |
| **Risk** | | | | |
| `risk_score` | `int` | NULL | — | 0–100. `CHECK 0..100`. |
| `risk_band` | `risk_band` | NULL | — | Indexed. `low` · `medium` · `high` |
| `risk_level` | `varchar(32)` | NULL | — | Free-text label. |
| `risk_factors` | `jsonb` | NOT NULL | `'[]'` | Snapshot of the findings behind the score, so the UI can explain it **without re-running the engine**. |
| **Renewal** | | | | |
| `auto_renewal` | `bool` | NULL | — | |
| `auto_renewal_notice_days` | `int` | NULL | — | |
| `renewal_term_months` | `int` | NULL | — | |
| **AI insight flags** | | | | |
| `missing_mandatory_clauses` | `jsonb` | NOT NULL | `'[]'` | Clause types the profile requires that extraction did not find. Powers the "Missing Clauses" KPI and alert. |
| `has_unlimited_liability` | `bool` | NULL | — | |
| `has_liability_cap` | `bool` | NULL | — | |
| `liability_cap_amount` | `numeric(20,2)` | NULL | — | |
| `has_termination_for_convenience` | `bool` | NULL | — | |
| `has_data_protection_clause` | `bool` | NULL | — | |
| `termination_notice_days` | `int` | NULL | — | |
| **Counts** | | | | |
| `clause_count` | `int` | NOT NULL | `0` | |
| `obligation_count` | `int` | NOT NULL | `0` | |
| `risk_count` | `int` | NOT NULL | `0` | |
| `high_risk_count` | `int` | NOT NULL | `0` | |
| **Summary** | | | | |
| `summary` | `text` | NULL | — | One-paragraph AI summary. |
| `key_topics` | `jsonb` | NOT NULL | `'[]'` | |
| `extra` | `jsonb` | NOT NULL | `'{}'` | Anything a profile extracts that has no dedicated column. |
| `created_at`, `updated_at` | `timestamptz` | NOT NULL | `now()` | *(mixin)* |

**Constraints**

- `ck_contract_metadata_risk_score_range` — `CHECK (risk_score IS NULL OR risk_score BETWEEN 0 AND 100)`

**Indexes** — every one leads with `project_id`, because project isolation is applied
on every read and the planner needs it first to use the index at all.

| Index | Columns | Type | Purpose |
| --- | --- | --- | --- |
| `ix_contract_metadata_project_expiration` | `project_id`, `expiration_date` | btree | Expiry queries. |
| `ix_contract_metadata_project_effective` | `project_id`, `effective_date` | btree | |
| `ix_contract_metadata_project_risk` | `project_id`, `risk_band`, `risk_score` | btree | Risk filters and KPIs. |
| `ix_contract_metadata_project_vendor` | `project_id`, `vendor` | btree | |
| `ix_contract_metadata_project_customer` | `project_id`, `customer` | btree | |
| `ix_contract_metadata_project_category` | `project_id`, `category` | btree | |
| `ix_contract_metadata_project_value` | `project_id`, `contract_value` | btree | |
| `ix_contract_metadata_expiring` | `expiration_date`, `project_id` | partial `WHERE expiration_date IS NOT NULL` | "Expiring soon" — the only rows that matter are those with an expiry at all. |
| `ix_contract_metadata_auto_renewal` | `project_id`, `notice_deadline` | partial `WHERE auto_renewal = true` | The auto-renewal alert sweep. |
| `ix_contract_metadata_missing_clauses` | `missing_mandatory_clauses` | GIN | Containment: "which contracts lack a liability cap?" |
| `ix_contract_metadata_extra` | `extra` | GIN | Ad-hoc profile fields. |
| `ix_contract_metadata_summary_fts` | `to_tsvector('english', summary)` | GIN expression | Full-text over summaries. |

---

## 4.4 `document_profiles`

Document Intelligence Profiles — the configuration-driven brain. **A new contract
type is a new row here, with zero code changes.**

*Mixins: UUID PK · Timestamps · Soft delete*

| Column | Type | Null | Default | Description |
| --- | --- | --- | --- | --- |
| `id` | `uuid` | NOT NULL | `uuid_generate_v4()` | **PK** |
| `key` | `varchar(100)` | NOT NULL | — | Indexed. Stable across versions, e.g. `commercial_msa`. |
| `version` | `varchar(32)` | NOT NULL | `'1.0.0'` | |
| `name` | `varchar(255)` | NOT NULL | — | |
| `description` | `text` | NULL | — | |
| `category` | `varchar(100)` | NOT NULL | — | |
| `contract_type` | `varchar(64)` | NOT NULL | — | Indexed. Extensible. |
| `contract_subtype` | `varchar(64)` | NULL | — | |
| `supported_languages` | `jsonb` | NOT NULL | `'["en"]'` | |
| `is_active` | `bool` | NOT NULL | `true` | |
| `is_default` | `bool` | NOT NULL | `false` | The fallback when classification is inconclusive. Exactly one profile may carry this. |
| `priority` | `int` | NOT NULL | `0` | Higher wins when several profiles match. |
| `project_id` | `uuid` | NULL | — | **FK →** `projects.id` `→ CASCADE`. NULL = available platform-wide. |
| `classification_hints` | `jsonb` | NOT NULL | `'{}'` | `{title_patterns, required_phrases, negative_phrases, min_score}` — what the classifier scores against. |
| `extraction_strategy` | `jsonb` | NOT NULL | `'{}'` | `{prompt_templates, categories, examples, validation_hints}` |
| `mandatory_clauses` | `jsonb` | NOT NULL | `'[]'` | Clause types this type **must** contain. The gap becomes `contract_metadata.missing_mandatory_clauses`. |
| `optional_clauses` | `jsonb` | NOT NULL | `'[]'` | |
| `confidence_threshold` | `numeric(4,3)` | NOT NULL | `0.85` | Below this, the contract is flagged for review. |
| `review_rules` | `jsonb` | NOT NULL | `'{}'` | `{low_confidence, missing_mandatory_clause, conflicting_dates, high_risk_clause, validation_failure}` |
| `chunk_strategy` | `chunk_strategy` | NOT NULL | `'hybrid'` | |
| `chunk_config` | `jsonb` | NOT NULL | `'{}'` | `{max_tokens, min_tokens, overlap_tokens, preserve_tables, preserve_lists, merge_cross_page_clauses}` |
| `embedding_config` | `jsonb` | NOT NULL | `'{}'` | `{levels, metadata_weighting, similarity_threshold, summary_includes}` |
| `risk_mapping` | `jsonb` | NOT NULL | `'{}'` | Conditions → risk types and severities, plus the weights for the 0–100 score. |
| `compliance_rules` | `jsonb` | NOT NULL | `'[]'` | Named packs: procurement, HIPAA, SOX, GDPR, country packs. |
| `validation_rules` | `jsonb` | NOT NULL | `'{}'` | Business validation beyond schema: date ordering, monetary sanity. |
| `workflow_extensions` | `jsonb` | NOT NULL | `'[]'` | Optional stages: `ocr_enhance`, `translation`, `human_review`, `legal_approval`, `compliance_review`, `risk_assessment`, `external_api`. |
| `retention_policy` | `jsonb` | NOT NULL | `'{}'` | `{retain_years, purge_artifacts_after_days, legal_hold}` |
| `created_by` | `uuid` | NULL | — | **FK →** `users.id` `→ SET NULL` |
| `supersedes_id` | `uuid` | NULL | — | **FK →** `document_profiles.id` `→ SET NULL`. The edit-history chain. |
| `created_at`, `updated_at`, `deleted_at` | `timestamptz` | | | *(mixins)* |

**Constraints and indexes**

| Name | Definition | Purpose |
| --- | --- | --- |
| `uq_document_profiles_key_version` | UQ (`key`, `version`) | |
| `uq_document_profiles_active_key` | UNIQUE (`key`) partial `WHERE is_active AND project_id IS NULL AND deleted_at IS NULL` | **One active version per key.** |
| `uq_document_profiles_default` | UNIQUE (`is_default`) partial `WHERE is_default AND deleted_at IS NULL` | **Exactly one fallback profile.** |
| `ix_document_profiles_type_active` | (`contract_type`, `is_active`) | Classification lookup. |

\newpage

# Chapter 5 — The processing pipeline

## 5.1 `processing_jobs`

One processing run per contract. Holds the state machine.

*Mixins: UUID PK · Timestamps*

| Column | Type | Null | Default | Description |
| --- | --- | --- | --- | --- |
| `id` | `uuid` | NOT NULL | `uuid_generate_v4()` | **PK** |
| `contract_id` | `uuid` | NOT NULL | — | **FK →** `contracts.id` `→ CASCADE`, indexed |
| `project_id` | `uuid` | NOT NULL | — | **FK →** `projects.id` `→ CASCADE`, indexed |
| `state` | `job_state` | NOT NULL | `'QUEUED'` | Indexed. The 14-value state machine (Chapter 10). |
| `priority` | `job_priority` | NOT NULL | `'normal'` | `high` · `normal` · `low` |
| `current_stage` | `pipeline_stage` | NULL | — | Which of the 8 stages is running. |
| `progress` | `int` | NOT NULL | `0` | 0–100. `CHECK 0..100`. Weighted, not linear. |
| `retry_count` | `int` | NOT NULL | `0` | |
| `max_retries` | `int` | NOT NULL | `3` | |
| `error` | `jsonb` | NULL | — | `{stage, code, message, retryable, attempt, diagnostics}`. JSONB so a parser dump and a schema violation both fit without extra columns. |
| `resume_from_stage` | `pipeline_stage` | NULL | — | Set on retry so a resume never re-runs completed work. |
| `profile_id` | `uuid` | NULL | — | **FK →** `document_profiles.id` `→ SET NULL` |
| `profile_version` | `varchar(32)` | NULL | — | |
| `execution_plan` | `jsonb` | NOT NULL | `'{}'` | The Workflow Engine's plan, kept for audit — answers "why did this job skip chunking?" months later. |
| `trace_context` | `jsonb` | NOT NULL | `'{}'` | W3C `traceparent` captured at enqueue, so a stage span minutes later joins the upload's trace. |
| `metrics` | `jsonb` | NOT NULL | `'{}'` | Cumulative cost and usage, aggregated from stage runs. |
| `queued_at` | `timestamptz` | NULL | — | |
| `started_at` | `timestamptz` | NULL | — | |
| `finished_at` | `timestamptz` | NULL | — | |
| `heartbeat_at` | `timestamptz` | NULL | — | Worker heartbeat. **This is what distinguishes a slow parse from a dead worker.** |
| `worker_id` | `varchar(128)` | NULL | — | Which pod handled it. |
| `is_reprocess` | `bool` | NOT NULL | `false` | |
| `triggered_by` | `uuid` | NULL | — | **FK →** `users.id` `→ SET NULL` |
| `created_at`, `updated_at` | `timestamptz` | NOT NULL | `now()` | *(mixin)* |

**Constraints**

- `ck_processing_jobs_progress_range` — `CHECK (progress BETWEEN 0 AND 100)`

**Indexes**

| Index | Columns | Type | Purpose |
| --- | --- | --- | --- |
| `ix_processing_jobs_project_state` | `project_id`, `state` | btree | The Processing screen. |
| `ix_processing_jobs_state_created` | `state`, `created_at` | btree | Queue view. |
| `ix_processing_jobs_heartbeat` | `heartbeat_at` | partial `WHERE state IN (8 running states)` | The stalled-job sweep — only running jobs have a meaningful heartbeat. |

---

## 5.2 `job_stage_runs` — **the checkpoints**

One row per stage *attempt*. Append-only: a retry inserts a new row with an
incremented `attempt` rather than mutating the failed one, so the full processing
history of a contract is reconstructible for audit.

*Mixins: UUID PK (own `created_at`)*

| Column | Type | Null | Default | Description |
| --- | --- | --- | --- | --- |
| `id` | `uuid` | NOT NULL | `uuid_generate_v4()` | **PK** |
| `job_id` | `uuid` | NOT NULL | — | **FK →** `processing_jobs.id` `→ CASCADE`, indexed |
| `contract_id` | `uuid` | NOT NULL | — | **FK →** `contracts.id` `→ CASCADE`, indexed |
| `project_id` | `uuid` | NOT NULL | — | **FK →** `projects.id` `→ CASCADE` |
| `stage` | `pipeline_stage` | NOT NULL | — | Which of the 8. |
| `status` | `stage_status` | NOT NULL | `'pending'` | `pending` · `running` · `succeeded` · `failed` · `skipped` · `cancelled` |
| `attempt` | `int` | NOT NULL | `1` | |
| `artifact_ref` | `varchar(1024)` | NULL | — | Storage path of the primary artifact this run produced. |
| `versions` | `jsonb` | NOT NULL | `'{}'` | **The version set in effect.** Compared against the current registry to decide whether this checkpoint can be reused. |
| `stats` | `jsonb` | NOT NULL | `'{}'` | Stage counters: pages parsed, chunks created, tokens spent. |
| `error` | `jsonb` | NULL | — | |
| `worker_id` | `varchar(128)` | NULL | — | |
| `queue_job_id` | `varchar(128)` | NULL | — | The BullMQ job id, for cross-referencing queue logs. |
| `started_at` | `timestamptz` | NULL | — | |
| `finished_at` | `timestamptz` | NULL | — | |
| `duration_ms` | `int` | NULL | — | |
| `created_at` | `timestamptz` | NOT NULL | `now()` | |

**Constraints**

- `uq_job_stage_runs_job_stage_attempt` — UQ (`job_id`, `stage`, `attempt`)

**Indexes**

| Index | Columns | Type | Purpose |
| --- | --- | --- | --- |
| `ix_job_stage_runs_job_stage` | `job_id`, `stage`, `attempt` | btree | Stage timeline for one job. |
| `ix_job_stage_runs_checkpoint` | `contract_id`, `stage` | partial `WHERE status = 'succeeded'` | **The checkpoint lookup** — "latest successful run of this stage for this contract". Loaded by *contract*, not job, so a reprocess can reuse an earlier run's parse. |
| `ix_job_stage_runs_stage_status` | `stage`, `status` | btree | Pipeline statistics. |

---

## 5.3 `document_artifacts`

Pointers to stage outputs held in object storage. **Large payloads never enter
Postgres.**

*Mixins: UUID PK (own `created_at`)*

| Column | Type | Null | Default | Description |
| --- | --- | --- | --- | --- |
| `id` | `uuid` | NOT NULL | `uuid_generate_v4()` | **PK** |
| `contract_id` | `uuid` | NOT NULL | — | **FK →** `contracts.id` `→ CASCADE`, indexed |
| `project_id` | `uuid` | NOT NULL | — | **FK →** `projects.id` `→ CASCADE`, indexed |
| `job_id` | `uuid` | NULL | — | **FK →** `processing_jobs.id` `→ SET NULL` |
| `kind` | `artifact_kind` | NOT NULL | — | 21 values — see Chapter 10. |
| `storage_path` | `varchar(1024)` | NOT NULL | — | The object key. |
| `checksum` | `varchar(64)` | NOT NULL | — | |
| `size_bytes` | `bigint` | NULL | — | |
| `content_type` | `varchar(128)` | NOT NULL | `'application/json'` | |
| `versions` | `jsonb` | NOT NULL | `'{}'` | The version set that produced it. |
| `summary` | `jsonb` | NOT NULL | `'{}'` | Small facts kept inline (page count, chunk count) so **listing artifacts never requires reading object storage**. |
| `generation` | `int` | NOT NULL | `1` | `g1`, `g2`, `g3`… A regenerated artifact **supersedes** rather than overwrites. |
| `is_current` | `bool` | NOT NULL | `true` | |
| `created_at` | `timestamptz` | NOT NULL | `now()` | |

**Indexes**

| Index | Columns | Type | Purpose |
| --- | --- | --- | --- |
| `uq_document_artifacts_current` | `contract_id`, `kind` | **UNIQUE** partial `WHERE is_current = true` | Exactly one current artifact per (contract, kind) — the uniqueness that makes stage re-runs idempotent rather than accumulating duplicates. |
| `ix_document_artifacts_contract_kind` | `contract_id`, `kind`, `generation` | btree | Generation history. |
| `ix_document_artifacts_project_kind` | `project_id`, `kind` | btree | |

\newpage

# Chapter 6 — Retrieval substrate

## 6.1 `chunks`

Meaning-preserving passages — never fixed-size windows.

*Mixins: UUID PK · Timestamps · Evidence*

| Column | Type | Null | Default | Description |
| --- | --- | --- | --- | --- |
| `id` | `uuid` | NOT NULL | `uuid_generate_v4()` | **PK** |
| `contract_id` | `uuid` | NOT NULL | — | **FK →** `contracts.id` `→ CASCADE`, indexed |
| `project_id` | `uuid` | NOT NULL | — | **FK →** `projects.id` `→ CASCADE`, indexed |
| `parent_chunk_id` | `uuid` | NULL | — | **FK →** `chunks.id` `→ SET NULL`. Self-referential: document → section → clause → paragraph. `SET NULL` so a partial rebuild can never orphan a subtree into a dangling reference. |
| `section_id` | `varchar(128)` | NULL | — | Indexed. |
| `section_title` | `varchar(512)` | NULL | — | Weighted **'A'** in the search vector. |
| `clause_number` | `varchar(64)` | NULL | — | Indexed. Stored **as printed** ("4.2(b)(iii)"), not parsed — a citation must reproduce what the document says. |
| `level` | `int` | NOT NULL | `0` | Depth in the hierarchy; 0 = document-level. |
| `chunk_type` | `chunk_type` | NOT NULL | — | Indexed. 9 values — see Chapter 10. |
| `text` | `text` | NOT NULL | — | The passage. *(Python attribute: `text_content`.)* Weighted **'B'** in the search vector. |
| `reading_order` | `int` | NOT NULL | `0` | Global block index from the CDM — the deterministic ordering key. |
| `token_count` | `int` | NOT NULL | `0` | `CHECK >= 0`. |
| `char_count` | `int` | NOT NULL | `0` | `CHECK >= 0`. |
| `language` | `varchar(16)` | NULL | — | |
| `is_cross_page` | `bool` | NOT NULL | `false` | True when one logical clause was reassembled across a page break. |
| `table_data` | `jsonb` | NULL | — | Table chunks keep their structure so rows are never split mid-record. |
| `strategy_version` | `varchar(32)` | NOT NULL | `'1.0.0'` | |
| `engine_version` | `varchar(32)` | NOT NULL | `'1.0.0'` | |
| `strategy` | `varchar(32)` | NOT NULL | `'hybrid'` | Which chunking strategy produced it. |
| `version` | `int` | NOT NULL | `1` | Chunk-set generation. |
| `search_vector` | `tsvector` | NULL | — | **Trigger-maintained** — cannot drift from `text`. |
| `agreement_type` | `varchar(64)` | NULL | — | Denormalised from the contract so search can filter without a join. |
| `page_start`, `page_end`, `bounding_boxes`, `evidence` | | | | *(Evidence mixin)* |
| `created_at`, `updated_at` | `timestamptz` | NOT NULL | `now()` | *(mixin)* |

**Constraints**

- `uq_chunks_contract_version_order` — UQ (`contract_id`, `version`, `reading_order`)
  → deterministic identity, so a re-run upserts rather than duplicating
- `ck_chunks_token_count_non_negative`, `ck_chunks_char_count_non_negative`

**Indexes**

| Index | Columns | Type | Purpose |
| --- | --- | --- | --- |
| `ix_chunks_contract_order` | `contract_id`, `reading_order` | btree | Reading the document in order; neighbour expansion. |
| `ix_chunks_project_type` | `project_id`, `chunk_type` | btree | |
| `ix_chunks_parent` | `parent_chunk_id` | btree | Parent expansion during retrieval. |
| `ix_chunks_search_vector` | `search_vector` | **GIN** | The keyword leg of hybrid search. |
| `ix_chunks_text_trgm` | `text` | GIN trigram | Phrase search without full FTS parsing. |

---

## 6.2 `embeddings` — the three-level vector store

One physical table with a `level` discriminator. One table rather than three keeps
the retrieval planner's SQL uniform (a level filter instead of a table switch), while
per-level **partial** HNSW indexes give the same selectivity as separate tables.

*Mixins: UUID PK (own `created_at`)*

| Column | Type | Null | Default | Description |
| --- | --- | --- | --- | --- |
| `id` | `uuid` | NOT NULL | `uuid_generate_v4()` | **PK** |
| `project_id` | `uuid` | NOT NULL | — | **FK →** `projects.id` `→ CASCADE`, indexed |
| `contract_id` | `uuid` | NOT NULL | — | **FK →** `contracts.id` `→ CASCADE`, indexed |
| `level` | `embedding_level` | NOT NULL | — | Indexed. `document_summary` (L1) · `clause` (L2) · `chunk` (L3) |
| `ref_id` | `uuid` | NOT NULL | — | Indexed. The row this vector represents — a chunk id, a clause id, or the contract id for L1. **Polymorphic, so deliberately not a FK**; the `ON DELETE CASCADE` on `contract_id` already guarantees no vector outlives its contract. |
| `embedding` | `halfvec(2048)` | NOT NULL | — | The vector itself. Type and dimension come from `EMBEDDING_STORAGE` / `EMBEDDING_DIM`. |
| `source_text` | `text` | NULL | — | The exact text that was embedded — kept so a vector can be explained, and so re-embedding need not reconstruct the composed input. |
| `content_hash` | `varchar(64)` | NOT NULL | — | Indexed. SHA-256 of `source_text` after whitespace normalisation. **The duplicate-detection key.** |
| `token_count` | `int` | NULL | — | |
| `provider` | `varchar(64)` | NOT NULL | — | `nvidia`, `openai`, … |
| `model` | `varchar(128)` | NOT NULL | — | |
| `dim` | `int` | NOT NULL | `2048` | |
| `embedding_version` | `varchar(32)` | NOT NULL | `'v1'` | |
| `strategy_version` | `varchar(32)` | NOT NULL | `'1.0.0'` | |
| `profile_version` | `varchar(32)` | NULL | — | |
| `source_artifact_version` | `varchar(32)` | NULL | — | |
| `filter_metadata` | `jsonb` | NOT NULL | `'{}'` | Filterable attributes **duplicated onto the vector row** — the pre-filter must resolve before the ANN scan, and joining `contract_metadata` inside the vector query's hot path would defeat that. |
| `created_at` | `timestamptz` | NOT NULL | `now()` | |

**Constraints**

- `uq_embeddings_level_ref_model_version` — UQ (`level`, `ref_id`, `model`, `embedding_version`)
  → one current vector per (level, ref, model, version); re-running the embedding
  stage upserts instead of duplicating.

**Indexes**

| Index | Columns | Type | Purpose |
| --- | --- | --- | --- |
| `ix_embeddings_project_level` | `project_id`, `level` | btree | Metadata pre-filter path. |
| `ix_embeddings_contract_level` | `contract_id`, `level` | btree | |
| `ix_embeddings_filter_metadata` | `filter_metadata` | GIN | Attribute filters. |
| `ix_embeddings_reuse_lookup` | `project_id`, `level`, `model`, `embedding_version`, `strategy_version`, `content_hash`, `created_at`, `id` | btree | **The reuse lookup** — see the note below. |
| `ix_embeddings_hnsw_document_summary` | `embedding` | **HNSW** `halfvec_cosine_ops`, partial `WHERE level='document_summary'` | L1 similarity search. |
| `ix_embeddings_hnsw_clause` | `embedding` | **HNSW**, partial `WHERE level='clause'` | L2 similarity search. |
| `ix_embeddings_hnsw_chunk` | `embedding` | **HNSW**, partial `WHERE level='chunk'` | L3 similarity search. |

> **The reuse-lookup index column order is the whole point.** The five equality
> predicates come first, so the remaining three keys are returned already ordered by
> `(content_hash, created_at, id)` — exactly the `DISTINCT ON` ordering. That turns
> the plan from `Seq Scan → Sort → Unique` into a streaming `Index Only Scan →
> Unique`: the sort disappears and the heap is never touched, because every projected
> column is in the index.
>
> Measured on 40k rows (4k matching): **16.6 ms → 3.2 ms, 1041 → 53 buffers.** The
> gap widens with table size, since the eliminated step is the O(n log n) one — and
> this query runs once per level on **every** embedding stage.

> **Why the HNSW indexes are partial:** keeping each graph small means an L1
> candidate search never traverses millions of L3 chunk vectors.

\newpage

# Chapter 7 — Extracted knowledge

All seven tables in this chapter carry `contract_id` + `project_id` (both
`→ CASCADE`), the **Evidence** mixin where the item has text to point at, and the
**Extraction Provenance** mixin. Those columns are listed once in Chapter 2 and not
repeated below.

## 7.1 `clauses`

An extracted contract term. The unit the UI navigates, and the L2 embedding subject.

*Mixins: UUID PK · Timestamps · Evidence · Extraction provenance*

| Column | Type | Null | Default | Description |
| --- | --- | --- | --- | --- |
| `id` | `uuid` | NOT NULL | `uuid_generate_v4()` | **PK** |
| `contract_id` | `uuid` | NOT NULL | — | **FK →** `contracts.id` `→ CASCADE`, indexed |
| `project_id` | `uuid` | NOT NULL | — | **FK →** `projects.id` `→ CASCADE`, indexed |
| `chunk_id` | `uuid` | NULL | — | **FK →** `chunks.id` `→ SET NULL`. The audit link back to the exact text the model saw. |
| `clause_type` | `varchar(64)` | NOT NULL | — | Indexed. **Extensible** — administrators add categories through the Clause Master without a migration. |
| `title` | `varchar(512)` | NULL | — | |
| `text` | `text` | NOT NULL | — | The clause body. *(Python attribute: `text_content`.)* |
| `summary` | `text` | NULL | — | Short AI paraphrase for list views. |
| `section_id` | `varchar(128)` | NULL | — | |
| `section_title` | `varchar(512)` | NULL | — | |
| `clause_number` | `varchar(64)` | NULL | — | |
| `is_risk_flagged` | `bool` | NOT NULL | `false` | Set when the risk engine attributes a risk here. |
| `is_mandatory` | `bool` | NOT NULL | `false` | Satisfies one of the profile's mandatory clause types. |
| `deviation_score` | `numeric(5,4)` | NULL | — | 0..1 deviation from the Clause Master's standard language. Makes "this differs from our standard" measurable rather than a vibe. |
| `attributes` | `jsonb` | NOT NULL | `'{}'` | Profile-specific extraction output: notice periods, cap basis, carve-outs. |
| *Evidence + provenance columns* | | | | *(Chapter 2)* |
| `created_at`, `updated_at` | `timestamptz` | NOT NULL | `now()` | *(mixin)* |

**Indexes**

| Index | Columns | Type | Purpose |
| --- | --- | --- | --- |
| `ix_clauses_project_type` | `project_id`, `clause_type` | btree | Cross-contract clause search. |
| `ix_clauses_contract_type` | `contract_id`, `clause_type` | btree | Contract detail tabs. |
| `ix_clauses_project_confidence` | `project_id`, `confidence` | btree | Low-confidence sweeps. |
| `ix_clauses_review_pending` | `project_id` | partial `WHERE review_status = 'pending'` | The review queue. |
| `ix_clauses_flagged` | `project_id`, `clause_type` | partial `WHERE is_risk_flagged = true` | Risk views. |
| `ix_clauses_text_fts` | `to_tsvector('english', text)` | GIN expression | Keyword search over clause text. |
| `ix_clauses_attributes` | `attributes` | GIN | "Which contracts have an uncapped liability basis?" |

---

## 7.2 `entities`

A party, organisation or person named in the contract.

*Mixins: UUID PK · Timestamps · Evidence · Extraction provenance*

| Column | Type | Null | Default | Description |
| --- | --- | --- | --- | --- |
| `id` | `uuid` | NOT NULL | `uuid_generate_v4()` | **PK** |
| `contract_id` | `uuid` | NOT NULL | — | **FK →** `contracts.id` `→ CASCADE`, indexed |
| `project_id` | `uuid` | NOT NULL | — | **FK →** `projects.id` `→ CASCADE`, indexed |
| `chunk_id` | `uuid` | NULL | — | **FK →** `chunks.id` `→ SET NULL` |
| `entity_type` | `entity_type` | NOT NULL | — | Indexed. 9 values — see Chapter 10. |
| `name` | `varchar(512)` | NOT NULL | — | |
| `legal_name` | `varchar(512)` | NULL | — | Full registered name, e.g. "Acme Corporation Inc." for the defined term "Acme". |
| `aliases` | `jsonb` | NOT NULL | `'[]'` | Defined terms and short forms. **Resolving these is what lets "the Supplier shall…" be attributed correctly.** |
| `role` | `varchar(64)` | NULL | — | Indexed. `disclosing_party`, `vendor`, `licensor`, … |
| `jurisdiction` | `varchar(150)` | NULL | — | |
| `registration_number` | `varchar(128)` | NULL | — | |
| `address` | `text` | NULL | — | |
| `contact` | `jsonb` | NOT NULL | `'{}'` | |
| `is_primary` | `bool` | NOT NULL | `false` | Marks the two main signatories, surfaced as Party A / Party B. |
| *Evidence + provenance columns* | | | | *(Chapter 2)* |
| `created_at`, `updated_at` | `timestamptz` | NOT NULL | `now()` | *(mixin)* |

**Indexes**

| Index | Columns | Type | Purpose |
| --- | --- | --- | --- |
| `ix_entities_project_name` | `project_id`, `name` | btree | |
| `ix_entities_project_type_role` | `project_id`, `entity_type`, `role` | btree | |
| `ix_entities_name_trgm` | `name` | GIN trigram | Cross-contract party lookup ("every agreement with this vendor") — legal names vary between documents, so fuzzy matching is required, not optional. |
| `ix_entities_aliases` | `aliases` | GIN | Alias containment. |

---

## 7.3 `obligations`

Who must do what, by when, triggered by what.

*Mixins: UUID PK · Timestamps · Evidence · Extraction provenance*

| Column | Type | Null | Default | Description |
| --- | --- | --- | --- | --- |
| `id` | `uuid` | NOT NULL | `uuid_generate_v4()` | **PK** |
| `contract_id` | `uuid` | NOT NULL | — | **FK →** `contracts.id` `→ CASCADE`, indexed |
| `project_id` | `uuid` | NOT NULL | — | **FK →** `projects.id` `→ CASCADE`, indexed |
| `clause_id` | `uuid` | NULL | — | **FK →** `clauses.id` `→ SET NULL` |
| `chunk_id` | `uuid` | NULL | — | **FK →** `chunks.id` `→ SET NULL` |
| `responsible_party` | `varchar(512)` | NULL | — | Indexed. As named in the text. |
| `responsible_entity_id` | `uuid` | NULL | — | **FK →** `entities.id` `→ SET NULL`. Resolved link where the alias could be matched. |
| `action` | `text` | NOT NULL | — | What must be done. |
| `due_date` | `date` | NULL | — | |
| `due_description` | `varchar(512)` | NULL | — | Free-text deadline when no absolute date exists ("within 30 days of termination"). Kept **alongside** `due_date` rather than forcing a guess. |
| `trigger_event` | `varchar(512)` | NULL | — | What starts the clock. |
| `dependency` | `varchar(512)` | NULL | — | |
| `frequency` | `varchar(64)` | NULL | — | |
| `is_recurring` | `bool` | NOT NULL | `false` | |
| `status` | `obligation_status` | NOT NULL | `'unknown'` | `open` · `in_progress` · `fulfilled` · `breached` · `waived` · `unknown` |
| `penalty` | `text` | NULL | — | |
| *Evidence + provenance columns* | | | | *(Chapter 2)* |
| `created_at`, `updated_at` | `timestamptz` | NOT NULL | `now()` | *(mixin)* |

**Indexes**

| Index | Columns | Type | Purpose |
| --- | --- | --- | --- |
| `ix_obligations_project_due` | `project_id`, `due_date` | btree | |
| `ix_obligations_contract_party` | `contract_id`, `responsible_party` | btree | |
| `ix_obligations_upcoming` | `project_id`, `due_date` | partial `WHERE due_date IS NOT NULL AND status <> 'fulfilled'` | The upcoming-obligations panel — only dated, unfulfilled obligations are schedulable. |

---

## 7.4 `risks`

A detected contractual risk, attributed to the clause that creates it.

*Mixins: UUID PK · Timestamps · Evidence · Extraction provenance*

| Column | Type | Null | Default | Description |
| --- | --- | --- | --- | --- |
| `id` | `uuid` | NOT NULL | `uuid_generate_v4()` | **PK** |
| `contract_id` | `uuid` | NOT NULL | — | **FK →** `contracts.id` `→ CASCADE`, indexed |
| `project_id` | `uuid` | NOT NULL | — | **FK →** `projects.id` `→ CASCADE`, indexed |
| `clause_id` | `uuid` | NULL | — | **FK →** `clauses.id` `→ SET NULL` |
| `chunk_id` | `uuid` | NULL | — | **FK →** `chunks.id` `→ SET NULL` |
| `risk_type` | `varchar(64)` | NOT NULL | — | Indexed. Extensible. 21 seeded values. |
| `severity` | `risk_severity` | NOT NULL | — | Indexed. `critical` · `high` · `medium` · `low` |
| `description` | `text` | NOT NULL | — | |
| `recommendation` | `text` | NULL | — | What to do about it. |
| `score_contribution` | `int` | NULL | — | **How many of the 0–100 points this risk contributed** — what makes the score decomposable rather than opaque. |
| `category` | `varchar(64)` | NULL | — | |
| `is_omission` | `bool` | NOT NULL | `false` | True when the risk is the *absence* of something (no liability cap) — which has no bounding box to highlight, and is exempt from the evidence checks that assume there is something to quote. |
| *Evidence + provenance columns* | | | | *(Chapter 2)* |
| `created_at`, `updated_at` | `timestamptz` | NOT NULL | `now()` | *(mixin)* |

**Indexes**

| Index | Columns | Type | Purpose |
| --- | --- | --- | --- |
| `ix_risks_project_severity` | `project_id`, `severity` | btree | |
| `ix_risks_contract_severity` | `contract_id`, `severity` | btree | |
| `ix_risks_project_type` | `project_id`, `risk_type` | btree | |
| `ix_risks_high_severity` | `project_id`, `contract_id` | partial `WHERE severity IN ('critical','high')` | The "top risks" dashboard panel. |

---

## 7.5 `key_dates`

The contract timeline.

*Mixins: UUID PK · Timestamps · Evidence · Extraction provenance*

| Column | Type | Null | Default | Description |
| --- | --- | --- | --- | --- |
| `id` | `uuid` | NOT NULL | `uuid_generate_v4()` | **PK** |
| `contract_id` | `uuid` | NOT NULL | — | **FK →** `contracts.id` `→ CASCADE`, indexed |
| `project_id` | `uuid` | NOT NULL | — | **FK →** `projects.id` `→ CASCADE`, indexed |
| `clause_id` | `uuid` | NULL | — | **FK →** `clauses.id` `→ SET NULL` |
| `chunk_id` | `uuid` | NULL | — | **FK →** `chunks.id` `→ SET NULL` |
| `date_type` | `date_type` | NOT NULL | — | Indexed. 12 values — see Chapter 10. |
| `date_value` | `date` | NULL | — | Indexed. |
| `date_expression` | `varchar(512)` | NULL | — | Relative dates that cannot be resolved to a calendar date are **preserved verbatim** rather than discarded or guessed. |
| `description` | `text` | NULL | — | |
| `is_recurring` | `bool` | NOT NULL | `false` | |
| *Evidence + provenance columns* | | | | *(Chapter 2)* |
| `created_at`, `updated_at` | `timestamptz` | NOT NULL | `now()` | *(mixin)* |

**Indexes**

| Index | Columns | Purpose |
| --- | --- | --- |
| `ix_key_dates_project_type_value` | `project_id`, `date_type`, `date_value` | Timeline queries. |
| `ix_key_dates_contract_type` | `contract_id`, `date_type` | Contract detail. |

---

## 7.6 `knowledge_relationships`

Cross-references, definition links and dependencies found during extraction.

*Mixins: UUID PK · Timestamps · Extraction provenance*

| Column | Type | Null | Default | Description |
| --- | --- | --- | --- | --- |
| `id` | `uuid` | NOT NULL | `uuid_generate_v4()` | **PK** |
| `contract_id` | `uuid` | NOT NULL | — | **FK →** `contracts.id` `→ CASCADE`, indexed |
| `project_id` | `uuid` | NOT NULL | — | **FK →** `projects.id` `→ CASCADE`, indexed |
| `relation` | `graph_relation` | NOT NULL | — | 10 values — see Chapter 10. |
| `source_type` | `varchar(64)` | NOT NULL | — | |
| `source_ref` | `varchar(255)` | NOT NULL | — | As written in the text ("Section 4.2"). |
| `target_type` | `varchar(64)` | NOT NULL | — | |
| `target_ref` | `varchar(255)` | NOT NULL | — | |
| `source_id` | `uuid` | NULL | — | Resolved row id, where matched. Not a FK — polymorphic. |
| `target_id` | `uuid` | NULL | — | NULL when the contract references something not present in this document. |
| `label` | `varchar(512)` | NULL | — | |
| `attributes` | `jsonb` | NOT NULL | `'{}'` | Includes `origin`: `extracted` (what the text said) or `derived` (what indexing resolved). |
| `is_resolved` | `bool` | NOT NULL | `false` | |
| *Provenance columns* | | | | *(Chapter 2)* |
| `created_at`, `updated_at` | `timestamptz` | NOT NULL | `now()` | *(mixin)* |

**Indexes**

| Index | Columns | Purpose |
| --- | --- | --- |
| `ix_knowledge_relationships_contract_relation` | `contract_id`, `relation` | |
| `ix_knowledge_relationships_project_relation` | `project_id`, `relation` | |

> **Why both this table and the graph tables exist:** this is the relational record of
> what the *extraction* saw. Indexing resolves it into further rows in this same table
> traversal. Keeping both means a graph rebuild never needs the LLM again.

---

## 7.7 `contract_summaries`

Generated summaries — one row per format, so an executive summary and a risk summary
coexist without one overwriting the other.

*Mixins: UUID PK · Timestamps · Extraction provenance*

| Column | Type | Null | Default | Description |
| --- | --- | --- | --- | --- |
| `id` | `uuid` | NOT NULL | `uuid_generate_v4()` | **PK** |
| `contract_id` | `uuid` | NOT NULL | — | **FK →** `contracts.id` `→ CASCADE`, indexed |
| `project_id` | `uuid` | NOT NULL | — | **FK →** `projects.id` `→ CASCADE`, indexed |
| `summary_type` | `varchar(64)` | NOT NULL | — | `executive_summary`, `risk_report`, … |
| `content` | `text` | NOT NULL | — | |
| `sections` | `jsonb` | NOT NULL | `'[]'` | Section-wise breakdown where the format has one. |
| `key_points` | `jsonb` | NOT NULL | `'[]'` | |
| `citations` | `jsonb` | NOT NULL | `'[]'` | **A summary is an AI answer and obeys the same grounding rules as the Copilot.** |
| *Provenance columns* | | | | *(Chapter 2)* |
| `created_at`, `updated_at` | `timestamptz` | NOT NULL | `now()` | *(mixin)* |

**Constraints**

- `uq_contract_summaries_contract_type` — UNIQUE (`contract_id`, `summary_type`)

\newpage

# Chapter 8 — The knowledge graph

Stored as a property graph **in Postgres** rather than a separate graph database:
traversals here are shallow (2–3 hops, bounded by `RETRIEVAL_GRAPH_MAX_DEPTH`) and
must join against project-scoped relational filters in the same query — which a
recursive CTE does well and a cross-database hop does not.

> **Dropped (revision 0009).** `graph_nodes`, `graph_edges` and `clause_history`
> were created by the initial migration and never written to by anything. The
> knowledge graph lives in `knowledge_relationships` (§7), which is what
> `RetrievalEngine._expand_graph` traverses; clause review decisions are recorded
> in `audit_log` with the model's original output under `clauses.evidence`.
>
> The sections below are retained as a record of what the schema used to hold.

## 8.1 `graph_nodes`

*Mixins: UUID PK · Timestamps*

| Column | Type | Null | Default | Description |
| --- | --- | --- | --- | --- |
| `id` | `uuid` | NOT NULL | `uuid_generate_v4()` | **PK** |
| `project_id` | `uuid` | NOT NULL | — | **FK →** `projects.id` `→ CASCADE`, indexed |
| `contract_id` | `uuid` | NULL | — | **FK →** `contracts.id` `→ CASCADE`, indexed. **NULL for nodes that span contracts** — a vendor appearing in many agreements is *one* node, which is what makes cross-contract questions answerable. |
| `node_type` | `graph_node_type` | NOT NULL | — | Indexed. 11 values — see Chapter 10. |
| `ref_id` | `uuid` | NULL | — | Indexed. Id of the underlying row. Polymorphic, so not a FK. |
| `natural_key` | `varchar(512)` | NOT NULL | — | Stable key within a project, e.g. `party:acme-corporation`. Makes node creation idempotent and lets two contracts converge on one node. |
| `label` | `varchar(512)` | NOT NULL | — | Display label. |
| `attributes` | `jsonb` | NOT NULL | `'{}'` | |
| `version` | `int` | NOT NULL | `1` | |
| `graph_version` | `varchar(32)` | NOT NULL | `'1.0.0'` | |
| `created_at`, `updated_at` | `timestamptz` | NOT NULL | `now()` | *(mixin)* |

**Constraints**

- `uq_graph_nodes_project_type_key` — UQ (`project_id`, `node_type`, `natural_key`)

**Indexes**

| Index | Columns | Type | Purpose |
| --- | --- | --- | --- |
| `ix_graph_nodes_project_type` | `project_id`, `node_type` | btree | |
| `ix_graph_nodes_ref` | `node_type`, `ref_id` | btree | Row → node lookup. |
| `ix_graph_nodes_label_trgm` | `label` | GIN trigram | Fuzzy node search. |
| `ix_graph_nodes_attributes` | `attributes` | GIN | |

---

## 8.2 `graph_edges`

*Mixins: UUID PK · Timestamps*

| Column | Type | Null | Default | Description |
| --- | --- | --- | --- | --- |
| `id` | `uuid` | NOT NULL | `uuid_generate_v4()` | **PK** |
| `project_id` | `uuid` | NOT NULL | — | **FK →** `projects.id` `→ CASCADE`, indexed |
| `from_node` | `uuid` | NOT NULL | — | **FK →** `graph_nodes.id` `→ CASCADE`, indexed |
| `to_node` | `uuid` | NOT NULL | — | **FK →** `graph_nodes.id` `→ CASCADE`, indexed |
| `relation` | `graph_relation` | NOT NULL | — | Indexed. |
| `weight` | `numeric(5,4)` | NOT NULL | `1.0` | Traversal ranking — a strong `amends` edge outranks a weak inferred `references` edge when the planner prunes breadth. |
| `attributes` | `jsonb` | NOT NULL | `'{}'` | |
| `evidence` | `jsonb` | NOT NULL | `'{}'` | Where the edge came from: clause text, definition resolution, metadata. |
| `version` | `int` | NOT NULL | `1` | |
| `graph_version` | `varchar(32)` | NOT NULL | `'1.0.0'` | |
| `created_at`, `updated_at` | `timestamptz` | NOT NULL | `now()` | *(mixin)* |

**Constraints**

- `uq_graph_edges_from_to_relation` — UQ (`from_node`, `to_node`, `relation`)
- `ck_graph_edges_no_self_loop` — `CHECK (from_node <> to_node)`

**Indexes**

| Index | Columns | Purpose |
| --- | --- | --- |
| `ix_graph_edges_from_relation` | `from_node`, `relation` | Forward traversal: "everything this node points at". |
| `ix_graph_edges_to_relation` | `to_node`, `relation` | **Reverse** traversal — the direction that answers "which amendments affect this clause?" |
| `ix_graph_edges_project_relation` | `project_id`, `relation` | |

\newpage

# Chapter 9 — Governance, operations, Copilot and audit

## 9.1 `clause_master_categories`

The admin-editable clause taxonomy. **The extraction engine has no hardcoded clause
list — it reads these rows.**

*Mixins: UUID PK · Timestamps · Soft delete*

| Column | Type | Null | Default | Description |
| --- | --- | --- | --- | --- |
| `id` | `uuid` | NOT NULL | `uuid_generate_v4()` | **PK** |
| `key` | `varchar(64)` | NOT NULL | — | **UQ**, indexed. Used as `clauses.clause_type`. Stable — the display name can change without invalidating existing extractions. |
| `name` | `varchar(255)` | NOT NULL | — | |
| `description` | `text` | NULL | — | |
| `group_name` | `varchar(100)` | NULL | — | Indexed. UI grouping: Commercial, Legal, Risk, Compliance, Operational. |
| `mandatory` | `bool` | NOT NULL | `false` | Platform-wide default; profiles can override per document type. |
| `confidence_threshold` | `numeric(4,3)` | NOT NULL | `0.85` | Minimum confidence to be accepted without review. |
| `default_risk_severity` | `risk_severity` | NULL | — | Severity when this clause is missing or non-standard. |
| `is_active` | `bool` | NOT NULL | `true` | |
| `is_system` | `bool` | NOT NULL | `false` | Seeded categories can only be deactivated, never deleted — existing clauses reference their keys. |
| `priority` | `int` | NOT NULL | `999` | Indexed. **1 = highest.** Drives extraction ordering, so a partially-failed job still yields the terms that matter most. |
| `display_order` | `int` | NOT NULL | `100` | |
| `ui_config` | `jsonb` | NOT NULL | `'{}'` | How the frontend surfaces this category: `{placement: dedicated_tab\|list\|summary_card, tab_label, primary_fields, highlight_when}`. **Config rather than hardcoded UI** — promoting a clause to its own tab is an admin change. |
| `created_by` | `uuid` | NULL | — | **FK →** `users.id` `→ SET NULL` |
| `created_at`, `updated_at`, `deleted_at` | `timestamptz` | | | *(mixins)* |

**Indexes**

| Index | Columns | Type | Purpose |
| --- | --- | --- | --- |
| `ix_clause_master_categories_active` | `is_active`, `priority`, `display_order` | btree | Extraction ordering + UI sort. |
| `ix_clause_master_categories_mandatory` | `mandatory` | partial `WHERE is_active = true` | Missing-clause checks. |

---

## 9.2 `clause_master_rules`

Versioned extraction rules for one clause category.

*Mixins: UUID PK · Timestamps*

| Column | Type | Null | Default | Description |
| --- | --- | --- | --- | --- |
| `id` | `uuid` | NOT NULL | `uuid_generate_v4()` | **PK** |
| `category_id` | `uuid` | NOT NULL | — | **FK →** `clause_master_categories.id` `→ CASCADE`, indexed |
| `version` | `int` | NOT NULL | `1` | |
| `extraction_rule` | `jsonb` | NOT NULL | `'{}'` | The **deterministic pre-filter** applied before the LLM: `{heading_patterns, keywords, must_not_contain, min_tokens, search_scope}`. Cuts cost by not asking the model about chunks that cannot contain this clause. |
| `prompt_template` | `text` | NULL | — | Inline template. |
| `prompt_template_id` | `varchar(100)` | NULL | — | Or a reference into the prompt registry (`extraction.clauses`), which keeps the version. |
| `synonyms` | `jsonb` | NOT NULL | `'[]'` | Alternative headings the same clause appears under ("Term and Termination", "Duration", "Cancellation"). |
| `output_schema` | `jsonb` | NOT NULL | `'{}'` | JSON Schema fragment for the attributes this clause type must yield. Enforced by the extraction validator. |
| `examples` | `jsonb` | NOT NULL | `'[]'` | Few-shot examples. |
| `standard_text` | `text` | NULL | — | Baseline language for `clauses.deviation_score`. |
| `validation_rules` | `jsonb` | NOT NULL | `'{}'` | |
| `is_active` | `bool` | NOT NULL | `true` | |
| `created_by` | `uuid` | NULL | — | **FK →** `users.id` `→ SET NULL` |
| `change_note` | `varchar(500)` | NULL | — | |
| `created_at`, `updated_at` | `timestamptz` | NOT NULL | `now()` | *(mixin)* |

**Constraints and indexes**

| Name | Definition | Purpose |
| --- | --- | --- |
| `uq_clause_master_rules_category_version` | UQ (`category_id`, `version`) | |
| `uq_clause_master_rules_active` | UNIQUE (`category_id`) partial `WHERE is_active = true` | **One active rule per category.** |

---

## 9.3 `ai_settings`

Runtime-adjustable AI configuration — a thin, audited override layer over environment
configuration. **Single-row table** (enforced by a partial unique index); history is
retained for audit.

*Mixins: UUID PK · Timestamps*

| Column | Type | Null | Default | Description |
| --- | --- | --- | --- | --- |
| `id` | `uuid` | NOT NULL | `uuid_generate_v4()` | **PK** |
| `is_current` | `bool` | NOT NULL | `true` | |
| `providers` | `jsonb` | NOT NULL | `'{}'` | `{llm: {provider, model, temperature}, embedding: {...}, reranker: {...}}` |
| `thresholds` | `jsonb` | NOT NULL | `'{}'` | `{review_confidence, retrieval_min_similarity, context_token_budget, rerank_top_k}` |
| `model_routing` | `jsonb` | NOT NULL | `'{}'` | Which model handles simple vs complex requests. |
| `policies` | `jsonb` | NOT NULL | `'{}'` | Organisational policies injected into every prompt. |
| `feature_flags` | `jsonb` | NOT NULL | `'{}'` | Streaming, graph retrieval, human review, OCR. |
| `updated_by` | `uuid` | NULL | — | **FK →** `users.id` `→ SET NULL` |
| `change_note` | `varchar(500)` | NULL | — | |
| `created_at`, `updated_at` | `timestamptz` | NOT NULL | `now()` | *(mixin)* |

**Indexes**

- `uq_ai_settings_current` — UNIQUE (`is_current`) partial `WHERE is_current = true`

> Values are read through the settings service, which **falls back to the environment
> when a key is absent** — so a bad edit degrades to the deployed default rather than
> breaking the pipeline.

---

## 9.4 `alerts`

*Mixins: UUID PK · Timestamps*

| Column | Type | Null | Default | Description |
| --- | --- | --- | --- | --- |
| `id` | `uuid` | NOT NULL | `uuid_generate_v4()` | **PK** |
| `project_id` | `uuid` | NOT NULL | — | **FK →** `projects.id` `→ CASCADE`, indexed |
| `contract_id` | `uuid` | NULL | — | **FK →** `contracts.id` `→ CASCADE`, indexed. NULL for project-level alerts. |
| `alert_type` | `alert_type` | NOT NULL | — | Indexed. 7 values — see Chapter 10. |
| `severity` | `alert_severity` | NOT NULL | — | Indexed. `critical` · `high` · `medium` · `low` · `info` |
| `status` | `alert_status` | NOT NULL | `'open'` | Indexed. `open` · `acknowledged` · `resolved` · `dismissed` |
| `title` | `varchar(255)` | NOT NULL | — | |
| `message` | `text` | NOT NULL | — | |
| `details` | `jsonb` | NOT NULL | `'{}'` | Structured payload: days remaining, missing clause list, risk score, failing stage. Lets one row render a rich card. |
| `due_date` | `date` | NULL | — | Indexed. The date the alert is *about* — distinct from when it was generated. |
| `dedupe_key` | `varchar(255)` | NOT NULL | — | Stable identity of the condition, e.g. `expiring:<contract_id>:2026-09-30`. |
| `rule_id` | `uuid` | NULL | — | **FK →** `alert_rules.id` `→ SET NULL`. Lets retuning a threshold retire its alerts. |
| `acknowledged_at` | `timestamptz` | NULL | — | |
| `acknowledged_by` | `uuid` | NULL | — | **FK →** `users.id` `→ SET NULL` |
| `resolved_at` | `timestamptz` | NULL | — | |
| `note` | `text` | NULL | — | |
| `created_at`, `updated_at` | `timestamptz` | NOT NULL | `now()` | *(mixin)* |

**Indexes**

| Index | Columns | Type | Purpose |
| --- | --- | --- | --- |
| `uq_alerts_open_dedupe` | `dedupe_key` | **UNIQUE** partial `WHERE status = 'open'` | **De-duplication is structural.** An evaluator running hourly finds the existing open alert and leaves it alone, rather than creating 24 copies a day. |
| `ix_alerts_project_open` | `project_id`, `severity`, `due_date` | partial `WHERE status = 'open'` | The Alerts screen — most urgent first. |
| `ix_alerts_project_type_status` | `project_id`, `alert_type`, `status` | btree | |
| `ix_alerts_unacknowledged` | `project_id` | partial `WHERE acknowledged_at IS NULL AND status = 'open'` | The notification bell count. |

---

## 9.5 `alert_rules`

*Mixins: UUID PK · Timestamps*

| Column | Type | Null | Default | Description |
| --- | --- | --- | --- | --- |
| `id` | `uuid` | NOT NULL | `uuid_generate_v4()` | **PK** |
| `project_id` | `uuid` | NULL | — | **FK →** `projects.id` `→ CASCADE`, indexed. **NULL = the platform default**; a project row overrides it. |
| `name` | `varchar(255)` | NULL | — | "90-day renewal warning" is far more use to an administrator than "contract_expiring". |
| `alert_type` | `alert_type` | NOT NULL | — | Indexed. |
| `is_enabled` | `bool` | NOT NULL | `true` | |
| `severity` | `alert_severity` | NOT NULL | `'medium'` | |
| `config` | `jsonb` | NOT NULL | `'{}'` | Type-specific: `contract_expiring → {window_days, escalate_days}`; `high_risk → {risk_score_cutoff}`; `missing_mandatory_clause → {clause_types}`; `obligation_due → {window_days}` |
| `escalate_after_days` | `int` | NULL | — | Alerts older than this are raised a severity. |
| `notify_channels` | `jsonb` | NOT NULL | `'[]'` | In-app is always on; email/Slack/Teams/webhook are opt-in. |
| `updated_by` | `uuid` | NULL | — | **FK →** `users.id` `→ SET NULL` |
| `created_at`, `updated_at` | `timestamptz` | NOT NULL | `now()` | *(mixin)* |

**Constraints and indexes**

| Name | Definition |
| --- | --- |
| `uq_alert_rules_project_type` | UQ (`project_id`, `alert_type`) |
| `uq_alert_rules_global_type` | UNIQUE (`alert_type`) partial `WHERE project_id IS NULL` — exactly one platform default per type |

---

## 9.6 `export_jobs`

*Mixins: UUID PK · Timestamps*

| Column | Type | Null | Default | Description |
| --- | --- | --- | --- | --- |
| `id` | `uuid` | NOT NULL | `uuid_generate_v4()` | **PK** |
| `project_id` | `uuid` | NULL | — | **FK →** `projects.id` `→ CASCADE`, indexed. NULL for application-wide exports. |
| `requested_by` | `uuid` | NOT NULL | — | **FK →** `users.id` `→ CASCADE`, indexed |
| `scope` | `search_scope` | NOT NULL | — | `application` · `project` · `contract` |
| `scope_ref` | `uuid` | NULL | — | |
| `export_format` | `export_format` | NOT NULL | — | `xlsx` · `csv` · `json` · `pdf` |
| `status` | `export_status` | NOT NULL | `'queued'` | Indexed. `queued` · `running` · `completed` · `failed` · `expired` |
| `entities` | `jsonb` | NOT NULL | `'[]'` | Which entity types to include — one worksheet each in XLSX. |
| `fields` | `jsonb` | NOT NULL | `'{}'` | Column selection per entity. Empty = all columns. |
| `filters` | `jsonb` | NOT NULL | `'{}'` | **The same filter shape the repository and search endpoints accept**, so "export what I am looking at" is exact rather than approximate. |
| `storage_path` | `varchar(1024)` | NULL | — | |
| `file_name` | `varchar(512)` | NULL | — | |
| `file_size` | `bigint` | NULL | — | |
| `row_count` | `int` | NULL | — | |
| `checksum` | `varchar(64)` | NULL | — | |
| `progress` | `int` | NOT NULL | `0` | |
| `error` | `jsonb` | NULL | — | |
| `started_at` | `timestamptz` | NULL | — | |
| `finished_at` | `timestamptz` | NULL | — | |
| `expires_at` | `timestamptz` | NULL | — | Default 48 h. The scheduler purges expired files. |
| `downloaded_at` | `timestamptz` | NULL | — | |
| `download_count` | `int` | NOT NULL | `0` | |
| `created_at`, `updated_at` | `timestamptz` | NOT NULL | `now()` | *(mixin)* |

**Indexes**

| Index | Columns | Type | Purpose |
| --- | --- | --- | --- |
| `ix_export_jobs_user_created` | `requested_by`, `created_at` | btree | "My exports". |
| `ix_export_jobs_project_status` | `project_id`, `status` | btree | |
| `ix_export_jobs_expiring` | `expires_at` | partial `WHERE status = 'completed' AND expires_at IS NOT NULL` | The purge sweep — only completed exports have a file to expire. |

---

## 9.7 `chat_sessions`

*Mixins: UUID PK · Timestamps · Soft delete*

| Column | Type | Null | Default | Description |
| --- | --- | --- | --- | --- |
| `id` | `uuid` | NOT NULL | `uuid_generate_v4()` | **PK** |
| `project_id` | `uuid` | NULL | — | **FK →** `projects.id` `→ CASCADE`, indexed. Records which project context the session was opened from. |
| `user_id` | `uuid` | NOT NULL | — | **FK →** `users.id` `→ CASCADE`, indexed |
| `scope` | `search_scope` | NOT NULL | `'project'` | Application scope is only reachable by a System Admin. |
| `scope_ref` | `uuid` | NULL | — | Contract id when `scope = contract`; project id when `scope = project`. |
| `title` | `varchar(255)` | NOT NULL | `'New conversation'` | |
| `active_filters` | `jsonb` | NOT NULL | `'{}'` | Metadata filters pinned for the conversation, re-applied every turn so the user does not restate them. |
| `message_count` | `int` | NOT NULL | `0` | |
| `last_message_at` | `timestamptz` | NULL | — | |
| `is_pinned` | `bool` | NOT NULL | `false` | |
| `created_at`, `updated_at`, `deleted_at` | `timestamptz` | | | *(mixins)* |

**Indexes**

| Index | Columns | Purpose |
| --- | --- | --- |
| `ix_chat_sessions_user_updated` | `user_id`, `updated_at` | Session list, newest first. |
| `ix_chat_sessions_project_user` | `project_id`, `user_id` | |

> **Conversation state is externalised deliberately.** A follow-up question
> re-retrieves against the *current* index instead of inheriting a stale context
> window — which is what keeps answers correct after a contract is reprocessed.

---

## 9.8 `chat_messages`

*Mixins: UUID PK (own `created_at`)*

| Column | Type | Null | Default | Description |
| --- | --- | --- | --- | --- |
| `id` | `uuid` | NOT NULL | `uuid_generate_v4()` | **PK** |
| `session_id` | `uuid` | NOT NULL | — | **FK →** `chat_sessions.id` `→ CASCADE`, indexed |
| `project_id` | `uuid` | NULL | — | **FK →** `projects.id` `→ CASCADE` |
| `role` | `chat_role` | NOT NULL | — | `user` · `assistant` · `system` |
| `content` | `text` | NOT NULL | — | |
| `citations` | `jsonb` | NOT NULL | `'[]'` | `[{document_id, contract_title, clause_id, chunk_id, page_number, bounding_boxes, confidence, artifact_version, snippet}]` — exactly what the viewer needs to jump to a page and draw the highlight. |
| `evidence` | `jsonb` | NOT NULL | `'{}'` | The evidence package summary: strategy used, candidate counts, documents considered. **Answers "why did it say that?"** |
| `confidence` | `numeric(5,4)` | NULL | — | |
| `confidence_band` | `confidence_band` | NULL | — | `high` · `medium` · `low` |
| `confidence_breakdown` | `jsonb` | NOT NULL | `'{}'` | Component scores: retrieval, evidence quality, citation coverage, model, validation. |
| `response_format` | `response_format` | NULL | — | 10 values — see Chapter 10. |
| `retrieval_strategy` | `varchar(64)` | NULL | — | |
| `query_intent` | `varchar(64)` | NULL | — | |
| `model_version` | `varchar(128)` | NULL | — | |
| `prompt_version` | `varchar(32)` | NULL | — | |
| `versions` | `jsonb` | NOT NULL | `'{}'` | Full version set: planner, context engine, policy, output schema. |
| `token_usage` | `jsonb` | NOT NULL | `'{}'` | |
| `cost_usd` | `numeric(10,6)` | NULL | — | |
| `latency_ms` | `int` | NULL | — | |
| `was_regenerated` | `bool` | NOT NULL | `false` | Set when the answer was regenerated after failing validation. |
| `validation_result` | `jsonb` | NOT NULL | `'{}'` | |
| `insufficient_evidence` | `bool` | NOT NULL | `false` | True when the engine declared the evidence insufficient rather than guessing — **a first-class outcome, not an error.** |
| `user_rating` | `int` | NULL | — | Thumbs up/down. |
| `user_feedback` | `text` | NULL | — | |
| `created_at` | `timestamptz` | NOT NULL | `now()` | Indexed. |

**Indexes**

- `ix_chat_messages_session_created` — (`session_id`, `created_at`)

---

## 9.9 `audit_log`

Immutable. One row per mutating operation. **Never updated, never deleted by
application code.**

*Mixins: UUID PK (own `created_at`)*

| Column | Type | Null | Default | Description |
| --- | --- | --- | --- | --- |
| `id` | `uuid` | NOT NULL | `uuid_generate_v4()` | **PK** |
| `project_id` | `uuid` | NULL | — | **FK →** `projects.id` `→ SET NULL`, indexed. NULL for platform-level actions. |
| `user_id` | `uuid` | NULL | — | **FK →** `users.id` `→ SET NULL`, indexed. **`SET NULL`, not cascade** — deleting a user must not erase the record of what they did. |
| `user_email` | `varchar(320)` | NULL | — | Denormalised actor identity, kept because the user row may later be deleted or renamed. |
| `action` | `audit_action` | NOT NULL | — | Indexed. 18 values — see Chapter 10. |
| `entity_type` | `varchar(64)` | NOT NULL | — | Indexed. |
| `entity_id` | `uuid` | NULL | — | Indexed. Polymorphic. |
| `entity_label` | `varchar(512)` | NULL | — | Human-readable target, so the trail stays legible after the row is gone. |
| `before` | `jsonb` | NULL | — | |
| `after` | `jsonb` | NULL | — | |
| `ip` | `varchar(64)` | NULL | — | |
| `user_agent` | `varchar(512)` | NULL | — | |
| `request_id` | `varchar(64)` | NULL | — | Indexed. Cross-references the structured logs. |
| `trace_id` | `varchar(64)` | NULL | — | Cross-references Jaeger. |
| `route` | `varchar(255)` | NULL | — | |
| `succeeded` | `bool` | NOT NULL | `true` | **False when the operation was rejected** — a denied access attempt is exactly what an auditor wants to see. |
| `error_code` | `varchar(64)` | NULL | — | |
| `created_at` | `timestamptz` | NOT NULL | `now()` | Indexed. |

**Indexes**

| Index | Columns | Type | Purpose |
| --- | --- | --- | --- |
| `ix_audit_log_project_created` | `project_id`, `created_at` | btree | |
| `ix_audit_log_user_created` | `user_id`, `created_at` | btree | "What did this person do?" |
| `ix_audit_log_entity` | `entity_type`, `entity_id`, `created_at` | btree | "What happened to this contract?" |
| `ix_audit_log_action_created` | `action`, `created_at` | btree | |
| `ix_audit_log_failures` | `created_at` | partial `WHERE succeeded = false` | Security review. |

---

## 9.10 `contract_history`

Field-level change trail for a contract.

*Mixins: UUID PK (own `created_at`)*

| Column | Type | Null | Default | Description |
| --- | --- | --- | --- | --- |
| `id` | `uuid` | NOT NULL | `uuid_generate_v4()` | **PK** |
| `contract_id` | `uuid` | NOT NULL | — | **FK →** `contracts.id` `→ CASCADE`, indexed |
| `project_id` | `uuid` | NOT NULL | — | **FK →** `projects.id` `→ CASCADE` |
| `user_id` | `uuid` | NULL | — | **FK →** `users.id` `→ SET NULL` |
| `change_type` | `varchar(64)` | NOT NULL | — | |
| `field_name` | `varchar(128)` | NULL | — | |
| `old_value` | `text` | NULL | — | |
| `new_value` | `text` | NULL | — | |
| `source` | `varchar(32)` | NOT NULL | `'user'` | `user` · `ai_extraction` · `human_review` · `system`. **Lets the UI distinguish "the model said this" from "a lawyer decided this".** |
| `note` | `text` | NULL | — | |
| `payload` | `jsonb` | NOT NULL | `'{}'` | |
| `created_at` | `timestamptz` | NOT NULL | `now()` | |

**Indexes**

- `ix_contract_history_contract_created` — (`contract_id`, `created_at`)

---

> **Dropped (revision 0009).** `graph_nodes`, `graph_edges` and `clause_history`
> were created by the initial migration and never written to by anything. The
> knowledge graph lives in `knowledge_relationships` (§7), which is what
> `RetrievalEngine._expand_graph` traverses; clause review decisions are recorded
> in `audit_log` with the model's original output under `clauses.evidence`.
>
> The sections below are retained as a record of what the schema used to hold.

## 9.11 `clause_history`

Change trail for an extracted clause, **including human review decisions** — the
record that makes human-in-the-loop auditable.

*Mixins: UUID PK (own `created_at`)*

| Column | Type | Null | Default | Description |
| --- | --- | --- | --- | --- |
| `id` | `uuid` | NOT NULL | `uuid_generate_v4()` | **PK** |
| `clause_id` | `uuid` | NOT NULL | — | **FK →** `clauses.id` `→ CASCADE`, indexed |
| `contract_id` | `uuid` | NOT NULL | — | **FK →** `contracts.id` `→ CASCADE`, indexed |
| `project_id` | `uuid` | NOT NULL | — | **FK →** `projects.id` `→ CASCADE` |
| `user_id` | `uuid` | NULL | — | **FK →** `users.id` `→ SET NULL` |
| `change_type` | `varchar(64)` | NOT NULL | — | |
| `field_name` | `varchar(128)` | NULL | — | |
| `old_value` | `text` | NULL | — | |
| `new_value` | `text` | NULL | — | |
| `review_decision` | `varchar(32)` | NULL | — | `approved` · `rejected` · `corrected` when this row is a review decision. |
| `reason` | `text` | NULL | — | |
| `source` | `varchar(32)` | NOT NULL | `'ai_extraction'` | |
| `versions` | `jsonb` | NOT NULL | `'{}'` | Versions in effect when the change was made. |
| `created_at` | `timestamptz` | NOT NULL | `now()` | |

**Indexes**

| Index | Columns | Type | Purpose |
| --- | --- | --- | --- |
| `ix_clause_history_clause_created` | `clause_id`, `created_at` | btree | |
| `ix_clause_history_contract_created` | `contract_id`, `created_at` | btree | |
| `ix_clause_history_reviews` | `project_id`, `created_at` | partial `WHERE review_decision IS NOT NULL` | The review-activity report. |

---

## 9.12 `retrieval_audit`

What a search or Copilot answer retrieved, and how it was validated. Separated from
`audit_log` because its volume and retention differ.

*Mixins: UUID PK (own `created_at`)*

| Column | Type | Null | Default | Description |
| --- | --- | --- | --- | --- |
| `id` | `uuid` | NOT NULL | `uuid_generate_v4()` | **PK** |
| `project_id` | `uuid` | NULL | — | **FK →** `projects.id` `→ SET NULL`, indexed |
| `user_id` | `uuid` | NULL | — | **FK →** `users.id` `→ SET NULL`, indexed |
| `session_id` | `uuid` | NULL | — | **FK →** `chat_sessions.id` `→ SET NULL` |
| `message_id` | `uuid` | NULL | — | **FK →** `chat_messages.id` `→ SET NULL` |
| `operation` | `varchar(32)` | NOT NULL | — | Indexed. `search` · `copilot` · `summary` · `report` |
| `query_text` | `text` | NULL | — | |
| `query_intent` | `varchar(64)` | NULL | — | |
| `scope` | `varchar(32)` | NULL | — | |
| `strategy` | `varchar(64)` | NULL | — | Indexed. Which of the six retrieval strategies ran. |
| `filters` | `jsonb` | NOT NULL | `'{}'` | |
| `candidate_counts` | `jsonb` | NOT NULL | `'{}'` | `{documents, clauses, chunks, graph_nodes}` |
| `evidence_refs` | `jsonb` | NOT NULL | `'{}'` | Ids of what actually reached the context package, **so an answer can be reconstructed exactly.** |
| `graph_depth` | `int` | NULL | — | |
| `retrieval_ms` | `int` | NULL | — | |
| `rerank_ms` | `int` | NULL | — | |
| `inference_ms` | `int` | NULL | — | |
| `total_ms` | `int` | NULL | — | |
| `result_count` | `int` | NULL | — | |
| `citation_count` | `int` | NULL | — | |
| `citation_coverage` | `numeric(5,4)` | NULL | — | |
| `confidence` | `numeric(5,4)` | NULL | — | |
| `versions` | `jsonb` | NOT NULL | `'{}'` | Planner, reranking, context engine, prompt, model, policy. |
| `token_usage` | `jsonb` | NOT NULL | `'{}'` | |
| `cost_usd` | `numeric(10,6)` | NULL | — | |
| `validation_result` | `jsonb` | NOT NULL | `'{}'` | |
| `cache_hit` | `bool` | NOT NULL | `false` | |
| `created_at` | `timestamptz` | NOT NULL | `now()` | Indexed. |

**Indexes**

| Index | Columns | Purpose |
| --- | --- | --- |
| `ix_retrieval_audit_project_created` | `project_id`, `created_at` | |
| `ix_retrieval_audit_operation_created` | `operation`, `created_at` | Retrieval-quality metrics. |

\newpage

# Chapter 10 — Enum types

## 10.1 Native Postgres enums

30 closed sets are stored as **native Postgres enum types**. The database itself
rejects a bad value, and the type name is stable across environments.

Values are stored as the enum's *value* (`"high"`), not its member name (`"HIGH"`),
so the database contents match the API contract and are readable in ad-hoc SQL.

| Type | Values |
| --- | --- |
| `auth_provider` | `local` · `microsoft` |
| `role_name` | `system_admin` · `project_manager` · `reviewer` · `viewer` |
| `project_status` | `active` · `archived` · `on_hold` |
| `file_type` | `pdf` · `docx` |
| `contract_status` | `uploaded` · `processing` · `ready` · `failed` · `needs_review` · `archived` |
| `job_state` | `QUEUED` · `VALIDATING` · `PARSING` · `ENRICHING` · `CLASSIFYING` · `CHUNKING` · `AI_EXTRACTION` · `EMBEDDING` · `INDEXING` · `READY` · `FAILED` · `RETRYING` · `CANCELLED` · `PAUSED` *(uppercase — the only enum that is)* |
| `job_priority` | `high` · `normal` · `low` |
| `pipeline_stage` | `validation` · `parser` · `enrichment` · `classification` · `chunking` · `ai_extraction` · `embedding` · `indexing` |
| `stage_status` | `pending` · `running` · `succeeded` · `failed` · `skipped` · `cancelled` |
| `artifact_kind` | `validation` · `normalized_document` · `canonical_document` · `classification` · `chunks` · `chunk_statistics` · `chunk_validation` · `clauses` · `entities` · `obligations` · `risks` · `timelines` · `relationships` · `extraction_statistics` · `summary_embeddings` · `clause_embeddings` · `chunk_embeddings` · `embedding_statistics` · `index_statistics` · `statistics` · `export` |
| `chunk_type` | `section` · `clause` · `paragraph` · `table` · `list` · `definition` · `appendix` · `signature` · `footnote` |
| `chunk_strategy` | `section_based` · `heading_aware` · `clause_based` · `table_preserving` · `list_preserving` · `hybrid` |
| `entity_type` | `party` · `organization` · `person` · `vendor` · `customer` · `affiliate` · `guarantor` · `signatory` · `government_body` |
| `obligation_status` | `open` · `in_progress` · `fulfilled` · `breached` · `waived` · `unknown` |
| `risk_severity` | `critical` · `high` · `medium` · `low` |
| `risk_band` | `low` · `medium` · `high` |
| `date_type` | `effective_date` · `execution_date` · `expiration_date` · `renewal_date` · `notice_deadline` · `milestone` · `payment_due` · `delivery_date` · `review_date` · `termination_date` · `commencement_date` · `other` |
| `embedding_level` | `document_summary` · `clause` · `chunk` |
| `graph_node_type` | `contract` · `party` · `vendor` · `customer` · `clause` · `obligation` · `risk` · `definition` · `schedule` · `amendment` · `renewal` |
| `graph_relation` | `references` · `depends_on` · `defines` · `amends` · `replaces` · `belongs_to` · `contains` · `governed_by` · `assigned_to` · `renewed_by` |
| `search_scope` | `application` · `project` · `contract` |
| `chat_role` | `user` · `assistant` · `system` |
| `response_format` | `natural_language` · `json` · `executive_summary` · `risk_report` · `compliance_report` · `clause_comparison` · `timeline` · `action_items` · `contract_summary` · `obligation_report` |
| `confidence_band` | `high` · `medium` · `low` |
| `alert_type` | `contract_expiring` · `high_risk` · `missing_mandatory_clause` · `processing_failed` · `auto_renewal_notice` · `obligation_due` · `review_required` |
| `alert_severity` | `critical` · `high` · `medium` · `low` · `info` |
| `alert_status` | `open` · `acknowledged` · `resolved` · `dismissed` |
| `export_format` | `xlsx` · `csv` · `json` · `pdf` |
| `export_status` | `queued` · `running` · `completed` · `failed` · `expired` |
| `audit_action` | `create` · `update` · `delete` · `login` · `login_failed` · `logout` · `upload` · `download` · `export` · `search` · `copilot_query` · `job_retry` · `job_cancel` · `job_pause` · `job_resume` · `review_decision` · `permission_change` · `config_change` |

## 10.2 Extensible taxonomies (stored as `VARCHAR`, deliberately)

Five columns hold a taxonomy an **administrator can extend at runtime**. A native
enum would make adding a clause category a database migration, which contradicts the
"new document types = new profile, zero code changes" rule.

| Column | Baseline set | Extended by |
| --- | --- | --- |
| `clauses.clause_type` | 47 seeded clause types | Clause Master (admin UI) |
| `contracts.agreement_type` | 17 seeded agreement types | Classification / admin |
| `risks.risk_type` | 21 seeded risk types | Profile risk mapping |
| `clause_master_categories.key` | The seeded catalogue | Clause Master |
| `document_profiles.contract_type` | Agreement taxonomy | New profile rows |

The Python enums (`ClauseType`, `AgreementType`, `RiskType`) remain the typed
constant set used by prompts, mandatory-clause checks and the UI — they are the
*baseline*, not the ceiling.

\newpage

# Chapter 11 — Extensions, triggers and functions

## 11.1 Required PostgreSQL extensions

| Extension | Why it is needed |
| --- | --- |
| `uuid-ossp` | `uuid_generate_v4()` as a server-side default. |
| `vector` | **pgvector** — the embedding column type and the HNSW indexes. |
| `pg_trgm` | Trigram indexes for fuzzy name, title and filename search. |
| `citext` | Case-insensitive email uniqueness without `lower()` on both sides of every query. |
| `btree_gin` | Composite GIN indexes over scalar + JSONB columns. |

> **Deployment note:** `CREATE EXTENSION vector` requires superuser privileges on
> most managed PostgreSQL services. On a shared database where the application user
> is not a superuser, a DBA must create the extension once before the first
> migration runs.

## 11.2 Trigger functions

### `cip_set_updated_at()`

Keeps `updated_at` honest even for bulk `UPDATE`s issued outside the ORM.

```sql
CREATE OR REPLACE FUNCTION cip_set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
```

Attached as `trg_<table>_updated_at` (BEFORE UPDATE) to **26 tables**: `users`,
`roles`, `refresh_tokens`, `projects`, `project_members`, `contracts`,
`contract_metadata`, `processing_jobs`, `document_profiles`, `clauses`, `entities`,
`obligations`, `risks`, `key_dates`, `knowledge_relationships`, `contract_summaries`,
`chunks`, `graph_nodes`, `graph_edges`, `chat_sessions`, `clause_master_categories`,
`clause_master_rules`, `ai_settings`, `alerts`, `alert_rules`, `export_jobs`.

---

### `cip_chunks_search_vector()`

Maintains the keyword search index. **Weighted** so a match in the section title
outranks a match deep in body text — which is what makes keyword search on a
150-page contract return the right clause first.

```sql
CREATE OR REPLACE FUNCTION cip_chunks_search_vector()
RETURNS TRIGGER AS $$
BEGIN
    NEW.search_vector :=
        setweight(to_tsvector('english', coalesce(NEW.section_title, '')), 'A') ||
        setweight(to_tsvector('english', coalesce(NEW.text, '')), 'B');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
```

Attached as `trg_chunks_search_vector`, `BEFORE INSERT OR UPDATE OF text,
section_title ON chunks`.

> Because the vector is trigger-maintained, it **cannot drift** from the text. The
> indexing stage *verifies* coverage rather than computing it — turning a missing
> trigger (the kind of migration slip that produces silently degraded search for
> months) into a visible warning on the job.

---

### `cip_assert_project_scope()` — the isolation guard

Guards the platform's central invariant: **derived data may not point at a contract
in a different project.**

```sql
CREATE OR REPLACE FUNCTION cip_assert_project_scope()
RETURNS TRIGGER AS $$
DECLARE
    owner_project uuid;
BEGIN
    SELECT project_id INTO owner_project FROM contracts WHERE id = NEW.contract_id;
    IF owner_project IS NULL THEN
        RETURN NEW;  -- FK will raise; nothing to compare against
    END IF;
    IF NEW.project_id <> owner_project THEN
        RAISE EXCEPTION
            'project isolation violation on %: project_id % does not match contract % (project %)',
            TG_TABLE_NAME, NEW.project_id, NEW.contract_id, owner_project
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
```

Attached as `trg_<table>_project_scope`, `BEFORE INSERT OR UPDATE OF project_id,
contract_id`, on **14 tables**: `chunks`, `clauses`, `entities`, `obligations`,
`risks`, `key_dates`, `knowledge_relationships`, `contract_summaries`, `embeddings`,
`document_artifacts`, `job_stage_runs`, `processing_jobs`, `contract_versions`,
`contract_metadata`.

> **Why this is a trigger and not just application code:** a bug that mis-scoped a
> write would leak one project's clauses into another project's search results. That
> is the single worst failure this platform can have, so it is enforced where it
> cannot be bypassed — not trusted to every future code path remembering the rule.

## 11.3 Expression indexes declared in SQL

Two indexes cannot be declared portably through the ORM:

```sql
-- Full-text search over contract summaries
CREATE INDEX ix_contract_metadata_summary_fts
ON contract_metadata USING gin (to_tsvector('english', coalesce(summary, '')));

-- Case-insensitive contract number lookup (users paste them in any case)
CREATE INDEX ix_contracts_number_lower
ON contracts (project_id, lower(contract_number))
WHERE contract_number IS NOT NULL;
```

## 11.4 Planner statistics targets

The default sampling under-estimates high-cardinality UUID columns that **every**
project-scoped query filters by, so the target is raised on four of them:

```sql
ALTER TABLE chunks     ALTER COLUMN project_id SET STATISTICS 500;
ALTER TABLE clauses    ALTER COLUMN project_id SET STATISTICS 500;
ALTER TABLE embeddings ALTER COLUMN project_id SET STATISTICS 500;
ALTER TABLE contracts  ALTER COLUMN project_id SET STATISTICS 500;
```

## 11.5 Naming conventions

Constraint names are deterministic, so Alembic autogenerate produces stable
migrations instead of database-assigned names that differ per environment:

| Kind | Pattern | Example |
| --- | --- | --- |
| Primary key | `pk_<table>` | `pk_contracts` |
| Foreign key | `fk_<table>_<column>_<referred_table>` | `fk_clauses_contract_id_contracts` |
| Unique | `uq_<table>_<columns>` | `uq_contracts_project_id_sha256_hash` |
| Index | `ix_<table>_<columns>` | `ix_clauses_project_type` |
| Check | `ck_<table>_<name>` | `ck_contracts_file_size_positive` |

\newpage

# Chapter 12 — Migration history

| Revision | Down-revision | Date | What it does |
| --- | --- | --- | --- |
| `0001` | — | 2026-01-01 | **Initial schema.** Creates the five extensions, all 36 tables, every index and constraint, all 30 enum types, the three trigger functions and their 41 triggers, the two expression indexes, and the statistics targets. Built by `Base.metadata.create_all()` so the schema is defined once, in `app/models`. |
| `0002` | `0001` | 2026-07-31 | **NVIDIA embeddings.** Moves the vector store to `nvidia/nemotron-3-embed-1b`: drops the three HNSW indexes, `DELETE FROM embeddings` (vectors from two models are not comparable — a partially-migrated index is worse than an empty one), retypes `embeddings.embedding` from `vector(1536)` to `halfvec(2048)`, updates the `dim` default, and rebuilds the HNSW indexes with `halfvec_cosine_ops`. |
| `0003` | `0002` | 2026-07-31 | **Embedding reuse index.** Adds `ix_embeddings_reuse_lookup` — the eight-column covering index that turns the duplicate-detection query into a streaming index-only scan (16.6 ms → 3.2 ms on 40k rows). |

**Commands**

```bash
make migrate                              # apply everything
make migration m="add clause synonyms"    # autogenerate the next revision
make downgrade                            # roll back one
python -m app.cli current                 # show the applied revision
```

> **Revision 0001 is the only hand-written one.** Every subsequent revision uses
> `alembic revision --autogenerate`, which compares `app/models` against the live
> database — so the models remain the single source of truth for the schema.

\newpage

# Appendix A — Quick lookup: which table holds what?

| I need… | Look in |
| --- | --- |
| Who can sign in | `users` |
| What a role can do | `roles.permissions` |
| Whether a user can see a project | `project_members` |
| The uploaded PDF's storage key | `contracts.storage_path` |
| Every version of a document | `contract_versions` |
| Expiry date, risk score, party names | `contract_metadata` |
| Whether processing succeeded | `processing_jobs.state` |
| Which stage failed and why | `job_stage_runs` (status, error) |
| Where a stage's output JSON lives | `document_artifacts.storage_path` |
| How a contract type is processed | `document_profiles` |
| The searchable text passages | `chunks` |
| The keyword search index | `chunks.search_vector` |
| The semantic search vectors | `embeddings` |
| Extracted contract terms | `clauses` |
| Who the parties are | `entities` |
| Deadlines and duties | `obligations`, `key_dates` |
| Why a contract scored 78/100 | `risks.score_contribution` + `contract_metadata.risk_factors` |
| What clause types we can extract | `clause_master_categories` |
| The prompt used for a clause type | `clause_master_rules` |
| Open alerts | `alerts` (status = `open`) |
| A generated Excel file | `export_jobs.storage_path` |
| A Copilot answer's citations | `chat_messages.citations` |
| Who did what, when | `audit_log` |
| Who approved an AI extraction | `audit_log` (action `review_decision`), with the model's original output kept under `clauses.evidence["original"]` |
| What a search actually retrieved | `retrieval_audit.evidence_refs` |

# Appendix B — Cheat sheet

```
 ┌──────────────────────────────────────────────────────────────────────────┐
 │  36 TABLES · 30 NATIVE ENUMS · 5 EXTENSIONS · 3 TRIGGER FUNCTIONS        │
 ├──────────────────────────────────────────────────────────────────────────┤
 │  THE UNIVERSAL RULE                                                      │
 │  Every contract-derived row carries project_id.                          │
 │  A trigger REJECTS any row whose project_id disagrees with its           │
 │  contract's — enforced in the database, not just in code.                │
 ├──────────────────────────────────────────────────────────────────────────┤
 │  WHERE THINGS LIVE                                                       │
 │  PDFs and stage artifacts  →  object storage (pointers in Postgres)      │
 │  Structured facts          →  Postgres                                   │
 │  Vectors                   →  Postgres + pgvector (halfvec 2048)         │
 ├──────────────────────────────────────────────────────────────────────────┤
 │  THE CHECKPOINT                                                          │
 │  job_stage_runs WHERE status='succeeded', latest per (contract, stage)   │
 │  Looked up by CONTRACT, not job — so a reprocess reuses an earlier       │
 │  run's parse.                                                            │
 ├──────────────────────────────────────────────────────────────────────────┤
 │  THE REUSE KEY                                                           │
 │  embeddings: content_hash + model + embedding_version +                  │
 │              strategy_version + level + project_id                       │
 │  All must match, or the vector is regenerated.                           │
 ├──────────────────────────────────────────────────────────────────────────┤
 │  SOFT-DELETED TABLES (deleted_at IS NULL = live)                         │
 │  users · projects · contracts · document_profiles ·                      │
 │  clause_master_categories · chat_sessions                                │
 ├──────────────────────────────────────────────────────────────────────────┤
 │  APPEND-ONLY / NEVER MUTATED                                             │
 │  audit_log · job_stage_runs · contract_versions ·                        │
 │  contract_history · clause_history · retrieval_audit                     │
 └──────────────────────────────────────────────────────────────────────────┘
```
