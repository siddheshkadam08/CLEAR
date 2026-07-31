/**
 * Administration - Projects.
 *
 * The administrator's half of the workflow: create a project, then put people in
 * it. Contract work happens inside the project by its members, which is why this
 * screen has no upload affordance anywhere on it.
 *
 * Members are managed from a drawer on the row rather than a separate page: adding
 * someone is a step in creating a project, not a destination you navigate to.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { FolderKanban, FolderPlus, Search, Trash2, UserPlus, Users } from 'lucide-react';
import { useMemo, useState } from 'react';

import { admin as adminApi, projects as projectsApi } from '@/api/endpoints';
import { errorMessage } from '@/api/errors';
import type { ProjectListItem, RoleName, UUID } from '@/api/types';
import { Badge } from '@/components/common/Badge';
import { formatStatusLabel, getStatusVariant } from '@/lib/badges';
import { ErrorBanner, SuccessBanner } from '@/components/common/Banner';
import { Button } from '@/components/common/Button';
import { Card, PageHeader } from '@/components/common/Card';
import { EmptyState } from '@/components/common/EmptyState';
import { Field, inputClasses, selectClasses, SelectChevron } from '@/components/common/Field';
import { LoadingSpinner } from '@/components/common/LoadingSpinner';
import { Modal } from '@/components/common/Modal';
import { Pagination } from '@/components/common/Pagination';
import { formatDate, formatDateTimeFull, formatNumber } from '@/lib/format';

/** Roles an administrator can hand out. `system_admin` is deliberately absent:
 *  platform administration is granted on the account, not per project. */
const ASSIGNABLE_ROLES: { value: RoleName; label: string; hint: string }[] = [
  { value: 'project_manager', label: 'Contract Manager', hint: 'Upload, manage and review' },
  { value: 'reviewer', label: 'Reviewer', hint: 'Upload and correct extractions' },
  { value: 'viewer', label: 'Viewer', hint: 'Read-only' },
];

