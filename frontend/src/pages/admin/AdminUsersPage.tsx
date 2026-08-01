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
import { ChevronDown, KeyRound, Search, ShieldCheck, UserPlus, Users, X } from 'lucide-react';
import { useEffect, useMemo, useRef, useState } from 'react';

import { admin as adminApi, projects as projectsApi } from '@/api/endpoints';
import { errorMessage } from '@/api/errors';
import type { RoleName, UUID } from '@/api/types';
import { Badge } from '@/components/common/Badge';
import { ErrorBanner, NoticeBanner, SuccessBanner } from '@/components/common/Banner';
import { Button } from '@/components/common/Button';
import { Card, PageHeader } from '@/components/common/Card';
import { EmptyState } from '@/components/common/EmptyState';
import { Field, inputClasses, selectClasses, SelectChevron } from '@/components/common/Field';
import { LoadingSpinner } from '@/components/common/LoadingSpinner';
import { Modal } from '@/components/common/Modal';
import { Pagination } from '@/components/common/Pagination';
import { formatDateTimeFull } from '@/lib/format';

const ASSIGNABLE_ROLES: { value: RoleName; label: string; hint: string }[] = [
  { value: 'project_manager', label: 'Contract Manager', hint: 'Upload, manage and review' },
  { value: 'reviewer', label: 'Reviewer', hint: 'Upload and correct extractions' },
  { value: 'viewer', label: 'Viewer', hint: 'Read-only' },
];

