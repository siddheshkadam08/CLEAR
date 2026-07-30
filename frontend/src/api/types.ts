/**
 * API types, mirroring the backend response schemas.
 *
 * Hand-written rather than generated, because the generated shapes for a Python
 * `StrEnum` are unusably wide and the hand-written ones document intent. Where a
 * field's meaning is not obvious from its name it is commented - `is_omission` and
 * `is_missing` in particular, because both mean "this thing is absent" and getting
 * them backwards would show a clean contract as broken.
 */

export type UUID = string;

// =============================================================================
// Shared
// =============================================================================
export interface PageMeta {
  page: number;
  size: number;
  total: number;
  pages: number;
  has_next: boolean;
  /** `has_prev`, not `has_previous` - the backend's name. */
  has_prev: boolean;
}

export interface Paginated<T> {
  items: T[];
  meta: PageMeta;
}

export interface BoundingBox {
  page_number: number;
  x: number;
  y: number;
  width: number;
  height: number;
  page_width?: number | null;
  page_height?: number | null;
}

/** `ProvenanceInfo` on the backend - what produced a value and how sure it is. */
export interface Provenance {
  confidence?: number | null;
  validation_score?: number | null;
  review_status?: string | null;
  model_version?: string | null;
  prompt_version?: string | null;
  profile_version?: string | null;
  artifact_version?: number | null;
  embedding_model?: string | null;
  extraction_engine_version?: string | null;
}

export interface MessageResponse {
  message: string;
}

// =============================================================================
// Auth & identity
// =============================================================================
export interface TokenResponse {
  access_token: string;
  token_type: string;
  expires_in: number;
  expires_at: string;
  /** Omitted when the refresh token is delivered as an HttpOnly cookie. */
  refresh_token?: string | null;
  /** Login returns the user, so signing in costs one round trip, not two. */
  user: CurrentUser;
}

export interface ProjectMembership {
  project_id: UUID;
  project_name: string;
  project_slug: string;
  role: string;
  role_display_name?: string | null;
  permissions: string[];
  is_favourite: boolean;
}

export interface CurrentUser {
  id: UUID;
  email: string;
  full_name: string;
  is_active: boolean;
  is_system_admin: boolean;
  must_change_password: boolean;
  auth_provider?: string | null;
  job_title?: string | null;
  department?: string | null;
  avatar_url?: string | null;
  last_login_at?: string | null;
  /** Which projects this user can see, and with what role in each. */
  memberships: ProjectMembership[];
}

// =============================================================================
// Administration
// =============================================================================
/** `RoleName` on the backend. Assignable to a member of a project. */
export type RoleName = 'system_admin' | 'project_manager' | 'reviewer' | 'viewer';

export interface Role {
  id: UUID;
  name: RoleName;
  display_name: string;
  description?: string | null;
  permissions: string[];
  rank: number;
}

/** A row of `GET /users`. */
export interface UserListItem {
  id: UUID;
  email: string;
  full_name: string;
  is_active: boolean;
  is_system_admin: boolean;
  auth_provider: string;
  job_title?: string | null;
  department?: string | null;
  /** Highest-ranked role held anywhere - `primary_role`, not a list. */
  primary_role?: string | null;
  project_count: number;
  last_login_at?: string | null;
  created_at: string;
}

export interface UserProjectAssignment {
  project_id: UUID;
  role: RoleName;
}

export interface UserCreateRequest {
  email: string;
  full_name: string;
  /**
   * Omitted in favour of `use_default_password`, which makes the server issue the
   * deployment's configured starting credential. Keeping the choice server-side
   * means the password is never carried in a request body the browser composed.
   */
  use_default_password?: boolean;
  job_title?: string | null;
  department?: string | null;
  is_system_admin?: boolean;
  project_assignments?: UserProjectAssignment[];
}

/** A row of `GET /projects/{id}/members`. */
export interface ProjectMember {
  id: UUID;
  project_id: UUID;
  user: { id: UUID; email: string; full_name: string; avatar_url?: string | null };
  role: string;
  role_display_name: string;
  permissions: string[];
  permission_overrides: string[];
  /** When the membership row was written - `created_at`, not `added_at`. */
  created_at: string;
  last_accessed_at?: string | null;
}

export interface ProjectStats {
  contract_count: number;
  ready_contract_count: number;
  processing_count: number;
  failed_count: number;
  needs_review_count: number;
  high_risk_count: number;
  expiring_count: number;
  open_alert_count: number;
  member_count: number;
}

