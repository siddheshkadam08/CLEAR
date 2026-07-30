/**
 * Navigation is role-shaped, and getting it wrong is not cosmetic.
 *
 * An administrator holds every permission except `contract:upload`, which the
 * server refuses (`ADMIN_EXCLUDED_PERMISSIONS`). Showing them an Upload link would
 * advertise a screen where every submission returns 403; hiding the governance
 * links from a member would be the mirror-image mistake.
 */

import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it } from 'vitest';

import { useAuth } from '@/lib/auth';
import { Sidebar } from './Sidebar';

type PartialUser = { id: string; email: string; full_name: string; is_system_admin: boolean };

function signIn(user: PartialUser | null) {
  useAuth.setState({ user: user as never, initialising: false });
}

const renderSidebar = () =>
  render(
    <MemoryRouter initialEntries={['/']}>
      <Sidebar isOpen onClose={() => {}} />
    </MemoryRouter>,
  );

const ADMIN: PartialUser = {
  id: '00000000-0000-0000-0000-0000000000a1',
  email: 'admin@irisregtech.com',
  full_name: 'System Administrator',
  is_system_admin: true,
};

const MEMBER: PartialUser = {
  id: '00000000-0000-0000-0000-0000000000b2',
  email: 'priya@example.com',
  full_name: 'Priya Sharma',
  is_system_admin: false,
};

describe('Sidebar navigation', () => {
  beforeEach(() => signIn(null));

  it('offers Upload to a project member', () => {
    signIn(MEMBER);
    renderSidebar();
    expect(screen.getByRole('link', { name: /upload/i })).toBeInTheDocument();
  });

  it('does not offer Upload to an administrator', () => {
    signIn(ADMIN);
    renderSidebar();
    expect(screen.queryByRole('link', { name: /upload/i })).not.toBeInTheDocument();
  });

  it('offers the governance screens to an administrator', () => {
    signIn(ADMIN);
    renderSidebar();
    for (const label of [/projects/i, /users/i, /clause master/i]) {
      expect(screen.getByRole('link', { name: label })).toBeInTheDocument();
    }
  });

  it('hides the governance screens from a project member', () => {
    signIn(MEMBER);
    renderSidebar();
    for (const label of [/^projects$/i, /^users$/i, /clause master/i]) {
      expect(screen.queryByRole('link', { name: label })).not.toBeInTheDocument();
    }
  });

  it('gives both audiences the shared screens', () => {
    for (const user of [ADMIN, MEMBER]) {
      signIn(user);
      const view = renderSidebar();
      for (const label of [/dashboard/i, /contracts/i, /search/i, /copilot/i, /alerts/i]) {
        expect(screen.getByRole('link', { name: label })).toBeInTheDocument();
      }
      view.unmount();
    }
  });
});
