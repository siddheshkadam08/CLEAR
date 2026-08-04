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

  // "Business Unit", not "Projects": the label was renamed to match what the
  // business calls the thing. Retrieval Quality is admin-only for the same
  // reason as the rest - it names the questions users asked.
  it('offers the governance screens to an administrator', () => {
    signIn(ADMIN);
    renderSidebar();
    for (const label of [/business unit/i, /users/i, /clause master/i, /retrieval quality/i]) {
      expect(screen.getByRole('link', { name: label })).toBeInTheDocument();
    }
  });

  it('hides the governance screens from a project member', () => {
    signIn(MEMBER);
    renderSidebar();
    for (const label of [/business unit/i, /^users$/i, /clause master/i, /retrieval quality/i]) {
      expect(screen.queryByRole('link', { name: label })).not.toBeInTheDocument();
    }
  });

  it('gives both audiences the shared screens', () => {
    for (const user of [ADMIN, MEMBER]) {
      signIn(user);
      const view = renderSidebar();
      for (const label of [/dashboard/i, /contracts/i, /processing/i, /alerts/i]) {
        expect(screen.getByRole('link', { name: label })).toBeInTheDocument();
      }
      view.unmount();
    }
  });

  it('offers Search, which is a live screen', () => {
    // SearchPage was routable but had no way in: its nav entry was commented out
    // with no reason given, so the screen shipped unreachable.
    signIn(ADMIN);
    renderSidebar();
    expect(screen.getByRole('link', { name: /^search$/i })).toBeInTheDocument();
  });

  // Copilot stays out of NAV deliberately - it moved into the drawer on a
  // contract, though the /copilot route still exists. Asserting its absence
  // keeps that a decision: if someone re-adds it, this fails and they have to
  // choose rather than ship two entry points to the same feature.
  it('does not offer Copilot, which moved into the contract drawer', () => {
    signIn(ADMIN);
    renderSidebar();
    expect(screen.queryByRole('link', { name: /^copilot$/i })).not.toBeInTheDocument();
  });
});