/** Row shape from `GET /projects`. Carries counts the detail response does not. */
export interface ProjectListItem {
  id: UUID;
  name: string;
  slug: string;
  description?: string | null;
  client_name?: string | null;
  status: string;
  contract_count: number;
  ready_contract_count: number;
  member_count: number;
  /** The caller's own role in this project - `my_role`, not `role`. */
  my_role?: string | null;
  is_favourite: boolean;
  last_activity_at?: string | null;
  created_at: string;
}

/**
 * `GET /projects/{id}` and `POST /projects`.
 *
 * A different shape from the list row rather than a superset of it: counts live
 * under `stats` here, and the list row has no `stats` at all.
 */
export interface ProjectDetail {
  id: UUID;
  name: string;
  slug: string;
  description?: string | null;
  client_name?: string | null;
  status: string;
  my_role?: string | null;
  my_permissions: string[];
  stats?: ProjectStats | null;
  settings?: Record<string, unknown> | null;
  business_unit?: string | null;
  department?: string | null;
  default_language?: string | null;
  created_at: string;
  updated_at?: string | null;
  last_activity_at?: string | null;
}

// =============================================================================
// Contracts
// =============================================================================
export type ContractStatus =
  'uploaded' | 'processing' | 'ready' | 'failed' | 'needs_review' | 'archived';

export type RiskBand = 'low' | 'medium' | 'high';

export interface ProcessingState {
  state: string;
  progress: number;
  current_stage?: string | null;
}

export interface ContractListItem {
  id: UUID;
  project_id: UUID;
  title?: string | null;
  /** `original_file_name` on the wire - the name as uploaded. */
  original_file_name: string;
  file_type?: string | null;
  file_size: number;
  page_count?: number | null;
  contract_number?: string | null;
  agreement_type?: string | null;
  status: ContractStatus;
  risk_score?: number | null;
  risk_band?: RiskBand | null;
  effective_date?: string | null;
  expiration_date?: string | null;
  party_a?: string | null;
  party_b?: string | null;
  vendor?: string | null;
  contract_value?: number | null;
  currency?: string | null;
  clause_count?: number | null;
  missing_clause_count?: number | null;
  needs_review: boolean;
  created_at: string;
  processed_at?: string | null;
  uploaded_by?: Record<string, unknown> | null;
  processing?: ProcessingState | null;
}

export interface ProjectRef {
  id: UUID;
  name: string;
  slug: string;
}

/**
 * `GET /contracts/{id}`.
 *
 * Deliberately **not** an extension of the list row: the detail response keeps the
 * commercial terms under `contract_metadata` rather than flattening them, so
 * `contract.expiration_date` does not exist here even though it does in the list.
 */
export interface ContractDetail {
  id: UUID;
  title?: string | null;
  original_file_name: string;
  file_type?: string | null;
  mime_type?: string | null;
  file_size: number;
  sha256_hash?: string | null;
  page_count?: number | null;
  language?: string | null;
  status: ContractStatus;
  contract_number?: string | null;
  agreement_type?: string | null;
  agreement_subtype?: string | null;
  classification_confidence?: number | null;
  /** `current_version`, not `version`. */
  current_version: number;
  profile_id?: UUID | null;
  profile_name?: string | null;
  profile_version?: number | null;
  project?: ProjectRef | null;
  contract_metadata?: ContractMetadata | null;
  counts?: Record<string, number>;
  processing?: ProcessingState | null;
  needs_review: boolean;
  notes?: string | null;
  tags: string[];
  created_at: string;
  updated_at?: string | null;
  processed_at?: string | null;
  uploaded_by?: Record<string, unknown> | null;
}

/** `GET /contracts/{id}/file` - a short-lived URL for the viewer. */
export interface FileAccess {
  contract_id: UUID;
  url: string;
  /** Seconds, not an absolute timestamp. */
  expires_in: number;
  file_name: string;
  file_type?: string | null;
  file_size?: number | null;
  page_count?: number | null;
  is_proxied: boolean;
}

/**
 * `POST /projects/{id}/contracts/upload`.
 *
 * The endpoint takes a batch, so the response reports per-file outcomes: a
 * duplicate or a rejected file is a normal result, not an error, and each needs to
 * be surfaced against the file it belongs to.
 */
export interface UploadedFileResult {
  file_name: string;
  status: string;
  contract_id?: UUID | null;
  job_id?: UUID | null;
  existing_contract_id?: UUID | null;
  error_code?: string | null;
  message?: string | null;
  size?: number | null;
  sha256?: string | null;
}

export interface UploadResult {
  project_id: UUID;
  total: number;
  accepted: number;
  rejected: number;
  duplicates: number;
  files: UploadedFileResult[];
  job_ids: UUID[];
  message?: string | null;
}

