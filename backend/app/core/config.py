"""Application configuration.

Every knob is environment-driven (``pydantic-settings``); nothing is hardcoded and
no secret ever lives in code. Grouped into nested models so call sites read as
``settings.storage.provider`` rather than one flat namespace, while the env var
names stay flat and deployment-friendly (``STORAGE_PROVIDER``).

Import the singleton via :func:`get_settings` (cached) so a process parses the
environment exactly once.
"""

from __future__ import annotations

import os
import uuid
import warnings
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Final, Literal

from pydantic import (
    AliasChoices,
    Field,
    PostgresDsn,
    RedisDsn,
    computed_field,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

Environment = Literal["development", "test", "staging", "production"]
#: Parser options. Only ``idoc``, ``pymupdf`` and ``mock`` are implemented here.
#:
#: * ``idoc`` - the in-house layout service (see ``ParserSettings.idoc_endpoint``),
#:   which wraps the Azure Document Intelligence layout model. The default.
#: * ``pdfextract`` - a local checkout of pdf_text_extractor, run as a subprocess.
#:   Produces the *same* Azure ``prebuilt-layout`` payloads as ``idoc`` via its
#:   ``--adi`` export, so it is a drop-in for the whole pipeline rather than a
#:   degraded fallback: the document pipeline reads those raw payloads directly
#:   and cannot tell the two apart. Requires ``PDFEXTRACT_PATH``.
#: * ``pymupdf`` - local, dependency-light fallback when the service is unreachable.
#:   Unlike the two above it does *not* produce layout JSON, so the document
#:   pipeline cannot run on it.
#: * ``adi``, ``textract``, ``googledocai`` - reserved names, so a future deployment
#:   can add one without changing this contract. Selecting one raises an actionable
#:   error rather than failing obscurely.
#:
#: Docling is deliberately absent: it is not used by this deployment, and listing a
#: name the registry cannot build turns a configuration typo into a runtime parse
#: failure instead of a startup error naming the valid options.
ParserName = Literal["idoc", "pdfextract", "adi", "pymupdf", "textract", "googledocai"]

#: How a parser adapter obtains its response.
#:
#: * ``fixture`` - replay a recorded response from disk. Never touches the network,
#:   so tests, CI and offline development cannot burn the rate-limited layout
#:   service or fail because it is down. The default, deliberately: an accidental
#:   live call is a cost and a rate-limit hit, an accidental fixture replay is a
#:   loud, logged fallback.
#: * ``live`` - call the real service, and record the response as a fixture on the
#:   way through so the next run can replay it.
ParserMode = Literal["fixture", "live"]
StorageProvider = Literal["azure", "s3", "minio", "local"]
LLMProvider = Literal["gemini", "anthropic", "openai", "azure_openai", "local", "mock"]
EmbeddingProvider = Literal["nvidia", "openai", "azure_openai", "sentence_transformers", "mock"]

#: How the vector column is physically stored.
#:
#: ``halfvec`` is not an optimisation here, it is a requirement. pgvector's HNSW
#: index supports at most **2000** dimensions for the ``vector`` type, and the
#: default model (``nvidia/nemotron-3-embed-1b``) emits **2048**. A ``vector(2048)``
#: column stores fine and then cannot be indexed at all, so every search degrades to
#: a sequential scan. ``halfvec`` indexes up to 4000 dimensions and halves storage;
#: on L2-normalised embeddings the fp16 rounding is far below the margin that
#: separates a relevant hit from an irrelevant one.
VectorStorage = Literal["vector", "halfvec"]
QueueDriver = Literal["bullmq", "arq", "postgres"]


def _csv_list(value: str | list[str] | None) -> list[str]:
    """Parse a comma-separated env var into a clean list."""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return [part.strip() for part in value.split(",") if part.strip()]


# =============================================================================
# Nested configuration groups
# =============================================================================
def _group_config(*, allow_model_prefix: bool = False) -> SettingsConfigDict:
    """Config for a nested settings group. Every group must use this.

    Pydantic does not pass the root model's ``env_file`` down to nested models:
    each one loads its own sources, and a group without ``env_file`` therefore
    reads real environment variables only. Under compose that is invisible,
    because the allow-list exports the file's contents as real variables before
    the process starts. Run the same code locally and the file is simply not
    read - the setting keeps its default, and nothing anywhere reports that a
    value was present and ignored.

    ``COPILOT_SIMILARITY_THRESHOLD`` sat in ``.env`` unread for exactly this
    reason: the guardrail kept gating at its 0.45 default while the file said
    0.35. Real environment variables still take precedence over the file, so
    compose and the deployment scripts keep overriding it as before.

    ``CIP_DISABLE_DOTENV`` turns the file off. The test suite sets it, because a
    suite that reads ``.env`` asserts against whatever the developer last put in
    it: these groups pin the embedding shape the migrations were written for, and
    a local file running a different provider fails them for no reason connected
    to the change under test.
    """
    config = SettingsConfigDict(
        env_prefix="",
        extra="ignore",
        env_file=None if os.getenv("CIP_DISABLE_DOTENV") else (".env", "../.env"),
        env_file_encoding="utf-8",
    )
    if allow_model_prefix:
        # Groups holding fields like `embedding_model` and `rerank_model`, which
        # collide with the `model_` namespace pydantic reserves for itself.
        config["protected_namespaces"] = ()
    return config


class DatabaseSettings(BaseSettings):
    """PostgreSQL connection and pool tuning."""

    model_config = _group_config()

    url: Annotated[
        PostgresDsn,
        Field(
            validation_alias="DATABASE_URL",
            description="Async DSN, e.g. postgresql+asyncpg://user:pass@host:5432/db",
        ),
    ] = "postgresql+asyncpg://cip:cip_dev_password@localhost:5432/cip"  # type: ignore[assignment]

    #: Postgres schema the application's tables live in. Empty means ``public``.
    #:
    #: Exists for the case where the database is shared with another application
    #: that already owns table names we also use - ``contracts`` and ``clauses``
    #: are not distinctive, and two apps in one ``public`` schema would collide.
    #: Pointing this at a dedicated schema isolates the two without either side
    #: renaming anything.
    #:
    #: Applied as a connection-level ``search_path``, never per model, so the 36
    #: table definitions stay schema-agnostic and a redeployment elsewhere needs
    #: no code change. ``public`` is kept second on the path so extension-owned
    #: objects - the ``vector`` type, ``uuid_generate_v4()``, ``gin_trgm_ops`` -
    #: still resolve; extensions are installed once per database, in ``public``,
    #: and are not duplicated per schema.
    schema_name: Annotated[str, Field(validation_alias="DB_SCHEMA")] = ""

    pool_size: Annotated[int, Field(validation_alias="DB_POOL_SIZE", ge=1, le=200)] = 20
    max_overflow: Annotated[int, Field(validation_alias="DB_MAX_OVERFLOW", ge=0, le=200)] = 10
    pool_timeout: Annotated[int, Field(validation_alias="DB_POOL_TIMEOUT", ge=1)] = 30
    pool_recycle: Annotated[int, Field(validation_alias="DB_POOL_RECYCLE", ge=-1)] = 1800
    echo: Annotated[bool, Field(validation_alias="DB_ECHO")] = False
    statement_timeout_ms: Annotated[
        int, Field(validation_alias="DB_STATEMENT_TIMEOUT_MS", ge=0)
    ] = 60_000
    #: How long a connection may sit inside an open transaction before Postgres
    #: terminates the session. 0 disables it.
    #:
    #: This is not a tuning knob - it decides whether the pipeline works. A stage
    #: runs inside one transaction, and ``ai_extraction`` spends minutes in
    #: provider calls with that transaction open, so the connection is *idle in
    #: transaction* for the whole run. At the previous hardcoded 2 minutes,
    #: Postgres killed the session partway through every non-trivial contract:
    #: the extraction results were lost, the failure handler could not even
    #: record why (its own write hit ``PendingRollbackError`` on the dead
    #: session), and the job surfaced the generic "Extraction produced no
    #: clauses, parties or dates" - which points at prompts and profiles rather
    #: than at the database that severed the connection.
    #:
    #: The default is generous rather than absent so a genuinely stuck
    #: transaction still gets reclaimed. See the note in db/session.py: the real
    #: fix is for long provider work not to hold a transaction at all.
    idle_in_transaction_timeout_ms: Annotated[
        int, Field(validation_alias="DB_IDLE_IN_TRANSACTION_TIMEOUT_MS", ge=0)
    ] = 1_800_000

    @computed_field  # type: ignore[prop-decorator]
    @property
    def async_url(self) -> str:
        """DSN forced onto the asyncpg driver (what the app uses)."""
        raw = str(self.url)
        if "+asyncpg" in raw:
            return raw
        return raw.replace("postgresql://", "postgresql+asyncpg://", 1).replace(
            "postgresql+psycopg://", "postgresql+asyncpg://", 1
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def sync_url(self) -> str:
        """DSN forced onto psycopg (what Alembic uses)."""
        raw = str(self.url)
        return raw.replace("postgresql+asyncpg://", "postgresql+psycopg://", 1).replace(
            "postgresql://", "postgresql+psycopg://", 1
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def search_path(self) -> str:
        """``search_path`` every connection is opened with.

        ``public`` is always retained as the second entry: the Postgres
        extensions this schema depends on live there, and dropping it would break
        the ``vector`` column type and the ``uuid_generate_v4()`` defaults even
        though the tables themselves are elsewhere.
        """
        name = self.schema_name.strip()
        return f"{name},public" if name and name != "public" else "public"


class RedisSettings(BaseSettings):
    """Redis: cache, sessions, rate limiting and the queue backend."""

    model_config = _group_config()

    url: Annotated[RedisDsn, Field(validation_alias="REDIS_URL")] = "redis://localhost:6379/0"  # type: ignore[assignment]
    cache_db: Annotated[int, Field(validation_alias="REDIS_CACHE_DB", ge=0, le=15)] = 1
    queue_db: Annotated[int, Field(validation_alias="REDIS_QUEUE_DB", ge=0, le=15)] = 0
    cache_ttl_seconds: Annotated[int, Field(validation_alias="CACHE_TTL_SECONDS", ge=0)] = 300
    max_connections: Annotated[int, Field(validation_alias="REDIS_MAX_CONNECTIONS", ge=1)] = 50

    @computed_field  # type: ignore[prop-decorator]
    @property
    def cache_url(self) -> str:
        """Same server, dedicated logical DB, so a cache flush never hits the queue."""
        base = str(self.url).rsplit("/", 1)[0]
        return f"{base}/{self.cache_db}"


class SecuritySettings(BaseSettings):
    """JWT, password hashing, rate limits and the seeded admin account."""

    model_config = _group_config()

    # refuses to boot when it is still set, which is stronger than omitting a default.
    jwt_secret: Annotated[str, Field(validation_alias="JWT_SECRET", min_length=16)] = (
        "change-me-in-production-use-openssl-rand-hex-32"  # noqa: S105
    )
    jwt_algorithm: Annotated[str, Field(validation_alias="JWT_ALGORITHM")] = "HS256"
    access_token_expire_minutes: Annotated[
        int, Field(validation_alias="ACCESS_TOKEN_EXPIRE_MINUTES", ge=1, le=1440)
    ] = 30
    refresh_token_expire_days: Annotated[
        int, Field(validation_alias="REFRESH_TOKEN_EXPIRE_DAYS", ge=1, le=365)
    ] = 14
    cookie_secure: Annotated[bool, Field(validation_alias="AUTH_COOKIE_SECURE")] = True
    password_hash_scheme: Annotated[
        Literal["argon2", "bcrypt"], Field(validation_alias="PASSWORD_HASH_SCHEME")
    ] = "argon2"  # noqa: S105 - an algorithm name, not a credential
    password_min_length: Annotated[int, Field(validation_alias="PASSWORD_MIN_LENGTH", ge=8)] = 8

    rate_limit_enabled: Annotated[bool, Field(validation_alias="RATE_LIMIT_ENABLED")] = True
    rate_limit_default: Annotated[str, Field(validation_alias="RATE_LIMIT_DEFAULT")] = "120/minute"
    rate_limit_login: Annotated[str, Field(validation_alias="RATE_LIMIT_LOGIN")] = "10/minute"

    # Shared secret the queue shim presents to /internal/* endpoints.
    internal_api_token: Annotated[str, Field(validation_alias="INTERNAL_API_TOKEN")] = (
        "change-me-internal-token"  # noqa: S105 - placeholder; production boot rejects it
    )

    seed_admin_email: Annotated[str, Field(validation_alias="SEED_ADMIN_EMAIL")] = (
        "admin@irisregtech.com"
    )
    # The documented first-run password. Production refuses to start while it is
    # unchanged, so the default is a prompt to rotate rather than a shipped credential.
    seed_admin_password: Annotated[str, Field(validation_alias="SEED_ADMIN_PASSWORD")] = "Abc@1234"  # noqa: S105
    seed_admin_name: Annotated[str, Field(validation_alias="SEED_ADMIN_NAME")] = (
        "System Administrator"
    )
    seed_admin_force_password_change: Annotated[
        bool, Field(validation_alias="SEED_ADMIN_FORCE_PASSWORD_CHANGE")
    ] = False

    # Starting credential issued when an administrator provisions an account without
    # choosing a password. The account is always forced to change it at first
    # sign-in, and production refuses to start while this is left at the default.
    new_user_default_password: Annotated[
        str, Field(validation_alias="NEW_USER_DEFAULT_PASSWORD")
    ] = "Abc@1234"  # noqa: S105


class OIDCSettings(BaseSettings):
    """Microsoft / Azure AD OIDC single sign-on ("Sign in with Microsoft")."""

    model_config = _group_config()

    enabled: Annotated[bool, Field(validation_alias="OIDC_ENABLED")] = False
    provider: Annotated[str, Field(validation_alias="OIDC_PROVIDER")] = "microsoft"
    tenant_id: Annotated[str, Field(validation_alias="AZURE_AD_TENANT_ID")] = ""
    client_id: Annotated[str, Field(validation_alias="AZURE_AD_CLIENT_ID")] = ""
    client_secret: Annotated[str, Field(validation_alias="AZURE_AD_CLIENT_SECRET")] = ""
    redirect_uri: Annotated[str, Field(validation_alias="OIDC_REDIRECT_URI")] = (
        "http://localhost:8000/api/v1/auth/oidc/callback"
    )
    post_login_redirect: Annotated[str, Field(validation_alias="OIDC_POST_LOGIN_REDIRECT")] = (
        "http://localhost:5173/auth/callback"
    )
    auto_provision_users: Annotated[bool, Field(validation_alias="OIDC_AUTO_PROVISION_USERS")] = (
        True
    )
    scopes: Annotated[str, Field(validation_alias="OIDC_SCOPES")] = "openid profile email"

    #: Try to sign in without showing any Microsoft UI first.
    #:
    #: When the browser already has exactly one signed-in Entra session that
    #: satisfies the request, `prompt=none` completes invisibly and the user lands
    #: straight in the app. When it does not, Entra returns an *error* rather than
    #: a page - `login_required`, `interaction_required` or
    #: `account_selection_required` - and the callback retries with
    #: `prompt=select_account`.
    #:
    #: This is the correct shape of "try silently, fall back to the account
    #: picker". Simply omitting `prompt`, which is the obvious-looking version, is
    #: not silent: Entra still renders a picker whenever more than one session
    #: exists, so the fallback never triggers and the user sees a page flash.
    silent_first: Annotated[bool, Field(validation_alias="OIDC_SILENT_FIRST")] = True

    #: Restrict sign-in to these email domains, lower-cased and comma-separated.
    #:
    #: Only meaningful with a multi-tenant authority (`tenant_id` unset or
    #: `common`/`organizations`), where *any* Microsoft account can complete the
    #: flow. Empty with a pinned tenant is correct - Entra already refuses
    #: everyone outside it.
    allowed_email_domains: Annotated[
        list[str], NoDecode, Field(validation_alias="OIDC_ALLOWED_EMAIL_DOMAINS")
    ] = []

    @field_validator("allowed_email_domains", mode="before")
    @classmethod
    def _split_domains(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip().lower().lstrip("@") for item in value.split(",") if item.strip()]
        return value

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_confidential_client(self) -> bool:
        """True when a client secret is configured.

        A registration without one is a *public* client, which is the normal
        shape for a browser app: there is nowhere in a SPA to keep a secret that
        the user cannot read. Public clients authenticate the code exchange with
        PKCE instead, which is why ``is_configured`` does not require a secret.
        """
        return bool(self.client_secret)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def authority(self) -> str:
        tenant = self.tenant_id or "common"
        return f"https://login.microsoftonline.com/{tenant}"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def discovery_url(self) -> str:
        return f"{self.authority}/v2.0/.well-known/openid-configuration"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_configured(self) -> bool:
        """Enough configuration to attempt a sign-in.

        A client secret is deliberately **not** required. The exchange is
        protected by PKCE, which works for public and confidential clients alike,
        and demanding a secret would make the common SPA registration - client id
        and redirect URI, no secret - look unconfigured.

        The tenant *is* required. Without it the authority falls back to
        ``common``, which accepts any Microsoft account on earth, including
        personal ones; that is a deployment nobody intends and it should fail
        loudly at configuration rather than quietly at the first outside login.
        """
        return bool(self.enabled and self.client_id and self.tenant_id)


class StorageSettings(BaseSettings):
    """Object storage behind ``IObjectStorage`` - swap provider, change no logic."""

    model_config = _group_config()

    provider: Annotated[StorageProvider, Field(validation_alias="STORAGE_PROVIDER")] = "local"
    container: Annotated[str, Field(validation_alias="STORAGE_CONTAINER")] = "contracts"
    signed_url_ttl_seconds: Annotated[
        int, Field(validation_alias="STORAGE_SIGNED_URL_TTL_SECONDS", ge=30)
    ] = 900

    local_root: Annotated[str, Field(validation_alias="STORAGE_LOCAL_ROOT")] = (
        "/var/lib/cip/storage"
    )

    azure_connection_string: Annotated[
        str, Field(validation_alias="AZURE_STORAGE_CONNECTION_STRING")
    ] = ""
    azure_account_name: Annotated[str, Field(validation_alias="AZURE_STORAGE_ACCOUNT_NAME")] = ""
    azure_account_key: Annotated[str, Field(validation_alias="AZURE_STORAGE_ACCOUNT_KEY")] = ""

    #: Where the *application* reaches object storage. Inside compose or Kubernetes
    #: this is a service name (``http://minio:9000``) that only resolves on the
    #: internal network.
    s3_endpoint_url: Annotated[str, Field(validation_alias="S3_ENDPOINT_URL")] = ""
    #: Where a *browser* reaches it, for presigned URLs.
    #:
    #: These differ whenever storage is not on the public internet, and the failure
    #: is silent from the server's side: the API happily signs a URL pointing at
    #: ``http://minio:9000``, the browser cannot resolve that host, and the PDF
    #: viewer shows nothing while every server-side check reports healthy.
    #:
    #: The signature is computed against this host, not rewritten afterwards -
    #: SigV4 signs the Host header, so patching the hostname into an already-signed
    #: URL produces a 403 from the storage service.
    #:
    #: Empty means "same as s3_endpoint_url", which is correct for real S3.
    s3_public_endpoint_url: Annotated[str, Field(validation_alias="S3_PUBLIC_ENDPOINT_URL")] = ""

    @property
    def s3_signing_endpoint(self) -> str:
        """The endpoint presigned URLs are signed for."""
        return self.s3_public_endpoint_url or self.s3_endpoint_url

    s3_region: Annotated[str, Field(validation_alias="S3_REGION")] = "us-east-1"
    s3_access_key_id: Annotated[str, Field(validation_alias="S3_ACCESS_KEY_ID")] = ""
    s3_secret_access_key: Annotated[str, Field(validation_alias="S3_SECRET_ACCESS_KEY")] = ""
    s3_use_path_style: Annotated[bool, Field(validation_alias="S3_USE_PATH_STYLE")] = True

    @computed_field  # type: ignore[prop-decorator]
    @property
    def artifacts_container(self) -> str:
        """Pipeline artifacts are kept apart from source documents."""
        return f"{self.container}-artifacts"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def exports_container(self) -> str:
        return f"{self.container}-exports"


class UploadSettings(BaseSettings):
    """Upload limits and file validation."""

    model_config = _group_config()

    max_upload_size_mb: Annotated[int, Field(validation_alias="MAX_UPLOAD_SIZE_MB", ge=1)] = 200
    max_files_per_upload: Annotated[
        int, Field(validation_alias="MAX_FILES_PER_UPLOAD", ge=1, le=1000)
    ] = 100
    # `NoDecode` for the same reason as `cors_origins`: pydantic-settings tries to
    # JSON-decode a `list[str]` before any validator runs, so the CSV form this
    # file documents - ALLOWED_FILE_TYPES=pdf,docx - aborts startup with
    # `error parsing value for field "allowed_file_types"`. It went unnoticed
    # because compose never sets the variable, leaving containers on the default;
    # only a local run that reads .env hit it.
    allowed_file_types: Annotated[
        list[str], NoDecode, Field(validation_alias="ALLOWED_FILE_TYPES")
    ] = ["pdf", "doc", "docx", "zip"]
    #: Absolute path to the LibreOffice binary, for converting DOC and DOCX to
    #: PDF. Empty means "find it": `PATH` first, then the standard install
    #: locations on each platform - LibreOffice does not add itself to `PATH` on
    #: Windows, so a working install looks missing without that search.
    #:
    #: Only Word uploads depend on this. PDF and ZIP-of-PDFs are unaffected, which
    #: is why a missing binary is a per-document rejection rather than a refusal
    #: to start.
    libreoffice_path: Annotated[str, Field(validation_alias="LIBREOFFICE_PATH")] = ""
    #: Ceiling on one conversion. Generous because LibreOffice's first run on a
    #: cold profile is much slower than its steady state, and a large document
    #: with many embedded images is legitimately slow.
    conversion_timeout_seconds: Annotated[
        int, Field(validation_alias="CONVERSION_TIMEOUT_SECONDS", ge=10, le=1800)
    ] = 180
    #: Ceiling on the *uncompressed* size of one archive member, and on the number
    #: of members. Both are zip-bomb guards: a few hundred kilobytes of archive can
    #: expand to gigabytes, and the extraction happens before any size check the
    #: per-file path would apply.
    max_archive_members: Annotated[
        int, Field(validation_alias="MAX_ARCHIVE_MEMBERS", ge=1, le=5000)
    ] = 500
    max_archive_uncompressed_mb: Annotated[
        int, Field(validation_alias="MAX_ARCHIVE_UNCOMPRESSED_MB", ge=1, le=20_000)
    ] = 2048
    virus_scan_enabled: Annotated[bool, Field(validation_alias="VIRUS_SCAN_ENABLED")] = False
    clamav_host: Annotated[str, Field(validation_alias="CLAMAV_HOST")] = "clamav"
    clamav_port: Annotated[int, Field(validation_alias="CLAMAV_PORT")] = 3310

    #: Reject a file whose SHA-256 already exists in the project.
    #:
    #: Defaults to on because that is the correct behaviour: the same contract
    #: uploaded twice is one contract, and accepting it again spends a full
    #: pipeline run - parse, extract, embed - to produce a duplicate row.
    #:
    #: Currently switched **off** in configuration while the document pipeline is
    #: being iterated on, because re-running the same test PDF is the whole
    #: workflow. With it off, each upload of the same file creates a new contract
    #: and a new job; `replace_existing` remains the supported way to add a
    #: version to the existing contract. Turn it back on by setting
    #: ``UPLOAD_DUPLICATE_CHECK=true``.
    duplicate_check: Annotated[bool, Field(validation_alias="UPLOAD_DUPLICATE_CHECK")] = True

    @field_validator("allowed_file_types", mode="before")
    @classmethod
    def _parse_types(cls, value: str | list[str] | None) -> list[str]:
        return [t.lower().lstrip(".") for t in _csv_list(value)]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def max_upload_size_bytes(self) -> int:
        return self.max_upload_size_mb * 1024 * 1024


class ParserSettings(BaseSettings):
    """Parser selection, OCR and per-parser credentials."""

    model_config = _group_config()

    #: Defaults to the iDoc layout service: it returns real layout roles and
    #: coordinates, so section structure comes from the service rather than from
    #: font-size heuristics.
    active_parser: Annotated[ParserName, Field(validation_alias="ACTIVE_PARSER")] = "idoc"

    #: See :data:`ParserMode`. Defaults to ``fixture`` so nothing calls the layout
    #: service unless a deployment says so.
    parser_mode: Annotated[ParserMode, Field(validation_alias="PARSER_MODE")] = "fixture"
    #: Where recorded responses live. Committed fixtures make a fresh clone able to
    #: run the whole pipeline offline.
    fixture_dir: Annotated[str, Field(validation_alias="PARSER_FIXTURE_DIR")] = (
        "storage/fixtures/parser"
    )
    #: Record responses while in ``live`` mode. Off only when a deployment must not
    #: write contract-derived content to local disk.
    record_fixtures: Annotated[bool, Field(validation_alias="PARSER_RECORD_FIXTURES")] = True

    @property
    def is_fixture_mode(self) -> bool:
        return self.parser_mode == "fixture"

    timeout_seconds: Annotated[int, Field(validation_alias="PARSER_TIMEOUT_SECONDS", ge=10)] = 900
    max_retries: Annotated[int, Field(validation_alias="PARSER_MAX_RETRIES", ge=0, le=10)] = 3

    ocr_enabled: Annotated[bool, Field(validation_alias="OCR_ENABLED")] = True
    ocr_engine: Annotated[Literal["tesseract", "azure"], Field(validation_alias="OCR_ENGINE")] = (
        "tesseract"
    )
    tesseract_cmd: Annotated[str, Field(validation_alias="TESSERACT_CMD")] = "/usr/bin/tesseract"
    ocr_scanned_page_char_threshold: Annotated[
        int, Field(validation_alias="OCR_SCANNED_PAGE_CHAR_THRESHOLD", ge=0)
    ] = 40
    ocr_dpi: Annotated[int, Field(validation_alias="OCR_DPI", ge=72, le=600)] = 200

    azure_docintel_endpoint: Annotated[str, Field(validation_alias="AZURE_DOCINTEL_ENDPOINT")] = ""
    azure_docintel_key: Annotated[str, Field(validation_alias="AZURE_DOCINTEL_KEY")] = ""
    azure_docintel_model: Annotated[str, Field(validation_alias="AZURE_DOCINTEL_MODEL")] = (
        "prebuilt-layout"
    )

    # --- iDoc layout service --------------------------------------------------
    #: In-house document layout service. Takes a PDF on a multipart ``file`` field
    #: and returns a ZIP of per-page Document Intelligence layout JSON. Used in place
    #: of calling Azure Document Intelligence directly, so no Azure credential is
    #: needed in the application at all.
    idoc_endpoint: Annotated[str, Field(validation_alias="IDOC_ENDPOINT")] = (
        "https://devidocapi2.iriscarbon.com/pdf/upload-pdf-to-json"
    )
    #: Optional bearer token or API key, if the service is placed behind auth.
    idoc_api_key: Annotated[str, Field(validation_alias="IDOC_API_KEY")] = ""
    idoc_api_key_header: Annotated[str, Field(validation_alias="IDOC_API_KEY_HEADER")] = "X-API-Key"
    #: Layout analysis on a long agreement is slow, and this is a remote call - so it
    #: gets its own timeout rather than sharing the local-parse budget.
    idoc_timeout_seconds: Annotated[
        int, Field(validation_alias="IDOC_TIMEOUT_SECONDS", ge=30, le=3600)
    ] = 600
    #: Verify TLS. Only ever disabled for a self-signed internal deployment, and the
    #: production guard refuses to boot when it is off.
    idoc_verify_tls: Annotated[bool, Field(validation_alias="IDOC_VERIFY_TLS")] = True

    # --- pdf_text_extractor (local layout, no service) ------------------------
    #
    # A local checkout that produces the same Azure ``prebuilt-layout`` shape iDoc
    # returns, via its ``--adi`` export. Selecting ``ACTIVE_PARSER=pdfextract``
    # therefore changes only where the layout JSON comes from - every downstream
    # stage, including the document pipeline that reads the raw per-page payloads,
    # is unaffected.
    #
    # Invoked as a subprocess against its own virtualenv rather than imported. Its
    # dependency set is large and partly non-Python (Tesseract and poppler are OS
    # packages), and pulling that into this service to run one CLI would make every
    # deployment carry an OCR stack it may never use.
    #: The same extractor, reached over HTTP instead of spawned.
    #:
    #: When it runs as a service, `POST /extract?format=adi` returns the identical
    #: payload the CLI writes with `--adi` - so this changes only *how* the
    #: extractor is invoked, not what comes back, and every downstream stage is
    #: untouched. Set this and the subprocess path is not used; leave it empty and
    #: `PDFEXTRACT_PATH` behaves exactly as before.
    #:
    #: Preferred in a container: no checkout to mount, and no virtualenv that has
    #: to match the image's platform.
    pdfextract_url: Annotated[str, Field(validation_alias="PDFEXTRACT_URL")] = ""
    pdfextract_path: Annotated[str, Field(validation_alias="PDFEXTRACT_PATH")] = ""
    #: ``baseline`` is pdfplumber + Tesseract and needs no models. ``docling`` is
    #: more accurate on complex layouts and much heavier; it must be installed in
    #: the extractor's own environment.
    pdfextract_backend: Annotated[str, Field(validation_alias="PDFEXTRACT_BACKEND")] = "baseline"
    #: OCR render resolution. The extractor's own default is 400, which is slow on a
    #: long scanned agreement; 300 is the usual accuracy/time trade.
    pdfextract_dpi: Annotated[
        int, Field(validation_alias="PDFEXTRACT_DPI", ge=72, le=600)
    ] = 300
    #: Local, but not fast: OCR over a 100-page scan is minutes of CPU.
    pdfextract_timeout_seconds: Annotated[
        int, Field(validation_alias="PDFEXTRACT_TIMEOUT_SECONDS", ge=30, le=7200)
    ] = 1800

    @computed_field  # type: ignore[prop-decorator]
    @property
    def pdfextract_python(self) -> str:
        """The extractor's own interpreter, or "" when the path is unset.

        Its virtualenv is used deliberately: the point of the subprocess boundary
        is that the extractor's dependencies stay out of this service.
        """
        if not self.pdfextract_path:
            return ""
        root = Path(self.pdfextract_path)
        for candidate in (
            root / ".venv" / "Scripts" / "python.exe",  # Windows
            root / ".venv" / "bin" / "python",  # POSIX
        ):
            if candidate.is_file():
                return str(candidate)
        return ""


class BlankIsUnsetMixin:
    """An empty value means "not configured", not "configure it to nothing".

    Two callers make this necessary rather than tidy. Compose interpolates an
    unset variable to the empty string - `FOO: ${FOO:-}` forwards `FOO=`, not
    nothing - and a `.env` line of `SOME_SETTING=` is how people write "leave the
    default alone". Both otherwise abort startup on a parse of ''.

    It also matters wherever "was this supplied?" is asked rather than "what is
    its value?" - see ``RetrievalSettings.similarity_floor``. A blank that
    survived to validation would land in ``model_fields_set`` and read as an
    explicit setting. Dropping the key here keeps it out of that set, so blank
    and absent behave identically.

    A mixin rather than a copy per group: the trap is a property of how compose
    and dotenv files pass values, so it applies to every settings group that has
    an optional key, not just the first one that hit it.
    """

    @model_validator(mode="before")
    @classmethod
    def _blank_means_unset(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        return {
            key: value
            for key, value in data.items()
            if not (isinstance(value, str) and not value.strip())
        }


class LLMSettings(BlankIsUnsetMixin, BaseSettings):
    """Inference provider configuration with cost-aware model routing."""

    model_config = _group_config(allow_model_prefix=True)

    #: Gemini Flash by default. The choice is configuration, not architecture:
    #: every provider below sits behind ``IInferenceProvider`` and switching is one
    #: environment variable with no code change.
    #:
    #: Flash rather than a frontier model because the two workloads that dominate
    #: token spend here - clause extraction and classification - are constrained
    #: schema-filling, not open-ended reasoning, and a cheaper model does them just
    #: as well at a fraction of the cost.
    #:
    #: Set ``LLM_PROVIDER=mock`` to run with no vendor account at all; the pipeline
    #: still completes end to end, with synthesised extractions.
    provider: Annotated[LLMProvider, Field(validation_alias="LLM_PROVIDER")] = "gemini"
    #: Default model when the provider is Anthropic. Claude Opus 5 unless a
    #: deployment opts down explicitly.
    model: Annotated[str, Field(validation_alias="LLM_MODEL")] = "claude-opus-5"
    model_complex: Annotated[str, Field(validation_alias="LLM_MODEL_COMPLEX")] = "claude-opus-5"
    model_simple: Annotated[str, Field(validation_alias="LLM_MODEL_SIMPLE")] = "claude-haiku-4-5"
    #: Chain-of-thought tier. Empty falls back to `model_complex` - deliberately
    #: not to `model`, so an unset reasoning model degrades to "strong" rather
    #: than to whatever the default happens to be. See app.ai.routing.
    model_reasoning: Annotated[str, Field(validation_alias="LLM_MODEL_REASONING")] = ""

    # --- Google Gemini -------------------------------------------------------
    #: Read from the environment only. Never committed, never defaulted to a real
    #: value - the platform's rule is secrets via env or a secret manager.
    google_api_key: Annotated[str, Field(validation_alias="GOOGLE_API_KEY")] = ""
    gemini_base_url: Annotated[str, Field(validation_alias="GEMINI_BASE_URL")] = (
        "https://generativelanguage.googleapis.com/v1beta"
    )
    #: Flash-tier by default: the cost-per-quality sweet spot for clause extraction,
    #: which is a constrained-schema task rather than an open-ended reasoning one.
    #: `gemini_model_complex` is used only for the intents the router marks complex.
    gemini_model: Annotated[str, Field(validation_alias="GEMINI_MODEL")] = "gemini-2.5-flash"
    gemini_model_complex: Annotated[str, Field(validation_alias="GEMINI_MODEL_COMPLEX")] = (
        "gemini-2.5-flash"
    )
    gemini_model_simple: Annotated[str, Field(validation_alias="GEMINI_MODEL_SIMPLE")] = (
        "gemini-2.5-flash-lite"
    )

    #: Reasoning effort, inside ``output_config`` (low|medium|high|xhigh|max).
    #: Clause extraction is intelligence-sensitive, so the floor is ``high``;
    #: cheap mechanical calls override per request.
    effort: Annotated[str, Field(validation_alias="LLM_EFFORT")] = "high"
    effort_simple: Annotated[str, Field(validation_alias="LLM_EFFORT_SIMPLE")] = "low"
    effort_complex: Annotated[str, Field(validation_alias="LLM_EFFORT_COMPLEX")] = "xhigh"
    effort_reasoning: Annotated[str, Field(validation_alias="LLM_EFFORT_REASONING")] = ""

    #: Hard output ceiling. Generous because thinking counts against it on models
    #: where thinking is on by default - a tight budget truncates mid-answer.
    max_output_tokens: Annotated[
        int, Field(validation_alias="LLM_MAX_OUTPUT_TOKENS", ge=1024, le=128_000)
    ] = 16_000
    #: Streaming raises the practical ceiling; non-streaming risks HTTP timeouts
    #: above ~16k.
    max_output_tokens_streaming: Annotated[
        int, Field(validation_alias="LLM_MAX_OUTPUT_TOKENS_STREAMING", ge=1024, le=128_000)
    ] = 64_000
    timeout_seconds: Annotated[int, Field(validation_alias="LLM_TIMEOUT_SECONDS", ge=5)] = 600
    #: Per-tier timeouts. Empty (0) inherits `timeout_seconds`.
    #:
    #: One shared ceiling has to be set for the slowest legitimate case, which
    #: means a hung *extraction* holds a worker slot for as long as a genuine
    #: reasoning call would take. Splitting them lets a fast tier fail fast.
    timeout_seconds_simple: Annotated[
        int, Field(validation_alias="LLM_TIMEOUT_SECONDS_SIMPLE", ge=0)
    ] = 90
    timeout_seconds_complex: Annotated[
        int, Field(validation_alias="LLM_TIMEOUT_SECONDS_COMPLEX", ge=0)
    ] = 300
    #: Sampling temperature for the **extraction** purpose only. Answering and
    #: summarisation are untouched.
    #:
    #: Defaults to 1.0, which is what the provider has always used - no
    #: temperature has ever been sent, and Azure's default for chat completions
    #: is 1.0. Declaring it changes nothing until it is set.
    #:
    #: Why it exists: extraction was measured disagreeing with *itself* on 16.9%
    #: of fields across two runs over byte-identical prompts and evidence. Both
    #: upstream stages were proven deterministic over 180 comparisons, so
    #: sampling is the only remaining source. Structured extraction against a
    #: strict schema has no obvious need for sampling diversity - but that is a
    #: hypothesis, and this setting exists to test it rather than assume it.
    extraction_temperature: Annotated[
        float, Field(validation_alias="AI_EXTRACTION_TEMPERATURE", ge=0.0, le=2.0)
    ] = 1.0
    max_retries: Annotated[int, Field(validation_alias="LLM_MAX_RETRIES", ge=0, le=10)] = 3
    #: Base delay for exponential backoff between provider retries, in seconds.
    #: Doubles per attempt with jitter; see app.ai.rag.providers.
    retry_backoff_seconds: Annotated[
        float, Field(validation_alias="LLM_RETRY_BACKOFF_SECONDS", ge=0.0, le=60.0)
    ] = 1.0

    #: Server-side refusal fallback. Safety classifiers can decline a request with
    #: HTTP 200 + ``stop_reason: "refusal"``; without a fallback the request simply
    #: stops. Contract text (indemnities, security clauses, breach terms) sits close
    #: enough to those categories that a false positive is a real operational risk.
    refusal_fallback_enabled: Annotated[bool, Field(validation_alias="LLM_REFUSAL_FALLBACK")] = True

    #: Cache the stable prompt prefix. Extraction sends the same system prompt and
    #: output schema across thousands of chunks, so the prefix is the bulk of the
    #: spend and cache reads cost ~0.1x.
    prompt_caching_enabled: Annotated[bool, Field(validation_alias="LLM_PROMPT_CACHING")] = True

    anthropic_api_key: Annotated[str, Field(validation_alias="ANTHROPIC_API_KEY")] = ""
    #: How structured (JSON) output is requested from an OpenAI-compatible model.
    #:
    #: * ``json_schema`` - send ``response_format={"type": "json_schema", strict}``.
    #:   Correct for OpenAI, where malformed JSON becomes a provider-level
    #:   impossibility rather than something the validator has to catch.
    #: * ``none`` - send no ``response_format`` and recover the object from the
    #:   text instead.
    #:
    #: ``none`` exists because the strict modes are an OpenAI extension that many
    #: models behind an OpenAI-compatible gateway do not implement, and the
    #: failure is silent rather than an error: `z-ai/glm-4.7` on OpenRouter
    #: answers a `json_schema` request with 16 tokens and `content: null`, which
    #: surfaces only as "The model returned an empty structured response". The
    #: same model without `response_format` returns correct JSON, which
    #: `_salvage_json` already unwraps from a ```json fence.
    llm_structured_output: Annotated[
        Literal["json_schema", "none"], Field(validation_alias="LLM_STRUCTURED_OUTPUT")
    ] = "json_schema"

    openai_api_key: Annotated[str, Field(validation_alias="OPENAI_API_KEY")] = ""
    #: Base URL for the OpenAI-compatible endpoint. Empty means OpenAI itself.
    #:
    #: Exists so an OpenAI-compatible gateway - OpenRouter, Together, vLLM, LM
    #: Studio - can be used without a separate adapter: they all speak the same
    #: `/chat/completions` and `/embeddings` wire format, so the only thing that
    #: differs is where the request goes. Applies to both inference and
    #: embeddings, which is correct for a gateway that serves both.
    openai_base_url: Annotated[str, Field(validation_alias="OPENAI_BASE_URL")] = ""
    azure_openai_endpoint: Annotated[str, Field(validation_alias="AZURE_OPENAI_ENDPOINT")] = ""
    azure_openai_api_key: Annotated[str, Field(validation_alias="AZURE_OPENAI_API_KEY")] = ""
    azure_openai_api_version: Annotated[str, Field(validation_alias="AZURE_OPENAI_API_VERSION")] = (
        "2024-10-21"
    )
    azure_openai_deployment: Annotated[str, Field(validation_alias="AZURE_OPENAI_DEPLOYMENT")] = ""
    local_base_url: Annotated[str, Field(validation_alias="LLM_LOCAL_BASE_URL")] = (
        "http://localhost:8080/v1"
    )

    # Fallback chain: if the primary provider fails after retries, try these in
    # order before surfacing an error (§17 failure handling).
    fallback_providers: Annotated[list[str], Field(validation_alias="LLM_FALLBACK_PROVIDERS")] = []

    @field_validator("fallback_providers", mode="before")
    @classmethod
    def _parse_fallbacks(cls, value: str | list[str] | None) -> list[str]:
        return _csv_list(value)


#: Native output width per known model, so a truncation request can be recognised
#: as deliberate Matryoshka slicing rather than a misconfiguration. A model absent
#: from this table is trusted to match ``EMBEDDING_DIM`` and is verified against the
#: provider's first real response either way.
_NATIVE_DIMS: dict[str, int] = {
    "nvidia/nemotron-3-embed-1b": 2048,
    "nvidia/nv-embedqa-e5-v5": 1024,
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
    "sentence-transformers/all-MiniLM-L6-v2": 384,
}


class EmbeddingSettings(BaseSettings):
    """Embedding provider, dimensionality and index tuning."""

    model_config = _group_config(allow_model_prefix=True)

    provider: Annotated[EmbeddingProvider, Field(validation_alias="EMBEDDING_PROVIDER")] = "mock"
    model: Annotated[str, Field(validation_alias="EMBEDDING_MODEL")] = "nvidia/nemotron-3-embed-1b"
    #: 2048 is what ``nvidia/nemotron-3-embed-1b`` actually returns - not an
    #: assumption, and not negotiable without a re-embed. The model is Matryoshka,
    #: so 1024 and 512 are also valid *if* the vector is re-normalised after
    #: slicing; the provider does that when this is set below the native width.
    #: The ceiling is 4000 because that is pgvector's HNSW limit for ``halfvec``.
    dim: Annotated[int, Field(validation_alias="EMBEDDING_DIM", ge=64, le=4000)] = 2048
    storage: Annotated[VectorStorage, Field(validation_alias="EMBEDDING_STORAGE")] = "halfvec"
    batch_size: Annotated[int, Field(validation_alias="EMBEDDING_BATCH_SIZE", ge=1, le=1024)] = 64
    version: Annotated[str, Field(validation_alias="EMBEDDING_VERSION")] = "v1"
    sentence_transformers_model: Annotated[
        str, Field(validation_alias="SENTENCE_TRANSFORMERS_MODEL")
    ] = "sentence-transformers/all-MiniLM-L6-v2"
    max_retries: Annotated[int, Field(validation_alias="EMBEDDING_MAX_RETRIES", ge=0)] = 3
    timeout_seconds: Annotated[int, Field(validation_alias="EMBEDDING_TIMEOUT_SECONDS", ge=5)] = 60

    # --- Azure OpenAI --------------------------------------------------------
    #: Embeddings may live on a different Azure resource from the chat model, and
    #: routinely do: the chat deployment is often a dev resource in one region
    #: while embeddings run on a shared production one. The provider previously
    #: read `LLMSettings.azure_openai_*` for both, so pointing them at separate
    #: resources was not expressible - the embedding calls went to the chat
    #: endpoint and 404ed on a deployment that is not there.
    #:
    #: Each falls back to the corresponding LLM value when empty, so a deployment
    #: using one resource for both keeps working with nothing set here.
    azure_endpoint: Annotated[str, Field(validation_alias="AZURE_OPENAI_EMBEDDING_ENDPOINT")] = ""
    azure_api_key: Annotated[str, Field(validation_alias="AZURE_OPENAI_EMBEDDING_API_KEY")] = ""
    azure_api_version: Annotated[
        str, Field(validation_alias="AZURE_OPENAI_EMBEDDING_API_VERSION")
    ] = ""
    azure_deployment: Annotated[
        str, Field(validation_alias="AZURE_OPENAI_EMBEDDING_DEPLOYMENT")
    ] = ""

    # --- NVIDIA NIM ----------------------------------------------------------
    nvidia_api_key: Annotated[str, Field(validation_alias="NVIDIA_API_KEY")] = ""
    #: Hosted NIM by default; point at a self-hosted NIM container to keep contract
    #: text inside your own network.
    nvidia_base_url: Annotated[str, Field(validation_alias="NVIDIA_BASE_URL")] = (
        "https://integrate.api.nvidia.com/v1"
    )
    #: Nemotron 3 Embed is *asymmetric*: it expects `query:` before a search string
    #: and `passage:` before a document. Getting this wrong does not error - it
    #: quietly costs recall, which is the hardest kind of regression to notice.
    nvidia_query_prefix: Annotated[str, Field(validation_alias="NVIDIA_QUERY_PREFIX")] = "query:"
    nvidia_passage_prefix: Annotated[str, Field(validation_alias="NVIDIA_PASSAGE_PREFIX")] = (
        "passage:"
    )
    #: Connection pool for the NIM client, shared across concurrent embed calls.
    nvidia_max_connections: Annotated[
        int, Field(validation_alias="NVIDIA_MAX_CONNECTIONS", ge=1, le=200)
    ] = 20
    #: Probe the provider during startup. Off in tests and local UI work, where a
    #: network round trip per boot is pure friction.
    verify_on_startup: Annotated[bool, Field(validation_alias="EMBEDDING_VERIFY_ON_STARTUP")] = True

    @property
    def native_dim(self) -> int:
        """The model's full width, before any Matryoshka truncation."""
        return _NATIVE_DIMS.get(self.model, self.dim)

    @property
    def is_truncated(self) -> bool:
        return self.dim < self.native_dim

    hnsw_m: Annotated[int, Field(validation_alias="HNSW_M", ge=4, le=100)] = 16
    hnsw_ef_construction: Annotated[
        int, Field(validation_alias="HNSW_EF_CONSTRUCTION", ge=16, le=1000)
    ] = 64
    #: Candidate list size for an HNSW *query* - the only knob that trades recall
    #: for latency at query time, as opposed to `m`/`ef_construction`, which shape
    #: the graph when it is built.
    #:
    #: Declared here since the vector schema was introduced, and until now never
    #: applied: nothing issued the `SET hnsw.ef_search`, so every search ran at
    #: pgvector's default of 40 regardless of this value. See
    #: `RetrievalEngine._vector_search`, which now sets it per transaction.
    hnsw_ef_search: Annotated[int, Field(validation_alias="HNSW_EF_SEARCH", ge=16, le=1000)] = 80
    #: ``off`` | ``relaxed_order`` | ``strict_order`` (pgvector 0.8+).
    #:
    #: Defaults to ``relaxed_order`` because every similarity query here carries
    #: filters the HNSW index cannot use - project, contract, model, metadata. With
    #: a single-pass scan those filters are applied *after* the index returns its
    #: ``ef_search`` globally-nearest rows, so a large multi-project table returns
    #: nothing for questions that have a perfect answer in it. ``relaxed_order``
    #: keeps scanning until enough rows survive the filter.
    hnsw_iterative_scan: Annotated[
        Literal["off", "relaxed_order", "strict_order"],
        Field(validation_alias="HNSW_ITERATIVE_SCAN"),
    ] = "relaxed_order"
    #: Ceiling on an iterative scan, so a query no row can satisfy stops rather
    #: than walking the whole graph.
    hnsw_max_scan_tuples: Annotated[
        int, Field(validation_alias="HNSW_MAX_SCAN_TUPLES", ge=1000, le=1_000_000)
    ] = 20_000


#: Per-level similarity floors used when nothing is configured at all.
#:
#: Deliberately not equal: the floor has to suit the unit of text being matched.
#: These were field defaults on ``RetrievalSettings`` until 2026-08-03, where they
#: shadowed the base setting - see ``RetrievalSettings.similarity_floor``. Keeping
#: them as the no-configuration fallback is what makes that fix behaviour-preserving
#: for every deployment that sets none of these variables.
_LEVEL_SIMILARITY_DEFAULTS: Final[dict[str, float]] = {
    "document_summary": 0.35,
    "clause": 0.45,
    "chunk": 0.40,
}


class RetrievalSettings(BlankIsUnsetMixin, BaseSettings):
    """Retrieval planner, re-ranking and context assembly budgets."""

    model_config = _group_config(allow_model_prefix=True)

    max_documents: Annotated[int, Field(validation_alias="RETRIEVAL_MAX_DOCUMENTS", ge=1)] = 25
    max_clauses: Annotated[int, Field(validation_alias="RETRIEVAL_MAX_CLAUSES", ge=1)] = 40
    #: Top-K for the chunk level, per the architecture decision. The document and
    #: clause levels stay wider because they feed candidate selection rather than
    #: the answer context.
    max_chunks: Annotated[int, Field(validation_alias="RETRIEVAL_MAX_CHUNKS", ge=1)] = 20
    #: Cosine-similarity floor for a vector hit.
    #:
    #: Tuned for Nemotron 3 Embed, whose L2-normalised space puts unrelated
    #: contract text noticeably higher than OpenAI's ada-family did - 0.25 there is
    #: roughly 0.40 here, so the old value would admit almost everything and let
    #: the top-K cut do all the work. Per-level floors override this where the unit
    #: of text differs enough to matter.
    min_similarity: Annotated[
        float, Field(validation_alias="RETRIEVAL_MIN_SIMILARITY", ge=0.0, le=1.0)
    ] = 0.40
    #: A document summary is long and topical, so near-neighbours cluster high; a
    #: clause is short and formulaic, so boilerplate scores high against everything
    #: and needs a stricter floor to stay useful.
    #:
    #: ``None`` means "not configured", which is what lets ``similarity_floor``
    #: inherit. These carried eager defaults of 0.35/0.45/0.40 until 2026-08-03,
    #: which made ``RETRIEVAL_MIN_SIMILARITY`` unreachable - see that method.
    #: The defaults still apply; they live in ``_LEVEL_SIMILARITY_DEFAULTS`` now.
    #:
    #: Both spellings are accepted. ``RETRIEVAL_MIN_SIMILARITY_DOCUMENT`` is what
    #: the deployed configuration and docs use; ``RETRIEVAL_DOCUMENT_MIN_SIMILARITY``
    #: reads more naturally and is what people reach for first. Silently ignoring
    #: the other one is the exact failure this change exists to remove.
    min_similarity_document: Annotated[
        float | None,
        Field(
            validation_alias=AliasChoices(
                "RETRIEVAL_MIN_SIMILARITY_DOCUMENT", "RETRIEVAL_DOCUMENT_MIN_SIMILARITY"
            ),
            ge=0.0,
            le=1.0,
        ),
    ] = None
    min_similarity_clause: Annotated[
        float | None,
        Field(
            validation_alias=AliasChoices(
                "RETRIEVAL_MIN_SIMILARITY_CLAUSE", "RETRIEVAL_CLAUSE_MIN_SIMILARITY"
            ),
            ge=0.0,
            le=1.0,
        ),
    ] = None
    min_similarity_chunk: Annotated[
        float | None,
        Field(
            validation_alias=AliasChoices(
                "RETRIEVAL_MIN_SIMILARITY_CHUNK", "RETRIEVAL_CHUNK_MIN_SIMILARITY"
            ),
            ge=0.0,
            le=1.0,
        ),
    ] = None

    #: Hybrid fusion weights. Vector leads because the questions this platform
    #: answers are paraphrases far more often than they are exact phrases, but
    #: keyword weight is deliberately non-trivial: a lawyer searching for
    #: "gross negligence" wants that literal wording, and a purely semantic search
    #: will happily return "wilful misconduct" instead.
    vector_weight: Annotated[
        float, Field(validation_alias="RETRIEVAL_VECTOR_WEIGHT", ge=0.0, le=1.0)
    ] = 0.65
    keyword_weight: Annotated[
        float, Field(validation_alias="RETRIEVAL_KEYWORD_WEIGHT", ge=0.0, le=1.0)
    ] = 0.35

    def similarity_floor(self, level: str) -> float:
        """The floor for one embedding level, most specific setting wins.

        Three tiers, in order:

        1. the level's own variable, e.g. ``RETRIEVAL_MIN_SIMILARITY_CLAUSE``;
        2. ``RETRIEVAL_MIN_SIMILARITY``, when it was actually supplied;
        3. the per-level default in ``_LEVEL_SIMILARITY_DEFAULTS``.

        Tier 2 is the whole point, and it did not work. The per-level fields were
        typed ``float | None`` - the ``None`` being the "inherit from the base"
        sentinel - but were *declared* with eager defaults of 0.35/0.45/0.40. So
        the sentinel never occurred, tier 2 was unreachable, and setting
        ``RETRIEVAL_MIN_SIMILARITY`` changed a number that nothing read. It looked
        configurable at every layer: the variable is documented, it parses, it
        validates, ``settings.retrieval.min_similarity`` reports the new value, and
        the seed even writes it into ``profile.thresholds``. Only retrieval itself
        ignored it, which is the one place with no way to see it.

        Tier 3 is a deliberate asymmetry rather than "default to the base". The
        floors differ per level because the *unit of text* differs - a formulaic
        clause scores high against everything, so it needs a stricter floor than a
        long topical summary - and that relationship should survive an operator
        who never sets anything. But once they do set a base explicitly, they have
        stated an intent, and silently keeping three unrelated numbers over it is
        how this bug read from the outside.
        """
        override = {
            "document_summary": self.min_similarity_document,
            "clause": self.min_similarity_clause,
            "chunk": self.min_similarity_chunk,
        }.get(level)
        if override is not None:
            return override
        if "min_similarity" in self.model_fields_set:
            return self.min_similarity
        return _LEVEL_SIMILARITY_DEFAULTS.get(level, self.min_similarity)

    def effective_similarity_floors(self) -> dict[str, float]:
        """What retrieval will actually use, per level.

        For diagnostics and the benchmark snapshot, which otherwise record the
        *fields* - now mostly ``None`` - rather than the numbers in force.
        """
        return {level: self.similarity_floor(level) for level in _LEVEL_SIMILARITY_DEFAULTS}

    timeout_seconds: Annotated[int, Field(validation_alias="RETRIEVAL_TIMEOUT_SECONDS", ge=1)] = 20
    graph_max_depth: Annotated[int, Field(validation_alias="RETRIEVAL_GRAPH_MAX_DEPTH", ge=1)] = 3

    reranker_enabled: Annotated[bool, Field(validation_alias="RERANKER_ENABLED")] = False
    reranker_model: Annotated[str, Field(validation_alias="RERANKER_MODEL")] = (
        "cross-encoder/ms-marco-MiniLM-L-6-v2"
    )
    rerank_top_k: Annotated[int, Field(validation_alias="RERANK_TOP_K", ge=1)] = 20

    context_token_budget: Annotated[
        int, Field(validation_alias="CONTEXT_TOKEN_BUDGET", ge=1000)
    ] = 24_000
    # Reciprocal-rank-fusion constant used when blending keyword + vector hits.
    rrf_k: Annotated[int, Field(validation_alias="RETRIEVAL_RRF_K", ge=1)] = 60

    # --- Copilot query pipeline ----------------------------------------------
    #: Below this, a detected document type is discarded and retrieval runs
    #: unfiltered. The asymmetry is the reason it is set high: filtering to the
    #: wrong type removes the answer outright, whereas not filtering only makes
    #: the search wider. A guess is worse than no opinion.
    document_type_confidence_threshold: Annotated[
        float, Field(validation_alias="COPILOT_DOCTYPE_CONFIDENCE_THRESHOLD", ge=0.0, le=1.0)
    ] = 0.75
    #: Answer-level guardrail. Distinct from the per-level ``min_similarity``
    #: floors, which decide what retrieval *returns*: this decides whether the
    #: best of what came back is good enough to answer from at all. Below it the
    #: Copilot says so and no inference call is made.
    answer_similarity_threshold: Annotated[
        float, Field(validation_alias="COPILOT_SIMILARITY_THRESHOLD", ge=0.0, le=1.0)
    ] = 0.45
    #: Candidates pulled per level before re-ranking.
    copilot_top_k: Annotated[int, Field(validation_alias="COPILOT_TOP_K", ge=1, le=200)] = 25
    #: Passages that survive into the prompt. Fewer, better passages beat more:
    #: every marginal one dilutes the evidence the answer is graded against.
    top_context_chunks: Annotated[
        int, Field(validation_alias="COPILOT_CONTEXT_CHUNKS", ge=1, le=50)
    ] = 8

    # --- retrieval audit ------------------------------------------------------
    #: Whether the question's text is stored on the retrieval audit row.
    #:
    #: A question about a contract can carry counterparty names and deal terms, and
    #: some deployments cannot retain that. Off is a supported configuration: every
    #: other field - strategy, similarity, latency, cost, versions - is what the
    #: retrieval-quality metrics are built from, and none of them need the text.
    audit_store_query_text: Annotated[
        bool, Field(validation_alias="RETRIEVAL_AUDIT_STORE_QUERY_TEXT")
    ] = True
    #: Days to keep retrieval audit rows. Unbounded growth in a table holding user
    #: queries is a compliance finding on its own, not merely a disk-space one -
    #: there has to be a defined answer to "how long do you keep this".
    audit_retention_days: Annotated[
        int, Field(validation_alias="RETRIEVAL_AUDIT_RETENTION_DAYS", ge=1, le=3650)
    ] = 180


class QueueSettings(BaseSettings):
    """Queue/dispatch layer. BullMQ by default; ``arq`` keeps the same contract."""

    model_config = _group_config()

    driver: Annotated[QueueDriver, Field(validation_alias="QUEUE_DRIVER")] = "bullmq"
    prefix: Annotated[str, Field(validation_alias="QUEUE_PREFIX")] = "cip"
    #: Where the Python orchestrator posts jobs. The Node dispatcher owns BullMQ's
    #: key structures so queue semantics live in one language (§1.2).
    dispatcher_url: Annotated[str, Field(validation_alias="QUEUE_DISPATCHER_URL")] = (
        "http://queue:9100"
    )
    #: Where the Node dispatcher calls back to run a stage.
    internal_api_base_url: Annotated[str, Field(validation_alias="INTERNAL_API_BASE_URL")] = (
        "http://backend:8000"
    )
    stage_timeout_ms: Annotated[int, Field(validation_alias="QUEUE_STAGE_TIMEOUT_MS", ge=1000)] = (
        1_800_000
    )
    max_attempts: Annotated[int, Field(validation_alias="QUEUE_MAX_ATTEMPTS", ge=1, le=20)] = 3
    backoff_ms: Annotated[int, Field(validation_alias="QUEUE_BACKOFF_MS", ge=100)] = 5000
    dlq_name: Annotated[str, Field(validation_alias="QUEUE_DLQ_NAME")] = "dead-letter"

    #: How many documents one worker runs through a given stage at once.
    #:
    #: The two AI stages are capped far below the others deliberately. Each makes
    #: a dozen or more model calls per document, so running several documents in
    #: parallel does not go faster - it just queues them behind the same provider
    #: gateway, and makes rate limiting more likely.
    #:
    #: These values were the Node dispatcher's built-in defaults
    #: (``queue/src/config.ts``). They live here now because the Postgres driver's
    #: worker is Python, and losing them would have quietly uncapped the AI stages.
    concurrency_validation: Annotated[
        int, Field(validation_alias="WORKER_CONCURRENCY_VALIDATION")
    ] = 10
    concurrency_parser: Annotated[int, Field(validation_alias="WORKER_CONCURRENCY_PARSER")] = 20
    concurrency_docpipeline: Annotated[
        int, Field(validation_alias="WORKER_CONCURRENCY_DOCPIPELINE")
    ] = 4
    concurrency_extraction: Annotated[
        int, Field(validation_alias="WORKER_CONCURRENCY_EXTRACTION")
    ] = 4
    concurrency_embedding: Annotated[
        int, Field(validation_alias="WORKER_CONCURRENCY_EMBEDDING")
    ] = 15
    concurrency_indexing: Annotated[int, Field(validation_alias="WORKER_CONCURRENCY_INDEXING")] = 5
    concurrency_export: Annotated[int, Field(validation_alias="WORKER_CONCURRENCY_EXPORT")] = 4

    #: Retired stages, kept so a deployment that still sets them does not trip
    #: pydantic's ``extra`` handling. Nothing dispatches these - see STAGE_ORDER.
    concurrency_enrichment: Annotated[
        int, Field(validation_alias="WORKER_CONCURRENCY_ENRICHMENT")
    ] = 10
    concurrency_classification: Annotated[
        int, Field(validation_alias="WORKER_CONCURRENCY_CLASSIFICATION")
    ] = 10
    concurrency_chunking: Annotated[int, Field(validation_alias="WORKER_CONCURRENCY_CHUNKING")] = 20
    concurrency_ai_extraction: Annotated[
        int, Field(validation_alias="WORKER_CONCURRENCY_AI_EXTRACTION")
    ] = 10

    def concurrency_for(self, stage: str) -> int:
        """Worker concurrency for a stage, falling back to a conservative default.

        Looked up by name so adding a stage to ``STAGE_ORDER`` does not require a
        branch here - only a field, if the default is wrong for it.
        """
        return int(getattr(self, f"concurrency_{stage}", 4))


class AlertSettings(BaseSettings):
    """Thresholds for the background alert evaluator, and outbound notification.

    Two related concerns share this group. The ``*_days`` / ``*_cutoff`` fields tune
    the §19 evaluator that derives contract alerts from metadata. The ``provider``
    block below governs *delivery* - where an operational alert is sent once raised.

    Delivery is off-by-default in the sense that matters: ``console`` needs no
    credentials and no network, so the application starts and processes documents
    with nothing configured. A deployment opts in to Slack/Teams/webhook/email by
    naming the provider and supplying its endpoint.
    """

    model_config = _group_config()

    expiry_window_days: Annotated[
        int, Field(validation_alias="ALERT_EXPIRY_WINDOW_DAYS", ge=1, le=730)
    ] = 90
    risk_score_cutoff: Annotated[
        int, Field(validation_alias="ALERT_RISK_SCORE_CUTOFF", ge=0, le=100)
    ] = 67
    evaluator_interval_minutes: Annotated[
        int, Field(validation_alias="ALERT_EVALUATOR_INTERVAL_MINUTES", ge=1)
    ] = 60

    # --- delivery ------------------------------------------------------------
    #: Master switch. False disables outbound notification entirely; alerts are
    #: still persisted and still logged, so nothing is lost.
    enabled: Annotated[bool, Field(validation_alias="ALERT_ENABLED")] = True
    #: CSV, so a deployment can fan out to several channels:
    #: ``ALERT_PROVIDER=slack,webhook``. Unknown names fail validation at startup
    #: rather than silently dropping every alert.
    providers: Annotated[list[str], NoDecode, Field(validation_alias="ALERT_PROVIDER")] = [
        "console"
    ]
    #: Alerts below this level are dropped before any provider is called.
    min_level: Annotated[str, Field(validation_alias="ALERT_MIN_LEVEL")] = "ERROR"

    #: Per-attempt network timeout. An alert that cannot be delivered quickly is
    #: worth abandoning: the pipeline is already failing and must not be held up.
    timeout_seconds: Annotated[
        float, Field(validation_alias="ALERT_TIMEOUT_SECONDS", gt=0, le=120)
    ] = 10.0
    max_attempts: Annotated[int, Field(validation_alias="ALERT_RETRY_ATTEMPTS", ge=1, le=10)] = 3
    #: First backoff delay; doubles per attempt up to ``retry_backoff_max_seconds``.
    retry_backoff_seconds: Annotated[
        float, Field(validation_alias="ALERT_RETRY_BACKOFF_SECONDS", ge=0.0, le=60)
    ] = 0.5
    retry_backoff_max_seconds: Annotated[
        float, Field(validation_alias="ALERT_RETRY_BACKOFF_MAX_SECONDS", ge=0.0, le=300)
    ] = 8.0

    slack_webhook_url: Annotated[str, Field(validation_alias="SLACK_WEBHOOK_URL")] = ""
    teams_webhook_url: Annotated[str, Field(validation_alias="TEAMS_WEBHOOK_URL")] = ""
    webhook_url: Annotated[str, Field(validation_alias="ALERT_WEBHOOK_URL")] = ""
    #: Optional bearer token for the generic webhook, sent as ``Authorization``.
    webhook_token: Annotated[str, Field(validation_alias="ALERT_WEBHOOK_TOKEN")] = ""

    smtp_host: Annotated[str, Field(validation_alias="SMTP_HOST")] = ""
    smtp_port: Annotated[int, Field(validation_alias="SMTP_PORT", ge=1, le=65535)] = 587
    smtp_username: Annotated[str, Field(validation_alias="SMTP_USERNAME")] = ""
    smtp_password: Annotated[str, Field(validation_alias="SMTP_PASSWORD")] = ""
    smtp_from: Annotated[str, Field(validation_alias="SMTP_FROM")] = ""
    smtp_to: Annotated[list[str], NoDecode, Field(validation_alias="SMTP_TO")] = []
    smtp_use_tls: Annotated[bool, Field(validation_alias="SMTP_USE_TLS")] = True

    @field_validator("providers", "smtp_to", mode="before")
    @classmethod
    def _parse_csv(cls, value: str | list[str] | None) -> list[str]:
        return _csv_list(value)

    @field_validator("providers")
    @classmethod
    def _known_providers(cls, value: list[str]) -> list[str]:
        # Validated here rather than at dispatch time so a typo is a startup
        # failure, not an alert that silently never arrives.
        known = {"console", "slack", "teams", "webhook", "email", "null"}
        cleaned = [item.strip().lower() for item in value if item.strip()]
        unknown = sorted(set(cleaned) - known)
        if unknown:
            raise ValueError(
                f"ALERT_PROVIDER contains unknown provider(s) {unknown}; "
                f"valid values are {sorted(known)}"
            )
        return cleaned or ["console"]

    @field_validator("min_level")
    @classmethod
    def _upper_min_level(cls, value: str) -> str:
        allowed = {"INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = value.strip().upper()
        if upper not in allowed:
            raise ValueError(f"ALERT_MIN_LEVEL must be one of {sorted(allowed)}")
        return upper


class ObservabilitySettings(BaseSettings):
    """OpenTelemetry + Prometheus."""

    model_config = _group_config()

    otel_enabled: Annotated[bool, Field(validation_alias="OTEL_ENABLED")] = True
    service_name: Annotated[str, Field(validation_alias="OTEL_SERVICE_NAME")] = "cip-backend"
    otlp_endpoint: Annotated[str, Field(validation_alias="OTEL_EXPORTER_OTLP_ENDPOINT")] = (
        "http://otel-collector:4317"
    )
    sampler_ratio: Annotated[
        float, Field(validation_alias="OTEL_TRACES_SAMPLER_ARG", ge=0.0, le=1.0)
    ] = 1.0
    metrics_enabled: Annotated[bool, Field(validation_alias="METRICS_ENABLED")] = True


# =============================================================================
# Root settings
# =============================================================================
class Settings(BaseSettings):
    """Root configuration object. One instance per process."""

    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: Annotated[str, Field(validation_alias="APP_NAME")] = "Contract Intelligence Platform"
    app_env: Annotated[Environment, Field(validation_alias="APP_ENV")] = "development"
    debug: Annotated[bool, Field(validation_alias="DEBUG")] = False
    log_level: Annotated[str, Field(validation_alias="LOG_LEVEL")] = "INFO"
    log_format: Annotated[Literal["json", "console"], Field(validation_alias="LOG_FORMAT")] = "json"
    api_v1_prefix: Annotated[str, Field(validation_alias="API_V1_PREFIX")] = "/api/v1"
    # `NoDecode` is load-bearing, not decoration. pydantic-settings' dotenv source
    # runs `json.loads` on any complex-typed field *before* validators run, so a
    # plain CSV in .env raises a JSONDecodeError and the `mode="before"` splitter
    # below never gets a chance. Without this, `cp .env.example .env` - the first
    # step in the README - stops the app from starting at all.
    cors_origins: Annotated[list[str], NoDecode, Field(validation_alias="CORS_ORIGINS")] = [
        "http://localhost:5173"
    ]

    #: Permit the mock inference/embedding providers in production.
    #:
    #: Off by default, and the production guard refuses to boot without it, because
    #: the mock provider *synthesises* extractions. Shipping it unknowingly means
    #: presenting invented clause attributes and risk scores to a lawyer as though a
    #: model had read the contract - the single worst failure this platform has.
    #:
    #: Turning it on is a legitimate choice for a deployment that runs without any
    #: LLM vendor; it just has to be a stated one rather than an accident, and the
    #: application logs a warning on every start while it is set.
    allow_mock_ai: Annotated[bool, Field(validation_alias="ALLOW_MOCK_AI")] = False

    # Single-organization deployment: this is a label, NOT a security boundary.
    # The Project is the boundary (see reconciliation note §1.1).
    organization_id: Annotated[uuid.UUID, Field(validation_alias="ORGANIZATION_ID")] = uuid.UUID(
        "00000000-0000-0000-0000-000000000001"
    )
    organization_name: Annotated[str, Field(validation_alias="ORGANIZATION_NAME")] = "IRIS RegTech"

    #: Legal names and aliases by which this organisation appears in contracts.
    #: Extraction matches party names against these to decide which side of an
    #: agreement is "us", which is what makes per-side questions answerable -
    #: "can we terminate for convenience?", "do we retain our pre-existing IP?" -
    #: without a company name hardcoded anywhere in the pipeline.
    # Same NoDecode reasoning as `cors_origins` above: this is a CSV in .env, not
    # JSON, and the dotenv source would try to decode it first.
    organization_legal_names: Annotated[
        list[str], NoDecode, Field(validation_alias="ORGANIZATION_LEGAL_NAMES")
    ] = ["IRIS RegTech", "IRIS"]

    # Which stage endpoints this process serves; "all" for a monolithic run.
    worker_role: Annotated[str, Field(validation_alias="WORKER_ROLE")] = "all"

    #: Run the periodic maintenance sweeps inside each worker process.
    #:
    #: On by default, and that default is what removes the `scheduler` container from
    #: a deployment: stalled-job reclamation, export recovery and purge, and alert
    #: evaluation all run in the worker. A Postgres advisory lock means only one
    #: worker performs them per tick however many are running, so scaling a pool does
    #: not multiply the sweeps.
    #:
    #: Turn it off only when a standalone `app.cli scheduler` runs them instead.
    #: Turning it off with nothing else running them loses alert evaluation
    #: altogether - expiry and renewal deadlines are noticed by nothing else.
    worker_maintenance: Annotated[bool, Field(validation_alias="WORKER_MAINTENANCE")] = True

    #: Where benchmark artefacts are written and read from.
    #:
    #: The scheduled evaluation run and the API that serves its results are
    #: different processes and usually different pods, so this needs to point at a
    #: shared volume in any deployment where the dashboard is expected to show
    #: anything.
    evaluation_results_dir: Annotated[
        str, Field(validation_alias="EVALUATION_RESULTS_DIR")
    ] = "evaluation-results"

    review_confidence_threshold: Annotated[
        float, Field(validation_alias="REVIEW_CONFIDENCE_THRESHOLD", ge=0.0, le=1.0)
    ] = 0.85

    # --- nested groups -------------------------------------------------------
    db: DatabaseSettings = Field(default_factory=DatabaseSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    oidc: OIDCSettings = Field(default_factory=OIDCSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    upload: UploadSettings = Field(default_factory=UploadSettings)
    parser: ParserSettings = Field(default_factory=ParserSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    embedding: EmbeddingSettings = Field(default_factory=EmbeddingSettings)
    retrieval: RetrievalSettings = Field(default_factory=RetrievalSettings)
    queue: QueueSettings = Field(default_factory=QueueSettings)
    alerts: AlertSettings = Field(default_factory=AlertSettings)
    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)

    # --- validators ----------------------------------------------------------
    @field_validator("cors_origins", "organization_legal_names", mode="before")
    @classmethod
    def _parse_csv_fields(cls, value: str | list[str] | None) -> list[str]:
        return _csv_list(value)

    @field_validator("log_level")
    @classmethod
    def _upper_log_level(cls, value: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = value.upper()
        if upper not in allowed:
            raise ValueError(f"log_level must be one of {sorted(allowed)}")
        return upper

    @model_validator(mode="after")
    def _guard_production(self) -> Settings:
        """Fail fast on insecure production configuration.

        A misconfigured production deployment must not boot: a default JWT
        secret or a wildcard CORS origin is a security incident, not a warning.
        """
        if self.app_env != "production":
            return self

        problems: list[str] = []
        if "change-me" in self.security.jwt_secret:
            problems.append("JWT_SECRET is still the default value")
        if "change-me" in self.security.internal_api_token:
            problems.append("INTERNAL_API_TOKEN is still the default value")
        if self.security.seed_admin_password == "Abc@1234":  # noqa: S105 - guard, not a secret
            problems.append("SEED_ADMIN_PASSWORD is still the documented default")
        if self.security.new_user_default_password == "Abc@1234":  # noqa: S105 - guard
            problems.append("NEW_USER_DEFAULT_PASSWORD is still the documented default")
        if "*" in self.cors_origins:
            problems.append("CORS_ORIGINS may not contain '*' in production")
        if self.debug:
            problems.append("DEBUG must be false in production")
        # The mock providers invent their output. Allowed in production only when
        # the deployment says so explicitly - see `allow_mock_ai`.
        if not self.allow_mock_ai:
            if self.llm.provider == "mock":
                problems.append(
                    "LLM_PROVIDER=mock invents extractions. Configure a real provider, "
                    "or set ALLOW_MOCK_AI=true to run without one deliberately."
                )
            if self.embedding.provider == "mock":
                problems.append(
                    "EMBEDDING_PROVIDER=mock produces meaningless vectors, so semantic "
                    "search will not work. Configure a real provider, or set "
                    "ALLOW_MOCK_AI=true to run without one deliberately."
                )
        if not self.parser.idoc_verify_tls:
            # Contract text is uploaded to this service. Skipping certificate
            # verification would make that upload interceptable.
            problems.append("IDOC_VERIFY_TLS must be true in production")
        if self.parser.active_parser == "idoc" and not self.parser.idoc_endpoint:
            problems.append("ACTIVE_PARSER=idoc requires IDOC_ENDPOINT to be set")

        if problems:
            raise ValueError(
                "Refusing to start in production with insecure configuration:\n  - "
                + "\n  - ".join(problems)
            )

        # Warned about, not refused.
        #
        # `AUTH_COOKIE_SECURE=false` is a real choice for an internal deployment
        # that genuinely cannot terminate TLS, and refusing to boot would leave an
        # operator with no way to run at all. But it puts a multi-day refresh token
        # on the wire in cleartext, replayable by anyone who can observe it, so it
        # must not be something a deployment drifts into and forgets. Stated on
        # every boot, it stays visible.
        #
        # `warnings` rather than the logger: settings are constructed before
        # logging is configured, so a log call here would be swallowed.
        if not self.security.cookie_secure:
            warnings.warn(
                "AUTH_COOKIE_SECURE=false in production: the refresh cookie is sent "
                "without the Secure flag, so the session token travels in cleartext "
                "over HTTP and can be replayed by anyone who can observe the network. "
                "This is only appropriate where TLS is impossible. Prefer terminating "
                "TLS - a self-signed certificate trusted by the client machines is "
                "enough.",
                stacklevel=2,
            )

        return self

    # --- convenience ---------------------------------------------------------
    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_testing(self) -> bool:
        return self.app_env == "test"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def docs_url(self) -> str | None:
        """Swagger is disabled in production."""
        return None if self.is_production else "/docs"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def redoc_url(self) -> str | None:
        return None if self.is_production else "/redoc"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings singleton - safe to call from anywhere, including DI."""
    return Settings()


settings = get_settings()

__all__ = [
    "AlertSettings",
    "DatabaseSettings",
    "EmbeddingSettings",
    "LLMSettings",
    "OIDCSettings",
    "ObservabilitySettings",
    "ParserSettings",
    "QueueSettings",
    "RedisSettings",
    "RetrievalSettings",
    "SecuritySettings",
    "Settings",
    "StorageSettings",
    "UploadSettings",
    "get_settings",
    "settings",
]