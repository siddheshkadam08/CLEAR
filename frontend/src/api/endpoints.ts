/**
 * Typed endpoint functions, grouped by domain.
 *
 * One place that knows every URL, so a route change is a single edit rather than a
 * search through components. Every function returns a typed promise; React Query
 * handles caching and refetching on top.
 */

import { api, apiStream, apiUpload } from './client';
import type {
  DocPipelineInsights,
  Alert,
  AgreementClause,
  AgreementClauseUpsert,
  AgreementTypeClauses,
  AuditEntry,
  AuditFilters,
  AlertRule,
  AlertRuleInput,
  AlertStatus,
  AnswerResponse,
  AuthMethods,
  ChatSession,
  ClauseCategory,
  ClauseDefinitionUpsert,
  ClauseImportResult,
  ClauseImportRow,
  Clause,
  ContractDetail,
  ContractGraph,
  ContractKnowledge,
  ContractListItem,
  CopilotQueryResponse,
  CurrentUser,
  EvaluationLatest,
  EvaluationRun,
  Dashboard,
  EvidenceResolution,
  ExportCapabilities,
  ExportDownload,
  ExportEntity,
  ExportFormat,
  ExportJob,
  ExportStatus,
  DateType,
  ObligationStatus,
  PartyDirectoryEntry,
  PortfolioKeyDate,
  PortfolioObligation,
  PortfolioRisk,
  RiskSeverity,
  FileAccess,
  Job,
  JobListItem,
  MessageResponse,
  Paginated,
  PipelineHealth,
  ProjectDetail,
  ProjectListItem,
  ProjectMember,
  ResponseFormat,
  RiskAssessment,
  Role,
  RoleName,
  SearchResponse,
  TokenResponse,
  UploadResult,
  UserCreateRequest,
  UserListItem,
  UUID,
} from './types';

// =============================================================================
// Auth
// =============================================================================
export const auth = {
  /**
   * Which sign-in methods this deployment offers.
   *
   * Unauthenticated by design — the login screen calls it before anyone has a
   * session, which is what lets enabling SSO be purely a server-side config
   * change rather than a frontend rebuild.
   */
  methods: () => api.get<AuthMethods>('/auth/methods', { skipRefresh: true }),

  login: (email: string, password: string) =>
    api.post<TokenResponse>('/auth/login', { email, password }, { skipRefresh: true }),

  logout: () => api.post<MessageResponse>('/auth/logout'),

  // `/auth/me`, not `/users/me`: `/users/{user_id}` would swallow the latter and
  // fail the UUID parse with a 422.
  me: () => api.get<CurrentUser>('/auth/me'),

  // `confirm_password` is required by the endpoint - the server does the match
  // check rather than trusting the client to have done it.
  changePassword: (current_password: string, new_password: string, confirm_password: string) =>
    api.post<MessageResponse>('/auth/change-password', {
      current_password,
      new_password,
      confirm_password,
    }),

};

// =============================================================================
// Projects
// =============================================================================
export const projects = {
  list: (params: { page?: number; size?: number; search?: string } = {}) =>
    api.get<Paginated<ProjectListItem>>('/projects', {
      query: params as Record<string, string | number | undefined>,
    }),
  get: (id: UUID) => api.get<ProjectDetail>(`/projects/${id}`),
  create: (body: {
    name: string;
    description?: string;
    client_name?: string;
    department?: string;
  }) => api.post<ProjectDetail>('/projects', body),

  members: (id: UUID) => api.get<ProjectMember[]>(`/projects/${id}/members`),
  addMember: (id: UUID, body: { user_id: UUID; role: RoleName }) =>
    api.post<ProjectMember>(`/projects/${id}/members`, body),
  removeMember: (id: UUID, userId: UUID) =>
    api.delete<MessageResponse>(`/projects/${id}/members/${userId}`),
  updateMember: (id: UUID, userId: UUID, body: { role: RoleName }) =>
    api.patch<ProjectMember>(`/projects/${id}/members/${userId}`, body),
};