export interface ContractMetadata {
  effective_date?: string | null;
  execution_date?: string | null;
  expiration_date?: string | null;
  term_months?: number | null;
  governing_law?: string | null;
  jurisdiction?: string | null;
  currency?: string | null;
  contract_value?: number | null;
  payment_terms_days?: number | null;
  party_a?: string | null;
  party_b?: string | null;
  risk_score?: number | null;
  risk_band?: RiskBand | null;
  auto_renewal?: boolean | null;
  auto_renewal_notice_days?: number | null;
  missing_mandatory_clauses: string[];
  has_unlimited_liability: boolean;
  summary?: string | null;
  key_topics: string[];
  extra?: Record<string, unknown>;
}

// =============================================================================
// Knowledge
// =============================================================================
export interface Clause {
  id: UUID;
  contract_id: UUID;
  clause_type: string;
  title?: string | null;
  text: string;
  summary?: string | null;
  clause_number?: string | null;
  section_title?: string | null;
  /** Validated against the Clause Master's schema, so it is queryable structured data. */
  attributes: Record<string, unknown>;
  is_mandatory: boolean;
  is_risk_flagged: boolean;
  deviation_score?: number | null;
  page_start?: number | null;
  page_end?: number | null;
  bounding_boxes: BoundingBox[];
  chunk_id?: UUID | null;
  provenance?: Provenance | null;
  review_status: string;
  issues: Array<{ code: string; message: string; field?: string; severity: string }>;
}

/**
 * A clause category with its own tab.
 *
 * Entirely driven by `clause_master.ui_config` - the frontend renders what the
 * backend says rather than hardcoding which clauses matter.
 */
export interface ClauseTab {
  key: string;
  label: string;
  priority: number;
  /** Fields to show prominently, in order. */
  primary_fields: string[];
  /** An enumerated field rendered as a dropdown - the liability cap basis. */
  dropdown?: { field: string; options: string[] } | null;
  /** Attribute values that mark a clause for attention, e.g. an uncapped cap. */
  highlight_when: Record<string, unknown>;
  clauses: Clause[];
  /** The category is expected for this document type and was **not** found. */
  is_missing: boolean;
}

export interface Party {
  id: UUID;
  entity_type: string;
  name: string;
  legal_name?: string | null;
  role?: string | null;
  jurisdiction?: string | null;
  is_primary: boolean;
  page_start?: number | null;
  bounding_boxes: BoundingBox[];
}

export interface Obligation {
  id: UUID;
  clause_id?: UUID | null;
  action: string;
  responsible_party?: string | null;
  due_date?: string | null;
  /** The wording when the deadline is relative - that wording *is* the obligation. */
  due_description?: string | null;
  trigger_event?: string | null;
  is_recurring: boolean;
  status: string;
  page_start?: number | null;
  bounding_boxes: BoundingBox[];
}

export type RiskSeverity = 'critical' | 'high' | 'medium' | 'low';

export interface Risk {
  id: UUID;
  clause_id?: UUID | null;
  risk_type: string;
  severity: RiskSeverity;
  description: string;
  recommendation?: string | null;
  score_contribution?: number | null;
  /** The risk is the *absence* of something, so it has no clause text or coordinates. */
  is_omission: boolean;
  page_start?: number | null;
  bounding_boxes: BoundingBox[];
}

export interface KeyDate {
  id: UUID;
  date_type: string;
  date_value?: string | null;
  date_expression?: string | null;
  description?: string | null;
  page_start?: number | null;
  bounding_boxes: BoundingBox[];
}

export interface RiskAssessment {
  score: number;
  band: RiskBand;
  by_severity: Record<string, number>;
  missing_mandatory_clauses: string[];
  has_unlimited_liability: boolean;
  /** Per-finding contributions, so the score decomposes rather than being opaque. */
  breakdown: Array<{
    risk_type: string;
    severity: string;
    clause_type?: string | null;
    weight: number;
    applied: number;
    is_omission: boolean;
    description: string;
  }>;
  risks: Risk[];
}

export interface ContractKnowledge {
  contract_id: UUID;
  clause_count: number;
  tabs: ClauseTab[];
  clauses: Clause[];
  parties: Party[];
  obligations: Obligation[];
  key_dates: KeyDate[];
  assessment: RiskAssessment;
  summary?: string | null;
  key_topics: string[];
  needs_review: boolean;
  review_reasons: string[];
}

