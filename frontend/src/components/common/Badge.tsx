/**
 * Status badge.
 *
 * The domain mappers that decide which tint a given status gets live in
 * `@/lib/badges` - a module exporting both components and plain functions breaks
 * Fast Refresh.
 */

import type { HTMLAttributes } from 'react';

export type BadgeVariant = 'success' | 'warning' | 'danger' | 'info' | 'neutral';
export type BadgeSize = 'sm' | 'md';

const variantClasses: Record<BadgeVariant, string> = {
  success: 'bg-emerald-50 text-emerald-700 ring-emerald-200',
  warning: 'bg-amber-50 text-amber-700 ring-amber-200',
  danger: 'bg-rose-50 text-rose-700 ring-rose-200',
  info: 'bg-blue-50 text-blue-700 ring-blue-200',
  neutral: 'bg-slate-100 text-slate-700 ring-slate-200',
};

const sizeClasses: Record<BadgeSize, string> = {
  sm: 'px-2.5 py-1 text-xs',
  md: 'px-3 py-1.5 text-sm',
};

export interface BadgeProps extends HTMLAttributes<HTMLSpanElement> {
  text: string;
  variant?: BadgeVariant;
  size?: BadgeSize;
}

export const Badge = ({
  text,
  variant = 'neutral',
  size = 'sm',
  className = '',
  ...rest
}: BadgeProps) => (
  <span
    className={[
      'inline-flex items-center rounded-full font-medium ring-1 ring-inset',
      variantClasses[variant],
      sizeClasses[size],
      className,
    ].join(' ')}
    {...rest}
  >
    {text}
  </span>
);

export default Badge;