export function AdminUsersPage() {
  const queryClient = useQueryClient();
  const [search, setSearch] = useState('');
  const [page, setPage] = useState(1);
  const PAGE_SIZE = 10;
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

  // Reset to first page when filter changes
  useMemo(() => setPage(1), [search]); // eslint-disable-line react-hooks/exhaustive-deps
  const pagedItems = items.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE);
  const totalPages = Math.ceil(items.length / PAGE_SIZE);

  return (
    <div className="space-y-5">
      <PageHeader
        // title="Users"
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
                <thead className="bg-slate-50/80 text-xs dark:bg-slate-800/80">
                  <tr className="border-b border-slate-200 dark:border-slate-700">
                    <th className="px-5 py-3.5 font-semibold uppercase tracking-[0.06em] text-slate-500 dark:text-slate-400">Name</th>
                    <th className="px-5 py-3.5 font-semibold uppercase tracking-[0.06em] text-slate-500 dark:text-slate-400">Role</th>
                    <th className="px-5 py-3.5 text-right font-semibold uppercase tracking-[0.06em] text-slate-500 dark:text-slate-400">Business Units</th>
                    <th className="px-5 py-3.5 font-semibold uppercase tracking-[0.06em] text-slate-500 dark:text-slate-400">Last sign-in</th>
                    <th className="px-5 py-3.5 font-semibold uppercase tracking-[0.06em] text-slate-500 dark:text-slate-400">Status</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100/80 dark:divide-slate-700/50">
                  {pagedItems.map((user) => (
                    <tr key={user.id} className="transition hover:bg-blue-50/40 dark:hover:bg-blue-950/10">
                      <td className="px-5 py-3.5">
                        <p className="font-medium text-slate-900 dark:text-slate-100">{user.full_name}</p>
                        <p className="text-xs text-slate-500 dark:text-slate-400">{user.email}</p>
                      </td>
                      <td className="px-5 py-3.5">
                        {user.is_system_admin ? (
                          <Badge text="Administrator" variant="warning" />
                        ) : (
                          <span className="text-slate-600 dark:text-slate-300">
                            {roleLabel(user.primary_role) ?? '—'}
                          </span>
                        )}
                      </td>
                      <td className="px-5 py-3.5 text-right tabular-nums text-slate-700 dark:text-slate-300">
                        {user.project_count}
                      </td>
                      <td className="px-5 py-3.5 text-slate-500 dark:text-slate-400">
                        {user.last_login_at ? formatDateTimeFull(user.last_login_at) : 'Never'}
                      </td>
                      <td className="px-5 py-3.5">
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
            {totalPages > 1 && (
              <div className="border-t border-slate-200 bg-slate-50/80 px-5 py-3 dark:border-slate-700 dark:bg-slate-800/80">
                <Pagination page={page} pages={totalPages} total={items.length} pageSize={PAGE_SIZE} onPage={setPage} />
              </div>
            )}
          </Card>

          <div className="space-y-3 md:hidden">
            {items.map((user) => (
              <Card key={user.id} dense>
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <p className="truncate font-semibold text-slate-900 dark:text-slate-100">{user.full_name}</p>
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
                      ? `Last seen ${formatDateTimeFull(user.last_login_at)}`
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
// Multi-select dropdown
// =============================================================================
function MultiSelectDropdown({
  options,
  selected,
  onChange,
  placeholder = 'Select…',
}: {
  options: { value: UUID; label: string }[];
  selected: UUID[];
  onChange: (ids: UUID[]) => void;
  placeholder?: string;
}) {
  const [open, setOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const handler = (e: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, [open]);

  const toggle = (id: UUID) => {
    onChange(
      selected.includes(id) ? selected.filter((s) => s !== id) : [...selected, id],
    );
  };

  return (
    <div ref={containerRef} className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className={`${selectClasses} flex items-center gap-1 flex-wrap min-h-[2.5rem] text-left`}
      >
        {selected.length === 0 ? (
          <span className="text-slate-400">{placeholder}</span>
        ) : (
          selected.map((id) => {
            const opt = options.find((o) => o.value === id);
            return (
              <span
                key={id}
                className="inline-flex items-center gap-0.5 rounded bg-blue-100 dark:bg-blue-900 px-1.5 py-0.5 text-xs font-medium text-blue-800 dark:text-blue-200"
              >
                {opt?.label}
                <X
                  className="h-3 w-3 cursor-pointer"
                  onClick={(e) => { e.stopPropagation(); toggle(id); }}
                />
              </span>
            );
          })
        )}
        <ChevronDown className="ml-auto h-4 w-4 shrink-0 text-slate-400" />
      </button>

      {open && (
        <ul className="absolute z-50 mt-1 max-h-48 w-full overflow-auto rounded-md border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 py-1 shadow-lg">
          {options.map((opt) => (
            <li
              key={opt.value}
              onClick={() => toggle(opt.value)}
              className={`cursor-pointer select-none px-3 py-1.5 text-sm hover:bg-blue-50 dark:hover:bg-slate-700 ${
                selected.includes(opt.value) ? 'bg-blue-50 dark:bg-slate-700 font-medium' : ''
              }`}
            >
              <input
                type="checkbox"
                readOnly
                checked={selected.includes(opt.value)}
                className="mr-2 h-3.5 w-3.5 rounded border-slate-300 text-blue-600 pointer-events-none"
              />
              {opt.label}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

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
  const [selectedProjects, setSelectedProjects] = useState<UUID[]>([]);
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
        project_assignments: selectedProjects.map((pid) => ({ project_id: pid, role })),
      }),
    onSuccess: () => {
      const created = fullName.trim();
      setEmail('');
      setFullName('');
      setJobTitle('');
      setSelectedProjects([]);
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

        {/* <Field label="Job title" hint="Optional.">
          <input
            value={jobTitle}
            onChange={(event) => setJobTitle(event.target.value)}
            placeholder="Contracts Lead"
            className={inputClasses}
          />
        </Field> */}

        <div className="rounded-2xl border border-slate-200 bg-slate-50 p-4 dark:border-slate-700 dark:bg-slate-800/50">
          <div className="mb-3 flex items-center gap-2 text-sm font-semibold text-slate-700 dark:text-slate-200">
            <ShieldCheck className="h-4 w-4 text-blue-600" />
            Business Unit
          </div>

          {projects.isLoading ? (
            <LoadingSpinner size="sm" label="Loading projects..." />
          ) : projectOptions.length ? (
            <div className="space-y-3">
              <Field label="Business Unit" hint="Select one or more projects to assign this user to.">
                <MultiSelectDropdown
                  options={projectOptions.map((p) => ({ value: p.id as UUID, label: p.name }))}
                  selected={selectedProjects}
                  onChange={setSelectedProjects}
                  placeholder="Select projects…"
                />
              </Field>

              <Field label="Role in that Business Unit">
                <div className="relative">
                <select
                  value={role}
                  onChange={(event) => setRole(event.target.value as RoleName)}
                  className={selectClasses}
                  disabled={!selectedProjects.length}
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

              {!selectedProjects.length ? (
                <NoticeBanner message="Without a Business Unit this account can sign in but will see nothing. You can add them to one from the Projects screen later." />
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