export interface EvidenceResolution {
  contract_id: UUID;
  chunk_id?: UUID | null;
  text: string;
  page_start?: number | null;
  page_end?: number | null;
  bounding_boxes: BoundingBox[];
  clause_number?: string | null;
  section_title?: string | null;
  document_url?: string | null;
  expires_at?: string | null;
}

// =============================================================================
// Search & Copilot
// =============================================================================
export interface PlanExplanation {
  intent: string;
  strategy: string;
  scope: string;
  mode: string;
  filters: Record<string, unknown>;
  /** Why the planner interpreted the question this way. Shown when a search is empty. */
  reasoning: string[];
  levels: Array<{ level: string; limit: number; min_similarity: number }>;
}

export interface SearchHit {
  level: string;
  ref_id: UUID;
  contract_id: UUID;
  contract_title?: string | null;
  text: string;
  score: number;
  /** vector | keyword | fused | neighbour | graph */
  source: string;
  rank: number;
  clause_type?: string | null;
  clause_number?: string | null;
  section_title?: string | null;
  page_start?: number | null;
  bounding_boxes: BoundingBox[];
  chunk_id?: UUID | null;
}

export interface ContractMatch {
  contract_id: UUID;
  title?: string | null;
  agreement_type?: string | null;
  risk_score?: number | null;
  risk_band?: string | null;
  expiration_date?: string | null;
  party_a?: string | null;
  party_b?: string | null;
  has_unlimited_liability?: boolean | null;
  missing_mandatory_clauses: string[];
}

export interface SearchResponse {
  query: string;
  hits: SearchHit[];
  contracts: ContractMatch[];
  total_hits: number;
  plan: PlanExplanation;
  duration_ms: number;
  warnings: string[];
}

export interface Citation {
  label: number;
  contract_id: UUID;
  contract_title?: string | null;
  level: string;
  ref_id: UUID;
  text: string;
  clause_type?: string | null;
  clause_number?: string | null;
  section_title?: string | null;
  page_start?: number | null;
  page_end?: number | null;
  page_range: string;
  bounding_boxes: BoundingBox[];
  chunk_id?: UUID | null;
  score: number;
}

export type ConfidenceBand = 'high' | 'medium' | 'low';

export interface AnswerResponse {
  answer: string;
  citations: Citation[];
  confidence: number;
  confidence_band: ConfidenceBand;
  response_format: string;
  /** The model declined. A first-class outcome, not an error. */
  refused: boolean;
  /** A fabricated citation, no citations, or low grounding - check before relying. */
  needs_review: boolean;
  warnings: string[];
  plan?: PlanExplanation | null;
  session_id?: UUID | null;
  message_id?: UUID | null;
  model?: string | null;
  duration_ms: number;
  tokens: number;
  cost_usd: number;
}

export interface ChatSession {
  id: UUID;
  project_id?: UUID | null;
  contract_id?: UUID | null;
  title?: string | null;
  created_at: string;
  updated_at?: string | null;
  message_count: number;
  messages: ChatMessage[];
}

export interface ChatMessage {
  id: UUID;
  role: string;
  content: string;
  created_at: string;
  citations: Citation[];
  confidence?: number | null;
  needs_review: boolean;
}

// =============================================================================
// Processing
// =============================================================================
export type JobState =
  | 'queued'
  | 'validating'
  | 'parsing'
  | 'enriching'
  | 'classifying'
  | 'chunking'
  | 'ai_extraction'
  | 'embedding'
  | 'indexing'
  | 'ready'
  | 'failed'
  | 'retrying'
  | 'cancelled'
  | 'paused';

export interface StageRun {
  id: UUID;
  stage: string;
  status: string;
  attempt: number;
  started_at?: string | null;
  finished_at?: string | null;
  duration_ms?: number | null;
  /** The stage reused a valid checkpoint instead of re-running. */
  reused_checkpoint: boolean;
  error?: Record<string, unknown> | null;
  stats: Record<string, unknown>;
  warnings: string[];
}

/**
 * Row shape from `GET /jobs`.
 *
 * Carries no stage runs and no retryability: those are on the detail response, and
 * fetching them for every row of a 50-row list would mean fifty joins to render a
 * table nobody has expanded yet.
 */
export interface JobListItem {
  id: UUID;
  contract_id: UUID;
  contract_title?: string | null;
  project_id: UUID;
  state: JobState;
  current_stage?: string | null;
  progress: number;
  retry_count: number;
  priority?: string | null;
  /** Flattened to a string here; the detail response has the structured error. */
  error_message?: string | null;
  created_at: string;
  finished_at?: string | null;
}