// =============================================================================
// Administration
// =============================================================================
export const admin = {
  users: (
    params: { page?: number; size?: number; search?: string; is_active?: boolean } = {},
  ) =>
    api.get<Paginated<UserListItem>>('/users', {
      query: params as Record<string, string | number | boolean | undefined>,
    }),

  /**
   * Provision an account.
   *
   * `use_default_password` rather than a password field: the starting credential is
   * the deployment's configured one, so it is never typed into a browser, never
   * travels in a request body, and is identical for every account the admin creates.
   * The user is forced to change it at first sign-in.
   */
  createUser: (body: UserCreateRequest) =>
    api.post<UserListItem>('/users', { ...body, use_default_password: true }),

  updateUser: (id: UUID, body: { is_active?: boolean; full_name?: string }) =>
    api.patch<UserListItem>(`/users/${id}`, body),

  deleteUser: (id: UUID) => api.delete<MessageResponse>(`/users/${id}`),

  roles: () => api.get<Role[]>('/roles'),
};

// =============================================================================
// Contracts
// =============================================================================
export interface ContractFilters {
  page?: number;
  size?: number;
  /** Free text over title, party and summary.
   *
   * Named `search` because that is what the endpoint calls it. It was `q`, which
   * FastAPI simply ignores as an unknown query parameter - so the Contracts
   * search box issued a request, got the full unfiltered list back, and rendered
   * it without any sign that the term had been dropped. */
  search?: string;
  status?: string[];
  agreement_type?: string[];
  risk_band?: string[];
  needs_review?: boolean;
  /** Expiry window, inclusive. Named as the endpoint declares them.
   *
   * These were `expiring_after`/`expiring_before` - the same class of silent drop
   * as `q` above, and it made the dashboard's "Expiring in 90 days" tile open a
   * completely unfiltered list that still claimed one filter was active. */
  expiry_from?: string;
  expiry_to?: string;
  has_unlimited_liability?: boolean;
  /** Contracts missing any mandatory clause, whichever one. */
  missing_mandatory?: boolean;
  sort_by?: string;
  sort_dir?: string;
}

export const contracts = {
  list: (projectId: UUID | null, filters: ContractFilters = {}) =>
    api.get<Paginated<ContractListItem>>(
      projectId ? `/projects/${projectId}/contracts` : '/contracts',
      { query: filters as Record<string, string | number | boolean | string[] | undefined> },
    ),

  get: (id: UUID) => api.get<ContractDetail>(`/contracts/${id}`),

  /**
   * Batch upload.
   *
   * The endpoint takes repeated `files` parts and reports a per-file outcome, so
   * a duplicate or an unreadable document in the middle of a batch does not
   * abandon the rest of it.
   */
  upload: (
    projectId: UUID,
    files: File[],
    options: { onProgress?: (percent: number) => void; agreementType?: string } = {},
  ) => {
    const form = new FormData();
    for (const file of files) form.append('files', file);
    if (options.agreementType) form.append('agreement_type', options.agreementType);
    return apiUpload<UploadResult>(
      `/projects/${projectId}/contracts/upload`,
      form,
      options.onProgress,
    );
  },

  /** Short-lived URL for the source document, for the viewer. */
  fileAccess: (id: UUID) => api.get<FileAccess>(`/contracts/${id}/file`),
  /**
   * Where to fetch the document from, for a download rather than a view.
   *
   * `download=true` is not just a Content-Disposition hint: for a converted Word
   * upload it selects the *original* file, while the view path returns the PDF
   * the pipeline read. Two different objects, one endpoint.
   */
  fileAccessForDownload: (id: UUID) =>
    api.get<FileAccess>(`/contracts/${id}/file?download=true`),
};

