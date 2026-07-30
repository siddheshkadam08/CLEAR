/**
 * Administration - Users.
 *
 * Provisioning is deliberately password-free from the browser's point of view: the
 * server issues its configured starting credential and forces a change at first
 * sign-in, so no password is typed here, carried in a request body, or has to be
 * invented per person.
 *
 * A new account is created *with* its project assignment in a single request. An
 * account with no project can see nothing, so creating one and assigning it later
 * leaves a person who has signed in successfully and cannot explain why the app is
 * empty.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { KeyRound, Search, ShieldCheck, UserPlus, Users } from 'lucide-react';
import { useMemo, useState } from 'react';

import { admin as adminApi, projects as projectsApi } from '@/api/endpoints';
import { errorMessage } from '@/api/errors';
import type { RoleName, UUID } from '@/api/types';
import { Badge } from '@/components/common/Badge';
import { ErrorBanner, NoticeBanner, SuccessBanner } from '@/components/common/Banner';
import { Button } from '@/components/common/Button';
import { Card, PageHeader } from '@/components/common/Card';
import { EmptyState } from '@/components/common/EmptyState';
import { Field, inputClasses } from '@/components/common/Field';
import { LoadingSpinner } from '@/components/common/LoadingSpinner';
import { Modal } from '@/components/common/Modal';
import { formatDate } from '@/lib/format';

const ASSIGNABLE_ROLES: { value: RoleName; label: string; hint: string }[] = [
  { value: 'project_manager', label: 'Contract Manager', hint: 'Upload, manage and review' },
  { value: 'reviewer', label: 'Reviewer', hint: 'Upload and correct extractions' },
  { value: 'viewer', label: 'Viewer', hint: 'Read-only' },
];

export function AdminUsersPage() {
  const queryClient = useQueryClient();
  const [search, setSearch] = useState('');
  const [createOpen, setCreateOpen] = useState(false);
  const [notice, setNotice] = useState('');

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ['admin', 'users'],
    queryFn: () => adminApi.users({ size: 200 }),
  });

  const items = useMemo(() => {
    const rows = data?.items ?? [];
    const needle = search.trim().toLowerCase();
    if (!needle) return rows;
    return rows.filter((row) =>
      [row.full_name, row.email, row.department]
        .filter(Boolean)
        .some((value) => String(value).toLowerCase().includes(needle)),
    );
  }, [data, search]);

  return (
    <div className="space-y-5">
      <PageHeader
        title="Users"
        subtitle="Create accounts and assign them to the projects they work on."
        actions={
          <Button icon={UserPlus} onClick={() => setCreateOpen(true)}>
            Add user
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
            placeholder="Filter by name, email or department"
            aria-label="Filter users"
            className={`${inputClasses} pl-9`}
          />
        </div>
      </Card>

      {isLoading ? (
        <Card>
          <LoadingSpinner label="Loading users..." />
        </Card>
      ) : items.length ? (
        <>
          <Card className="hidden overflow-hidden p-0 md:block">
            <div className="overflow-x-auto">
              <table className="w-full text-left text-sm">
                <thead className="border-b border-slate-200 bg-slate-50 text-xs uppercase tracking-wider text-slate-500">
                  <tr>
                    <th className="px-5 py-3 font-semibold">Name</th>
                    <th className="px-5 py-3 font-semibold">Role</th>
                    <th className="px-5 py-3 text-right font-semibold">Projects</th>
                    <th className="px-5 py-3 font-semibold">Last sign-in</th>
                    <th className="px-5 py-3 font-semibold">Status</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {items.map((user) => (
                    <tr key={user.id} className="transition hover:bg-slate-50">
                      <td className="px-5 py-3">
                        <p className="font-medium text-slate-900">{user.full_name}</p>
                        <p className="text-xs text-slate-500">{user.email}</p>
                      </td>
                      <td className="px-5 py-3">
                        {user.is_system_admin ? (
                          <Badge text="Administrator" variant="warning" />
                        ) : (
                          <span className="text-slate-600">
                            {roleLabel(user.primary_role) ?? '—'}
                          </span>
                        )}
                      </td>
                      <td className="px-5 py-3 text-right tabular-nums text-slate-700">
                        {user.project_count}
                      </td>
                      <td className="px-5 py-3 text-slate-500">
                        {user.last_login_at ? formatDate(user.last_login_at) : 'Never'}
                      </td>
                      <td className="px-5 py-3">
                        <Badge
                          text={user.is_active ? 'Active' : 'Disabled'}
                          variant={user.is_active ? 'success' : 'neutral'}
                        />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>

          <div className="space-y-3 md:hidden">
            {items.map((user) => (
              <Card key={user.id} dense>
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <p className="truncate font-semibold text-slate-900">{user.full_name}</p>
                    <p className="truncate text-xs text-slate-500">{user.email}</p>
                  </div>
                  <Badge
                    text={user.is_active ? 'Active' : 'Disabled'}
                    variant={user.is_active ? 'success' : 'neutral'}
                  />
                </div>
                <div className="mt-3 flex flex-wrap items-center gap-2 text-xs text-slate-500">
                  {user.is_system_admin ? (
                    <Badge text="Administrator" variant="warning" />
                  ) : user.primary_role ? (
                    <Badge
                      text={roleLabel(user.primary_role) ?? user.primary_role}
                      variant="info"
                    />
                  ) : null}
                  <span>
                    {user.project_count} project{user.project_count === 1 ? '' : 's'}
                  </span>
                  <span aria-hidden>·</span>
                  <span>
                    {user.last_login_at
                      ? `Last seen ${formatDate(user.last_login_at)}`
                      : 'Never signed in'}
                  </span>
                </div>
              </Card>
            ))}
          </div>
        </>
      ) : (
        <EmptyState
          icon={Users}
          title={search ? 'No users match that filter' : 'No users yet'}
          description={
            search
              ? 'Clear the filter to see every account on the platform.'
              : 'Create an account for each person who will work on contracts, and assign them to a project so they have something to work on.'
          }
          action={
            search ? (
              <Button variant="secondary" onClick={() => setSearch('')}>
                Clear filter
              </Button>
            ) : (
              <Button icon={UserPlus} onClick={() => setCreateOpen(true)}>
                Add the first user
              </Button>
            )
          }
        />
      )}

      <CreateUserDialog
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        onCreated={(name) => {
          setCreateOpen(false);
          setNotice(
            `${name} can now sign in with the default password and will be asked to change it.`,
          );
          void queryClient.invalidateQueries({ queryKey: ['admin', 'users'] });
          void queryClient.invalidateQueries({ queryKey: ['admin', 'projects'] });
        }}
      />
    </div>
  );
}

const roleLabel = (role?: string | null) =>
  role ? (ASSIGNABLE_ROLES.find((option) => option.value === role)?.label ?? role) : null;

// =============================================================================
// Create
// =============================================================================
function CreateUserDialog({
  open,
  onClose,
  onCreated,
}: {
  open: boolean;
  onClose: () => void;
  onCreated: (name: string) => void;
}) {
  const [email, setEmail] = useState('');
  const [fullName, setFullName] = useState('');
  const [jobTitle, setJobTitle] = useState('');
  const [projectId, setProjectId] = useState<UUID | ''>('');
  const [role, setRole] = useState<RoleName>('project_manager');

  const projects = useQuery({
    queryKey: ['admin', 'projects'],
    queryFn: () => projectsApi.list({ size: 200 }),
    enabled: open,
  });

  const mutation = useMutation({
    mutationFn: () =>
      adminApi.createUser({
        email: email.trim().toLowerCase(),
        full_name: fullName.trim(),
        job_title: jobTitle.trim() || undefined,
        project_assignments: projectId ? [{ project_id: projectId as UUID, role }] : [],
      }),
    onSuccess: () => {
      const created = fullName.trim();
      setEmail('');
      setFullName('');
      setJobTitle('');
      setProjectId('');
      onCreated(created);
    },
  });

  const close = () => {
    mutation.reset();
    onClose();
  };

  const projectOptions = projects.data?.items ?? [];
  const canSubmit = Boolean(email.trim() && fullName.trim());

  return (
    <Modal
      open={open}
      onClose={close}
      title="Add user"
      description="They receive the default starting password and must change it at first sign-in."
      footer={
        <>
          <Button variant="secondary" onClick={close}>
            Cancel
          </Button>
          <Button
            busy={mutation.isPending}
            disabled={!canSubmit}
            onClick={() => mutation.mutate()}
          >
            Create account
          </Button>
        </>
      }
    >
      <form
        className="space-y-4"
        onSubmit={(event) => {
          event.preventDefault();
          if (canSubmit) mutation.mutate();
        }}
      >
        {mutation.isError ? <ErrorBanner message={errorMessage(mutation.error)} /> : null}

        <Field label="Full name" required>
          <input
            value={fullName}
            onChange={(event) => setFullName(event.target.value)}
            placeholder="Priya Sharma"
            className={inputClasses}
            autoFocus
          />
        </Field>

        <Field label="Email" required hint="Used to sign in. Cannot be changed later.">
          <input
            type="email"
            value={email}
            onChange={(event) => setEmail(event.target.value)}
            placeholder="priya@company.com"
            className={inputClasses}
          />
        </Field>

        <Field label="Job title" hint="Optional.">
          <input
            value={jobTitle}
            onChange={(event) => setJobTitle(event.target.value)}
            placeholder="Contracts Lead"
            className={inputClasses}
          />
        </Field>

        <div className="rounded-2xl border border-slate-200 bg-slate-50 p-4">
          <div className="mb-3 flex items-center gap-2 text-sm font-semibold text-slate-700">
            <ShieldCheck className="h-4 w-4 text-blue-600" />
            Project access
          </div>

          {projects.isLoading ? (
            <LoadingSpinner size="sm" label="Loading projects..." />
          ) : projectOptions.length ? (
            <div className="space-y-3">
              <Field label="Project">
                <select
                  value={projectId}
                  onChange={(event) => setProjectId(event.target.value as UUID)}
                  className={inputClasses}
                >
                  <option value="">No project yet</option>
                  {projectOptions.map((project) => (
                    <option key={project.id} value={project.id}>
                      {project.name}
                    </option>
                  ))}
                </select>
              </Field>

              <Field label="Role in that project">
                <select
                  value={role}
                  onChange={(event) => setRole(event.target.value as RoleName)}
                  className={inputClasses}
                  disabled={!projectId}
                >
                  {ASSIGNABLE_ROLES.map((option) => (
                    <option key={option.value} value={option.value}>
                      {option.label} — {option.hint}
                    </option>
                  ))}
                </select>
              </Field>

              {!projectId ? (
                <NoticeBanner message="Without a project this account can sign in but will see nothing. You can add them to one from the Projects screen later." />
              ) : null}
            </div>
          ) : (
            <NoticeBanner message="There are no projects yet. Create one first, then this account can be given access to it." />
          )}
        </div>

        <div className="flex items-start gap-3 rounded-2xl border border-blue-200 bg-blue-50 px-4 py-3 text-sm text-blue-800">
          <KeyRound className="mt-0.5 h-4 w-4 shrink-0" />
          <span>
            The starting password is set by the server and the account is forced to change it on
            first sign-in.
          </span>
        </div>
      </form>
    </Modal>
  );
}

export default AdminUsersPage;
