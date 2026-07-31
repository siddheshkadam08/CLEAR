/**
 * Empty state.
 *
 * The description is required and must say what would make content appear. An
 * empty screen with no explanation is the most common way a working product looks
 * broken.
 */

import type { LucideIcon } from 'lucide-react';
import type { ReactNode } from 'react';

export interface EmptyStateProps {
  icon: LucideIcon;
  title: string;
  description: string;
  action?: ReactNode;
}

export const EmptyState = ({ icon: Icon, title, description, action }: EmptyStateProps) => (
  <div className="flex flex-col items-center justify-center rounded-xl border border-dashed border-[#E4E7EC] bg-white px-6 py-14 text-center shadow-sm">
    <div className="mb-4 rounded-full bg-blue-50 p-4 text-blue-600">
      <Icon className="h-8 w-8" />
    </div>
    <h3 className="text-lg font-semibold text-slate-900">{title}</h3>
    <p className="mt-2 max-w-md text-sm leading-6 text-slate-500">{description}</p>
    {action ? <div className="mt-5">{action}</div> : null}
  </div>
);

export default EmptyState;
