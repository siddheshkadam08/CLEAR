/**
 * Typed endpoint functions, grouped by domain.
 *
 * One place that knows every URL, so a route change is a single edit rather than a
 * search through components. Every function returns a typed promise; React Query
 * handles caching and refetching on top.
 */

import { api, apiStream, apiUpload } from './client';
import type {
  Alert,
  AlertStatus,
  AnswerResponse,
  ChatSession,
  ClauseCategory,
  Clause,
  ContractDetail,
  ContractKnowledge,
  ContractListItem,
  CurrentUser,
  Dashboard,
  EvidenceResolution,
  ExportCapabilities,
  ExportDownload,
  ExportEntity,
  ExportFormat,
  ExportJob,
  FileAccess,
  Job,
  JobListItem,
  MessageResponse,
  Paginated,
  PipelineHealth,
  ProjectDetail,
  ProjectListItem,
  ProjectMember,
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
  q?: string;
  status?: string[];
  agreement_type?: string[];
  risk_band?: string[];
  needs_review?: boolean;
  expiring_before?: string;
  expiring_after?: string;
  has_unlimited_liability?: boolean;
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
    response_format?: string | null;
    scope?: string;
  }) => api.post<AnswerResponse>('/copilot/ask', body),

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
      response_format?: string | null;
      scope?: string;
      mode?: string;
    },
    handlers: {
      onEvent: (event: string, data: unknown) => void;
      onError?: (error: Error) => void;
      signal?: AbortSignal;
    },
  ) => apiStream('/copilot/stream', body, handlers),

  sessions: () => api.get<ChatSession[]>('/copilot/sessions'),
  session: (id: UUID) => api.get<ChatSession>(`/copilot/sessions/${id}`),
  createSession: (body: {
    project_id?: UUID | null;
    contract_id?: UUID | null;
    title?: string;
  }) => api.post<ChatSession>('/copilot/sessions', body),
  deleteSession: (id: UUID) => api.delete<MessageResponse>(`/copilot/sessions/${id}`),
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

export const clauseMaster = {
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

  list: (page = 1) => api.get<Paginated<ExportJob>>('/exports', { query: { page, size: 20 } }),

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

export const alerts = {
  list: (filters: { project_id?: UUID; status?: string[]; page?: number } = {}) =>
    api.get<Paginated<Alert>>('/alerts', {
      query: filters as Record<string, string | number | string[] | undefined>,
    }),

  update: (id: UUID, status: AlertStatus, note?: string) =>
    api.patch<Alert>(`/alerts/${id}`, { status, note }),
};