export function AdminProjectsPage() {
  const queryClient = useQueryClient();
  const [search, setSearch] = useState('');
  const [page, setPage] = useState(1);
  const PAGE_SIZE = 10;
  const [createOpen, setCreateOpen] = useState(false);
  const [membersFor, setMembersFor] = useState<ProjectListItem | null>(null);
  const [notice, setNotice] = useState('');

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ['admin', 'projects'],
    queryFn: () => projectsApi.list({ size: 200 }),
  });

  const items = useMemo(() => {
    const rows = data?.items ?? [];
    const needle = search.trim().toLowerCase();
    if (!needle) return rows;
    return rows.filter((row) =>
      [row.name, row.client_name, row.description]
        .filter(Boolean)
        .some((value) => String(value).toLowerCase().includes(needle)),
    );
  }, [data, search]);

  // Reset to first page when filter changes
  useMemo(() => setPage(1), [search]); // eslint-disable-line react-hooks/exhaustive-deps
  const pagedItems = items.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE);
  const totalPages = Math.ceil(items.length / PAGE_SIZE);

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ['admin', 'projects'] });
    // The header's project switcher reads the same list.
    void queryClient.invalidateQueries({ queryKey: ['projects'] });
  };

  return (
    <div className="space-y-5">
      <PageHeader
        title="Projects"
        subtitle="Create projects and decide who works in them."
        actions={
          <Button icon={FolderPlus} onClick={() => setCreateOpen(true)}>
            New project
          </Button>
        }
      />

      {notice ? <SuccessBanner message={notice} /> : null}
      {error ? (
        <ErrorBanner message={errorMessage(error)} onRetry={() => void refetch()} />
      ) : null}

      <Card dense>
        <div className="relative">
          <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-400" />
          <input
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder="Filter by name or client"
            aria-label="Filter projects"
            className={`${inputClasses} pl-9`}
          />
        </div>
      </Card>

      {isLoading ? (
        <Card>
          <LoadingSpinner label="Loading projects..." />
        </Card>
      ) : items.length ? (
        <>
          {/* Table above `md`, stacked cards below it. A ten-column table on a
              phone is a horizontal scroll nobody discovers. */}
          <Card className="hidden overflow-hidden p-0 md:block">
            <div className="overflow-x-auto">
              <table className="w-full text-left text-sm">
                <thead className="bg-slate-50/80 text-xs dark:bg-slate-800/80">
                  <tr className="border-b border-slate-200 dark:border-slate-700">
                    <th className="px-5 py-3.5 font-semibold uppercase tracking-[0.06em] text-slate-500 dark:text-slate-400">Project</th>
                    <th className="px-5 py-3.5 font-semibold uppercase tracking-[0.06em] text-slate-500 dark:text-slate-400">Status</th>
                    <th className="px-5 py-3.5 text-right font-semibold uppercase tracking-[0.06em] text-slate-500 dark:text-slate-400">Contracts</th>
                    <th className="px-5 py-3.5 text-right font-semibold uppercase tracking-[0.06em] text-slate-500 dark:text-slate-400">Members</th>
                    <th className="px-5 py-3.5 font-semibold uppercase tracking-[0.06em] text-slate-500 dark:text-slate-400">Created On</th>
                    <th className="px-5 py-3.5" />
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100/80 dark:divide-slate-700/50">
                  {pagedItems.map((project) => (
                    <tr key={project.id} className="transition hover:bg-blue-50/40 dark:hover:bg-blue-950/10">
                      <td className="px-5 py-3.5">
                        <p className="font-medium text-slate-900 dark:text-slate-100">{project.name}</p>
                        {project.client_name ? (
                          <p className="text-xs text-slate-500 dark:text-slate-400">{project.client_name}</p>
                        ) : null}
                      </td>
                      <td className="px-5 py-3.5">
                        <Badge
                          text={formatStatusLabel(project.status)}
                          variant={getStatusVariant(project.status)}
                        />
                      </td>
                      <td className="px-5 py-3.5 text-right tabular-nums text-slate-700 dark:text-slate-300">
                        {formatNumber(project.contract_count)}
                      </td>
                      <td className="px-5 py-3.5 text-right tabular-nums text-slate-700 dark:text-slate-300">
                        {formatNumber(project.member_count)}
                      </td>
                      <td className="px-5 py-3.5 text-slate-500 dark:text-slate-400">
                        {formatDateTimeFull(project.created_at)}
                      </td>
                      <td className="px-5 py-3.5 text-right">
                        <Button
                          variant="secondary"
                          size="sm"
                          icon={Users}
                          onClick={() => setMembersFor(project)}
                        >
                          Members
                        </Button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {totalPages > 1 && (
              <div className="border-t border-slate-200 bg-slate-50/80 px-5 py-3 dark:border-slate-700 dark:bg-slate-800/80">
                <Pagination page={page} pages={totalPages} total={items.length} pageSize={PAGE_SIZE} onPage={setPage} />
              </div>
            )}
          </Card>

          <div className="space-y-3 md:hidden">
            {items.map((project) => (
              <Card key={project.id} dense>
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <p className="truncate font-semibold text-slate-900 dark:text-slate-100">{project.name}</p>
                    {project.client_name ? (
                      <p className="truncate text-xs text-slate-500 dark:text-slate-400">{project.client_name}</p>
                    ) : null}
                  </div>
                  <Badge
                    text={formatStatusLabel(project.status)}
                    variant={getStatusVariant(project.status)}
                  />
                </div>
                <dl className="mt-3 grid grid-cols-3 gap-2 text-center">
                  <div className="rounded-xl bg-slate-50 py-2 dark:bg-slate-700/50">
                    <dt className="text-xs text-slate-500 dark:text-slate-400">Contracts</dt>
                    <dd className="text-sm font-semibold text-slate-900 dark:text-slate-100">
                      {formatNumber(project.contract_count)}
                    </dd>
                  </div>
                  <div className="rounded-xl bg-slate-50 py-2 dark:bg-slate-700/50">
                    <dt className="text-xs text-slate-500 dark:text-slate-400">Members</dt>
                    <dd className="text-sm font-semibold text-slate-900 dark:text-slate-100">
                      {formatNumber(project.member_count)}
                    </dd>
                  </div>
                  <div className="rounded-xl bg-slate-50 py-2 dark:bg-slate-700/50">
                    <dt className="text-xs text-slate-500 dark:text-slate-400">Created</dt>
                    <dd className="text-sm font-semibold text-slate-900 dark:text-slate-100">
                      {formatDateTimeFull(project.created_at)}
                    </dd>
                  </div>
                </dl>
                <Button
                  variant="secondary"
                  icon={Users}
                  className="mt-3 w-full"
                  onClick={() => setMembersFor(project)}
                >
                  Manage members
                </Button>
              </Card>
            ))}
          </div>
        </>
      ) : (
        <EmptyState
          icon={FolderKanban}
          title={search ? 'No projects match that filter' : 'No projects yet'}
          description={
            search
              ? 'Clear the filter to see every project on the platform.'
              : 'A project is the security boundary: contracts, extractions and search results never cross one. Create the first project, then add the people who will work in it.'
          }
          action={
            search ? (
              <Button variant="secondary" onClick={() => setSearch('')}>
                Clear filter
              </Button>
            ) : (
              <Button icon={FolderPlus} onClick={() => setCreateOpen(true)}>
                Create a project
              </Button>
            )
          }
        />
      )}

      <CreateProjectDialog
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        onCreated={(name) => {
          setCreateOpen(false);
          setNotice(`Project "${name}" created. Add members to let them upload contracts.`);
          invalidate();
        }}
      />

      <ManageMembersDialog
        project={membersFor}
        onClose={() => setMembersFor(null)}
        onChanged={invalidate}
      />
    </div>
  );
}

// =============================================================================
// Create
// =============================================================================
function CreateProjectDialog({
  open,
  onClose,
  onCreated,
}: {
  open: boolean;
  onClose: () => void;
  onCreated: (name: string) => void;
}) {
  const [name, setName] = useState('');
  const [clientName, setClientName] = useState('');
  const [description, setDescription] = useState('');

  const mutation = useMutation({
    mutationFn: () =>
      projectsApi.create({
        name: name.trim(),
        client_name: clientName.trim() || undefined,
        description: description.trim() || undefined,
      }),
    onSuccess: () => {
      const created = name.trim();
      setName('');
      setClientName('');
      setDescription('');
      onCreated(created);
    },
  });

  const close = () => {
    mutation.reset();
    onClose();
  };

  return (
    <Modal
      open={open}
      onClose={close}
      title="New project"
      description="Contracts, extractions and search results stay inside it."
      footer={
        <>
          <Button variant="secondary" onClick={close}>
            Cancel
          </Button>
          <Button
            busy={mutation.isPending}
            disabled={!name.trim()}
            onClick={() => mutation.mutate()}
          >
            Create project
          </Button>
        </>
      }
    >
      <form
        className="space-y-4"
        onSubmit={(event) => {
          event.preventDefault();
          if (name.trim()) mutation.mutate();
        }}
      >
        {mutation.isError ? <ErrorBanner message={errorMessage(mutation.error)} /> : null}

        <Field label="Project name" required>
          <input
            value={name}
            onChange={(event) => setName(event.target.value)}
            placeholder="Vendor agreements 2026"
            className={inputClasses}
            autoFocus
          />
        </Field>

        <Field label="Client" hint="Optional. Shown on the project card.">
          <input
            value={clientName}
            onChange={(event) => setClientName(event.target.value)}
            placeholder="Acme Corp"
            className={inputClasses}
          />
        </Field>

        <Field label="Description" hint="Optional. What this project is for.">
          <textarea
            value={description}
            onChange={(event) => setDescription(event.target.value)}
            rows={3}
            className={`${inputClasses} resize-y`}
          />
        </Field>
      </form>
    </Modal>
  );
}

// =============================================================================
// Members
// =============================================================================
function ManageMembersDialog({
  project,
  onClose,
  onChanged,
}: {
  project: ProjectListItem | null;
  onClose: () => void;
  onChanged: () => void;
}) {
  const [userId, setUserId] = useState<UUID | ''>('');
  const [role, setRole] = useState<RoleName>('project_manager');
  const [confirmRemove, setConfirmRemove] = useState<{ id: UUID; name: string } | null>(null);
  const projectId = project?.id;

  const members = useQuery({
    queryKey: ['admin', 'project-members', projectId],
    queryFn: () => projectsApi.members(projectId as UUID),
    enabled: Boolean(projectId),
  });

  const users = useQuery({
    queryKey: ['admin', 'users', 'all'],
    queryFn: () => adminApi.users({ size: 200, is_active: true }),
    enabled: Boolean(projectId),
  });

  const addMember = useMutation({
    mutationFn: () =>
      projectsApi.addMember(projectId as UUID, { user_id: userId as UUID, role }),
    onSuccess: () => {
      setUserId('');
      void members.refetch();
      onChanged();
    },
  });

  const removeMember = useMutation({
    mutationFn: (memberUserId: UUID) =>
      projectsApi.removeMember(projectId as UUID, memberUserId),
    onSuccess: () => {
      void members.refetch();
      onChanged();
    },
  });

  // Only offer people who are not already in the project, and never the
  // administrator accounts - platform administration is not a project role.
  const candidates = useMemo(() => {
    const existing = new Set((members.data ?? []).map((member) => member.user.id));
    return (users.data?.items ?? []).filter(
      (user) => !existing.has(user.id) && !user.is_system_admin,
    );
  }, [members.data, users.data]);

  return (
    <Modal
      open={Boolean(project)}
      onClose={onClose}
      title={project ? `Members of ${project.name}` : 'Members'}
      description="Members do the contract work: uploading, reviewing and searching."
      footer={
        <Button variant="secondary" onClick={onClose}>
          Done
        </Button>
      }
    >
      <div className="space-y-5">
        {addMember.isError ? <ErrorBanner message={errorMessage(addMember.error)} /> : null}
        {removeMember.isError ? (
          <ErrorBanner message={errorMessage(removeMember.error)} />
        ) : null}

        <div className="space-y-3 rounded-2xl border border-slate-200 bg-slate-50 p-4 dark:border-slate-700 dark:bg-slate-800/50">
          <Field label="Add a person">
            <div className="relative">
            <select
              value={userId}
              onChange={(event) => setUserId(event.target.value as UUID)}
              className={selectClasses}
              disabled={users.isLoading}
            >
              <option value="">{users.isLoading ? 'Loading users...' : 'Select a user'}</option>
              {candidates.map((user) => (
                <option key={user.id} value={user.id}>
                  {user.full_name} — {user.email}
                </option>
              ))}
            </select>
            <SelectChevron />
            </div>
          </Field>

          <Field label="Role">
            <div className="relative">
            <select
              value={role}
              onChange={(event) => setRole(event.target.value as RoleName)}
              className={selectClasses}
            >
              {ASSIGNABLE_ROLES.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label} — {option.hint}
                </option>
              ))}
            </select>
            <SelectChevron />
            </div>
          </Field>

          <Button
            icon={UserPlus}
            className="w-full"
            disabled={!userId}
            busy={addMember.isPending}
            onClick={() => addMember.mutate()}
          >
            Add to project
          </Button>

          {!users.isLoading && !candidates.length ? (
            <p className="text-xs text-slate-500 dark:text-slate-400">
              Everyone available is already a member. Create an account on the Users screen
              first.
            </p>
          ) : null}
        </div>

        <div>
          <p className="mb-2 text-xs font-semibold uppercase tracking-wider text-slate-400 dark:text-slate-400">
            Current members
          </p>
          {members.isLoading ? (
            <LoadingSpinner size="sm" label="Loading members..." />
          ) : members.data?.length ? (
            <ul className="divide-y divide-slate-100 rounded-2xl border border-slate-200 dark:divide-slate-700 dark:border-slate-700">
              {members.data.map((member) => (
                <li
                  key={member.id}
                  className="flex items-center justify-between gap-3 px-4 py-3"
                >
                  <div className="min-w-0">
                    <p className="truncate text-sm font-medium text-slate-900 dark:text-slate-100">
                      {member.user.full_name}
                    </p>
                    <p className="truncate text-xs text-slate-500 dark:text-slate-400">{member.user.email}</p>
                  </div>
                  <div className="flex shrink-0 items-center gap-2">
                    <Badge text={member.role_display_name} variant="info" />
                    {member.role !== 'system_admin' && member.user.full_name !== 'System Administrator' && (
                      <button
                        type="button"
                        aria-label={`Remove ${member.user.full_name}`}
                        onClick={() =>
                          setConfirmRemove({ id: member.user.id, name: member.user.full_name })
                        }
                        disabled={removeMember.isPending}
                        className="rounded-lg p-2 text-slate-400 transition hover:bg-rose-50 hover:text-rose-600 disabled:opacity-50 dark:hover:bg-rose-950 dark:hover:text-rose-400"
                      >
                        <Trash2 className="h-4 w-4" />
                      </button>
                    )}
                  </div>
                </li>
              ))}
            </ul>
          ) : (
            <p className="rounded-2xl border border-dashed border-slate-300 px-4 py-6 text-center text-sm text-slate-500 dark:border-slate-600 dark:text-slate-400">
              Nobody has been added yet. Until someone is, no contracts can be uploaded here.
            </p>
          )}
        </div>
      </div>

      <Modal
        open={Boolean(confirmRemove)}
        onClose={() => setConfirmRemove(null)}
        title="Remove member"
        description={
          confirmRemove ? `Are you sure you want to remove ${confirmRemove.name} from this project?` : ''
        }
        footer={
          <>
            <Button variant="secondary" onClick={() => setConfirmRemove(null)}>
              No
            </Button>
            <Button
              busy={removeMember.isPending}
              onClick={() => {
                if (confirmRemove) {
                  removeMember.mutate(confirmRemove.id);
                  setConfirmRemove(null);
                }
              }}
            >
              Yes, remove
            </Button>
          </>
        }
      />
    </Modal>
  );
}

export default AdminProjectsPage;