// =============================================================================
// Knowledge
// =============================================================================
export const knowledge = {
  /** Everything the detail screen needs, in one round trip. */
  all: (contractId: UUID) => api.get<ContractKnowledge>(`/contracts/${contractId}/knowledge`),

  clauses: (contractId: UUID, clauseType?: string[]) =>
    api.get<Clause[]>(`/contracts/${contractId}/clauses`, {
      query: { clause_type: clauseType },
    }),

  risks: (contractId: UUID) => api.get<RiskAssessment>(`/contracts/${contractId}/risks`),

  /**
   * Nodes and edges, built on request.
   *
   * Nodes are not stored anywhere — only the derived edges are, as
   * `knowledge_relationships` rows with string references and no labels. So the
   * graph is rebuilt from the extracted rows each time, which also means it
   * reflects the contract as it stands rather than as indexing last left it.
   */
  graph: (contractId: UUID) => api.get<ContractGraph>(`/contracts/${contractId}/graph`),

  /** Resolve a citation to its page, coordinates and a document URL. */
  evidence: (contractId: UUID, chunkId: UUID) =>
    api.get<EvidenceResolution>(`/contracts/${contractId}/evidence/${chunkId}`),

  clause: (contractId: UUID, clauseId: UUID) =>
    api.get<Clause>(`/contracts/${contractId}/clauses/${clauseId}`),

  // Contract-scoped: the route resolves the owning project from `contract_id`,
  // which is how the review is confined to a project the caller belongs to.
  reviewClause: (
    contractId: UUID,
    clauseId: UUID,
    body: {
      review_status: string;
      attributes?: Record<string, unknown>;
      text?: string;
      note?: string;
    },
  ) => api.post<Clause>(`/contracts/${contractId}/clauses/${clauseId}/review`, body),
};

// =============================================================================
// Search & Copilot
// =============================================================================
export const search = {
  query: (body: {
    query: string;
    project_id?: UUID | null;
    scope?: string;
    mode?: string;
    contract_ids?: UUID[];
    limit?: number;
  }) => api.post<SearchResponse>('/search', body),
};

export const copilot = {
  ask: (body: {
    query: string;
    project_id?: UUID | null;
    contract_ids?: UUID[];
    session_id?: UUID | null;
    response_format?: ResponseFormat | null;
    scope?: string;
  }) => api.post<AnswerResponse>('/copilot/ask', body),

  /**
   * Document-type-aware answer, returned in one piece.
   *
   * camelCase body and response - this endpoint has its own agreed field naming.
   * Use it where a whole answer is wanted at once (an export, a test, a retry
   * after a stream broke); the page itself streams, so the answer starts
   * appearing before retrieval and generation have finished.
   */
  query: (body: { query: string; projectId?: UUID | null; contractId?: UUID | null; sessionId?: UUID | null }) =>
    api.post<CopilotQueryResponse>('/copilot/query', body),

  /**
   * Streamed answer.
   *
   * Events, in order: `plan` (before any token, so the UI can say what is being
   * searched), `token` repeatedly, then exactly one `done` carrying the citations,
   * confidence and warnings - or `error` if the stream broke mid-answer.
   *
   * Citations arrive only in `done` because a citation cannot be checked until the
   * text containing it exists; streaming an unverified label would put a reference
   * on screen that might then be withdrawn.
   */
  stream: (
    body: {
      query: string;
      project_id?: UUID | null;
      contract_ids?: UUID[];
      session_id?: UUID | null;
      response_format?: ResponseFormat | null;
      scope?: string;
      mode?: string;
    },
    handlers: {
      onEvent: (event: string, data: unknown) => void;
      onError?: (error: Error) => void;
      signal?: AbortSignal;
    },
  ) => apiStream('/copilot/stream', body, handlers),

  /**
   * Your own conversations. `projectId` narrows to one business unit; `null` is
   * "All Business Units" and returns every conversation you own.
   *
   * It matters more here than on other screens: a conversation is tied to the
   * corpus it was asked against, so one from another unit opens a thread whose
   * follow-ups would search the unit you have selected now.
   */
  sessions: (projectId?: UUID | null) =>
    api.get<ChatSession[]>('/copilot/sessions', {
      query: { project_id: projectId ?? undefined },
    }),
  session: (id: UUID) => api.get<ChatSession>(`/copilot/sessions/${id}`),
  createSession: (body: {
    project_id?: UUID | null;
    contract_id?: UUID | null;
    title?: string;
  }) => api.post<ChatSession>('/copilot/sessions', body),
  deleteSession: (id: UUID) => api.delete<MessageResponse>(`/copilot/sessions/${id}`),
};

