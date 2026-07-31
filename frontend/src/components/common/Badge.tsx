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
  success: 'bg-[#ECFDF5] text-[#10B981]',
  warning: 'bg-[#FFFBEB] text-[#D97706]',
  danger: 'bg-[#FEF2F2] text-[#DC2626]',
  info: 'bg-[#EFF4FF] text-[#2563EB]',
  neutral: 'bg-[#F1F5F9] text-[#5B6478]',
};

const sizeClasses: Record<BadgeSize, string> = {
  sm: 'px-[10px] py-[3px] text-[11.5px]',
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
      'inline-flex items-center rounded-full font-semibold',
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
