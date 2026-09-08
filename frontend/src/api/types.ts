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

/**
 * Which sign-in methods a deployment offers.
 *
 * Served unauthenticated so the login screen can render itself correctly before
 * anyone has a session. It deliberately carries no secrets — the client id and
 * tenant never reach the browser, because the whole authorization URL is built
 * server-side.
 */
export interface AuthMethods {
  password_enabled: boolean;
  microsoft_sso_enabled: boolean;
  microsoft_button_label: string;
  self_signup_enabled: boolean;
  contact_admin_message: string;
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
  /** The type being *processed* - always `pdf`. See `original_file_type`. */
  file_type?: string | null;
  /**
   * What the user uploaded: `pdf`, `doc` or `docx`.
   *
   * Differs from `file_type` for a Word upload, which is converted at upload so
   * the pipeline and the evidence viewer only ever see a PDF.
   */
  original_file_type?: string | null;
  /** True when a Word original was converted, so both files can be offered. */
  has_converted_pdf?: boolean;
  /** The archive this document was extracted from, if any. */
  source_archive_id?: UUID | null;
  source_archive_name?: string | null;
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

/**
 * The `extra` blob on contract metadata.
 *
 * These are answers the extraction stage derives from clause attributes and
 * stores where the UI can read them without re-deriving. Typed rather than left
 * as `unknown` because the Meta info tab reads every one of them, and a silent
 * rename on the backend should fail the build rather than blank a field.
 *
 * Every key is optional: it is a JSONB column written by a pipeline that skips
 * nulls, so an older contract will simply not have some of them.
 */
export interface ContractMetadataExtra {
  liability_cap_basis?: string | null;
  liability_cap_multiple?: number | null;
  liability_carve_outs?: string[] | null;
  can_we_terminate?: string | null;
  we_retain_pre_existing_ip?: string | null;
  dispute_resolution?: string | null;
  review_reasons?: string[] | null;
  [key: string]: unknown;
}

export interface ContractMetadata {
  effective_date?: string | null;
  execution_date?: string | null;
  expiration_date?: string | null;
  renewal_date?: string | null;
  notice_deadline?: string | null;
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
  renewal_term_months?: number | null;
  missing_mandatory_clauses: string[];
  has_unlimited_liability: boolean;
  // Liability, privacy and termination flags the pipeline projects from the
  // matching clauses. `has_*` is false both when the clause was absent and when
  // it was found without the feature, so pair them with the clause tab.
  has_liability_cap?: boolean | null;
  liability_cap_amount?: number | null;
  has_data_protection_clause?: boolean | null;
  has_termination_for_convenience?: boolean | null;
  termination_notice_days?: number | null;
  summary?: string | null;
  key_topics: string[];
  extra?: ContractMetadataExtra;
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

/** One row of the clause-by-clause summary table. */
export interface SummaryRow {
  /** The clause tab this row came from. Absent on the Parties & Background row. */
  clause_key?: string | null;
  heading: string;
  lines: string[];
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
  /**
   * The summary table. Holds only clauses the document actually contains, so an
   * absent clause has no row rather than an empty one.
   *
   * Empty for contracts extracted before the digest existed — the screen falls
   * back to the `summary` prose, so both must stay rendered.
   */
  summary_rows: SummaryRow[];
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

/**
 * Mirrors the backend `ResponseFormat` enum.
 *
 * This was typed as a bare `string`, so the Copilot's format picker could offer
 * `bullet_points` and `table` - neither of which the enum contains - and nothing
 * complained until the request came back 422. Two of its three options were
 * unusable in production while typechecking cleanly.
 */
export type ResponseFormat =
  | 'natural_language'
  | 'json'
  | 'executive_summary'
  | 'risk_report'
  | 'compliance_report'
  | 'clause_comparison'
  | 'timeline'
  | 'action_items'
  | 'contract_summary'
  | 'obligation_report';

export interface AnswerResponse {
  answer: string;
  citations: Citation[];
  confidence: number;
  confidence_band: ConfidenceBand;
  response_format: ResponseFormat;
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

/**
 * Copilot query API.
 *
 * camelCase, unlike everything else in this file: `/copilot/query` has an agreed
 * external field naming that the backend serialises by alias. It is not an
 * inconsistency to tidy up - renaming it here would just stop it matching the
 * wire.
 */
export interface CopilotSource {
  contractId: UUID;
  contractName?: string | null;
  clauseHeading?: string | null;
  sectionNumber?: string | null;
  pageNumber?: number | null;
  /**
   * Cosine similarity in [0, 1] - not the hybrid fusion score.
   *
   * Null for a keyword-only match, which has no similarity to report. Do not
   * render it as 0: that reads as "irrelevant" beside what may be the best
   * exact-phrase match in the corpus.
   */
  similarityScore?: number | null;
  /** The re-ranker's relevance judgement, when one ran. */
  rerankScore?: number | null;
  /** semantic | keyword | hybrid | context */
  matchType: string;
  text: string;
  /** The `[n]` marker this passage carries in the answer text. */
  label: number;
}

export interface CopilotQueryMetadata {
  /** True when a known document type actually narrowed the search. */
  documentTypeDetected: boolean;
  documentType?: string | null;
  documentTypeConfidence: number;
  /** DocumentTypeFiltered | ContractScoped | Unfiltered */
  retrievalMode: string;
  retrievedChunks: number;
  topSimilarity: number;
  /** True when nothing retrieved cleared the threshold, so no model was asked. */
  insufficientContext: boolean;
  /** True when retrieval worked but the model could not be reached. */
  generationFailed: boolean;
  /** True when a document-type filter matched nothing and the search was widened. */
  relaxedFilters: boolean;
  /** True when more contracts matched than the pre-filter carries. */
  scopeTruncated: boolean;
  confidence: number;
  confidenceBand: ConfidenceBand;
  needsReview: boolean;
  refused: boolean;
  warnings: string[];
  model?: string | null;
  tokens: number;
  costUsd: number;
  timings: Record<string, number>;
}

export interface CopilotQueryResponse {
  answer: string;
  sources: CopilotSource[];
  metadata: CopilotQueryMetadata;
  session_id?: UUID | null;
  message_id?: UUID | null;
}

/** The single `done` event that closes a `/copilot/stream` response. */
export interface CopilotStreamDone {
  citations?: Citation[];
  confidence?: number;
  confidence_band?: ConfidenceBand;
  needs_review?: boolean;
  warnings?: string[];
  /** Sent only when a fabricated citation had to be stripped - re-render, do not append. */
  text?: string | null;
  sources?: CopilotSource[];
  metadata?: CopilotQueryMetadata;
}

/**
 * Retrieval evaluation.
 *
 * Shapes read straight off the benchmark's `summary.json` and `evaluation.json`,
 * so the metric names match the ones in the scorecard rather than being renamed
 * for the UI — a dashboard that renamed them would make a CI failure and a
 * dashboard reading disagree about what regressed.
 */
export interface EvaluationRun {
  label: string;
  recorded_at: string;
  dataset: string;
  passed: boolean;
  cases: number;
  metrics: Record<string, number>;
}

export interface EvaluationCaseRef {
  id: string;
  question: string;
}

export interface EvaluationLatest {
  summary: {
    dataset: string;
    label: string;
    cases: number;
    failures: number;
    passed: boolean;
    summary: string;
    composite: number;
    metrics: Record<string, number>;
    blocking_failures: string[];
  };
  guardrail?: {
    true_accept: number;
    true_reject: number;
    false_accept: number;
    false_reject: number;
    accuracy: number;
    document_summary_would_have_passed: number;
    hallucinations_prevented: number;
  } | null;
  calibration?: {
    raw: { samples: number; ece: number; mce: number; brier: number; mean_bias: number };
    recommendation: string;
  } | null;
  by_tag?: Record<string, Record<string, number>> | null;
  failing: {
    zero_recall: EvaluationCaseRef[];
    false_accept: EvaluationCaseRef[];
    false_reject: EvaluationCaseRef[];
    worst_cited: EvaluationCaseRef[];
    false_filtering: EvaluationCaseRef[];
    most_expensive: EvaluationCaseRef[];
    slowest: EvaluationCaseRef[];
  };
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
/**
 * Upper case, unlike every other status union here.
 *
 * `JobState` is the one backend enum whose *values* are upper case - the others
 * (`ContractStatus`, `AlertStatus`, `ExportStatus`) are lower case, and this
 * type was written to match them rather than to match its own enum. The API
 * sends `"READY"`; the type promised `"ready"`. Nothing complained, because a
 * string literal union only constrains what the frontend writes, never what the
 * server actually sends - so every comparison against a lower case literal
 * silently failed while typechecking perfectly.
 */
export type JobState =
  | 'QUEUED'
  | 'VALIDATING'
  | 'PARSING'
  | 'ENRICHING'
  | 'CLASSIFYING'
  | 'CHUNKING'
  | 'AI_EXTRACTION'
  | 'EMBEDDING'
  | 'INDEXING'
  | 'READY'
  | 'FAILED'
  | 'RETRYING'
  | 'CANCELLED'
  | 'PAUSED';

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
  /**
   * The archive this document came out of, when it did.
   *
   * Grouping only. Every document extracted from a ZIP is an independent job with
   * its own state, progress, retry and logs - these fields let the list show that
   * fifty rows were one upload, not that they are one job.
   */
  source_archive_id?: UUID | null;
  source_archive_name?: string | null;
  /** What was uploaded. The job always processes a PDF; this may say `docx`. */
  original_file_type?: string | null;
  original_file_name?: string | null;
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

/**
 * One clause, as it applies to one agreement type.
 *
 * Flattens two things the screen shows as one row: what the clause *is* comes
 * from the Clause Master, whether it applies here comes from the mapping.
 */
export interface AgreementClause {
  clause_key: string;
  name: string;
  description?: string | null;
  group_name?: string | null;
  /** Alternative headings. The strongest signal the clause detector has. */
  synonyms: string[];
  /** Applied to **new uploads only** — already-extracted contracts are untouched. */
  is_active: boolean;
  is_mandatory: boolean;
  display_order: number;
  /** False when the clause exists but is not attached to this agreement type. */
  is_mapped: boolean;
}

export interface AgreementTypeClauses {
  agreement_type: string;
  /** The profile's display name where one exists, else the humanised type. */
  label: string;
  clauses: AgreementClause[];
}

export interface AgreementClauseUpsert {
  clause_key: string;
  is_active: boolean;
  is_mandatory: boolean;
  display_order?: number | null;
}

export interface ClauseDefinitionUpsert {
  /** Required on create; the key is the identity and cannot change. */
  key?: string | null;
  name: string;
  description?: string | null;
  group_name?: string | null;
  synonyms: string[];
}

/** One row of the import/export sheet. The two use identical columns. */
export interface ClauseImportRow {
  agreement_type: string;
  clause_key: string;
  name?: string | null;
  description?: string | null;
  group_name?: string | null;
  synonyms: string[];
  is_active: boolean;
  is_mandatory: boolean;
  display_order?: number | null;
}

export interface ClauseImportResult {
  clauses_created: number;
  clauses_updated: number;
  mappings_created: number;
  mappings_updated: number;
  mappings_deactivated: number;
  /** Rows that could not be applied, with the reason. Never fatal. */
  skipped: { row: string; clause_key: string; reason: string }[];
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

// =============================================================================
// Knowledge graph
// =============================================================================
export interface GraphNode {
  node_type: string;
  /** Stable within the contract. This is what edges point at, not `row_id`. */
  ref: string;
  label: string;
  row_id?: UUID | null;
  attributes: Record<string, unknown>;
}

export interface GraphEdge {
  relation: string;
  source_type: string;
  source_ref: string;
  target_type: string;
  target_ref: string;
  label?: string | null;
  source_id?: UUID | null;
  target_id?: UUID | null;
  attributes: Record<string, unknown>;
  /** False when an endpoint could not be matched to a node. Drawn anyway. */
  is_resolved: boolean;
  /** `derived` - structural - or `extracted`, which is a claim the document made. */
  origin: string;
}

export interface DanglingReference {
  /** The reference as the document wrote it — "Section 9.2", "the Supplier". */
  reference: string;
  relation: string;
  reason: string;
}

export interface ContractGraph {
  contract_id: UUID;
  contract_title?: string | null;
  nodes: GraphNode[];
  edges: GraphEdge[];
  /** References the text made that nothing in the contract satisfies. */
  dangling: DanglingReference[];
  statistics: {
    nodes?: number;
    edges?: number;
    resolved_edges?: number;
    unresolved_edges?: number;
    dangling_references?: number;
    by_node_type?: Record<string, number>;
    by_relation?: Record<string, number>;
  };
  warnings: string[];
}

// =============================================================================
// Portfolio - cross-contract registers
// =============================================================================
/**
 * `ResponseSchema` serialises dates and datetimes as epoch **seconds**, not ISO
 * strings. `formatDate` and `daysUntil` both accept either; anything else that
 * touches these must too.
 */
export type ApiDate = number | string;

/** Where a portfolio row came from. Every register row carries this. */
export interface PortfolioItem {
  contract_id: UUID;
  project_id: UUID;
  contract_title?: string | null;
  contract_number?: string | null;
}

export type ObligationStatus =
  | 'open'
  | 'in_progress'
  | 'fulfilled'
  | 'breached'
  | 'waived'
  | 'unknown';

// `RiskSeverity` is already declared above, with the per-contract knowledge types.

export type DateType =
  | 'effective_date'
  | 'execution_date'
  | 'expiration_date'
  | 'renewal_date'
  | 'notice_deadline'
  | 'milestone'
  | 'payment_due'
  | 'delivery_date'
  | 'review_date'
  | 'termination_date'
  | 'commencement_date'
  | 'other';

export interface PortfolioObligation extends PortfolioItem {
  id: UUID;
  action: string;
  responsible_party?: string | null;
  due_date?: ApiDate | null;
  /** The contract's own relative wording, kept when no calendar date resolved. */
  due_description?: string | null;
  trigger_event?: string | null;
  frequency?: string | null;
  is_recurring: boolean;
  status: ObligationStatus;
  penalty?: string | null;
  clause_id?: UUID | null;
}

export interface PortfolioKeyDate extends PortfolioItem {
  id: UUID;
  date_type: DateType;
  date_value?: ApiDate | null;
  /** Preserved verbatim where extraction could not resolve a calendar date. */
  date_expression?: string | null;
  description?: string | null;
  is_recurring: boolean;
}

export interface PortfolioRisk extends PortfolioItem {
  id: UUID;
  risk_type: string;
  severity: RiskSeverity;
  description: string;
  recommendation?: string | null;
  category?: string | null;
  score_contribution?: number | null;
  /** True when the finding is the *absence* of something - no clause to point at. */
  is_omission: boolean;
  clause_id?: UUID | null;
  contract_risk_score?: number | null;
}

/**
 * One counterparty, aggregated across contracts.
 *
 * Grouped by exact lower-cased name, not entity-resolved: "Acme Corp" and "Acme
 * Corporation Inc." are two rows. `total_value` is keyed by currency and never
 * summed across them.
 */
export interface PartyDirectoryEntry {
  key: string;
  name: string;
  entity_types: string[];
  roles: string[];
  jurisdictions: string[];
  contract_count: number;
  is_primary_anywhere: boolean;
  total_value: Record<string, number>;
  next_expiry?: ApiDate | null;
  sample_contract_id?: UUID | null;
}

/**
 * Mirrors `AlertStatus` on the backend, exactly.
 *
 * There is no `escalated` member and there must not be: escalation raises an
 * alert's *severity*, it does not move it to another status. This list used to
 * carry one, and because the Alerts screen sent it as a default filter, every
 * page load answered 422 - `list[AlertStatus]` rejects the value before the
 * handler runs. A status the server does not know is not a harmless extra.
 */
export type AlertStatus = 'open' | 'acknowledged' | 'resolved' | 'dismissed';

export type AlertSeverity = 'critical' | 'high' | 'medium' | 'low' | 'info';

export type AlertType =
  | 'contract_expiring'
  | 'high_risk'
  | 'missing_mandatory_clause'
  | 'processing_failed'
  | 'auto_renewal_notice'
  | 'obligation_due'
  | 'review_required';

export interface Alert {
  id: UUID;
  project_id: UUID;
  contract_id?: UUID | null;
  contract_title?: string | null;
  alert_type: AlertType | string;
  severity: AlertSeverity | string;
  status: AlertStatus;
  title: string;
  message: string;
  /** Days remaining, missing clause names, risk score - whatever raised it. */
  details?: Record<string, unknown> | null;
  due_date?: string | null;
  created_at: string;
  note?: string | null;
}

/**
 * A configurable threshold for one alert type.
 *
 * `project_id: null` is the platform default, which applies everywhere. A row
 * with a project overrides it for that project alone, so one business unit can
 * watch a 180-day expiry window while the rest use 90.
 */
export interface AlertRule {
  id: UUID;
  project_id?: UUID | null;
  name: string;
  alert_type: AlertType;
  is_enabled: boolean;
  severity: AlertSeverity;
  /** Type-specific thresholds. See `RULE_FIELDS` on the Alerts screen. */
  config: Record<string, unknown>;
  escalate_after_days?: number | null;
  notify_channels: string[];
  created_at: string;
  updated_at?: string | null;
}

export interface AlertRuleInput {
  name: string;
  alert_type: AlertType;
  severity: AlertSeverity;
  is_enabled: boolean;
  config: Record<string, unknown>;
  escalate_after_days?: number | null;
  notify_channels: string[];
  project_id?: UUID | null;
}


// =============================================================================
// Audit trail
// =============================================================================
export interface AuditEntry {
  id: UUID;
  /** Epoch seconds, as `ResponseSchema` serialises datetimes. */
  created_at: number | string;
  action: string;
  entity_type: string;
  entity_id?: UUID | null;
  entity_label?: string | null;
  project_id?: UUID | null;
  user_id?: UUID | null;
  /** Denormalised on write, so a deleted user's actions stay attributable. */
  user_email?: string | null;
  succeeded: boolean;
  error_code?: string | null;
  ip?: string | null;
  route?: string | null;
  /** Correlates the row with the request that produced it, and with the logs. */
  request_id?: string | null;
  trace_id?: string | null;
  /** Already redacted on write - secrets never reach the row. */
  before?: Record<string, unknown> | null;
  after?: Record<string, unknown> | null;
}

export interface AuditFilters {
  page?: number;
  size?: number;
  project_id?: UUID;
  user_id?: UUID;
  action?: string;
  entity_type?: string;
  succeeded?: boolean;
}


// =============================================================================
// Clause coverage  (route: /docpipeline)
// =============================================================================
// Read from `contracts`, `clauses`, `embeddings` and `document_profiles`.
// `coverage` is clauses found divided by the number that document type's profile
// expects, and is null when the type has no profile entry.
export interface DocPipelineTotals {
  documents: number;
  clauses: number;
  embedded: number;
  clause_page_regions: number;
  clauses_per_document: number;
  average_coverage: number | null;
}

export interface DocPipelineDocumentRow {
  /** Contract id. A UUID string - it was an integer under the old tables. */
  docid: string;
  doc_type: string | null;
  doc_path: string | null;
  json_path: string | null;
  clauses_found: number;
  clauses_expected: number;
  coverage: number | null;
}

export interface DocPipelineInsights {
  totals: DocPipelineTotals;
  documents: DocPipelineDocumentRow[];
  by_doc_type: { doc_type: string | null; documents: number; clauses_expected: number }[];
  clause_frequency: { clause: string; documents: number }[];
  missing_clauses: {
    doc_type: string;
    clause: string;
    description: string;
    expected_in: number;
  }[];
  expected_by_doc_type: Record<string, number>;
}

export interface DocPipelineClause {
  id: number;
  clause: string;
  page_numbers: number[];
  /** One box per page: 8 floats per entry in `page_numbers` order. */
  polygon: number[];
  json_file: string | null;
  text: string;
  chars: number;
}

export interface DocPipelineDocument {
  docid: string;
  doc_type: string | null;
  doc_path: string | null;
  json_path: string | null;
  clauses: DocPipelineClause[];
}