// =============================================================================
// Retrieval evaluation (administrator only)
// =============================================================================
export const evaluation = {
  /** Every recorded run's summary, for the trend and the regression history. */
  runs: (dataset?: string) =>
    api.get<{ results_dir: string; runs: EvaluationRun[] }>('/admin/evaluation/runs', {
      query: dataset ? { dataset } : undefined,
    }),

  /** The newest run, with the failing-case lists worth triaging. */
  latest: (dataset?: string) =>
    api.get<EvaluationLatest>('/admin/evaluation/runs/latest', {
      query: dataset ? { dataset } : undefined,
    }),

  datasets: () => api.get<{ datasets: Array<Record<string, unknown>> }>('/admin/evaluation/datasets'),
};

// =============================================================================
// Processing
// =============================================================================
export const jobs = {
  // The list returns a lighter row than the detail endpoint - no stage runs, no
  // structured error, no retryability. Expanding a row fetches the full job.
  list: (filters: { project_id?: UUID; state?: string[]; page?: number; size?: number } = {}) =>
    api.get<Paginated<JobListItem>>('/jobs', {
      query: filters as Record<string, string | number | string[] | undefined>,
    }),

  get: (id: UUID) => api.get<Job>(`/jobs/${id}`),
  forContract: (contractId: UUID) => api.get<Job[]>(`/contracts/${contractId}/jobs`),

  retry: (id: UUID) => api.post<Job>(`/jobs/${id}/retry`),
  cancel: (id: UUID) => api.post<MessageResponse>(`/jobs/${id}/cancel`),

  reprocess: (contractId: UUID, body: { from_stage: string; force?: boolean }) =>
    api.post<Job>(`/contracts/${contractId}/jobs/reprocess`, body),

  health: () => api.get<PipelineHealth>('/jobs/-/health'),
};

// =============================================================================
// Dashboards, Clause Master, Alerts
// =============================================================================
export const dashboard = {
  overview: (projectId?: UUID | null) =>
    api.get<Dashboard>('/dashboard', { query: { project_id: projectId ?? undefined } }),
};

/**
 * Clause coverage: what extraction found, against what each document type's
 * profile expects. Reads the platform's own `contracts` / `clauses` /
 * `embeddings` tables, so it counts the same clauses `dashboard` does - it just
 * measures them against the taxonomy instead of totalling them.
 *
 * The route keeps its `/docpipeline` path; only the screen was renamed.
 */
export const docpipeline = {
  // `document(docid)` used to sit here, calling GET /docpipeline/documents/{docid}.
  // The API registers no such route - the only docpipeline endpoint is this one -
  // so it could only ever have 404'd. Nothing called it.
  insights: (projectId?: UUID | null, limit = 50) =>
    api.get<DocPipelineInsights>('/docpipeline', {
      query: { limit, project_id: projectId ?? undefined },
    }),
};

/**
 * The Clause Master, grouped by agreement type.
 *
 * Two things behind one screen: a **clause** is a row in the global taxonomy, a
 * **mapping** is "this agreement type is checked for that clause, and right now
 * that check is on". Every call below is one or the other.
 *
 * Reading is open to any authenticated user — "why was my contract checked for
 * this?" is a fair question. Writing is system-admin only, enforced server-side.
 */
