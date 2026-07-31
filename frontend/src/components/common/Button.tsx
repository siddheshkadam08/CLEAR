/**
 * Button.
 *
 * `busy` rather than a separate spinner prop: a button that fires a request must
 * disable itself while it is in flight, and coupling the two means no call site can
 * show a spinner while still accepting a second click.
 */

import { Loader2 } from 'lucide-react';
import type { ButtonHTMLAttributes, ReactNode } from 'react';
import type { LucideIcon } from 'lucide-react';

export type ButtonVariant = 'primary' | 'secondary' | 'ghost' | 'danger';
export type ButtonSize = 'sm' | 'md';

const variantClasses: Record<ButtonVariant, string> = {
  primary:
    'bg-[#2563EB] text-white hover:bg-[#1D4ED8] disabled:hover:bg-[#2563EB]',
  secondary:
    'border border-[#E4E7EC] bg-white text-[#0F172A] hover:bg-[#F7F8FA] hover:border-slate-300',
  ghost: 'text-[#5B6478] hover:bg-[#F7F8FA] hover:text-[#0F172A]',
  danger: 'border border-rose-200 bg-white text-rose-700 hover:bg-rose-50',
};

const sizeClasses: Record<ButtonSize, string> = {
  sm: 'h-8 px-3 text-[12.5px]',
  md: 'h-[38px] px-4 text-[13.5px]',
};

export interface ButtonProps extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, 'children'> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  icon?: LucideIcon;
  busy?: boolean;
  children?: ReactNode;
}

export const Button = ({
  variant = 'primary',
  size = 'md',
  icon: Icon,
  busy = false,
  disabled,
  className = '',
  children,
  type = 'button',
  ...rest
}: ButtonProps) => (
  <button
    type={type}
    disabled={disabled || busy}
    className={[
      'inline-flex items-center justify-center gap-2 rounded-lg font-medium transition',
      'disabled:cursor-not-allowed disabled:opacity-60',
      variantClasses[variant],
      sizeClasses[size],
      className,
    ].join(' ')}
    {...rest}
  >
    {busy ? (
      <Loader2 className="h-4 w-4 shrink-0 animate-spin" />
    ) : Icon ? (
      <Icon className="h-4 w-4 shrink-0" />
    ) : null}
    {children}
  </button>
);

export default Button;