/** `GET /jobs/{id}`, `GET /contracts/{id}/jobs`, and the retry/reprocess results. */
export interface Job {
  id: UUID;
  contract_id: UUID;
  project_id: UUID;
  state: JobState;
  current_stage?: string | null;
  progress: number;
  retry_count: number;
  max_retries: number;
  priority?: string | null;
  error?: Record<string, unknown> | null;
  created_at: string;
  started_at?: string | null;
  finished_at?: string | null;
  heartbeat_at?: string | null;
  duration_ms?: number | null;
  profile_id?: UUID | null;
  profile_version?: number | null;
  /** Whether a retry could plausibly help - drives whether the button is offered. */
  is_retryable: boolean;
  stages: StageRun[];
}

export interface PipelineHealth {
  registered_stages: string[];
  unavailable_stages: string[];
  import_errors: Record<string, string>;
  queues: Array<{
    queue: string;
    waiting: number;
    active: number;
    completed: number;
    failed: number;
    delayed: number;
  }>;
  dead_letter_count: number;
  in_flight: number;
  stalled: number;
}

// =============================================================================
// Admin
// =============================================================================
export interface KpiTile {
  key: string;
  label: string;
  value: number;
  delta?: number | null;
  unit?: string | null;
  /** The filter this tile links to, so the number is clickable. */
  drilldown?: Record<string, unknown> | null;
}

export interface DistributionBucket {
  label: string;
  value: number;
  percentage: number;
}

export interface Dashboard {
  scope: string;
  project_ids: UUID[];
  kpis: KpiTile[];
  risk_distribution: DistributionBucket[];
  agreement_type_distribution: DistributionBucket[];
  status_distribution: DistributionBucket[];
  expiring_soon: Array<{
    contract_id: UUID;
    title?: string | null;
    expiration_date?: string | null;
    days_remaining?: number | null;
    risk_band?: string | null;
    auto_renewal?: boolean | null;
  }>;
  top_risks: Array<{
    contract_id: UUID;
    title?: string | null;
    risk_score?: number | null;
    risk_band?: string | null;
  }>;
  uploads_over_time: Array<{ period: string; value: number }>;
  generated_at: string;
}

export interface ClauseCategory {
  id: UUID;
  key: string;
  name: string;
  description?: string | null;
  group_name?: string | null;
  priority: number;
  mandatory: boolean;
  confidence_threshold: number;
  is_active: boolean;
  is_system: boolean;
  ui_config: Record<string, unknown>;
  current_rule?: {
    id: UUID;
    version: number;
    extraction_rule: Record<string, unknown>;
    output_schema: Record<string, unknown>;
    synonyms: string[];
    change_note?: string | null;
    created_at: string;
  } | null;
  created_at: string;
}

// =============================================================================
// Exports
// =============================================================================
export type ExportStatus = 'queued' | 'running' | 'completed' | 'failed' | 'expired';
export type ExportFormat = 'xlsx' | 'csv' | 'json' | 'pdf';
export type ExportEntity =
  'contracts' | 'clauses' | 'obligations' | 'risks' | 'key_dates' | 'entities';

export interface ExportJob {
  id: UUID;
  project_id?: UUID | null;
  requested_by: UUID;
  scope: string;
  scope_ref?: UUID | null;
  export_format: ExportFormat;
  status: ExportStatus;
  progress: number;
  entities: string[];
  filters: Record<string, unknown>;
  file_name?: string | null;
  file_size?: number | null;
  row_count?: number | null;
  /** SHA-256 of the stored bytes. */
  checksum?: string | null;
  error?: Record<string, unknown> | null;
  created_at: string;
  started_at?: string | null;
  finished_at?: string | null;
  /** After this the file is purged; the job row remains as the audit record. */
  expires_at?: string | null;
  download_count: number;
}

export interface ExportDownload {
  export_id: UUID;
  url: string;
  file_name?: string | null;
  file_size?: number | null;
  /** Seconds. The file outlives the URL — request another when it lapses. */
  expires_in: number;
}

/** What this deployment can actually produce, so the UI never offers a dead option. */
export interface ExportCapabilities {
  formats: ExportFormat[];
  entities: ExportEntity[];
  default_entities: ExportEntity[];
  retention_hours: number;
}

export type AlertStatus = 'open' | 'acknowledged' | 'resolved' | 'dismissed' | 'escalated';

export interface Alert {
  id: UUID;
  project_id: UUID;
  contract_id?: UUID | null;
  contract_title?: string | null;
  alert_type: string;
  severity: string;
  status: AlertStatus;
  title: string;
  message: string;
  due_date?: string | null;
  created_at: string;
  note?: string | null;
}