export const clauseMaster = {
  /** The whole screen in one call, including clauses not yet attached to a type. */
  byAgreementType: (includeUnmapped = true) =>
    api.get<AgreementTypeClauses[]>('/clause-master/by-agreement-type', {
      query: { include_unmapped: includeUnmapped },
    }),

  /** The taxonomy itself, independent of any agreement type. */
  clauses: () => api.get<AgreementClause[]>('/clause-master/clauses'),

  createClause: (body: ClauseDefinitionUpsert) =>
    api.post<AgreementClause>('/clause-master/clauses', body),

  updateClause: (clauseKey: string, body: ClauseDefinitionUpsert) =>
    api.patch<AgreementClause>(`/clause-master/clauses/${clauseKey}`, body),

  /** Soft delete. Historical extractions keep rendering; new ones skip it. */
  deleteClause: (clauseKey: string) =>
    api.delete<MessageResponse>(`/clause-master/clauses/${clauseKey}`),

  /** Attach, toggle active, mark mandatory or reorder — one idempotent upsert. */
  setMapping: (agreementType: string, body: AgreementClauseUpsert) =>
    api.put<AgreementClause>(
      `/clause-master/by-agreement-type/${agreementType}/clauses`,
      body,
    ),

  /** Detach from this type only. The clause stays in the taxonomy. */
  removeMapping: (agreementType: string, clauseKey: string) =>
    api.delete<MessageResponse>(
      `/clause-master/by-agreement-type/${agreementType}/clauses/${clauseKey}`,
    ),

  /** Flat rows, rendered to CSV or XLSX in the browser. */
  exportRows: () => api.get<ClauseImportRow[]>('/clause-master/export'),

  importRows: (
    rows: ClauseImportRow[],
    options: { deactivate_missing?: boolean; create_missing_clauses?: boolean } = {},
  ) =>
    api.post<ClauseImportResult>('/clause-master/import', {
      rows,
      deactivate_missing: options.deactivate_missing ?? false,
      create_missing_clauses: options.create_missing_clauses ?? true,
    }),

  // --- the legacy per-category endpoints, still used by nothing on screen ---
  list: (includeInactive = false) =>
    api.get<ClauseCategory[]>('/clause-master', {
      query: { include_inactive: includeInactive },
    }),
  get: (id: UUID) => api.get<ClauseCategory>(`/clause-master/${id}`),
  update: (id: UUID, body: Partial<ClauseCategory>) =>
    api.patch<ClauseCategory>(`/clause-master/${id}`, body),
};

// =============================================================================
// Exports
// =============================================================================
export const exports = {
  capabilities: () => api.get<ExportCapabilities>('/exports/capabilities'),

  /**
   * Exports *this user* requested - the server scopes to the requester, not to
   * the project, because an export is a copy of data taken by a named person.
   *
   * `projectId` narrows within that, so the list follows the business-unit
   * selector like every other screen. `null` is "All Business Units" and returns
   * everything you requested.
   */
  list: (
    filters: {
      page?: number;
      size?: number;
      status?: ExportStatus[];
      projectId?: UUID | null;
    } = {},
  ) =>
    api.get<Paginated<ExportJob>>('/exports', {
      query: {
        page: filters.page ?? 1,
        size: filters.size ?? 20,
        status: filters.status?.length ? filters.status : undefined,
        project_id: filters.projectId ?? undefined,
      },
    }),

  get: (id: UUID) => api.get<ExportJob>(`/exports/${id}`),

  /**
   * Request an export.
   *
   * `filters` takes the same shape the contract list accepts, which is what makes
   * "export what I am looking at" exact rather than approximate — the screen and
   * the workbook run through the same filter builder on the server.
   */
  create: (body: {
    export_format?: ExportFormat;
    scope?: string;
    project_id?: UUID | null;
    scope_ref?: UUID | null;
    entities?: ExportEntity[];
    filters?: Record<string, unknown>;
    fields?: Record<string, string[]>;
  }) => api.post<ExportJob>('/exports', body),

  download: (id: UUID) => api.get<ExportDownload>(`/exports/${id}/download`),
};

// =============================================================================
// Portfolio - cross-contract registers
// =============================================================================
/**
 * Reads across every contract the caller can see, narrowed by `project_id`.
 *
 * The per-contract equivalents live under `/contracts/{id}/...` and answer "what
 * is in this agreement?". These answer what falls due, who carries exposure and
 * where the risk sits - none of which can be asked of one document.
 */
export const portfolio = {
  obligations: (filters: {
    page?: number;
    size?: number;
    project_id?: UUID | null;
    status?: ObligationStatus[];
    responsible_party?: string;
    due_from?: string;
    due_to?: string;
    undated?: boolean;
    q?: string;
  } = {}) =>
    api.get<Paginated<PortfolioObligation>>('/obligations', {
      query: cleaned(filters),
    }),

  keyDates: (filters: {
    page?: number;
    size?: number;
    project_id?: UUID | null;
    date_type?: DateType[];
    date_from?: string;
    date_to?: string;
    unresolved?: boolean;
  } = {}) => api.get<Paginated<PortfolioKeyDate>>('/key-dates', { query: cleaned(filters) }),

  risks: (filters: {
    page?: number;
    size?: number;
    project_id?: UUID | null;
    severity?: RiskSeverity[];
    risk_type?: string;
    category?: string;
    omissions?: boolean;
    q?: string;
  } = {}) => api.get<Paginated<PortfolioRisk>>('/risks', { query: cleaned(filters) }),

  parties: (filters: {
    page?: number;
    size?: number;
    project_id?: UUID | null;
    q?: string;
    primary_only?: boolean;
  } = {}) => api.get<Paginated<PartyDirectoryEntry>>('/parties', { query: cleaned(filters) }),
};

/**
 * Drops empty values before they reach the query string.
 *
 * `project_id: null` means "every project I can see", which the server expresses
 * by the parameter being *absent*. Sending `project_id=` would be a validation
 * error, and sending `status=` an empty repeated parameter - both turn "no filter"
 * into a 422.
 */
function cleaned(filters: Record<string, unknown>): Record<string, string | number | string[] | undefined> {
  const out: Record<string, string | number | string[] | undefined> = {};
  for (const [key, value] of Object.entries(filters)) {
    if (value === null || value === undefined || value === '') continue;
    if (Array.isArray(value)) {
      if (value.length) out[key] = value as string[];
      continue;
    }
    out[key] = typeof value === 'boolean' ? String(value) : (value as string | number);
  }
  return out;
}

export const alerts = {
  list: (filters: { project_id?: UUID; status?: string[]; page?: number } = {}) =>
    api.get<Paginated<Alert>>('/alerts', {
      query: filters as Record<string, string | number | string[] | undefined>,
    }),

  update: (id: UUID, status: AlertStatus, note?: string) =>
    api.patch<Alert>(`/alerts/${id}`, { status, note }),

  /**
   * The thresholds the evaluator reads.
   *
   * Listing is open to any member - "why did this alert fire?" is a fair
   * question from whoever received it. Writing is system-admin only, enforced
   * server-side; the screen mirrors that by rendering the controls read-only.
   */
  rules: {
    list: (projectId?: UUID | null) =>
      api.get<AlertRule[]>('/alerts/rules', {
        query: { project_id: projectId ?? undefined },
      }),

    create: (body: AlertRuleInput) => api.post<AlertRule>('/alerts/rules', body),

    update: (id: UUID, body: Partial<AlertRuleInput>) =>
      api.patch<AlertRule>(`/alerts/rules/${id}`, body),

    remove: (id: UUID) => api.delete<MessageResponse>(`/alerts/rules/${id}`),
  },
};

/**
 * The compliance trail.
 *
 * Rows are scoped server-side to the caller's projects, plus the platform-level
 * ones that carry no project at all - a login, a user being provisioned - which
 * are usually where an investigation starts.
 */
export const audit = {
  list: (filters: AuditFilters = {}) =>
    api.get<Paginated<AuditEntry>>('/audit', {
      query: filters as Record<string, string | number | boolean | undefined>,
    }),
};
